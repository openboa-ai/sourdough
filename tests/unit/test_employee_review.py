import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.duty_run import DutyRefused
from sourdough.employee_process import EmployeeProcess, _ResultEndpoint


class EmployeeReview(unittest.TestCase):
    def test_profile_composition_requires_the_declared_sandbox_selection(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        peer = folder / "peer"
        peer.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "assert json.load(sys.stdin)['operation'] == 'identify'\n"
            "print(json.dumps({'provider':'test','model':'scripted','revision':'1',"
            "'sandbox_backend':'local-macos'}))\n"
        )
        peer.chmod(0o700)

        def compose(*options):
            return subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "sourdough.cli",
                    "employee-profile",
                    str(peer),
                    *options,
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        chosen = compose("--sandbox", "local-macos")
        self.assertEqual(chosen.returncode, 0, chosen.stderr)
        self.assertEqual(
            json.loads(chosen.stdout),
            {
                "execution_profile": EmployeeProcess(peer).profile,
                "sandbox_backend": "local-macos",
            },
        )
        self.assertEqual(compose("--sandbox", "local-macos").stdout, chosen.stdout)
        for options in ((), ("--sandbox", "hosted")):
            refused = compose(*options)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(refused.stdout, "")

        peer.write_text(peer.read_text().replace("'local-macos'", "'hosted'"))
        refused = compose("--sandbox", "hosted")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("sandbox backend is not qualified", refused.stderr)

    def test_binding_executes_a_scoped_review_process(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        peer = folder / "peer"
        peer.write_text(
            f"#!{sys.executable}\n"
            "import json, socket, sys\n"
            "request = json.load(sys.stdin)\n"
            "if request['operation'] == 'identify':\n"
            "    result = {'provider':'test','model':'scripted','revision':'1'}\n"
            "else:\n"
            "    assert request['operation'] == 'review_duty'\n"
            "    with socket.socket(socket.AF_UNIX) as client:\n"
            "        client.connect(request['read_endpoint'])\n"
            "        with client.makefile() as stream:\n"
            "            result = json.load(stream)\n"
            "print(json.dumps(result))\n"
        )
        peer.chmod(0o700)
        employee = EmployeeProcess(peer)
        self.assertEqual(
            employee.review(lambda: {"revision": "book-v1", "position_quantity": 2}),
            {"revision": "book-v1", "position_quantity": 2},
        )

        def deny():
            raise DutyRefused("not current")

        self.assertEqual(
            employee.review(deny),
            {"error": "Work result is not available to this claim"},
        )

    def test_review_mcp_rejects_order_and_requires_a_prior_read(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        outbox = folder / "proposal"
        script = Path(__file__).resolve().parents[2] / "tools/codex_employee.py"
        proposal = {
            "observation_revision": "book-v1",
            "action": "investigate",
            "rationale": "The booked position is 2 units.",
            "next_check_seconds": 240,
        }

        def call(request_id, name, arguments):
            return {
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }

        requests = [
            {"id": 1, "method": "tools/list"},
            call(2, "record_review_decision", proposal),
            call(3, "paper_buy", {"quantity": 1, "limit_minor_units": 100}),
            call(4, "read_work_result", {}),
            call(
                5,
                "record_review_decision",
                proposal | {"observation_revision": "other"},
            ),
            call(6, "record_review_decision", proposal),
            call(7, "record_review_decision", proposal),
        ]
        with _ResultEndpoint(
            str(folder / "result.sock"),
            lambda: {"revision": "book-v1", "position_quantity": 2},
        ) as endpoint:
            with subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--mcp-review",
                    endpoint.server_address,
                    str(outbox),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            ) as process:
                endpoint.timeout = 5
                process.stdin.write(
                    "".join(json.dumps(request) + "\n" for request in requests)
                )
                process.stdin.flush()
                endpoint.handle_request()
                output, _ = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
        tools, *results = [json.loads(line)["result"] for line in output.splitlines()]
        self.assertEqual(
            [tool["name"] for tool in tools["tools"]],
            ["read_work_result", "record_review_decision"],
        )
        self.assertEqual(
            [result["isError"] for result in results],
            [True, True, False, True, False, True],
        )
        self.assertEqual(
            json.loads(results[2]["content"][0]["text"])["position_quantity"], 2
        )
        record = json.loads(outbox.read_bytes())
        self.assertEqual(record.pop("tool"), "record_review_decision")
        self.assertTrue(record.pop("call_id"))
        self.assertEqual(record, proposal)
