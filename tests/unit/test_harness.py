import json
import os
import runpy
import stat
import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from sourdough.agent_desk import desk_directory, save_draft
from sourdough.company import FoundingTerms, _connect, found
from sourdough.company_assets import Selection, assign_project, publish_selection
from sourdough.duty_run import PAPER_EXECUTION, DutyRefused, start_run
from sourdough.employee_process import EmployeeProcess, _ToolEndpoint
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

    def test_the_same_harness_accepts_a_work_wait_as_a_terminal_result(self):
        publish = self.workflow("publish_assessment")
        wait = self.workflow("request_peer_response")
        workflow = replace(
            publish,
            tools=publish.tools + wait.tools,
            terminal_tool=("publish_assessment", "request_peer_response"),
        )
        run = ScopedRun(self.writer, self.claim, workflow, self.invoke)
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(run.manifest()))
        outbox = self.root / "outbox"
        address = str(self.root / "tools.sock")
        with _ToolEndpoint(address, run.call) as endpoint:
            thread = Thread(
                target=endpoint.serve_forever, kwargs={"poll_interval": 0.05}
            )
            thread.start()
            try:
                listed, denied, accepted = self.send(
                    address, manifest, outbox, "request_peer_response"
                )
            finally:
                endpoint.shutdown()
                thread.join()
        self.assertEqual(
            [tool["name"] for tool in listed["tools"]],
            ["publish_assessment", "request_peer_response"],
        )
        self.assertTrue(denied["isError"])
        self.assertFalse(accepted["isError"])
        self.assertEqual(
            json.loads(outbox.read_bytes()), {"receipt": "recorded-by-company"}
        )
        self.assertEqual(
            self.calls, [("request_peer_response", {"record": "company document"})]
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
        prompt = runpy.run_path(str(ADAPTER))["scoped_work_prompt"]
        self.assertIn("Do not use the shell", prompt(original))
        original_data = prompt(original).split("\n")[-1]
        self.assertEqual(json.loads(original_data), original["drafts"])
        self.assertNotIn(draft.content, prompt(outsider))

    def test_private_drafts_physically_isolated_outside_company_sqlite(self):
        with closing(_connect(self.store, "ro")) as database:
            self.assertIsNone(
                database.execute(
                    "SELECT name FROM sqlite_schema WHERE name = 'desk_draft'"
                ).fetchone()
            )
        draft = save_draft(
            self.writer,
            self.claim,
            PAPER_EXECUTION,
            name="private-memo",
            content="Public excerpt. Secret draft appendix.",
            expected_revision=None,
        )
        agent_desk = desk_directory(self.store, self.claim.founder_id)
        drafts_dir = agent_desk / ".desk" / "drafts"
        self.assertEqual(stat.S_IMODE(agent_desk.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(drafts_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((drafts_dir / "private-memo.json").stat().st_mode), 0o600
        )
        self.assertEqual((drafts_dir / "private-memo.md").read_text(), draft.content)

        # Publish excerpt and confirm audit_event stores metadata only (zero raw draft text)
        with closing(_connect(self.store, "rw")) as database, database:
            database.execute(
                "UPDATE standing_grant SET scope = 'manage-company' WHERE id = ?",
                (self.claim.grant_id,),
            )
        project_rev = assign_project(
            self.writer,
            self.claim.grant_id,
            "memo-proj",
            (self.claim.founder_id,),
            publishers=(self.claim.founder_id,),
        )
        with closing(_connect(self.store, "rw")) as database, database:
            database.execute(
                "UPDATE standing_grant SET scope = 'paper' WHERE id = ?",
                (self.claim.grant_id,),
            )
        asset = publish_selection(
            self.writer,
            self.claim,
            PAPER_EXECUTION,
            Selection(
                "pub-1",
                draft.name,
                draft.revision,
                "memo-proj",
                project_rev,
                (self.claim.founder_id,),
                0,
                15,
            ),
        )
        self.assertEqual(asset.content, "Public excerpt.")
        with closing(_connect(self.store, "ro")) as database:
            audit_rows = database.execute(
                "SELECT payload_json FROM audit_event WHERE event_type = 'company.asset_published'"
            ).fetchall()
        self.assertEqual(len(audit_rows), 1)
        self.assertNotIn("Secret draft appendix", audit_rows[0][0])
        self.assertNotIn("Public excerpt", audit_rows[0][0])

        # Symlink inside private Desk drafts is refused fail-closed
        escape_target = self.root / "outside.txt"
        escape_target.write_text("escape")
        os.symlink(escape_target, drafts_dir / "evil.json")
        with self.assertRaisesRegex(DutyRefused, "regular file"):
            save_draft(
                self.writer,
                self.claim,
                PAPER_EXECUTION,
                name="evil",
                content="Should fail",
                expected_revision=None,
            )

    def test_two_plane_agent_session_trace_offload_and_fencing(self):
        large_body = "x" * 5000
        run = ScopedRun(
            self.writer,
            self.claim,
            self.workflow("record_note"),
            lambda _name, _args: {"receipt": "ok", "blob": large_body},
            offload_threshold_bytes=4096,
        )
        session = run.session
        self.assertEqual(session.company_id, self.claim.company_id)
        self.assertEqual(session.agent_id, self.claim.founder_id)
        self.assertEqual(session.run_id, self.claim.run_id)
        self.assertGreaterEqual(session.fencing_token, 1)
        # Trace file lives strictly outside the Agent Desk
        with self.assertRaises(ValueError):
            session.trace_path.relative_to(session.desk_path)

        offloaded = run.call("record_note", {"record": "secret-private-note"})
        self.assertTrue(offloaded["offloaded"])
        self.assertGreater(offloaded["bytes"], 4096)
        log_file = session.desk_path / offloaded["log_path"]
        self.assertTrue(log_file.is_file())
        self.assertEqual(stat.S_IMODE(log_file.stat().st_mode), 0o600)
        self.assertEqual(json.loads(log_file.read_text())["blob"], large_body)

        with self.assertRaisesRegex(DutyRefused, "not available"):
            run.call("unauthorized_tool", {})

        trace_lines = [
            json.loads(line)
            for line in session.trace_path.read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(len(trace_lines), 2)
        self.assertEqual(trace_lines[0]["status"], "ok")
        self.assertEqual(trace_lines[0]["fencing_token"], session.fencing_token)
        self.assertEqual(trace_lines[1]["status"], "refused")
        self.assertNotIn("secret-private-note", session.trace_path.read_text())

        # Fencing the work_runs row blocks subsequent broker tool calls immediately
        with closing(_connect(self.store, "rw")) as database, database:
            database.execute(
                "UPDATE work_runs SET status = 'fenced' WHERE run_id = ?",
                (self.claim.run_id,),
            )
        with self.assertRaisesRegex(DutyRefused, "fenced and no longer current"):
            run.call("record_note", {"record": "after-fence"})

    @unittest.skipUnless(sys.platform == "darwin", "macOS sandbox-exec boundary test")
    def test_real_macos_sandbox_exec_isolates_desk_and_brokers_capabilities(self):
        # Seed sibling agent Desk and a trace canary
        peer_desk = desk_directory(self.store, "peer-agent")
        (peer_desk / ".desk" / "drafts").mkdir(parents=True, mode=0o700)
        peer_secret = peer_desk / ".desk" / "drafts" / "secret.md"
        peer_secret.write_text("PEER_ONLY_SECRET")
        save_draft(
            self.writer,
            self.claim,
            PAPER_EXECUTION,
            name="own-note",
            content="FOUNDER_OWN_DRAFT",
            expected_revision=None,
        )
        script = self.root / "sandboxed_employee"
        script.write_text(
            f"#!{sys.executable}\n"
            + f"""
import errno, json, socket, sys
from pathlib import Path

req = json.load(sys.stdin)
op = req["operation"]
if op == "identify":
    print(json.dumps({{"provider": "fixture", "model": "sandbox-check", "revision": "1", "sandbox_backend": "local-macos"}}))
    raise SystemExit(0)

desk = Path(req["desk_path"])
assert (desk / ".desk/drafts/own-note.md").read_text() == "FOUNDER_OWN_DRAFT"
(desk / "scratch.txt").write_text("INNER_PLANE_WRITE_OK")

for blocked in ({str(self.store)!r}, {str(peer_secret)!r}):
    try:
        Path(blocked).read_text()
        raise AssertionError("Should be blocked by sandbox-exec: " + blocked)
    except PermissionError:
        pass

try:
    socket.create_connection(("127.0.0.1", 80), timeout=1)
    raise AssertionError("Outbound network should be blocked by sandbox-exec")
except OSError as err:
    assert err.errno in (errno.EPERM, errno.EACCES), err

with socket.socket(socket.AF_UNIX) as client:
    client.connect(req["read_endpoint"])
    client.sendall(json.dumps({{"name": "record_note", "arguments": {{"record": "via-broker"}}}}).encode() + b"\\n")
    with client.makefile() as stream:
        reply = json.load(stream)
assert reply == {{"receipt": "recorded-by-company"}}, reply

# Trace directory created by Outer Broker must also remain invisible to Inner Plane
trace_root = Path({str(self.root / ".sourdough-traces")!r})
try:
    list(trace_root.iterdir())
    raise AssertionError("Trace root should be blocked by sandbox-exec")
except PermissionError:
    pass

print(json.dumps({{"status": "isolated", "reply": reply}}))
"""
        )
        script.chmod(0o700)
        employee = EmployeeProcess(script)
        with closing(_connect(self.store, "rw")) as database, database:
            database.execute(
                "UPDATE agent SET execution_profile = ? WHERE id = ?",
                (employee.profile, self.claim.founder_id),
            )
        claim = replace(self.claim, execution_profile=employee.profile)
        run = ScopedRun(self.writer, claim, self.workflow("record_note"), self.invoke)
        output = employee.run_sandboxed_scoped("assessment_duty", run, store=self.store)
        self.assertEqual(
            output, {"status": "isolated", "reply": {"receipt": "recorded-by-company"}}
        )
        self.assertEqual(
            (run.session.desk_path / "scratch.txt").read_text(), "INNER_PLANE_WRITE_OK"
        )
        self.assertEqual(self.calls, [("record_note", {"record": "via-broker"})])
        trace_records = [
            json.loads(line) for line in run.session.trace_path.read_text().splitlines()
        ]
        self.assertEqual(len(trace_records), 1)
        self.assertEqual(trace_records[0]["tool"], "record_note")
        self.assertEqual(trace_records[0]["status"], "ok")
