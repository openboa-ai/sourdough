"""Opt-in installed observation of an actual hire and company-only recovery."""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from importlib.util import find_spec
from pathlib import Path
from time import monotonic, sleep, time


WITHHELD_PUBLICATION = """
import json, os, sys
from dataclasses import asdict
from pathlib import Path
from time import sleep
from sourdough import assessment_run, cli, company_assessment
barrier = Path(sys.argv.pop(1))
publish = company_assessment.publish_assessment
pending = assessment_run.pending_publications
def observe(*args, **kwargs):
    result = publish(*args, **kwargs)
    with barrier.open("x") as stream:
        json.dump({"pid": os.getpid(), "receipt": asdict(result)}, stream)
    sleep(120)
    return result
company_assessment.publish_assessment = observe
def replay(writer):
    receipts = pending(writer)
    return receipts + receipts if barrier.exists() else receipts
assessment_run.pending_publications = replay
raise SystemExit(cli.main())
"""
ACTIVATION = """
import sys
from sourdough import cli
barrier, worker = sys.argv[1:3]
del sys.argv[1:3]
supervise = cli.supervise_worker
def pass_transport(command, store, company_id, **options):
    child = [sys.executable, "-IB", "-c", worker, barrier, *command[4:]]
    return supervise(child, store, company_id, **options)
cli.supervise_worker = pass_transport
raise SystemExit(cli.main())
"""


def cli(command, *arguments):
    completed = subprocess.run(
        [sys.executable, "-IB", "-m", "sourdough.cli", command, *map(str, arguments)],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
    )
    return json.loads(completed.stdout)


def rows(store, sql, parameters=()):
    with closing(sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(sql, parameters).fetchall()


def store_json(path, body):
    path.write_text(json.dumps(body, sort_keys=True))


def prepare(output, employee, *, hiring_only=False):
    root = Path(__file__).resolve().parents[3]
    installed = Path(find_spec("sourdough").origin).parent
    for source in (root / "src/sourdough").glob("*.py"):
        assert (installed / source.name).read_bytes() == source.read_bytes(), source
    assert employee.read_bytes() == (root / "tools/codex_employee.py").read_bytes()
    head, tree = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD", "HEAD^{tree}"], text=True
    ).splitlines()
    company, envelope, source = (
        output / name for name in ("company.sqlite", "envelope.json", "source.json")
    )
    profile = cli("employee-profile", employee)["execution_profile"]
    starter = cli(
        "found",
        company,
        "--setup-identity",
        "assessment-continuity",
        "--charter-revision",
        "local-record-integrity",
        "--founder-scope",
        "manage-company",
        "--execution-profile",
        profile,
    )
    identity = ("--company-id", starter["company_id"])
    clock = int(time())
    store_json(
        envelope,
        {
            "authority": {
                "manager_grant_id": starter["grant_id"],
                "execution_profile": profile,
                "allowed_scope": "assessment",
                "slots": 1,
                "turns": 5,
            },
            "need": {
                "duty_id": "records-backlog",
                "summary": (
                    "Assess fourteen overdue company records independently. The only current "
                    "reviewer is fully assigned to a time-sensitive obligation. Reassignment "
                    "delays that obligation; deferral leaves the backlog open. The assessment "
                    "has no authority to modify source records or operate external systems."
                ),
                "backlog": 14,
                "available_capacity": 0,
                "assigned_capacity": 1,
                "available_at": clock,
            },
        },
    )
    store_json(
        source,
        [
            {
                "record_id": "quality",
                "subject": "Verified quality reports",
                "recorded": 18,
                "observed": 14,
                "available_at": clock,
            },
            {
                "record_id": "reviews",
                "subject": "Completed operating reviews",
                "recorded": 7,
                "observed": 7,
                "available_at": clock,
            },
            {
                "record_id": "handoffs",
                "subject": "Accepted administrative handoffs",
                "recorded": 10,
                "observed": 8,
                "available_at": clock + 90,
            },
        ],
    )
    startup = cli("stage-staffing", company, envelope, *identity)
    sealed_source = (
        None
        if hiring_only
        else cli("stage-assessment-source", company, source, *identity)
    )
    initial_roster = rows(company, "SELECT id,execution_profile FROM agent")
    assert initial_roster == [(starter["founder_id"], profile)]
    trial = {
        "revision": head,
        "tree": tree,
        "client": False,
        "environment": "hiring-only" if hiring_only else "company-only",
        "founding": starter,
        "profile": profile,
        "staffing": startup,
        "source": sealed_source,
        "turn_budget": 2 if hiring_only else 3,
        "wall_seconds": 240 if hiring_only else 360,
        "max_restarts": 0 if hiring_only else 1,
    }
    store_json(output / "trial.json", trial)
    return company, identity, trial


def observe(output, employee):
    output.mkdir(mode=0o700)
    company, identity, trial = prepare(output, employee)
    before_source = rows(company, "SELECT revision,records FROM assessment_source")
    print(json.dumps(trial), flush=True)
    barrier = output / "publication.json"
    command = [
        sys.executable,
        "-IB",
        "-c",
        ACTIVATION,
        str(barrier),
        WITHHELD_PUBLICATION,
        "supervise-assessment",
        str(company),
        *identity,
        "--employee",
        str(employee),
        "--max-restarts",
        "1",
        "--turn-budget",
        "3",
        "--idle-seconds",
        "140",
        "--max-seconds",
        "360",
    ]
    started = monotonic()
    with (
        (output / "worker.stdout").open("w") as stdout,
        (output / "supervision.stderr").open("w") as stderr,
        subprocess.Popen(command, stdout=stdout, stderr=stderr) as product,
    ):
        try:
            while product.poll() is None and monotonic() - started < 200:
                if barrier.exists():
                    try:
                        cut = json.loads(barrier.read_text())
                        break
                    except ValueError:
                        pass
                sleep(0.05)
            else:
                print(
                    json.dumps(
                        {
                            "verdict": "INCOMPLETE",
                            "reason": "Publication cut was not reached",
                        }
                    ),
                    flush=True,
                )
                return 2
            (publication,) = rows(
                company,
                """SELECT p.duty_id,p.run_id,p.action_id,p.revision,p.contents,p.author_id,p.audience_id,
                          p.published_at,p.review_at,d.state
                   FROM assessment_publication p JOIN duty d ON d.id=p.duty_id""",
            )
            assert publication[-1] == "running"
            assert (publication[2], publication[3]) == (
                cut["receipt"]["action_id"],
                cut["receipt"]["revision"],
            )
            assert rows(company, "SELECT count(*) FROM work_follow_up") == [(0,)]
            assert rows(
                company, "SELECT duty_id,run_id FROM onboarding_acceptance"
            ) == [publication[:2]]
            os.kill(cut["pid"], signal.SIGKILL)
            assert product.wait(timeout=max(1, 365 - (monotonic() - started))) == 0
        finally:
            if product.poll() is None:
                product.terminate()
                product.wait(timeout=10)

    worker, supervisor = [
        json.loads(line) for line in (output / "worker.stdout").read_text().splitlines()
    ]
    killed, replacement = supervisor["attempts"]
    assert killed["pid"] == cut["pid"] and killed["returncode"] == -signal.SIGKILL
    assert replacement["pid"] != killed["pid"]
    assert killed["exited_at_ns"] < replacement["ready_at_ns"]
    assert supervisor["completed"] and replacement["returncode"] == 0
    assert worker["restored"] == [cut["receipt"], cut["receipt"]]
    review = worker["review"]
    assert review is not None
    author = publication[5]
    assert author != trial["founding"]["founder_id"]
    assert publication[6] == trial["founding"]["founder_id"]
    claim = review["claim"]
    assert (claim["founder_id"], claim["execution_profile"]) == (
        author,
        trial["profile"],
    )
    assert claim["duty_id"] != publication[0] and claim["run_id"] != publication[1]
    assert rows(company, "SELECT count(*) FROM hire_receipt") == [(1,)]
    assert rows(company, "SELECT count(*) FROM assessment_publication") == [(1,)]
    assert rows(company, "SELECT count(*) FROM duty_run") == [(3,)]
    assert rows(
        company,
        "SELECT count(*) FROM work_follow_up WHERE previous_duty_id=?",
        (publication[0],),
    ) == [(1,)]
    (hiring,) = rows(
        company,
        """SELECT a.execution_profile,g.scope,h.allocated_turns,c.terms,r.snapshot
           FROM hire_receipt h JOIN agent a ON a.id=h.agent_id
           JOIN standing_grant g ON g.id=h.grant_id
           JOIN staffing_choice c ON c.choice_id=h.choice_id
           JOIN staffing_read r ON r.run_id=c.run_id
           WHERE a.id=? AND h.manager_grant_id=?""",
        (author, trial["founding"]["grant_id"]),
    )
    assert hiring[:2] == (trial["profile"], "assessment") and 2 <= hiring[2] <= 5
    staffing_choice, staffing_input = map(json.loads, hiring[3:])
    assert staffing_choice["action"] == "hire"
    assert set(staffing_choice["alternatives"]) == {"reassign", "defer"}
    assert staffing_input["backlog"] == 14 and staffing_input["available_capacity"] == 0
    (evidence,) = rows(
        company,
        """SELECT r.result_revision,r.read_at,d.decided_at,d.rationale,w.due_at
           FROM work_result_read r JOIN work_review_decision d ON d.run_id=r.run_id
           JOIN work_follow_up w ON w.duty_id=r.duty_id WHERE r.run_id=?""",
        (claim["run_id"],),
    )
    assert evidence[0] == publication[3] == review["decision"]["observation_revision"]
    assert evidence[4] == publication[8] <= evidence[1] <= evidence[2]
    (original_read,) = rows(
        company,
        "SELECT contents FROM assessment_read WHERE run_id=?",
        (publication[1],),
    )
    assessment = json.loads(publication[4])
    original_records = json.loads(original_read[0])
    (examined,) = [
        item
        for item in original_records["records"]
        if item["record_id"] == assessment["record_id"]
    ]
    (sealed_record,) = [
        item
        for item in json.loads(before_source[0][1])
        if item["record_id"] == assessment["record_id"]
    ]
    assert examined == sealed_record
    assert examined["available_at"] <= original_records["cut_at"]
    assert (
        rows(company, "SELECT revision,records FROM assessment_source") == before_source
    )
    assert (
        rows(company, "SELECT name FROM sqlite_schema WHERE name LIKE 'paper_%'") == []
    )
    assert rows(company, "PRAGMA foreign_key_check") == []
    result = {
        "verdict": "OBSERVED",
        "trial": trial,
        "publication": cut["receipt"],
        "assessment": assessment,
        "independent_record": sealed_record,
        "review": review,
        "staffing_choice": staffing_choice,
        "supervisor": supervisor,
        "post_activation_operating_commands": 0,
        "elapsed_seconds": round(monotonic() - started, 3),
    }
    store_json(output / "observer.json", result)
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--employee", required=True, type=Path)
    options = parser.parse_args()
    raise SystemExit(observe(options.output.absolute(), options.employee.absolute()))
