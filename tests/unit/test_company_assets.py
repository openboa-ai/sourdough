import unittest
from contextlib import closing
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

from sourdough.agent_desk import save_draft
from sourdough.collaboration import company_work_run
from sourdough.company import FoundingTerms, _connect, found
from sourdough.company_assessment import (
    ASSESSMENT_REVIEW_WORKFLOW,
    MANAGER_ASSESSMENT,
    AssessmentProposal,
    SourceRecord,
    publish_assessment,
    read_assessment_inputs,
    read_assessment_result,
    review_result_allowed,
    seal_assessment_source,
    settle_assessment,
)
from sourdough.company_assets import (
    Selection,
    assign_project,
    project_access,
    publish_selection,
    read_shared_asset,
    seal_project_review,
)
from sourdough.conversations import Post, append_message
from sourdough.duty_run import ASSESSMENT_REVIEW, DutyRefused, start_run
from sourdough.harness import ScopedWorkflow, tool
from sourdough.onboarding import (
    accept_first_duty,
    claim_hired_first,
    provision_hire,
    read_first_duty,
)
from sourdough.staffing import (
    HireRequest,
    StaffingAuthority,
    StaffingChoice,
    StaffingNeed,
    admit_hire,
    claim_staffing,
    read_need,
    record_choice,
    stage_staffing,
)
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
from sourdough.work_peer_response import (
    PeerQuestion,
    answer_peer_question,
    claim_peer_response,
    claim_resumed_peer_work,
    read_peer_outcome,
    read_peer_question,
    request_peer_response,
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
        self.founder = company.founder_id
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

    def prepare_project_hire(self):
        revision = assign_project(
            self.writer,
            self.manager,
            "onboarding",
            (self.founder, "author"),
            publishers=("author",),
        )
        draft = save_draft(
            self.writer,
            self.claims["author"],
            "assessment",
            name="source",
            content="Restricted",
            expected_revision=None,
        )
        asset = publish_selection(
            self.writer,
            self.claims["author"],
            "assessment",
            Selection(
                "old-source",
                draft.name,
                draft.revision,
                "onboarding",
                revision,
                (self.founder,),
                0,
                len(draft.content),
            ),
        )
        now = int(time())
        stage_staffing(
            self.writer,
            StaffingAuthority(self.manager, "profile", "assessment", 1, 4),
            StaffingNeed(
                "hire-for-project",
                "Check records",
                5,
                0,
                1,
                now,
                project_id="onboarding",
            ),
        )
        manager = claim_staffing(self.writer, "profile", now=now)
        observed = read_need(self.writer, manager)
        self.assertEqual(observed["project_id"], "onboarding")
        choice = record_choice(
            self.writer,
            manager,
            StaffingChoice(
                observed["revision"],
                "hire",
                "There is no free reviewer.",
                ("reassign", "defer"),
                60,
            ),
        )
        hire = admit_hire(
            self.writer,
            manager,
            HireRequest("one-hire", choice, "reviewer", "profile", "assessment", 2),
        )
        return now, revision, asset.asset_id, hire

    def test_first_duty_gets_only_participation_in_the_designated_project(self):
        now, old_revision, asset_id, hire = self.prepare_project_hire()
        ready = provision_hire(
            self.writer, hire.agent_id, "assessment", lambda _: True, now=now
        )
        self.assertIsNotNone(ready.first_duty_id)
        self.assertEqual(
            provision_hire(
                self.writer, hire.agent_id, "assessment", lambda _: True, now=now
            ),
            ready,
        )
        claim = claim_hired_first(self.writer, hire.agent_id, "profile")
        (project,) = project_access(self.writer, claim, "assessment")
        self.assertEqual(project.project_id, "onboarding")
        self.assertNotEqual(project.revision, old_revision)
        self.assertFalse(project.can_publish)
        with self.assertRaises(DutyRefused):
            read_shared_asset(self.writer, claim, "assessment", asset_id)

    def test_withdrawn_project_authority_blocks_start_without_discarding_the_hire(self):
        now, revision, _, hire = self.prepare_project_hire()
        assign_project(
            self.writer,
            self.manager,
            "onboarding",
            ("author",),
            publishers=("author",),
            expected_revision=revision,
        )
        status = provision_hire(
            self.writer, hire.agent_id, "assessment", lambda _: True, now=now
        )
        self.assertEqual(
            (status.state, status.reason, status.first_duty_id),
            ("blocked", "project_access_unavailable", None),
        )
        self.assertIsNone(claim_hired_first(self.writer, hire.agent_id, "profile"))
        with closing(_connect(self.writer.store, "ro")) as database:
            retained = database.execute(
                "SELECT agent_id,allocated_turns FROM hire_receipt WHERE agent_id=?",
                (hire.agent_id,),
            ).fetchone()
            self.assertEqual(retained, (hire.agent_id, 2))

    def prepare_peer_question(self):
        now, _, manager_only_asset, hire = self.prepare_project_hire()
        provision_hire(
            self.writer, hire.agent_id, "assessment", lambda _: True, now=now
        )
        claim = claim_hired_first(self.writer, hire.agent_id, "profile")
        mandate = read_first_duty(self.writer, claim)
        accept_first_duty(self.writer, claim, mandate["revision"], "Review the count.")
        question = PeerQuestion(
            "reconcile-timing",
            self.founder,
            "Reconcile now or wait for the next batch?",
            "Choose when to reconcile.",
            now + 60,
        )
        return now, claim, question, manager_only_asset

    def test_peer_question_resumes_the_same_owner_before_publication(self):
        now, claim, question, manager_only_asset = self.prepare_peer_question()
        (project,) = project_access(self.writer, claim, "assessment")
        append_message(
            self.writer,
            claim,
            "assessment",
            Post(
                project.project_id,
                project.revision,
                "discussion",
                question.question,
                "",
            ),
        )
        self.assertIsNone(claim_peer_response(self.writer, "profile", now=now))
        with self.assertRaisesRegex(DutyRefused, "assigned project route"):
            request_peer_response(
                self.writer, claim, replace(question, recipient_id="outsider"), now=now
            )
        with self.assertRaisesRegex(DutyRefused, "asset is unavailable"):
            request_peer_response(
                self.writer,
                claim,
                replace(
                    question, asset_id=manager_only_asset, asset_revision="unshared"
                ),
                now=now,
            )
        waiting = request_peer_response(self.writer, claim, question, now=now)
        self.assertEqual(
            request_peer_response(self.writer, claim, question, now=now), waiting
        )
        with self.assertRaisesRegex(DutyRefused, "original receipt"):
            request_peer_response(
                self.writer,
                claim,
                replace(question, question="Different question."),
                now=now,
            )
        with closing(_connect(self.writer.store, "ro")) as database:
            self.assertEqual(
                database.execute(
                    "SELECT agent_id,state FROM duty WHERE id=?", (claim.duty_id,)
                ).fetchone(),
                (claim.founder_id, "waiting"),
            )
            self.assertIsNone(
                database.execute(
                    "SELECT name FROM sqlite_schema WHERE name='assessment_publication'"
                ).fetchone()
            )
        self.assertIsNone(claim_resumed_peer_work(self.writer, "profile", now=now))
        response = claim_peer_response(self.writer, "profile", now=now)
        self.assertEqual(
            (response.duty_id, response.founder_id),
            (waiting.response_duty, self.founder),
        )
        self.assertIsNone(claim_peer_response(self.writer, "profile", now=now))
        with self.assertRaisesRegex(DutyRefused, "requires a current read"):
            answer_peer_question(self.writer, response, "answered", "Wait.", now=now)
        self.assertEqual(
            read_peer_question(self.writer, response, now=now),
            {
                "question": question.question,
                "expected_response": question.expected_response,
                "expires_at": question.expires_at,
                "evidence": None,
            },
        )
        answer = answer_peer_question(
            self.writer, response, "answered", "Wait.", now=now + 1
        )
        self.assertEqual(
            answer_peer_question(
                self.writer, response, "answered", "Wait.", now=now + 1
            ),
            answer,
        )
        resumed = claim_resumed_peer_work(self.writer, "profile", now=now + 1)
        self.assertEqual(
            (resumed.duty_id, resumed.founder_id), (claim.duty_id, claim.founder_id)
        )
        self.assertNotEqual(resumed.run_id, claim.run_id)
        self.assertIsNone(claim_resumed_peer_work(self.writer, "profile", now=now + 1))
        self.assertEqual(
            read_peer_outcome(self.writer, resumed),
            {
                "request_key": question.request_key,
                "outcome": "answered",
                "answer": "Wait.",
            },
        )
        with self.assertRaises(DutyRefused):
            read_peer_outcome(self.writer, claim)

    def test_peer_expiry_resumes_without_waking_a_withdrawn_recipient(self):
        now, claim, question, _ = self.prepare_peer_question()
        (project,) = project_access(self.writer, claim, "assessment")
        waiting = request_peer_response(self.writer, claim, question, now=now)
        assign_project(
            self.writer,
            self.manager,
            project.project_id,
            ("author", claim.founder_id),
            publishers=("author",),
            expected_revision=project.revision,
        )
        self.assertIsNone(claim_peer_response(self.writer, "profile", now=now))
        self.assertIsNone(claim_resumed_peer_work(self.writer, "profile", now=now))
        resumed = claim_resumed_peer_work(
            self.writer, "profile", now=question.expires_at
        )
        self.assertEqual(resumed.duty_id, claim.duty_id)
        self.assertEqual(read_peer_outcome(self.writer, resumed)["outcome"], "expired")
        with closing(_connect(self.writer.store, "ro")) as database:
            self.assertEqual(
                database.execute(
                    "SELECT count(*) FROM duty_run WHERE duty_id=?",
                    (waiting.response_duty,),
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                database.execute("PRAGMA foreign_key_check").fetchall(), []
            )

    def settle_project_assessment(self):
        now, _, _, hire = self.prepare_project_hire()
        provision_hire(
            self.writer, hire.agent_id, "assessment", lambda _: True, now=now
        )
        claim = claim_hired_first(self.writer, hire.agent_id, "profile")
        mandate = read_first_duty(self.writer, claim)
        accept_first_duty(self.writer, claim, mandate["revision"], "Review the count.")
        (project,) = project_access(self.writer, claim, "assessment")
        message = append_message(
            self.writer,
            claim,
            "assessment",
            Post("onboarding", project.revision, "progress", "Source reviewed.", ""),
        )
        with closing(_connect(self.writer.store, "ro")) as database:
            self.assertEqual(
                database.execute("SELECT count(*) FROM work_follow_up").fetchone(), (0,)
            )
        seal_assessment_source(
            self.writer, (SourceRecord("count", "Documents", 8, 6, now),)
        )
        inputs = read_assessment_inputs(self.writer, claim, now=now)
        publication = publish_assessment(
            self.writer,
            claim,
            AssessmentProposal(
                inputs["revision"], "count", "investigate", "Two missing.", 60
            ),
            now=now,
        )
        next_hire = settle_assessment(self.writer, claim, publication)
        self.assertEqual(settle_assessment(self.writer, claim, publication), next_hire)
        return claim, publication, next_hire, project, message

    def test_project_result_schedules_one_separate_manager_response(self):
        claim, publication, next_hire, project, message = (
            self.settle_project_assessment()
        )
        allowed = partial(review_result_allowed, company_id=claim.company_id)
        with closing(_connect(self.writer.store, "ro")) as database:
            (manager_duty, due_at, prior_duty) = database.execute(
                """SELECT w.duty_id,w.due_at,w.previous_duty_id
                   FROM work_follow_up w JOIN duty d ON d.id=w.duty_id
                   WHERE d.agent_id=? AND w.result_kind=? AND w.result_id=?""",
                (self.founder, MANAGER_ASSESSMENT, publication.action_id),
            ).fetchone()
            self.assertGreaterEqual(due_at, publication.published_at)
            self.assertEqual(prior_duty, "hire-for-project")
        query = (self.writer.store, claim.company_id, self.founder, "profile")
        self.assertIsNone(
            claim_assessment_review(*query, now=due_at - 1, eligible_result=allowed)
        )
        review = claim_assessment_review(*query, now=due_at, eligible_result=allowed)
        self.assertEqual(review.claim.duty_id, manager_duty)
        self.assertNotEqual(review.claim.run_id, claim.run_id)
        self.assertEqual(review.claim.grant_id, self.manager)
        result = read_assessment_result(self.writer, review, now=due_at)
        self.assertEqual(set(result), {"revision", "assessment", "project_id"})
        self.assertEqual(result["revision"], publication.revision)
        self.assertEqual(result["project_id"], project.project_id)
        employee = company_work_run(
            self.writer,
            review.claim,
            ASSESSMENT_REVIEW_WORKFLOW,
            lambda *_: {},
            project_id=project.project_id,
        )
        self.assertEqual(employee.manifest()["drafts"], [])
        with self.assertRaisesRegex(DutyRefused, "different project"):
            employee.call(
                "read_project_discussion", {"project_id": "project", "after": ""}
            )
        thread = employee.call(
            "read_project_discussion", {"project_id": project.project_id, "after": ""}
        )
        self.assertEqual(thread["messages"], [asdict(message)])
        reply = employee.call(
            "post_project_message",
            {
                "request_id": "reply",
                "project_id": project.project_id,
                "project_revision": project.revision,
                "body": "Please reconcile the two missing documents.",
                "asset_id": "",
            },
        )
        self.assertEqual(reply["sender_id"], self.founder)
        next_manager = settle_review(
            self.writer.store,
            review,
            ReviewDecision(
                "reply-review",
                result["revision"],
                "investigate",
                "Reconcile missing documents.",
                60,
            ),
            now=due_at,
            successor_purpose=ASSESSMENT_REVIEW,
            eligible_result=allowed,
        )
        with closing(_connect(self.writer.store, "ro")) as database:
            pending = database.execute(
                "SELECT id,agent_id FROM duty WHERE state='scheduled'"
            ).fetchall()
            self.assertCountEqual(
                pending,
                [
                    (next_hire.duty_id, claim.founder_id),
                    (next_manager.duty_id, self.founder),
                ],
            )
            self.assertEqual(
                database.execute("PRAGMA foreign_key_check").fetchall(), []
            )

    def test_revoked_project_cannot_start_the_manager_review(self):
        claim, publication, next_hire, project, _ = self.settle_project_assessment()
        assign_project(
            self.writer,
            self.manager,
            project.project_id,
            ("author", claim.founder_id),
            publishers=("author",),
            expected_revision=project.revision,
        )
        allowed = partial(review_result_allowed, company_id=claim.company_id)
        now = publication.published_at + 86400
        self.assertIsNone(
            claim_assessment_review(
                self.writer.store,
                claim.company_id,
                self.founder,
                "profile",
                now=now,
                eligible_result=allowed,
            )
        )
        next_review = claim_assessment_review(
            self.writer.store,
            claim.company_id,
            claim.founder_id,
            "profile",
            now=now,
            eligible_result=allowed,
        )
        self.assertEqual(next_review.claim.duty_id, next_hire.duty_id)

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
                "read_project_inbox",
                "acknowledge_project_delivery",
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
                "read_project_inbox",
                "acknowledge_project_delivery",
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
        inbox = {"after": ""}
        [delivery] = peer.call("read_project_inbox", inbox)["deliveries"]
        self.assertEqual(delivery["message_id"], question["message_id"])
        self.assertIsNone(delivery["read_run"])
        self.assertNotIn("body", delivery)
        self.assertNotIn("asset_id", delivery)
        self.assertEqual(
            peer.call("read_project_inbox", inbox)["deliveries"], [delivery]
        )
        with self.assertRaisesRegex(DutyRefused, "unavailable"):
            author.call(
                "acknowledge_project_delivery", {"delivery_id": delivery["delivery_id"]}
            )
        acknowledgment = {"delivery_id": delivery["delivery_id"]}
        seen = peer.call("acknowledge_project_delivery", acknowledgment)
        self.assertEqual(seen["read_run"], self.claims["peer"].run_id)
        self.assertEqual(
            run("peer").call("acknowledge_project_delivery", acknowledgment), seen
        )
        self.assertEqual(
            peer.call("read_project_inbox", {"after": delivery["delivery_id"]})[
                "deliveries"
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
        with self.assertRaisesRegex(DutyRefused, "discussion is unavailable"):
            peer.call("read_project_discussion", thread)
        self.assertEqual(peer.call("read_project_inbox", inbox)["deliveries"], [])
        with self.assertRaisesRegex(DutyRefused, "unavailable"):
            peer.call("acknowledge_project_delivery", acknowledgment)
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

    def test_review_briefs_follow_the_assigned_claim_not_the_project_audience(self):
        with closing(_connect(self.writer.store, "rw")) as database, database:
            for agent in ("author", "peer"):
                database.execute(
                    "INSERT INTO duty VALUES (?,?,'scheduled')",
                    (f"{agent}-review", agent),
                )
                database.execute(
                    """INSERT INTO work_follow_up VALUES
                       (?,?,?,'assessment',20,'fixture','report','original',20,?)""",
                    (
                        f"{agent}-duty",
                        f"{agent}-review",
                        f"{agent}-grant",
                        ASSESSMENT_REVIEW,
                    ),
                )
        with self.assertRaisesRegex(DutyRefused, "requires current company management"):
            seal_project_review(
                self.writer, "peer-grant", "author-review", "project", "Wrong grant."
            )
        for _attempt in range(2):
            seal_project_review(
                self.writer,
                self.manager,
                "author-review",
                "project",
                "Prepare a private draft.",
            )
        with self.assertRaisesRegex(DutyRefused, "already has a brief"):
            seal_project_review(
                self.writer,
                self.manager,
                "author-review",
                "project",
                "Replace the original brief.",
            )
        seal_project_review(
            self.writer,
            self.manager,
            "peer-review",
            "project",
            "Review only shared material.",
        )
        workflow = ScopedWorkflow(
            ASSESSMENT_REVIEW,
            "Review the result.",
            (tool("finish", "Commit the result.", {}),),
            "finish",
        )
        claims = {
            agent: claim_assessment_review(
                self.writer.store, self.writer.company_id, agent, "profile", now=20
            ).claim
            for agent in ("author", "peer")
        }

        def prepare(agent):
            return company_work_run(
                self.writer, claims[agent], workflow, lambda *_: {"finished": True}
            )

        def manifest(agent):
            return prepare(agent).manifest()

        prepared = prepare("author")
        self.assertIn("Prepare a private draft.", prepared.manifest()["instruction"])
        self.assertNotIn("Prepare a private draft.", manifest("peer")["instruction"])
        self.assertIn("Review only shared material.", manifest("peer")["instruction"])
        assign_project(
            self.writer,
            self.manager,
            "project",
            ("peer",),
            publishers=(),
            expected_revision=self.project,
        )
        with self.assertRaisesRegex(DutyRefused, "context changed before disclosure"):
            prepared.manifest()
        reopened = manifest("author")
        self.assertEqual(reopened["instruction"], workflow.instruction)
        self.assertEqual([entry["name"] for entry in reopened["tools"]], ["finish"])
        self.assertIn("Review only shared material.", manifest("peer")["instruction"])
        with closing(_connect(self.writer.store, "ro")) as database:
            self.assertEqual(
                database.execute(
                    "SELECT count(*) FROM project_review_brief"
                ).fetchone(),
                (2,),
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
