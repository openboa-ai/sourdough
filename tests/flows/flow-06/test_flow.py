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
    choice = invoke("record_staffing_choice", {
        "evidence_revision": need["revision"], "action": "hire",
        "rationale": "Backlog exceeds available capacity.",
        "alternatives": ["reassign", "defer"], "next_check_seconds": 300,
    })
    result = invoke("request_hire", {
        "request_id": "fixture-hire", "choice_id": choice["choice_id"],
        "role": "reviewer", "execution_profile": need["profile"], "scope": need["scope"],
        "turns": 3,
    })
elif operation in ("assessment_duty", "hired_research_duty"):
    first = invoke("read_first_duty", {})
    invoke("accept_first_duty", {"revision": first["revision"], "rationale": first["assignment_summary"]})
    if operation == "assessment_duty":
        inputs = invoke("read_assessment_inputs", {})
        result = invoke("publish_assessment", {
            "source_revision": inputs["revision"], "record_id": "record",
            "disposition": "recommend_change", "rationale": "Observed six rather than eight.",
            "next_check_seconds": 60,
        })
    else:
        inputs = invoke("read_research_inputs", {})
        start, end = inputs["observations"]
        gross = end["price_minor_units"] - start["price_minor_units"]
        fees = sum((record["price_minor_units"] * inputs["fee_bps"] + 5000) // 10000 for record in (start, end))
        finding = invoke("publish_research_finding", {
            "snapshot_revision": inputs["revision"], "hypothesis": "Compare available fixture prices.",
            "start_id": start["observation_id"], "end_id": end["observation_id"],
            "gross_change_minor_units": gross, "round_trip_fee_minor_units": fees,
            "net_change_minor_units": gross - fees, "uncertainty": "Only an execution fixture.",
        })
        decision = invoke("record_research_decision", {
            "finding_id": finding["finding_id"], "finding_revision": finding["revision"],
            "call_id": "fixture-research", "action": "buy", "quantity": 1, "limit_minor_units": 5000,
            "rationale": "Use the measured positive change for one bounded synthetic order.", "next_check_seconds": 60,
        })
        result = (
            invoke("request_research_order", {"decision_id": decision["decision_id"]})
            if submit_request else decision
        )
elif operation in ("assessment_review", "review_duty"):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(request["read_endpoint"])
        with client.makefile() as stream:
            prior = json.load(stream)
    result = {
        "tool": "record_review_decision", "observation_revision": prior["revision"],
        "call_id": "fixture-review", "action": "hold", "next_check_seconds": 300,
        "rationale": "Check the original result: " + str(prior["revision"]),
    }
else:
    raise RuntimeError(operation)
print(json.dumps(result))
"""

WORKER = """
import os, signal, sqlite3, sys
from contextlib import closing
from pathlib import Path
from time import time
from sourdough import assessment_run, cli, company_assessment, hired_paper_run, onboarding
from sourdough.paper_venue import FixedQuoteVenue
marker = Path(sys.argv.pop(1))
fault_site = sys.argv.pop(1)
def interrupted():
    if not marker.exists():
        marker.touch()
        os.kill(os.getpid(), signal.SIGKILL)
class WithheldFill(FixedQuoteVenue):
    failed_history = False
    failed_sync = False

    def submit(self, *args):
        result = super().submit(*args)
        if fault_site in ("after_fill", "after_fill_with_lookup_timeout"):
            interrupted()
        return result

    def observe_execution(self, order_id):
        if fault_site == "after_fill_with_lookup_timeout" and not self.failed_history:
            self.failed_history = True
            raise TimeoutError("One missed history response")
        return super().observe_execution(order_id)

    def sync_account_execution(self, account_id, order_id):
        if fault_site == "after_fill_with_lookup_timeout" and not self.failed_sync:
            self.failed_sync = True
            raise TimeoutError("One missed account-sync response")
        return super().sync_account_execution(account_id, order_id)

apply_order = hired_paper_run._apply_paper_order
def withheld_order(store, claim, intent, venue, writer):
    if fault_site == "before_send":
        with closing(sqlite3.connect(venue.journal)) as db:
            assert db.execute("SELECT count(*) FROM venue_execution").fetchone() == (0,)
        interrupted()
    return apply_order(store, claim, intent, venue, writer)
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
def environment_clock():
    return time() + (121 if marker.exists() else 0)
assessment_run.time = environment_clock
hired_paper_run.time = environment_clock
hired_paper_run._apply_paper_order = withheld_order
cli.FixedQuoteVenue = WithheldFill
company_assessment.publish_assessment = withheld_assessment
onboarding.accept_first_duty = withheld_acceptance
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


def rows(store, sql):
    with closing(sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(sql).fetchall()


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
        for scope, submit_request, fault_site in (
            ("assessment", True, "after_publication"),
            ("assessment", True, "after_acceptance"),
            ("paper", True, "after_fill"),
            ("paper", True, "after_acceptance"),
            ("paper", True, "after_fill_with_lookup_timeout"),
            ("paper", True, "before_send"),
            ("paper", False, "none"),
        ):
            with (
                self.subTest(scope=scope, fault_site=fault_site),
                TemporaryDirectory() as local,
            ):
                root = Path(local)
                employee, store, venue = (
                    root / name
                    for name in ("employee", "company.sqlite", "venue.sqlite")
                )
                employee.write_text(
                    f"#!{sys.executable}\nsubmit_request = {submit_request!r}\n"
                    + EMPLOYEE
                )
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
                                "allowed_scope": scope,
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
                if scope == "assessment":
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
                    entry = ("supervise-assessment", str(store))
                else:
                    cli(
                        "bind-paper",
                        store,
                        venue,
                        *identity,
                        "--charter-revision",
                        "synthetic",
                        "--revision",
                        "mandate",
                        "--account-id",
                        "test",
                        "--rules",
                        "fixture",
                        "--starting-minor-units",
                        50000,
                        "--exposure-minor-units",
                        25000,
                        "--quote-minor-units",
                        5000,
                        "--fee-bps",
                        4,
                    )
                    source.write_text(
                        json.dumps(
                            [
                                {
                                    "observation_id": label,
                                    "price_minor_units": price,
                                    "observed_at": now + offset,
                                    "available_at": now + offset + 5,
                                }
                                for label, price, offset in (
                                    ("first", 4800, -120),
                                    ("last", 5000, -60),
                                )
                            ]
                        )
                    )
                    cli("stage-paper-observations", store, source, *identity)
                    entry = ("supervise-hired-paper", str(store), str(venue))
                response = subprocess.run(
                    [
                        sys.executable,
                        "-IB",
                        "-c",
                        DRIVER,
                        WORKER,
                        str(root / "cut"),
                        fault_site,
                        *entry,
                        *identity,
                        "--employee",
                        str(employee),
                        "--idle-seconds",
                        "6" if fault_site == "after_fill_with_lookup_timeout" else "2",
                        "--turn-budget",
                        "4" if fault_site == "after_acceptance" else "3",
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
                if not submit_request:
                    self.assertEqual(len(supervisor["attempts"]), 1)
                    self.assertEqual(result["research"]["decision"]["action"], "buy")
                    self.assertIsNone(result["review"])
                    self.assertEqual(
                        rows(venue, "SELECT count(*) FROM venue_execution"), [(0,)]
                    )
                    continue
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
                expected_runs = 4 if fault_site == "after_acceptance" else 3
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
                if scope == "assessment":
                    self.assertEqual(
                        rows(store, "SELECT count(*) FROM assessment_publication"),
                        [(1,)],
                    )
                    self.assertFalse(venue.exists())
                else:
                    self.assertEqual(
                        rows(
                            store, "SELECT count(*) FROM hired_research_order_request"
                        ),
                        [(1,)],
                    )
                    self.assertEqual(
                        rows(store, "SELECT count(*) FROM paper_fill"), [(1,)]
                    )
                    self.assertEqual(
                        rows(venue, "SELECT count(*) FROM venue_execution"), [(1,)]
                    )
                    if fault_site == "after_fill_with_lookup_timeout":
                        self.assertEqual(
                            rows(
                                store,
                                "SELECT attempts, next_query_at, sources FROM paper_reconciliation",
                            ),
                            [(2, None, "history,account_sync")],
                        )
                        self.assertEqual(result["reconciliation"]["unresolved"], 0)
