import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.agent_desk import save_draft
from sourdough.collaboration import company_work_run
from sourdough.company import FoundingTerms, _connect, found
from sourdough.company_assets import (
    Selection,
    assign_project,
    publish_selection,
    read_shared_asset,
)
from sourdough.duty_run import DutyRefused, start_run
from sourdough.harness import ScopedWorkflow, tool
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
        content = "Private notes\nReviewed conclusion\nUnshared attachment details"
        draft = save_draft(
            self.writer,
            self.claims["author"],
            "assessment",
            name="owner-only",
            content=content,
            expected_revision=None,
        )
        start = content.index("Reviewed")
        selection = Selection(
            "selection",
            draft.name,
            draft.revision,
            "project",
            self.project,
            ("peer",),
            start,
            start + len("Reviewed conclusion"),
        )
        asset = self.publish(selection)
        self.assertEqual(asset.content, "Reviewed conclusion")
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
                "publish_draft_selection",
            ],
        )
        self.assertEqual(
            [entry["name"] for entry in peer.manifest()["tools"]],
            ["finish", "read_project_assignments", "read_shared_asset"],
        )
        [assigned] = author.call("read_project_assignments", {})["projects"]
        self.assertTrue(assigned["can_publish"])
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
        self.assertEqual(
            peer.call("read_shared_asset", {"asset_id": published["asset_id"]})[
                "content"
            ],
            "Share only this.",
        )
        with self.assertRaisesRegex(DutyRefused, "not available to this run"):
            peer.call("publish_draft_selection", request)
