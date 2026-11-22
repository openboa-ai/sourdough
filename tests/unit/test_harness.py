import json
import runpy
import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from sourdough.agent_desk import save_draft
from sourdough.company import FoundingTerms, _connect, found
from sourdough.duty_run import PAPER_EXECUTION, DutyRefused, start_run
from sourdough.employee_process import _ToolEndpoint
from sourdough.first_duty import claim_first_duty
from sourdough.harness import ScopedRun, ScopedWorkflow, tool
from sourdough.writer_lease import hold_company_writer


ADAPTER = Path(__file__).resolve().parents[2] / "tools/codex_employee.py"


class ScopedHarness(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="sd-")))
        self.store = self.root / "company.sqlite"
        self.founding = found(
            self.store, FoundingTerms("company", "charter", "paper", "profile")
        )
        self.claim = claim_first_duty(self.store, self.founding.company_id, "profile")
        self.writer = self.enterContext(
            hold_company_writer(
                self.store,
                self.founding.company_id,
                lease_directory=self.root / "leases",
            )
        )
        with closing(_connect(self.store, "rw")) as database, database:
            database.execute(
                "CREATE TABLE private_desk (agent_id TEXT PRIMARY KEY, manifest_revision TEXT NOT NULL)"
            )
            database.execute(
                "INSERT INTO private_desk VALUES (?,?)",
                (self.claim.founder_id, "current-desk"),
            )
        self.calls = []

    def workflow(self, name):
        return ScopedWorkflow(
            PAPER_EXECUTION,
            "Complete the assigned company-only action.",
            (
                tool(
                    name,
                    "Complete the selected workflow.",
                    {"record": {"type": "string"}},
                ),
            ),
            name,
        )

    def invoke(self, name, args):
        self.calls.append((name, args))
        return {"receipt": "recorded-by-company"}

    def test_same_transport_lists_only_the_selected_workflow(self):
        for name in ("save_private_draft", "publish_assessment"):
            with self.subTest(name=name):
                run = ScopedRun(
                    self.writer, self.claim, self.workflow(name), self.invoke
                )
                manifest = self.root / "manifest.json"
                manifest.write_text(json.dumps(run.manifest()))
                outbox = self.root / name
                address = str(self.root / "tools.sock")
                with _ToolEndpoint(address, run.call) as endpoint:
                    thread = Thread(
                        target=endpoint.serve_forever, kwargs={"poll_interval": 0.05}
                    )
                    thread.start()
                    try:
                        result = self.send(address, manifest, outbox, name)
                    finally:
                        endpoint.shutdown()
                        thread.join()
                        Path(address).unlink()
                listed, denied, accepted = result
                self.assertEqual([item["name"] for item in listed["tools"]], [name])
                self.assertTrue(denied["isError"])
                self.assertFalse(accepted["isError"])
                self.assertEqual(
                    json.loads(outbox.read_bytes()), {"receipt": "recorded-by-company"}
                )
        self.assertEqual(
            self.calls,
            [
                (name, {"record": "company document"})
                for name in ("save_private_draft", "publish_assessment")
            ],
        )

    def send(self, address, manifest, outbox, name):
        requests = [
            {"id": 1, "method": "tools/list"},
            {"id": 2, "method": "tools/call", "params": {"name": "submit_order"}},
            {
                "id": 3,
                "method": "tools/call",
                "params": {"name": name, "arguments": {"record": "company document"}},
            },
        ]
        response = subprocess.run(
            [
                sys.executable,
                "-IB",
                str(ADAPTER),
                "--mcp-manifest",
                address,
                str(manifest),
                str(outbox),
            ],
            input="".join(json.dumps(item) + "\n" for item in requests),
            text=True,
            capture_output=True,
            timeout=12,
            check=True,
        )
        return [json.loads(line)["result"] for line in response.stdout.splitlines()]

    def test_company_rechecks_claim_and_desk_even_when_adapter_has_a_manifest(self):
        workflow = self.workflow("record_note")
        with self.assertRaisesRegex(DutyRefused, "identity mismatch"):
            ScopedRun(
                self.writer,
                replace(self.claim, execution_profile="another"),
                workflow,
                self.invoke,
            )
        run = ScopedRun(self.writer, self.claim, workflow, self.invoke)
        with self.assertRaisesRegex(DutyRefused, "scoped manifest"):
            run.call("record_note", {"record": "valid", "authority": "invented"})
        with closing(_connect(self.store, "rw")) as database, database:
            database.execute("UPDATE private_desk SET manifest_revision='replaced'")
        with self.assertRaisesRegex(DutyRefused, "Desk changed"):
            run.call("record_note", {"record": "valid"})
        current = ScopedRun(self.writer, self.claim, workflow, self.invoke)
        with closing(_connect(self.store, "rw")) as database, database:
            database.execute("UPDATE duty_run SET is_current=0")
        with self.assertRaisesRegex(DutyRefused, "no longer current"):
            current.call("record_note", {"record": "valid"})
        self.assertEqual(self.calls, [])

    def test_durable_private_draft_is_mounted_only_for_the_original_agent(self):
        def save(name, content, expected=None):
            return save_draft(
                self.writer,
                self.claim,
                PAPER_EXECUTION,
                name=name,
                content=content,
                expected_revision=expected,
            )

        draft = save("comparison", "Use the independently confirmed count.")
        self.assertEqual(draft, save("comparison", draft.content))
        with self.assertRaisesRegex(DutyRefused, "revision changed"):
            save("comparison", "A conflicting overwrite.")
        with self.assertRaisesRegex(DutyRefused, "Invalid private draft"):
            save("../AGENTS.md", "Not an authorized rule.")
        with closing(_connect(self.store, "rw")) as database, database:
            database.execute(
                "INSERT INTO agent VALUES ('peer', ?, 'profile')",
                (self.claim.company_id,),
            )
            database.execute(
                "INSERT INTO standing_grant VALUES ('peer-grant', 'peer', 'paper')"
            )
            database.execute("INSERT INTO duty VALUES ('peer-duty', 'peer', 'due')")
            database.execute("INSERT INTO private_desk VALUES ('peer', 'peer-desk')")
            peer = start_run(
                database,
                self.claim.company_id,
                "peer",
                "peer-grant",
                "peer-duty",
                "profile",
                purpose=PAPER_EXECUTION,
                grant_scope="paper",
            )
        workflow = self.workflow("record_note")
        original = ScopedRun(self.writer, self.claim, workflow, self.invoke).manifest()
        outsider = ScopedRun(self.writer, peer, workflow, self.invoke).manifest()
        self.assertEqual(outsider["drafts"], [])
        self.assertEqual(
            original["drafts"],
            [
                {
                    "name": draft.name,
                    "revision": draft.revision,
                    "content": draft.content,
                }
            ],
        )
        prepare_workspace = runpy.run_path(str(ADAPTER))["prepare_workspace"]
        workspace = prepare_workspace(self.root, original["drafts"])
        self.assertEqual(
            (workspace / "drafts/comparison.md").read_text(), draft.content
        )
        self.assertFalse((workspace / "AGENTS.md").exists())
        self.assertEqual(
            json.loads((workspace / "drafts/index.json").read_bytes()),
            {"comparison": draft.revision},
        )
