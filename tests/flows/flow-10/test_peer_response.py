import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

from sourdough.company_assets import assign_project
from sourdough.writer_lease import hold_company_writer


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


EMPLOYEE = r"""
import json, socket, sys

request = json.load(sys.stdin)
operation = request["operation"]

def call(name, **arguments):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(request["read_endpoint"])
        client.sendall(json.dumps({"name": name, "arguments": arguments}).encode() + b"\n")
        with client.makefile() as stream:
            result = json.load(stream)
    assert "error" not in result, result
    return result

if operation == "identify":
    result = {"provider": "fixture", "model": "dependent-work", "revision": "1"}
elif operation == "ready":
    result = {"ready": True}
elif operation == "staffing_duty":
    need = call("read_staffing_need")
    terms = dict(evidence_revision=need["revision"], action="hire",
                 rationale="No available reviewer.", alternatives=["reassign", "defer"],
                 next_check_seconds=300,
                 hire=dict(request_id="hire-reviewer", role="reviewer", turns=2))
    result = call("submit_staffing_decision", **terms)
    assert call("submit_staffing_decision", **terms) == result
    assert result["hire"]["allocated_turns"] == 2
elif operation == "assessment_duty":
    tools = {item["name"] for item in request["harness"]["tools"]}
    duty = call("read_first_duty")
    call("accept_first_duty", revision=duty["revision"], rationale="Review the count.")
    records = call("read_assessment_inputs")
    if "read_peer_outcome" not in tools:
        peer = call("read_assigned_peer")
        assert peer["project_id"] == "records"
        result = call("request_peer_response", request_key="timing", recipient_id=peer["recipient_id"],
                      question="Reconcile now or await the next batch?", expected_response="Choose the timing.",
                      expires_at=peer["company_time"] + 60, asset_id="", asset_revision="")
    else:
        reply = call("read_peer_outcome")
        assert reply["outcome"] == "answered"
        result = call("publish_assessment", source_revision=records["revision"], record_id="count",
                      disposition="investigate", rationale=reply["answer"], next_check_seconds=600)
else:
    assert operation == "assessment_review", operation
    assert {item["name"] for item in request["harness"]["tools"]} == {
        "read_peer_question", "answer_peer_question"
    }
    assert request["harness"]["drafts"] == []
    question = call("read_peer_question")
    assert question["question"] == "Reconcile now or await the next batch?"
    result = call("answer_peer_question", outcome="answered", answer="Check after the next batch.")
print(json.dumps(result))
"""


class PeerResponse(unittest.TestCase):
    def test_company_runs_response_and_resumes_original_duty_without_a_second_start(
        self,
    ):
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
            employee.write_text(f"#!{sys.executable}\n" + EMPLOYEE)
            employee.chmod(0o700)
            profile = cli("employee-profile", employee)["execution_profile"]
            company = cli(
                "found",
                store,
                "--setup-identity",
                "peer-decision",
                "--charter-revision",
                "work",
                "--founder-scope",
                "manage-company",
                "--execution-profile",
                profile,
            )
            identity = ("--company-id", company["company_id"])
            with hold_company_writer(store, company["company_id"]) as writer:
                assign_project(
                    writer,
                    company["grant_id"],
                    "records",
                    (company["founder_id"],),
                    publishers=(),
                )
            now = int(time())
            envelope.write_text(
                json.dumps(
                    {
                        "authority": {
                            "manager_grant_id": company["grant_id"],
                            "execution_profile": profile,
                            "allowed_scope": "assessment",
                            "slots": 1,
                            "turns": 2,
                        },
                        "need": {
                            "duty_id": "check-records",
                            "summary": "Review records in the project.",
                            "backlog": 4,
                            "available_capacity": 0,
                            "assigned_capacity": 1,
                            "available_at": now,
                            "project_id": "records",
                        },
                    }
                )
            )
            source.write_text(
                json.dumps(
                    [
                        {
                            "record_id": "count",
                            "subject": "Documents",
                            "recorded": 8,
                            "observed": 6,
                            "available_at": now,
                        },
                    ]
                )
            )
            cli("stage-staffing", store, envelope, *identity)
            cli("stage-assessment-source", store, source, *identity)
            activation = subprocess.run(
                [
                    sys.executable,
                    "-IB",
                    "-m",
                    "sourdough.cli",
                    "supervise-assessment",
                    str(store),
                    *identity,
                    "--employee",
                    str(employee),
                    "--turn-budget",
                    "4",
                    "--idle-seconds",
                    "0",
                    "--max-restarts",
                    "0",
                ],
                capture_output=True,
                text=True,
                timeout=25,
            )
            self.assertEqual(activation.returncode, 0, activation.stderr)
            _, supervision = map(json.loads, activation.stdout.splitlines())
            self.assertTrue(supervision["completed"])
            self.assertEqual(len(supervision["attempts"]), 1)
            [(requester,)] = rows(store, "SELECT agent_id FROM hire_receipt")
            [
                (
                    first_duty,
                    original_run,
                    response_duty,
                    answer_run,
                    resumed_run,
                    outcome,
                )
            ] = rows(
                store,
                """SELECT requester_work_item_id,requester_run_id,response_work_item_id,
                          answered_run_id,resumed_run_id,state
                   FROM work_peer_requests""",
            )
            self.assertEqual(outcome, "answered")
            self.assertEqual(
                rows(
                    store,
                    """SELECT d.agent_id,d.id,r.run_id,r.purpose FROM duty_run r
                       JOIN duty d ON d.id=r.duty_id ORDER BY r.rowid""",
                )[1:],
                [
                    (requester, first_duty, original_run, "assessment"),
                    (company["founder_id"], response_duty, answer_run, "peer_response"),
                    (requester, first_duty, resumed_run, "assessment"),
                ],
            )
            self.assertNotEqual(original_run, resumed_run)
            self.assertEqual(
                rows(
                    store,
                    "SELECT duty_id,run_id,json_extract(contents,'$.rationale') FROM assessment_publication",
                ),
                [(first_duty, resumed_run, "Check after the next batch.")],
            )
            self.assertEqual(rows(store, "PRAGMA foreign_key_check"), [])
