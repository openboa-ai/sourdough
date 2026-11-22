import json
import os
import signal
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep


class ConnectedFlow(unittest.TestCase):
    def prepare(self, *, skip_read=False, limit=100, later_action="buy", fee_bps=0):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.venue = directory / "venue.sqlite"
        self.peer = directory / "employee"
        self.peer.write_text(
            f"#!{sys.executable}\n"
            f"skip_read = {skip_read}\n"
            f"limit = {limit}\n"
            f"later_action = {later_action!r}\n"
            """import json, socket, sys
request = json.load(sys.stdin)
operation = request["operation"]
if operation == "identify":
    result = {"provider": "fixture", "model": "scripted", "revision": "1"}
elif operation == "ready":
    result = {"ready": True}
elif operation == "first_duty":
    result = {"tool": "paper_buy", "call_id": "first", "quantity": 2, "limit_minor_units": limit}
elif operation in ("review_duty", "trade_duty"):
    revision = "invented"
    rationale = "No result was read."
    if not skip_read:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(request["read_endpoint"])
            with client.makefile() as stream:
                observation = json.load(stream)
        revision = observation["revision"]
        rationale = f"Investigate the existing exposure: {observation['position_quantity']} units."
    result = {
        "tool": "record_review_decision", "call_id": "next", "action": "investigate",
        "rationale": rationale, "observation_revision": revision, "next_check_seconds": 600,
    }
    if operation == "trade_duty":
        result["tool"] = "record_paper_decision"
        result["action"] = later_action
        if later_action == "buy":
            assert observation["quote_minor_units"] == 100
            assert observation["available_exposure_minor_units"] == 800
            result.update(quantity=1, limit_minor_units=100)
else:
    raise RuntimeError(operation)
print(json.dumps(result))
"""
        )
        self.peer.chmod(0o700)
        profile = self.success("employee-profile", self.peer)["execution_profile"]
        self.company = self.success(
            "found",
            self.store,
            "--setup-identity",
            "setup",
            "--charter-revision",
            "charter",
            "--founder-scope",
            "paper",
            "--execution-profile",
            profile,
        )
        self.identity = ("--company-id", self.company["company_id"])
        self.success(
            "bind-paper",
            self.store,
            self.venue,
            *self.identity,
            "--charter-revision",
            "charter",
            "--revision",
            "mandate",
            "--account-id",
            "account",
            "--rules",
            "contract",
            "--starting-minor-units",
            "5000",
            "--exposure-minor-units",
            "1000",
            "--quote-minor-units",
            "100",
            "--fee-bps",
            str(fee_bps),
        )

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-IB", "-m", "sourdough.cli", *map(str, args)],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def success(self, *args):
        response = self.cli(*args)
        self.assertEqual(response.returncode, 0, response.stderr)
        return json.loads(response.stdout)

    def operate(self, *options):
        return self.cli(
            "operate-paper",
            self.store,
            self.venue,
            *self.identity,
            "--employee",
            self.peer,
            *options,
        )

    def read(self, path, query):
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute(query).fetchall()

    def test_end_to_end(self):
        self.prepare()
        run = self.operate()
        self.assertEqual(run.returncode, 0, run.stderr)
        trace = json.loads(run.stdout)
        first = trace["first_trade"]
        review = trace["review"]
        order = first["order"]["posting"]
        first_claim, review_claim = first["claim"], review["claim"]
        for field in ("company_id", "founder_id", "grant_id", "execution_profile"):
            self.assertEqual(first_claim[field], review_claim[field])
        self.assertEqual(first_claim["duty_id"], self.company["due_work_id"])
        self.assertNotEqual(first_claim["run_id"], review_claim["run_id"])
        self.assertNotEqual(first_claim["duty_id"], review_claim["duty_id"])
        fills = [(order["order_id"], order["fill_id"], 2)]
        self.assertEqual(
            fills,
            self.read(
                self.venue, "SELECT order_id, fill_id, quantity FROM venue_execution"
            ),
        )
        self.assertEqual(
            fills,
            self.read(self.store, "SELECT order_id, fill_id, quantity FROM paper_fill"),
        )
        (position,) = self.read(
            self.store,
            "SELECT fill_id, position_quantity FROM paper_position_observation",
        )
        self.assertEqual(position, (order["fill_id"], 2))
        (read,) = self.read(
            self.store,
            "SELECT duty_id, run_id, result_revision, read_at FROM work_result_read",
        )
        (saved,) = self.read(
            self.store,
            "SELECT duty_id, run_id, result_revision, decided_at, rationale FROM work_review_decision",
        )
        self.assertEqual(read[:3], saved[:3])
        self.assertEqual(read[:2], (review_claim["duty_id"], review_claim["run_id"]))
        self.assertLessEqual(read[3], saved[3])
        self.assertIn("2 units", saved[4])
        next_duty = review["next_duty"]
        self.assertEqual(next_duty["due_at"], saved[3] + 600)
        (pending,) = self.read(
            self.store,
            "SELECT w.previous_duty_id, w.duty_id, w.due_at FROM work_follow_up w "
            "JOIN duty d ON d.id = w.duty_id WHERE d.state = 'scheduled'",
        )
        self.assertEqual(
            pending,
            (review_claim["duty_id"], next_duty["duty_id"], next_duty["due_at"]),
        )
        self.assertEqual(self.read(self.store, "PRAGMA foreign_key_check"), [])

    def test_report_without_authorized_read_leaves_review_incomplete(self):
        self.prepare(skip_read=True)
        result = self.operate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("preceding authorized result read", result.stderr)
        self.assertEqual(
            self.read(self.store, "SELECT state FROM duty ORDER BY state"),
            [("incomplete",), ("settled",)],
        )
        self.assertEqual(
            self.read(self.store, "SELECT COUNT(*) FROM work_follow_up"), [(1,)]
        )
        self.assertEqual(
            self.read(self.venue, "SELECT COUNT(*) FROM venue_execution"), [(1,)]
        )

    def test_unfilled_order_never_starts_the_review(self):
        self.prepare(limit=99)
        result = self.operate()
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = json.loads(result.stdout)
        self.assertIsNone(trace["first_trade"]["order"]["posting"])
        self.assertIsNone(trace["review"])
        self.assertEqual(
            self.read(self.store, "SELECT COUNT(*) FROM work_follow_up"), [(0,)]
        )

    def test_two_paper_orders_from_one_activation_preserve_the_next_duty(self):
        self.prepare()
        response = self.operate("--repeat-paper")
        self.assertEqual(response.returncode, 0, response.stderr)
        trace = json.loads(response.stdout)
        first = trace["first_trade"]
        (later,) = trace["later_trades"]
        self.assertIsNone(trace["review"])
        self.assertEqual(first["claim"]["founder_id"], later["claim"]["founder_id"])
        self.assertEqual(
            first["claim"]["execution_profile"], later["claim"]["execution_profile"]
        )
        self.assertNotEqual(first["claim"]["run_id"], later["claim"]["run_id"])
        self.assertNotEqual(first["claim"]["duty_id"], later["claim"]["duty_id"])
        fills = self.read(
            self.venue,
            "SELECT order_id, fill_id, quantity FROM venue_execution ORDER BY order_id",
        )
        self.assertEqual(len(fills), 2)
        self.assertEqual(
            fills,
            self.read(
                self.store,
                "SELECT order_id, fill_id, quantity FROM paper_fill ORDER BY order_id",
            ),
        )
        self.assertEqual(
            self.read(self.store, "SELECT sum(quantity) FROM paper_fill"), [(3,)]
        )
        (chain,) = self.read(
            self.store,
            "SELECT d.duty_id, d.run_id, d.result_revision, d.decision_id, r.read_at, "
            "d.decided_at, o.action_id, w.purpose "
            "FROM paper_decision d JOIN work_result_read r ON d.duty_id = r.duty_id "
            "JOIN paper_order o ON o.action_id = d.decision_id "
            "JOIN work_follow_up w ON w.previous_duty_id = d.duty_id",
        )
        self.assertEqual(
            chain[:2], (later["claim"]["duty_id"], later["claim"]["run_id"])
        )
        self.assertEqual(
            chain[2], later["decision"]["decision"]["observation_revision"]
        )
        self.assertEqual(chain[3], later["decision"]["decision_id"])
        self.assertLessEqual(chain[4], chain[5])
        self.assertEqual(chain[6:], (chain[3], "paper_execution"))
        self.assertEqual(later["next_duty"]["due_at"], chain[5] + 600)
        self.assertEqual(
            self.read(
                self.store, "SELECT count(*) FROM duty WHERE state = 'scheduled'"
            ),
            [(1,)],
        )

    def test_run_budget_stops_before_the_scheduled_trading_duty(self):
        self.prepare()
        response = self.operate("--repeat-paper", "--turn-budget", "1")
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(json.loads(response.stdout)["later_trades"], [])
        self.assertEqual(
            self.read(self.venue, "SELECT count(*) FROM venue_execution"), [(1,)]
        )
        self.assertEqual(
            self.read(self.store, "SELECT purpose FROM work_follow_up"),
            [("paper_execution",)],
        )

    def test_reasoned_hold_schedules_the_next_duty_without_an_order(self):
        self.prepare(later_action="hold")
        response = self.operate("--repeat-paper")
        self.assertEqual(response.returncode, 0, response.stderr)
        (later,) = json.loads(response.stdout)["later_trades"]
        self.assertEqual(later["decision"]["decision"]["action"], "hold")
        self.assertIsNone(later["order_id"])
        self.assertIsNone(later["posting"])
        self.assertEqual(later["next_duty"]["purpose"], "paper_execution")
        self.assertEqual(
            self.read(self.venue, "SELECT count(*) FROM venue_execution"), [(1,)]
        )

    def test_supervisor_rebooks_original_fill_after_a_physical_receipt_cut(self):
        self.prepare(fee_bps=50)
        marker = self.store.with_name("venue-barrier")
        worker = """
import json, os, sys
from pathlib import Path
from time import sleep
from sourdough import cli
from sourdough.paper_venue import FixedQuoteVenue

marker = Path(sys.argv.pop(1))
class ReceiptBarrier(FixedQuoteVenue):
    def submit(self, order_id, account_id, intent):
        super().submit(order_id, account_id, intent)
        with marker.open("x") as stream:
            json.dump({"pid":os.getpid(),"order_id":order_id},stream)
        sleep(30)
cli.FixedQuoteVenue = ReceiptBarrier
raise SystemExit(cli.main())
"""
        driver = """
from dataclasses import asdict
import json, sys
from pathlib import Path
from sourdough.supervisor import supervise_worker

worker, marker, store, journal, company, employee = sys.argv[1:]
events = []
bound = supervise_worker(
    [sys.executable, "-IB", "-c", worker, marker, "_paper-worker", store, journal,
     "--company-id", company, "--employee", employee, "--turn-budget", "2", "--budget-from", "0"],
    Path(store), company, max_restarts=1, startup_seconds=5, max_seconds=12, emit=events.append,
)
print(json.dumps({"supervisor":asdict(bound),"events":events}),flush=True)
raise SystemExit(0 if bound.completed else 1)
"""
        with subprocess.Popen(
            [
                sys.executable,
                "-IB",
                "-c",
                driver,
                worker,
                str(marker),
                str(self.store),
                str(self.venue),
                self.company["company_id"],
                str(self.peer),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as parent:
            deadline = monotonic() + 5
            cut = None
            while parent.poll() is None and monotonic() < deadline:
                if marker.is_file():
                    try:
                        cut = json.loads(marker.read_text())
                        break
                    except ValueError:
                        pass
                sleep(0.02)
            if cut is None:
                _, diagnostic = parent.communicate(timeout=14)
                self.fail(f"The durable venue cut was not observed: {diagnostic}")
            (original,) = self.read(
                self.venue, "SELECT order_id, fill_id, quantity FROM venue_execution"
            )
            self.assertEqual(original[0], cut["order_id"])
            self.assertIsNotNone(original[1])
            self.assertEqual(
                self.read(
                    self.store,
                    "SELECT name FROM sqlite_schema WHERE name = 'paper_fill'",
                ),
                [],
            )
            os.kill(cut["pid"], signal.SIGKILL)
            output, diagnostic = parent.communicate(timeout=14)
        self.assertEqual(parent.returncode, 0, diagnostic)
        worker_receipt, supervisor_receipt = [
            json.loads(line) for line in output.splitlines()
        ]
        prior, replacement = supervisor_receipt["supervisor"]["attempts"]
        self.assertEqual(
            (prior["pid"], prior["returncode"]), (cut["pid"], -signal.SIGKILL)
        )
        self.assertNotEqual(replacement["pid"], prior["pid"])
        self.assertLess(prior["exited_at_ns"], replacement["ready_at_ns"])
        self.assertEqual(replacement["returncode"], 0)
        self.assertEqual(
            self.read(
                self.venue, "SELECT order_id, fill_id, quantity FROM venue_execution"
            ),
            [original],
        )
        self.assertEqual(
            self.read(self.store, "SELECT order_id, fill_id, quantity FROM paper_fill"),
            [original],
        )
        self.assertEqual(
            self.read(self.store, "SELECT fee_minor_units FROM paper_fill_fee"), [(1,)]
        )
        self.assertEqual(
            self.read(self.venue, "SELECT fee_minor_units FROM venue_fill_fee"), [(1,)]
        )
        self.assertEqual(
            self.read(self.store, "SELECT reserved_minor_units FROM paper_order"),
            [(200,)],
        )
        (recovered,) = worker_receipt["reconciliation"]["recovered"]
        self.assertEqual(recovered["sources"], ["history", "account_sync"])
        self.assertEqual(recovered["posting"]["fill_id"], original[1])
        decision = worker_receipt["review"]
        self.assertEqual(decision["claim"]["founder_id"], self.company["founder_id"])
        self.assertNotEqual(
            decision["claim"]["duty_id"], recovered["original_claim"]["duty_id"]
        )
        self.assertEqual(self.read(self.store, "SELECT count(*) FROM duty_run"), [(2,)])
        self.assertEqual(
            self.read(self.store, "SELECT count(*) FROM work_review_decision"), [(1,)]
        )

    def test_installed_supervisor_entry_completes_the_normal_operating_path(self):
        self.prepare()
        response = self.cli(
            "supervise-paper",
            self.store,
            self.venue,
            *self.identity,
            "--employee",
            self.peer,
            "--max-restarts",
            "0",
            "--turn-budget",
            "2",
        )
        self.assertEqual(response.returncode, 0, response.stderr)
        worker, supervisor = [json.loads(line) for line in response.stdout.splitlines()]
        self.assertTrue(supervisor["completed"])
        (attempt,) = supervisor["attempts"]
        self.assertIsNotNone(attempt["ready_at_ns"])
        self.assertEqual(
            worker["review"]["claim"]["founder_id"], self.company["founder_id"]
        )
        self.assertEqual(self.read(self.store, "SELECT count(*) FROM duty_run"), [(2,)])
