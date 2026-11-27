import json
import sqlite3
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
from sourdough.runtime.work_kernel import sync_work_item
from sourdough.writer_lease import hold_company_writer


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
elif operation == "review_duty":
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
            "audience": ["author", "recipient"], "start": 0, "end": 20,
        })
        call("post_project_message", {
            "project_id": project["project_id"], "project_revision": project["revision"],
            "request_id": "question", "body": "Can you check the discrepancy?",
            "asset_id": asset["asset_id"],
        })
        call("offer_handoff", {
            "request_id": "transfer", "recipient_id": "recipient", "asset_id": asset["asset_id"],
            "expected_result": "Independently check the discrepancy.", "expires_at": 4102444800,
        })
        rationale = "Pass the check to another Agent."
    elif evidence["asset_id"] == "recipient-input":
        [project] = call("read_project_assignments", {"after": ""})["projects"]
        [delivery] = call("read_project_inbox", {"after": ""})["deliveries"]
        assert delivery["read_run"] is None, delivery
        [message] = call("read_project_discussion", {
            "project_id": project["project_id"], "after": "",
        })["messages"]
        assert message["sender_id"] == "author" and message["body"] == "Can you check the discrepancy?"
        assert message["message_id"] == delivery["message_id"]
        assert call("acknowledge_project_delivery", {"delivery_id": delivery["delivery_id"]})["read_run"]
        call("post_project_message", {
            "project_id": project["project_id"], "project_revision": project["revision"],
            "request_id": "answer", "body": "I will check the discrepancy.", "asset_id": "",
        })
        [offer] = call("read_handoff_offers", {})["offers"]
        assert offer["sender_id"] == "author", offer
        call("accept_handoff", {"offer_id": offer["offer_id"]})
        rationale = "Accepted the offered check in a separate review."
    else:
        assert evidence["author_id"] == "author"
        assert evidence["content"] == "Checked discrepancy."
        assert request["harness"]["drafts"] == []
        assert prior["handoff"]["expected_result"] == "Independently check the discrepancy.", prior
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
                sync_work_item(
                    database,
                    company_id=company_id,
                    work_item_id=history,
                    owner_agent_id=company["founder_id"],
                    duty_kind="work.staffing",
                    state="settled",
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
                       (asset_id,company_id,project_id,author_id,work_item_id,content,request_id,request_digest,audience) VALUES
                       (?,?,'analysis',?,?,?,'seed','seed',?)""",
                    (
                        f"{agent}-input",
                        company_id,
                        agent,
                        f"{agent}-hire-history",
                        agent,
                        json.dumps([agent]),
                    ),
                )


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
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
                rows(
                    store,
                    "SELECT successor_work_item_id,accepted_run_id FROM work_handoff",
                ),
                [(next_review["claim"]["duty_id"], recipient["claim"]["run_id"])],
            )
            self.assertEqual(
                rows(
                    store,
                    "SELECT sender_id,run_id,body FROM project_message ORDER BY rowid",
                ),
                [
                    (
                        "author",
                        sender["claim"]["run_id"],
                        "Can you check the discrepancy?",
                    ),
                    (
                        "recipient",
                        recipient["claim"]["run_id"],
                        "I will check the discrepancy.",
                    ),
                ],
            )
            self.assertEqual(
                rows(
                    store,
                    """SELECT m.sender_id,d.recipient_id,d.read_run_id
                       FROM project_delivery d JOIN project_message m USING(message_id)
                       ORDER BY d.rowid""",
                ),
                [
                    ("author", "recipient", recipient["claim"]["run_id"]),
                    ("recipient", "author", None),
                ],
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
