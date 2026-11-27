"""Connected Flow 06: Core OS assessment, staffing, onboarding, and SIGKILL recovery."""

import json
import signal
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time


EMPLOYEE = r"""
import json, socket, sys
request = json.load(sys.stdin)
operation = request["operation"]
def invoke(name, args):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(request["read_endpoint"])
        client.sendall(json.dumps({"name": name, "arguments": args}).encode() + b"\n")
        with client.makefile() as stream:
            response = json.load(stream)
    assert "error" not in response, response
    return response
if operation == "identify":
    result = {"provider": "fixture", "model": "scripted", "revision": "1"}
elif operation == "ready":
    result = {"ready": True}
elif operation == "staffing_duty":
    need = invoke("read_staffing_need", {})
    choice = need.get("saved_choice") or invoke("record_staffing_choice", {
        "evidence_revision": need["revision"], "action": "hire",
        "rationale": "Backlog exceeds available capacity.",
        "alternatives": ["reassign", "defer"], "next_check_seconds": 300,
    })
    result = invoke("request_hire", {
        "request_id": "fixture-hire", "choice_id": choice["choice_id"],
        "role": "reviewer", "execution_profile": need["profile"], "scope": need["scope"],
        "turns": 3,
    })
elif operation == "assessment_duty":
    first = invoke("read_first_duty", {})
    invoke("accept_first_duty", {"revision": first["revision"], "rationale": first["assignment_summary"]})
    inputs = invoke("read_assessment_inputs", {})
    invoke("save_private_draft", {
        "name": "private-research", "content": "Private comparison notes.",
        "expected_revision": "",
    })
    result = invoke("publish_assessment", {
        "source_revision": inputs["revision"], "record_id": "record",
        "disposition": "recommend_change", "rationale": "Observed six rather than eight.",
        "next_check_seconds": 60,
    })
elif operation == "assessment_review":
    tools = [tool["name"] for tool in request["harness"]["tools"]]
    assert tools == ["save_private_draft", "read_work_result", "record_review_decision"]
    drafts = request["harness"]["drafts"]
    assert len(drafts) == 1 and drafts[0]["content"] == "Private comparison notes."
    prior = invoke("read_work_result", {})
    result = invoke("record_review_decision", {
        "observation_revision": prior["revision"], "action": "hold", "next_check_seconds": 300,
        "rationale": "Check the original result: " + str(prior["revision"]),
    })
else:
    raise RuntimeError(operation)
print(json.dumps(result))
"""

WORKER = """
import os, signal, sys
from pathlib import Path
from time import time
from sourdough import assessment_run, cli, company_assessment, onboarding, staffing_tools
marker = Path(sys.argv.pop(1))
fault_site = sys.argv.pop(1)
def interrupted():
    if not marker.exists():
        marker.touch()
        os.kill(os.getpid(), signal.SIGKILL)
publish = company_assessment.publish_assessment
def withheld_assessment(*args, **kwargs):
    result = publish(*args, **kwargs)
    interrupted()
    return result
accept = onboarding.accept_first_duty
def withheld_acceptance(*args, **kwargs):
    result = accept(*args, **kwargs)
    if fault_site == "after_acceptance":
        interrupted()
    return result
record_choice = staffing_tools.record_choice
def withheld_choice(*args, **kwargs):
    result = record_choice(*args, **kwargs)
    if fault_site == "after_staffing_choice":
        interrupted()
    return result
def environment_clock():
    return time() + (121 if marker.exists() else 0)
assessment_run.time = environment_clock
company_assessment.publish_assessment = withheld_assessment
onboarding.accept_first_duty = withheld_acceptance
staffing_tools.record_choice = withheld_choice
raise SystemExit(cli.main())
"""

DRIVER = """
import sys
from sourdough import cli
worker, marker, fault_site = sys.argv[1:4]
del sys.argv[1:4]
supervise = cli.supervise_worker
def start(command, store, company_id, **options):
    candidate = [sys.executable, "-IB", "-c", worker, marker, fault_site, *command[4:]]
    return supervise(candidate, store, company_id, **options)
cli.supervise_worker = start
raise SystemExit(cli.main())
"""


def cli(command, *arguments):
    result = subprocess.run(
        [sys.executable, "-IB", "-m", "sourdough.cli", command, *map(str, arguments)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def rows(store, sql, parameters=()):
    with closing(sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(sql, parameters).fetchall()


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
        for fault_site in (
            "after_publication",
            "after_acceptance",
            "after_staffing_choice",
        ):
            with (
                self.subTest(fault_site=fault_site),
                TemporaryDirectory() as local,
            ):
                root = Path(local)
                employee, store = root / "employee", root / "company.sqlite"
                employee.write_text(f"#!{sys.executable}\n" + EMPLOYEE)
                employee.chmod(0o700)
                profile = cli("employee-profile", employee)["execution_profile"]
                founded = cli(
                    "found",
                    store,
                    "--setup-identity",
                    "fixture",
                    "--charter-revision",
                    "synthetic",
                    "--founder-scope",
                    "manage-company",
                    "--execution-profile",
                    profile,
                )
                identity = ("--company-id", founded["company_id"])
                now = int(time())
                envelope, source = root / "envelope.json", root / "records.json"
                envelope.write_text(
                    json.dumps(
                        {
                            "authority": {
                                "manager_grant_id": founded["grant_id"],
                                "execution_profile": profile,
                                "allowed_scope": "assessment",
                                "slots": 1,
                                "turns": 5,
                            },
                            "need": {
                                "duty_id": "capacity",
                                "summary": "Inspect available evidence",
                                "backlog": 5,
                                "available_capacity": 0,
                                "assigned_capacity": 1,
                                "available_at": now,
                            },
                        }
                    )
                )
                cli("stage-staffing", store, envelope, *identity)
                source.write_text(
                    json.dumps(
                        [
                            {
                                "record_id": "record",
                                "subject": "Reviewed documents",
                                "recorded": 8,
                                "observed": 6,
                                "available_at": now,
                            }
                        ]
                    )
                )
                cli("stage-assessment-source", store, source, *identity)
                extra_run = fault_site in (
                    "after_acceptance",
                    "after_staffing_choice",
                )
                response = subprocess.run(
                    [
                        sys.executable,
                        "-IB",
                        "-c",
                        DRIVER,
                        WORKER,
                        str(root / "cut"),
                        fault_site,
                        "supervise-assessment",
                        str(store),
                        *identity,
                        "--employee",
                        str(employee),
                        "--idle-seconds",
                        "2",
                        "--turn-budget",
                        "4" if extra_run else "3",
                        "--max-restarts",
                        "1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(response.returncode, 0, response.stderr)
                result, supervisor = [
                    json.loads(line) for line in response.stdout.splitlines()
                ]
                original, resumed = supervisor["attempts"]
                self.assertEqual(original["returncode"], -signal.SIGKILL)
                self.assertLess(original["exited_at_ns"], resumed["ready_at_ns"])
                claim = result["review"]["claim"]
                ((agent_id, first_duty),) = rows(
                    store,
                    """SELECT a.agent_id,a.first_duty_id FROM hire_onboarding a
                       JOIN onboarding_acceptance w ON w.duty_id=a.first_duty_id""",
                )
                self.assertNotEqual(agent_id, founded["founder_id"])
                self.assertEqual(claim["agent_id"], agent_id)
                self.assertNotEqual(claim["duty_id"], first_duty)
                self.assertEqual(claim["execution_profile"], profile)
                expected_runs = 4 if extra_run else 3
                self.assertEqual(
                    rows(store, "SELECT count(*) FROM duty_run"), [(expected_runs,)]
                )
                if fault_site == "after_acceptance":
                    self.assertEqual(
                        rows(
                            store,
                            """SELECT accepted.is_current, count(resumed.run_id)
                               FROM onboarding_acceptance receipt
                               JOIN duty_run accepted ON accepted.run_id=receipt.run_id
                               JOIN duty_run resumed ON resumed.duty_id=receipt.duty_id
                                   AND resumed.is_current=1
                               GROUP BY accepted.run_id""",
                        ),
                        [(0, 1)],
                    )
                self.assertEqual(
                    rows(store, "SELECT count(*) FROM work_follow_up"), [(2,)]
                )
                self.assertEqual(rows(store, "PRAGMA foreign_key_check"), [])
                if fault_site == "after_staffing_choice":
                    self.assertEqual(
                        rows(
                            store,
                            """SELECT decision.run_id!=admission.run_id, origin.is_current
                               FROM staffing_choice decision
                               JOIN duty_run origin ON origin.run_id=decision.run_id
                               JOIN hire_receipt hired ON hired.choice_id=decision.choice_id
                               JOIN hire_admission_run admission ON admission.request_id=hired.request_id""",
                        ),
                        [(1, 0)],
                    )
                self.assertEqual(
                    rows(store, "SELECT count(*) FROM assessment_publication"),
                    [(1,)],
                )
                self.assertEqual(
                    rows(
                        store,
                        "SELECT domain FROM installed_domains ORDER BY domain",
                    ),
                    [("core",)],
                )
