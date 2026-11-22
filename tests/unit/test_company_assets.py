import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.agent_desk import save_draft
from sourdough.collaboration import company_work_run
from sourdough.company import FoundingTerms, _connect, found
from sourdough.company_assessment import read_assessment_result
from sourdough.company_assets import (
    Selection,
    assign_project,
    publish_selection,
    read_shared_asset,
)
from sourdough.duty_run import ASSESSMENT_REVIEW, DutyRefused, start_run
from sourdough.harness import ScopedWorkflow, tool
from sourdough.work import (
    ReviewDecision,
    claim_assessment_review,
    read_follow_up_result,
    settle_review,
)
from sourdough.work_handoff import (
    HandoffOffer,
    accept_handoff,
    discover_handoffs,
    offer_handoff,
)
from sourdough.writer_lease import hold_company_writer


class CompanyAssets(unittest.TestCase):
    def setUp(self):
        scratch = Path(self.enterContext(TemporaryDirectory()))
        store = scratch / "company.sqlite"
        company = found(
            store, FoundingTerms("assets", "charter", "manage-company", "profile")
        )
        self.manager = company.grant_id
        self.writer = self.enterContext(
            hold_company_writer(
                store, company.company_id, lease_directory=scratch / "leases"
            )
        )
        with closing(_connect(store, "rw")) as database, database:
            database.execute(
                "CREATE TABLE private_desk (agent_id TEXT PRIMARY KEY, manifest_revision TEXT NOT NULL)"
            )
            self.claims = {}
            for agent in ("author", "peer", "outsider"):
                grant, duty = f"{agent}-grant", f"{agent}-duty"
                database.execute(
                    "INSERT INTO agent VALUES (?,?,'profile')",
                    (agent, company.company_id),
                )
                database.execute(
                    "INSERT INTO standing_grant VALUES (?,?,'assessment')",
                    (grant, agent),
                )
                database.execute("INSERT INTO duty VALUES (?,?,'due')", (duty, agent))
                database.execute(
                    "INSERT INTO private_desk VALUES (?,?)", (agent, f"{agent}-desk")
                )
                self.claims[agent] = start_run(
                    database,
                    company.company_id,
                    agent,
                    grant,
                    duty,
                    "profile",
                    purpose="assessment",
                    grant_scope="assessment",
                )
        self.members = ("peer", "author", "outsider")
        self.project = assign_project(
            self.writer, self.manager, "project", self.members, publishers=("author",)
        )

    def publish(self, selection, author="author"):
        return publish_selection(
            self.writer, self.claims[author], "assessment", selection
        )

    def test_publication_moves_only_excerpts_to_the_selected_audience(self):
        self.assertEqual(
            assign_project(
                self.writer,
                self.manager,
                "project",
                self.members,
                publishers=("author",),
            ),
            self.project,
        )
        excerpt = "    Reviewed conclusion\n"
        content = f"Private notes\n{excerpt}Unshared attachment details"
        draft = save_draft(
            self.writer,
            self.claims["author"],
            "assessment",
            name="owner-only",
            content=content,
            expected_revision=None,
        )
        start = content.index(excerpt)
        selection = Selection(
            "selection",
            draft.name,
            draft.revision,
            "project",
            self.project,
            ("peer",),
            start,
            start + len(excerpt),
        )
        asset = self.publish(selection)
        self.assertEqual(asset.content, excerpt)
        self.assertEqual(self.publish(selection), asset)
        self.assertNotIn(draft.name, str(asdict(asset)))
        self.assertEqual(
            read_shared_asset(
                self.writer, self.claims["peer"], "assessment", asset.asset_id
            ),
            asset,
        )
        with self.assertRaisesRegex(DutyRefused, "asset is unavailable"):
            read_shared_asset(
                self.writer, self.claims["outsider"], "assessment", asset.asset_id
            )
        with self.assertRaisesRegex(DutyRefused, "original selection"):
            self.publish(replace(selection, end=selection.end + 1))
        with self.assertRaisesRegex(DutyRefused, "project audience"):
            self.publish(selection, "peer")
        assign_project(
            self.writer,
            self.manager,
            "project",
            ("author", "outsider"),
            publishers=("author",),
            expected_revision=self.project,
        )
        with self.assertRaisesRegex(DutyRefused, "asset is unavailable"):
            read_shared_asset(
                self.writer, self.claims["peer"], "assessment", asset.asset_id
            )
        with self.assertRaisesRegex(DutyRefused, "project audience"):
            self.publish(selection)
        with closing(_connect(self.writer.store, "ro")) as database:
            self.assertEqual(
                database.execute("SELECT count(*) FROM shared_publication").fetchone(),
                (1,),
            )
            self.assertEqual(
                database.execute(
                    "SELECT agent_id,state FROM duty WHERE id=?", (asset.duty_id,)
                ).fetchone(),
                ("author", "running"),
            )

    def test_harness_only_offers_project_tools_for_current_assignments(self):
        workflow = ScopedWorkflow(
            "assessment",
            "Review the current work.",
            (tool("finish", "Commit the result.", {}),),
            "finish",
        )
        author = company_work_run(
            self.writer, self.claims["author"], workflow, lambda *_: {"finished": True}
        )
        peer = company_work_run(
            self.writer, self.claims["peer"], workflow, lambda *_: {"finished": True}
        )
        self.assertEqual(
            [entry["name"] for entry in author.manifest()["tools"]],
            [
                "finish",
                "read_project_assignments",
                "read_shared_asset",
                "read_project_discussion",
                "post_project_message",
                "publish_draft_selection",
            ],
        )
        self.assertEqual(
            [entry["name"] for entry in peer.manifest()["tools"]],
            [
                "finish",
                "read_project_assignments",
                "read_shared_asset",
                "read_project_discussion",
                "post_project_message",
            ],
        )
        [assigned] = author.call("read_project_assignments", {"after": ""})["projects"]
        self.assertTrue(assigned["can_publish"])
        with self.assertRaisesRegex(DutyRefused, "Current private draft"):
            author.call(
                "publish_draft_selection",
                {
                    "request_id": "first",
                    "draft_name": "journal",
                    "draft_revision": "not-saved",
                    "project_id": assigned["project_id"],
                    "project_revision": assigned["revision"],
                    "audience": ["peer"],
                    "start": 0,
                    "end": 16,
                },
            )
        draft = save_draft(
            self.writer,
            self.claims["author"],
            "assessment",
            name="journal",
            content="Share only this. Private.",
            expected_revision=None,
        )
        request = {
            "request_id": "first",
            "draft_name": draft.name,
            "draft_revision": draft.revision,
            "project_id": assigned["project_id"],
            "project_revision": assigned["revision"],
            "audience": ["peer"],
            "start": 0,
            "end": 16,
        }
        published = author.call("publish_draft_selection", request)
        [listed] = peer.call("read_project_assignments", {"after": ""})["shared_assets"]
        self.assertEqual(listed["asset_id"], published["asset_id"])
        self.assertNotIn("content", listed)
        self.assertEqual(
            peer.call("read_shared_asset", {"asset_id": listed["asset_id"]})["content"],
            "Share only this.",
        )
        with self.assertRaisesRegex(DutyRefused, "not available to this run"):
            peer.call("publish_draft_selection", request)
        outsider = company_work_run(
            self.writer,
            self.claims["outsider"],
            workflow,
            lambda *_: {"finished": True},
        )
        self.assertEqual(
            outsider.call("read_project_assignments", {"after": ""})["shared_assets"],
            [],
        )
        hidden = author.call(
            "publish_draft_selection",
            request | {"request_id": "outsider-only", "audience": ["outsider"]},
        )
        for index in range(32):
            author.call(
                "publish_draft_selection", request | {"request_id": f"peer-{index}"}
            )
        first_page = peer.call("read_project_assignments", {"after": ""})
        self.assertEqual(len(first_page["shared_assets"]), 32)
        self.assertNotIn(listed, first_page["shared_assets"])
        last_page = peer.call(
            "read_project_assignments", {"after": first_page["next_cursor"]}
        )
        self.assertEqual(last_page["shared_assets"], [listed])
        self.assertEqual(last_page["next_cursor"], "")
        self.assertEqual(
            peer.call("read_project_assignments", {"after": hidden["asset_id"]})[
                "shared_assets"
            ],
            [],
        )
        assign_project(
            self.writer,
            self.manager,
            "project",
            ("author", "outsider"),
            publishers=("author",),
            expected_revision=self.project,
        )
        self.assertEqual(
            peer.call("read_project_assignments", {"after": first_page["next_cursor"]})[
                "shared_assets"
            ],
            [],
        )

    def test_discussion_reopens_in_order_without_transferring_work_or_asset_access(
        self,
    ):
        workflow = ScopedWorkflow(
            "assessment",
            "Read current work.",
            (tool("finish", "Commit.", {}),),
            "finish",
        )

        def run(agent):
            return company_work_run(
                self.writer, self.claims[agent], workflow, lambda *_: {"finished": True}
            )

        author = run("author")
        thread = {"project_id": "project", "after": ""}
        self.assertEqual(author.call("read_project_discussion", thread)["messages"], [])
        draft = save_draft(
            self.writer,
            self.claims["author"],
            "assessment",
            name="summary",
            content="Approved fact. Private rationale.",
            expected_revision=None,
        )
        asset = self.publish(
            Selection(
                "asset",
                draft.name,
                draft.revision,
                "project",
                self.project,
                ("author", "peer"),
                0,
                14,
            )
        )
        post = {
            "project_id": "project",
            "project_revision": self.project,
            "request_id": "question",
            "body": "Can you verify the record?",
            "asset_id": asset.asset_id,
        }
        question = author.call("post_project_message", post)
        self.assertEqual(run("author").call("post_project_message", post), question)
        with self.assertRaisesRegex(DutyRefused, "original post"):
            author.call("post_project_message", post | {"body": "Changed message."})
        reply = run("peer").call(
            "post_project_message",
            post
            | {"request_id": "answer", "body": "I will review it.", "asset_id": ""},
        )
        self.assertEqual(
            run("peer").call("read_project_discussion", thread)["messages"],
            [question, reply],
        )
        self.assertEqual(
            run("peer").call(
                "read_project_discussion", thread | {"after": question["message_id"]}
            )["messages"],
            [reply],
        )
        self.assertEqual(
            run("outsider").call("read_project_discussion", thread)["messages"],
            [question | {"asset_id": None}, reply],
        )
        peer = run("peer")
        assign_project(
            self.writer,
            self.manager,
            "project",
            ("author", "outsider"),
            publishers=("author",),
            expected_revision=self.project,
        )
        with self.assertRaisesRegex(DutyRefused, "discussion is unavailable"):
            peer.call("read_project_discussion", thread)
        with closing(_connect(self.writer.store, "ro")) as database:
            self.assertEqual(
                database.execute(
                    "SELECT agent_id,state FROM duty WHERE id='author-duty'"
                ).fetchone(),
                ("author", "running"),
            )
            self.assertEqual(
                database.execute("SELECT count(*) FROM work_follow_up").fetchone(), (0,)
            )

    def test_explicit_acceptance_fences_the_old_owner_and_schedules_the_recipient(self):
        with closing(_connect(self.writer.store, "rw")) as database, database:
            database.execute(
                "INSERT INTO duty VALUES ('review-due','author','scheduled')"
            )
            database.execute(
                """INSERT INTO work_follow_up VALUES
                   ('author-duty','review-due','author-grant','assessment',20,
                    'fixture','report','original',20,?)""",
                (ASSESSMENT_REVIEW,),
            )
        current = claim_assessment_review(
            self.writer.store, self.writer.company_id, "author", "profile", now=20
        )
        sender = current.claim
        draft = save_draft(
            self.writer,
            sender,
            ASSESSMENT_REVIEW,
            name="report",
            content="Reviewed finding. Private notes.",
            expected_revision=None,
        )
        asset = publish_selection(
            self.writer,
            sender,
            ASSESSMENT_REVIEW,
            Selection(
                "share",
                draft.name,
                draft.revision,
                "project",
                self.project,
                ("peer",),
                0,
                17,
            ),
        )
        offered = HandoffOffer(
            "offer", "peer", asset.asset_id, 160, "Recheck the finding."
        )
        offer_id = offer_handoff(self.writer, sender, offered, now=20)
        self.assertEqual(offer_handoff(self.writer, sender, offered, now=21), offer_id)
        target = self.claims["peer"]
        self.assertEqual(
            discover_handoffs(self.writer, target, "assessment", now=21), []
        )
        with self.assertRaisesRegex(DutyRefused, "offer is unavailable"):
            accept_handoff(self.writer, target, "assessment", offer_id, now=21)
        competing_id = offer_handoff(
            self.writer, sender, replace(offered, request_id="competing"), now=21
        )
        expired_id = offer_handoff(
            self.writer,
            sender,
            replace(offered, request_id="expiring", expires_at=22),
            now=21,
        )
        read_follow_up_result(
            self.writer.store,
            current,
            now=22,
            read=lambda *_: {"revision": "original"},
            purpose=ASSESSMENT_REVIEW,
        )
        original = settle_review(
            self.writer.store,
            current,
            ReviewDecision(
                "sender-review", "original", "hold", "Share this finding.", 60
            ),
            now=22,
            successor_purpose=ASSESSMENT_REVIEW,
        )
        with closing(_connect(self.writer.store, "ro")) as database:
            self.assertEqual(
                database.execute(
                    "SELECT agent_id,state FROM duty WHERE id=?", (original.duty_id,)
                ).fetchone(),
                ("author", "scheduled"),
            )
        self.assertEqual(
            {
                offer["offer_id"]
                for offer in discover_handoffs(
                    self.writer, target, "assessment", now=23
                )
            },
            {offer_id, competing_id},
        )
        with self.assertRaisesRegex(DutyRefused, "offer is unavailable"):
            accept_handoff(
                self.writer, self.claims["outsider"], "assessment", offer_id, now=23
            )
        with self.assertRaisesRegex(DutyRefused, "not actionable"):
            accept_handoff(self.writer, target, "assessment", expired_id, now=23)
        successor = accept_handoff(self.writer, target, "assessment", offer_id, now=23)
        self.assertEqual(
            accept_handoff(self.writer, target, "assessment", offer_id, now=24),
            successor,
        )
        with self.assertRaisesRegex(DutyRefused, "not actionable"):
            accept_handoff(self.writer, target, "assessment", competing_id, now=24)
        with self.assertRaisesRegex(DutyRefused, "current running duty"):
            offer_handoff(self.writer, sender, offered, now=25)
        self.assertIsNone(
            claim_assessment_review(
                self.writer.store, self.writer.company_id, "author", "profile", now=82
            )
        )
        self.assertIsNone(
            claim_assessment_review(
                self.writer.store, self.writer.company_id, "peer", "profile", now=81
            )
        )
        work = claim_assessment_review(
            self.writer.store, self.writer.company_id, "peer", "profile", now=82
        )
        self.assertEqual(work.claim.duty_id, successor)
        received = read_assessment_result(self.writer, work, now=82)
        self.assertEqual(received["shared_evidence"]["content"], "Reviewed finding.")
        self.assertEqual(received["shared_evidence"]["author_id"], "author")
        self.assertEqual(
            received["handoff"],
            {
                "offer_id": offer_id,
                "sender_id": "author",
                "expected_result": "Recheck the finding.",
            },
        )
        self.assertEqual(work.claim.founder_id, "peer")
        decision = ReviewDecision(
            "new-owner-review",
            received["revision"],
            "hold",
            "Verified the shared result.",
            60,
        )
        follow_up = settle_review(
            self.writer.store,
            work,
            decision,
            now=83,
            successor_purpose=ASSESSMENT_REVIEW,
        )
        self.assertEqual(follow_up.result.resource_id, asset.asset_id)
        with closing(_connect(self.writer.store, "ro")) as database:
            self.assertEqual(
                database.execute(
                    "SELECT agent_id,state FROM duty WHERE id=?", (follow_up.duty_id,)
                ).fetchone(),
                ("peer", "scheduled"),
            )
