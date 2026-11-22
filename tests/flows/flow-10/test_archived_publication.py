import json
import os
import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

from sourdough.company import _connect
from sourdough.company_assets import Selection, publish_selection
from sourdough.duty_run import ASSESSMENT_REVIEW, DutyClaim, DutyRefused
from sourdough.retirement import (
    Retirement,
    TERMINATION_SCOPE,
    employment_revision,
    retire_agent,
)
from sourdough.writer_lease import hold_company_writer

from test_flow import SUPERVISOR, WORKER, cli, seed_assessment_reviews, rows


EMPLOYEE = r"""
import json, socket, sys
request = json.load(sys.stdin)
def call(name, arguments):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(request["read_endpoint"])
        connection.sendall(json.dumps({"name": name, "arguments": arguments}).encode() + b"\n")
        with connection.makefile() as stream:
            reply = json.load(stream)
    assert "error" not in reply, reply
    return reply

if request["operation"] == "identify":
    result = {"provider": "fixture", "model": "publication-custody", "revision": "1"}
else:
    assert request["operation"] == "assessment_review"
    prior = call("read_work_result", {})
    scope = call("read_project_assignments", {"after": ""})
    [project] = scope["projects"]
    if prior["shared_evidence"]["asset_id"] == "author-input":
        draft = call("save_private_draft", {
            "name": "report", "content": "Published finding. Private appendix.",
            "expected_revision": "",
        })
        call("publish_draft_selection", {
            "request_id": "publish", "draft_name": draft["name"],
            "draft_revision": draft["revision"], "project_id": project["project_id"],
            "project_revision": project["revision"], "audience": ["recipient"],
            "start": 0, "end": 18,
        })
        raise RuntimeError("The independently committed publication reply should be withheld")
    assert prior["shared_evidence"]["asset_id"] == "recipient-input"
    [published] = [asset for asset in scope["shared_assets"] if asset["author_id"] == "author"]
    evidence = call("read_shared_asset", {"asset_id": published["asset_id"]})
    assert evidence["content"] == "Published finding." and request["harness"]["drafts"] == []
    result = call("record_review_decision", {
        "observation_revision": prior["revision"], "action": "hold",
        "rationale": "Verified the original publication " + evidence["asset_id"],
        "next_check_seconds": 86400,
    })
print(json.dumps(result))
"""

CUT = """
import json, os
from dataclasses import asdict
from pathlib import Path
from sourdough import collaboration
marker = Path(os.environ.pop("SOURDOUGH_TEST_PUBLICATION_CUT"))
publish = collaboration.publish_selection
def publish_then_exit(writer, claim, purpose, selection):
    asset = publish(writer, claim, purpose, selection)
    marker.write_text(json.dumps({
        "asset": asdict(asset), "claim": asdict(claim), "request": asdict(selection),
    }))
    os._exit(76)
collaboration.publish_selection = publish_then_exit
"""


class ArchivedPublication(unittest.TestCase):
    def test_restart_preserves_publication_but_fences_the_archived_author(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        employee, store, marker = (
            root / name for name in ("employee", "company.sqlite", "publication.json")
        )
        employee.write_text(f"#!{sys.executable}\n" + EMPLOYEE)
        employee.chmod(0o700)
        profile = cli("employee-profile", employee)["execution_profile"]
        company = cli(
            "found",
            store,
            "--setup-identity",
            "publication-custody",
            "--charter-revision",
            "work",
            "--founder-scope",
            "manage-company",
            "--execution-profile",
            profile,
        )
        company_id = company["company_id"]
        seed_assessment_reviews(store, company, profile, int(time()))
        with hold_company_writer(store, company_id):
            with closing(_connect(store, "rw")) as database, database:
                database.execute(
                    "INSERT INTO agent VALUES ('manager',?,?)", (company_id, profile)
                )
                database.execute(
                    "INSERT INTO standing_grant VALUES ('retirement','manager',?)",
                    (TERMINATION_SCOPE,),
                )
        command = [
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
        crashed = subprocess.run(
            [sys.executable, "-IB", "-c", SUPERVISOR, CUT + WORKER, *command],
            capture_output=True,
            text=True,
            timeout=15,
            env=os.environ | {"SOURDOUGH_TEST_PUBLICATION_CUT": str(marker)},
        )
        self.assertNotEqual(crashed.returncode, 0)
        self.assertTrue(marker.is_file(), crashed.stderr)
        [exit_event] = [
            json.loads(line)
            for line in crashed.stderr.splitlines()
            if line.startswith('{"event": "worker_exited"')
        ]
        self.assertEqual(exit_event["returncode"], 76)
        witnessed = json.loads(marker.read_text())
        asset_id = witnessed["asset"]["asset_id"]
        claim = DutyClaim(**witnessed["claim"])
        self.assertEqual(claim.founder_id, "author")

        def publication():
            return rows(
                store,
                """SELECT asset_id,author_id,duty_id,content
                   FROM shared_publication WHERE request_id='publish'""",
            )

        expected = [(asset_id, claim.founder_id, claim.duty_id, "Published finding.")]
        self.assertEqual(publication(), expected)

        request = witnessed["request"]
        selection = Selection(**(request | {"audience": tuple(request["audience"])}))
        with hold_company_writer(store, company_id) as writer:
            revision = employment_revision(writer, "manager", "retirement", "author")
            retire_agent(
                writer,
                "manager",
                "retirement",
                Retirement("archive", "author", revision, "End the role."),
            )
            for stale in (selection, replace(selection, request_id="late")):
                with self.assertRaisesRegex(DutyRefused, "no longer current"):
                    publish_selection(writer, claim, ASSESSMENT_REVIEW, stale)

        resumed = subprocess.run(
            [sys.executable, "-IB", "-c", SUPERVISOR, WORKER, *command],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        result, supervision = map(json.loads, resumed.stdout.splitlines())
        self.assertTrue(supervision["completed"])
        self.assertEqual(result["review"]["claim"]["agent_id"], "recipient")
        self.assertIn(asset_id, result["review"]["decision"]["rationale"])
        self.assertEqual(publication(), expected)
        self.assertEqual(
            rows(store, "SELECT id,state FROM duty WHERE agent_id='author'"),
            [(claim.duty_id, "archived")],
        )
        self.assertEqual(
            rows(
                store,
                """SELECT d.state,r.run_id,r.is_current FROM duty d
                   JOIN duty_run r ON r.duty_id=d.id WHERE d.agent_id='author'""",
            ),
            [("archived", claim.run_id, 0)],
        )
        self.assertEqual(
            rows(store, "SELECT name FROM sqlite_schema WHERE name GLOB 'paper_*'"), []
        )
