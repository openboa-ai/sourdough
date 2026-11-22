import json
import runpy
import subprocess
import sys
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

from sourdough.company import _connect
from sourdough.company_assets import assign_project
from sourdough.staffing import SCHEMA as STAFFING_SCHEMA
from sourdough.writer_lease import hold_company_writer


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

HANDOFF_EMPLOYEE = r"""
import json, socket, sys, traceback
from pathlib import Path

def report_error(kind, error, stack):
    Path(__file__).with_suffix(".error").write_text("".join(traceback.format_exception(kind, error, stack)))

sys.excepthook = report_error

request = json.load(sys.stdin)
operation = request["operation"]

def call(name, arguments):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(request["read_endpoint"])
        client.sendall(json.dumps({"name": name, "arguments": arguments}).encode() + b"\n")
        with client.makefile() as stream:
            result = json.load(stream)
    assert "error" not in result, result
    return result

if operation == "identify":
    result = {"provider": "fixture", "model": "collaboration-script", "revision": "1"}
else:
    assert operation == "assessment_review", operation
    prior = call("read_work_result", {})
    evidence = prior["shared_evidence"]
    if evidence["asset_id"] == "author-input":
        [project] = call("read_project_assignments", {"after": ""})["projects"]
        draft = call("save_private_draft", {
            "name": "review", "content": "Checked discrepancy. Private details.",
            "expected_revision": "",
        })
        asset = call("publish_draft_selection", {
            "request_id": "selected", "draft_name": draft["name"], "draft_revision": draft["revision"],
            "project_id": project["project_id"], "project_revision": project["revision"],
            "audience": ["recipient"], "start": 0, "end": 20,
        })
        call("offer_handoff", {
            "request_id": "transfer", "recipient_id": "recipient", "asset_id": asset["asset_id"],
            "expected_result": "Independently check the discrepancy.", "expires_at": 4102444800,
        })
        rationale = "Pass the check to another Agent."
    elif evidence["asset_id"] == "recipient-input":
        [offer] = call("read_handoff_offers", {})["offers"]
        assert offer["sender_id"] == "author", offer
        call("accept_handoff", {"offer_id": offer["offer_id"]})
        rationale = "Accepted the offered check in a separate review."
    else:
        assert evidence["author_id"] == "author"
        assert evidence["content"] == "Checked discrepancy."
        assert request["harness"]["drafts"] == []
        rationale = "Independently reviewed the shared discrepancy."
    result = call("record_review_decision", {
        "observation_revision": prior["revision"], "action": "hold",
        "rationale": rationale, "next_check_seconds": 5400 if evidence["asset_id"] == "recipient-input" else 60,
    })
print(json.dumps(result))
"""


def seed_assessment_reviews(store, company, profile, now):
    company_id, manager = company["company_id"], company["grant_id"]
    with hold_company_writer(store, company_id) as writer:
        with closing(_connect(store, "rw")) as database, database:
            for schema in STAFFING_SCHEMA:
                database.execute(schema)
            database.execute(
                "CREATE TABLE private_desk (agent_id TEXT PRIMARY KEY, manifest_revision TEXT NOT NULL)"
            )
            database.execute(
                "INSERT INTO staffing_authority VALUES (?,?,'assessment',2,4,'manage-company')",
                (manager, profile),
            )
            for agent, due in (("author", now - 20), ("recipient", now - 10)):
                request_id, choice, grant, review = (
                    f"{agent}-{suffix}"
                    for suffix in ("request", "choice", "grant", "review")
                )
                history, run_id = f"{agent}-hire-history", f"{agent}-hire-run"
                database.execute(
                    "INSERT INTO duty VALUES (?,?,'settled')",
                    (history, company["founder_id"]),
                )
                database.execute(
                    "INSERT INTO duty_run VALUES (?,?,?,'manage-company','staffing',0)",
                    (run_id, history, manager),
                )
                database.execute(
                    "INSERT INTO staffing_assignment VALUES (?,?,?,'fixture',?)",
                    (history, manager, "{}", now),
                )
                database.execute(
                    "INSERT INTO staffing_read VALUES (?,'{}','fixture',?)",
                    (run_id, now),
                )
                database.execute(
                    "INSERT INTO staffing_choice VALUES (?,?,?,'{}',?)",
                    (history, run_id, choice, now),
                )
                database.execute(
                    "INSERT INTO agent VALUES (?,?,?)", (agent, company_id, profile)
                )
                database.execute(
                    "INSERT INTO standing_grant VALUES (?,?,'assessment')",
                    (grant, agent),
                )
                database.execute(
                    "INSERT INTO hire_receipt VALUES (?,?,?,?,?,4,'{}',?)",
                    (request_id, choice, manager, agent, grant, now),
                )
                database.execute(
                    "INSERT INTO private_desk VALUES (?,?)", (agent, f"{agent}-desk")
                )
                database.execute(
                    "INSERT INTO duty VALUES (?,?,'scheduled')", (review, agent)
                )
                database.execute(
                    """INSERT INTO work_follow_up VALUES
                       (?,?,?,'assessment',?,'shared_asset',?,?,?,'assessment_review')""",
                    (
                        history,
                        review,
                        grant,
                        due,
                        f"{agent}-input",
                        sha256(agent.encode()).hexdigest(),
                        due,
                    ),
                )
        assign_project(
            writer, manager, "analysis", ("author", "recipient"), publishers=("author",)
        )
        with closing(_connect(store, "rw")) as database, database:
            for agent in ("author", "recipient"):
                database.execute(
                    """INSERT INTO shared_publication
                       (asset_id,project_id,author_id,duty_id,content,request_id,request_digest,audience) VALUES
                       (?,'analysis',?,?,?,'seed','seed',?)""",
                    (
                        f"{agent}-input",
                        agent,
                        f"{agent}-hire-history",
                        agent,
                        json.dumps([agent]),
                    ),
                )


class CompanyShifts(unittest.TestCase):
    def test_sequential_agents_offer_accept_and_review_with_the_client_closed(self):
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            employee, store = root / "employee", root / "company.sqlite"
            employee.write_text(f"#!{sys.executable}\n" + HANDOFF_EMPLOYEE)
            employee.chmod(0o700)
            profile = cli("employee-profile", employee)["execution_profile"]
            company = cli(
                "found",
                store,
                "--setup-identity",
                "collaboration",
                "--charter-revision",
                "work",
                "--founder-scope",
                "manage-company",
                "--execution-profile",
                profile,
            )
            seed_assessment_reviews(store, company, profile, int(time()))
            activation = subprocess.run(
                [
                    sys.executable,
                    "-IB",
                    "-c",
                    SUPERVISOR,
                    WORKER,
                    "supervise-assessment",
                    str(store),
                    "--company-id",
                    company["company_id"],
                    "--employee",
                    str(employee),
                    "--turn-budget",
                    "3",
                    "--idle-seconds",
                    "2",
                    "--max-restarts",
                    "0",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            error = employee.with_suffix(".error")
            self.assertEqual(
                activation.returncode,
                0,
                activation.stderr + (error.read_text() if error.exists() else ""),
            )
            result, supervision = map(json.loads, activation.stdout.splitlines())
            self.assertTrue(supervision["completed"])
            sender = result["review"]
            recipient, next_review = result["later_reviews"]
            self.assertEqual(
                [
                    review["claim"]["agent_id"]
                    for review in (sender, recipient, next_review)
                ],
                ["author", "recipient", "recipient"],
            )
            self.assertEqual(
                len(
                    {
                        review["claim"]["run_id"]
                        for review in (sender, recipient, next_review)
                    }
                ),
                3,
            )
            self.assertEqual(
                rows(store, "SELECT successor_id,accepted_run FROM work_handoff"),
                [(next_review["claim"]["duty_id"], recipient["claim"]["run_id"])],
            )
            self.assertEqual(
                rows(
                    store,
                    "SELECT agent_id,state FROM duty WHERE id=?",
                    (sender["next_duty"]["duty_id"],),
                ),
                [("author", "handed_off")],
            )
            self.assertEqual(
                rows(
                    store,
                    """SELECT a.author_id,a.content,r.run_id,n.agent_id,n.state
                       FROM work_follow_up w JOIN shared_publication a ON a.asset_id=w.result_id
                       JOIN work_result_read r ON r.duty_id=w.duty_id
                       JOIN work_follow_up s ON s.previous_duty_id=w.duty_id
                       JOIN duty n ON n.id=s.duty_id WHERE w.duty_id=?""",
                    (next_review["claim"]["duty_id"],),
                ),
                [
                    (
                        "author",
                        "Checked discrepancy.",
                        next_review["claim"]["run_id"],
                        "recipient",
                        "scheduled",
                    )
                ],
            )

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
