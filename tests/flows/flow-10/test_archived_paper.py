import json
import runpy
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

from sourdough.company import _connect
from sourdough.duty_run import DutyClaim, DutyRefused
from sourdough.paper_order import PaperIntent, submit_paper_order
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.retirement import (
    Retirement,
    TERMINATION_SCOPE,
    employment_revision,
    retire_agent,
)
from sourdough.writer_lease import hold_company_writer


FIXTURE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "flow-06/test_flow.py")
)
cli = FIXTURE["cli"]
rows = FIXTURE["rows"]


class ArchivedPaper(unittest.TestCase):
    def test_existing_fill_is_booked_without_reopening_the_archived_agent(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        employee, store, venue = (root / name for name in ("employee", "book", "venue"))
        employee.write_text(
            f"#!{sys.executable}\nsubmit_request = True\n" + FIXTURE["EMPLOYEE"]
        )
        employee.chmod(0o700)
        profile = cli("employee-profile", employee)["execution_profile"]
        company = cli(
            "found",
            store,
            "--setup-identity",
            "archive",
            "--charter-revision",
            "paper",
            "--founder-scope",
            "manage-company",
            "--execution-profile",
            profile,
        )
        company_id = company["company_id"]
        identity = ("--company-id", company_id)
        now = int(time())
        envelope = root / "staffing.json"
        envelope.write_text(
            json.dumps(
                {
                    "authority": {
                        "manager_grant_id": company["grant_id"],
                        "execution_profile": profile,
                        "allowed_scope": "paper",
                        "slots": 1,
                        "turns": 4,
                    },
                    "need": {
                        "duty_id": "paper-forecast",
                        "summary": "Assess the current evidence",
                        "backlog": 2,
                        "available_capacity": 0,
                        "assigned_capacity": 1,
                        "available_at": now,
                    },
                }
            )
        )
        cli("stage-staffing", store, envelope, *identity)
        cli(
            "bind-paper",
            store,
            venue,
            *identity,
            "--charter-revision",
            "paper",
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
        observations = root / "observations.json"
        observations.write_text(
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
        cli("stage-paper-observations", store, observations, *identity)
        invocation = (
            "supervise-hired-paper",
            str(store),
            str(venue),
            *identity,
            "--employee",
            str(employee),
            "--idle-seconds",
            "0",
            "--turn-budget",
            "3",
            "--max-restarts",
            "0",
        )
        marker = root / "cut"
        interrupted = subprocess.run(
            [
                sys.executable,
                "-IB",
                "-c",
                FIXTURE["DRIVER"],
                FIXTURE["WORKER"],
                str(marker),
                "after_fill",
                *invocation,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertTrue(marker.is_file(), interrupted.stderr)
        [(order_id, fill_id)] = rows(
            venue, "SELECT order_id,fill_id FROM venue_execution"
        )
        self.assertIsNotNone(fill_id)
        self.assertEqual(rows(store, "SELECT COUNT(*) FROM paper_fill"), [(0,)])
        [(agent_id, grant, duty, run)] = rows(
            store,
            """SELECT d.agent_id,r.grant_id,d.id,r.run_id FROM paper_order o
               JOIN duty_run r ON r.run_id=o.run_id JOIN duty d ON d.id=r.duty_id""",
        )
        with hold_company_writer(store, company_id) as writer:
            with closing(_connect(store, "rw")) as connection, connection:
                connection.execute(
                    "INSERT INTO agent VALUES ('manager',?,?)", (company_id, profile)
                )
                connection.execute(
                    "INSERT INTO standing_grant VALUES ('termination','manager',?)",
                    (TERMINATION_SCOPE,),
                )
            revision = employment_revision(writer, "manager", "termination", agent_id)
            retire_agent(
                writer,
                "manager",
                "termination",
                Retirement("archive", agent_id, revision, "End employment."),
            )
            with self.assertRaisesRegex(DutyRefused, "no longer current"):
                submit_paper_order(
                    store,
                    DutyClaim(company_id, agent_id, grant, duty, run, profile),
                    PaperIntent("late", "fixture", 1, 5000),
                    FixedQuoteVenue(venue),
                    writer=writer,
                )
        for _attempt in range(2):
            restarted = subprocess.run(
                [sys.executable, "-IB", "-m", "sourdough.cli", *invocation],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            result, supervisor = map(json.loads, restarted.stdout.splitlines())
            self.assertTrue(supervisor["completed"])
            self.assertEqual(result["blocked_work"], [])
        self.assertEqual(
            rows(venue, "SELECT order_id,fill_id FROM venue_execution"),
            [(order_id, fill_id)],
        )
        self.assertEqual(
            rows(store, "SELECT order_id,fill_id FROM paper_fill"),
            [(order_id, fill_id)],
        )
        self.assertEqual(
            rows(store, "SELECT id,state FROM duty WHERE agent_id=?", (agent_id,)),
            [(duty, "archived")],
        )
        self.assertEqual(
            rows(
                store, "SELECT run_id,is_current FROM duty_run WHERE duty_id=?", (duty,)
            ),
            [(run, 0)],
        )
        self.assertEqual(rows(store, "PRAGMA foreign_key_check"), [])
