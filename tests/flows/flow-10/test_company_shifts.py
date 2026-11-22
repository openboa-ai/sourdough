import json
import runpy
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time


FIXTURE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "flow-06/test_flow.py")
)
EMPLOYEE = FIXTURE["EMPLOYEE"]
cli = FIXTURE["cli"]
rows = FIXTURE["rows"]
SUPERVISOR = """
import sys
from sourdough import cli
worker = sys.argv.pop(1)
supervise = cli.supervise_worker

def start(command, *args, **options):
    child = [sys.executable, "-IB", "-c", worker, *command[4:]]
    return supervise(child, *args, **options)
cli.supervise_worker = start
raise SystemExit(cli.main())
"""
WORKER = """
from itertools import count
from time import time
from sourdough import assessment_run, cli

clock = count(int(time()) + 600, 301)
assessment_run.time = lambda: next(clock)
raise SystemExit(cli.main())
"""


class CompanyShifts(unittest.TestCase):
    def test_one_company_start_runs_two_due_reviews_without_a_market_or_client(self):
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            employee, store, envelope, source = (
                root / name
                for name in (
                    "employee",
                    "company.sqlite",
                    "staffing.json",
                    "source.json",
                )
            )
            employee.write_text(
                f"#!{sys.executable}\nsubmit_request = False\n" + EMPLOYEE
            )
            employee.chmod(0o700)
            profile = cli("employee-profile", employee)["execution_profile"]
            company = cli(
                "found",
                store,
                "--setup-identity",
                "company-only",
                "--charter-revision",
                "work",
                "--founder-scope",
                "manage-company",
                "--execution-profile",
                profile,
            )
            identity = ("--company-id", company["company_id"])
            now = int(time())
            envelope.write_text(
                json.dumps(
                    {
                        "authority": {
                            "manager_grant_id": company["grant_id"],
                            "execution_profile": profile,
                            "allowed_scope": "assessment",
                            "slots": 1,
                            "turns": 3,
                        },
                        "need": {
                            "duty_id": "records",
                            "summary": "Check company records",
                            "backlog": 4,
                            "available_capacity": 0,
                            "assigned_capacity": 1,
                            "available_at": now,
                        },
                    }
                )
            )
            source.write_text(
                json.dumps(
                    [
                        {
                            "record_id": "record",
                            "subject": "Published document count",
                            "recorded": 8,
                            "observed": 6,
                            "available_at": now,
                        }
                    ]
                )
            )
            cli("stage-staffing", store, envelope, *identity)
            cli("stage-assessment-source", store, source, *identity)
            activation = subprocess.run(
                [
                    sys.executable,
                    "-IB",
                    "-c",
                    SUPERVISOR,
                    WORKER,
                    "supervise-assessment",
                    str(store),
                    *identity,
                    "--employee",
                    str(employee),
                    "--turn-budget",
                    "4",
                    "--idle-seconds",
                    "2",
                    "--max-restarts",
                    "0",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(activation.returncode, 0, activation.stderr)
            result, supervision = map(json.loads, activation.stdout.splitlines())
            self.assertTrue(supervision["completed"])
            self.assertEqual(len(supervision["attempts"]), 1)
            first = result["review"]
            (later,) = result["later_reviews"]
            self.assertEqual(first["claim"]["agent_id"], later["claim"]["agent_id"])
            self.assertEqual(first["next_duty"]["duty_id"], later["claim"]["duty_id"])
            self.assertNotEqual(first["claim"]["run_id"], later["claim"]["run_id"])
            self.assertEqual(
                rows(
                    store,
                    "SELECT state FROM duty WHERE id=?",
                    (later["next_duty"]["duty_id"],),
                ),
                [("scheduled",)],
            )
            self.assertEqual(
                rows(store, "SELECT count(*) FROM work_review_decision"), [(2,)]
            )
            self.assertEqual(
                rows(
                    store,
                    "SELECT count(*) FROM work_result_read r JOIN work_follow_up w USING(duty_id) WHERE r.read_at >= w.due_at",
                ),
                [(2,)],
            )
            self.assertEqual(
                rows(
                    store,
                    "SELECT name FROM sqlite_schema WHERE name IN ('paper_binding', 'paper_order')",
                ),
                [],
            )
