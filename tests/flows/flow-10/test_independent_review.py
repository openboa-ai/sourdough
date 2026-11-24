import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

from sourdough.agent_desk import resolve_desk
from sourdough.duty_run import ASSESSMENT_REVIEW, DutyClaim, DutyRefused
from sourdough.writer_lease import hold_company_writer

from test_flow import cli, rows, seed_assessment_reviews


EMPLOYEE = """
import json, socket, sys

request = json.load(sys.stdin)
if request["operation"] == "identify":
    print(json.dumps({"provider": "fixture", "model": "independent-reviews", "revision": "1"}))
    raise SystemExit(0)
assert request["operation"] == "assessment_review"

def call(name, arguments):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(request["read_endpoint"])
        connection.sendall(json.dumps({"name": name, "arguments": arguments}).encode() + b"\\n")
        with connection.makefile() as stream:
            result = json.load(stream)
    assert "error" not in result, result
    return result

prior = call("read_work_result", {})
drafts = request["harness"]["drafts"]
if prior["shared_evidence"]["asset_id"] == "author-input":
    if not drafts:
        call("save_private_draft", {
            "name": "report", "content": "Keep the unfinished review.", "expected_revision": "",
        })
        raise SystemExit(42)
    assert len(drafts) == 1 and drafts[0]["content"] == "Keep the unfinished review."
else:
    assert prior["shared_evidence"]["asset_id"] == "recipient-input"
    assert drafts == []
result = call("record_review_decision", {
    "observation_revision": prior["revision"], "action": "hold",
    "rationale": "Verified my assigned company record.", "next_check_seconds": 86400,
})
print(json.dumps(result))
"""


class IndependentReview(unittest.TestCase):
    def test_interrupted_review_does_not_stop_other_due_work(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        employee, store = root / "employee", root / "company.sqlite"
        employee.write_text(f"#!{sys.executable}\n" + EMPLOYEE)
        employee.chmod(0o700)
        profile = cli("employee-profile", employee)["execution_profile"]
        company = cli(
            "found",
            store,
            "--setup-identity",
            "independent-review",
            "--charter-revision",
            "work",
            "--founder-scope",
            "manage-company",
            "--execution-profile",
            profile,
        )
        company_id = company["company_id"]
        seed_assessment_reviews(store, company, profile, int(time()))
        command = [
            sys.executable,
            "-IB",
            "-m",
            "sourdough.cli",
            "supervise-assessment",
            str(store),
            "--company-id",
            company_id,
            "--employee",
            str(employee),
            "--turn-budget",
            "3",
            "--idle-seconds",
            "0",
            "--max-restarts",
            "0",
        ]
        failed = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(json.loads(failed.stdout)["completed"])
        self.assertEqual(
            rows(
                store,
                "SELECT name FROM sqlite_schema WHERE name='work_review_decision'",
            ),
            [("work_review_decision",)],
            "The independent reviewer must commit before the worker reports the failure",
        )
        self.assertEqual(
            rows(store, "SELECT duty_id FROM work_review_decision"),
            [("recipient-review",)],
        )
        self.assertEqual(
            rows(store, "SELECT state FROM duty WHERE id='author-review'"),
            [("incomplete",)],
        )
        [(old_run,)] = rows(
            store, "SELECT run_id FROM duty_run WHERE duty_id='author-review'"
        )
        old_claim = DutyClaim(
            company_id, "author", "author-grant", "author-review", old_run, profile
        )
        with hold_company_writer(store, company_id) as writer:
            with self.assertRaisesRegex(DutyRefused, "current running duty"):
                resolve_desk(writer, old_claim, ASSESSMENT_REVIEW, include_drafts=True)

        recovered = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        result, supervision = map(json.loads, recovered.stdout.splitlines())
        self.assertTrue(supervision["completed"])
        claim = result["review"]["claim"]
        self.assertEqual(
            (claim["agent_id"], claim["duty_id"]), ("author", "author-review")
        )
        self.assertNotEqual(claim["run_id"], old_run)
        self.assertEqual(
            rows(
                store,
                "SELECT run_id,is_current FROM duty_run WHERE duty_id='author-review' ORDER BY rowid",
            ),
            [(old_run, 0), (claim["run_id"], 1)],
        )
        self.assertEqual(
            rows(store, "SELECT duty_id FROM work_review_decision ORDER BY rowid"),
            [("recipient-review",), ("author-review",)],
        )
        self.assertEqual(rows(store, "PRAGMA foreign_key_check"), [])
