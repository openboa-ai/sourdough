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
from time import monotonic, sleep, time

from sourdough.staffing import StaffingAuthority, StaffingNeed, stage_staffing
from sourdough.writer_lease import hold_company_writer


class ConnectedFlow(unittest.TestCase):
    def prepare(
        self,
        *,
        skip_read=False,
        limit=100,
        later_action="buy",
        fee_bps=0,
        research_action="buy",
        research_reply="complete",
        long_finding=False,
    ):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.venue = directory / "venue.sqlite"
        self.peer = directory / "employee"
        self.peer.write_text(
            f"#!{sys.executable}\n"
            f"skip_read = {skip_read}\n"
            f"limit = {limit}\n"
            f"later_action = {later_action!r}\n"
            f"research_action = {research_action!r}\n"
            f"research_reply = {research_reply!r}\n"
            f"long_finding = {long_finding}\n"
            """import json, os, signal, socket, sys
request = json.load(sys.stdin)
operation = request["operation"]
if operation == "identify":
    result = {"provider": "fixture", "model": "scripted", "revision": "1"}
elif operation == "ready":
    result = {"ready": True}
elif operation == "first_duty":
    result = {"tool": "paper_buy", "call_id": "first", "quantity": 2, "limit_minor_units": limit}
elif operation == "research_duty":
    def invoke(name, args):
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(request["read_endpoint"])
            client.sendall(json.dumps({"name":name,"arguments":args}).encode()+b"\\n")
            with client.makefile() as stream:
                reply = json.load(stream)
        assert "error" not in reply, reply
        return reply
    snapshot = invoke("read_research_inputs", {})
    start, end = snapshot["observations"]
    prices = (start["price_minor_units"], end["price_minor_units"])
    gross = prices[1] - prices[0]
    fees = sum((value * snapshot["fee_bps"] + 5000) // 10000 for value in prices)
    finding = invoke("publish_research_finding", {
        "snapshot_revision":snapshot["revision"],"hypothesis":"🧪" * 2000 if long_finding else "Compare move and synthetic costs",
        "start_id":start["observation_id"],"end_id":end["observation_id"],
        "gross_change_minor_units":gross,"round_trip_fee_minor_units":fees,"net_change_minor_units":gross-fees,
        "uncertainty":"証" * 2000 if long_finding else "A fixture does not provide market evidence.",
    })
    proposal = {
        "finding_id":finding["finding_id"],"finding_revision":finding["revision"],"call_id":"research-decision",
        "action":research_action,"next_check_seconds":60,
        "rationale":f"The measured move {gross} after fees {fees} is {gross-fees} in the fixture.",
    }
    if research_action == "buy":
        proposal.update(quantity=2, limit_minor_units=100)
    result = invoke("record_research_decision", proposal)
    if research_reply == "exit":
        raise SystemExit(37)
    if research_reply == "kill":
        os.kill(os.getppid(), signal.SIGKILL)
        raise SystemExit(38)
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
        if operation == "review_duty" and "current_observation" in observation:
            rationale += f" Current quote: {observation['current_observation']['quote_minor_units']} minor units."
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

    def test_supervisor_resumes_a_later_decision_committed_before_admission(self):
        worker = """
import os, signal, sys
from pathlib import Path
from sourdough import cli
from sourdough.trading import cli as trading_cli, company_run
marker = Path(sys.argv.pop(1))
persist = company_run.persist_paper_decision
def cut(*args, **kwargs):
    receipt = persist(*args, **kwargs)
    if not marker.exists():
        marker.write_text(receipt.decision_id)
        os.kill(os.getpid(), signal.SIGKILL)
    return receipt
company_run.persist_paper_decision = cut
raise SystemExit(cli.main())
"""
        driver = """
import sys
from sourdough import cli
from sourdough.trading import cli as trading_cli
worker, marker = sys.argv[1:3]
del sys.argv[1:3]
supervise = trading_cli.supervise_worker
def launch(command, store, company_id, **options):
    child = [sys.executable, "-IB", "-c", worker, marker, *command[4:]]
    return supervise(child, store, company_id, **options)
trading_cli.supervise_worker = launch
raise SystemExit(cli.main())
"""
        for action, count in (("hold", 1), ("buy", 2)):
            with self.subTest(action=action):
                self.prepare(later_action=action)
                marker = self.store.with_name("committed-later-decision")
                process = subprocess.run(
                    [
                        sys.executable,
                        "-IB",
                        "-c",
                        driver,
                        worker,
                        str(marker),
                        "supervise-paper",
                        str(self.store),
                        str(self.venue),
                        *self.identity,
                        "--employee",
                        str(self.peer),
                        "--repeat-paper",
                        "--turn-budget",
                        "2",
                        "--max-restarts",
                        "1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=18,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                resumed, summary = [
                    json.loads(line) for line in process.stdout.splitlines()
                ]
                original, replacement = summary["attempts"]
                self.assertEqual(original["returncode"], -signal.SIGKILL)
                self.assertEqual(replacement["returncode"], 0)
                (trade,) = resumed["later_trades"]
                (saved,) = self.read(
                    self.store,
                    "SELECT decision_id, action, duty_id FROM paper_decision",
                )
                self.assertEqual(
                    saved, (marker.read_text(), action, trade["claim"]["duty_id"])
                )
                self.assertEqual(trade["decision"]["decision_id"], saved[0])
                self.assertIsNotNone(trade["next_duty"])
                self.assertEqual(
                    self.read(self.store, "SELECT count(*) FROM duty_run"), [(2,)]
                )
                self.assertEqual(
                    self.read(self.store, "SELECT count(*) FROM work_follow_up"), [(2,)]
                )
                self.assertEqual(
                    self.read(self.venue, "SELECT count(*) FROM venue_execution"),
                    [(count,)],
                )
                self.assertEqual(
                    self.read(self.store, "SELECT count(*) FROM paper_fill"), [(count,)]
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
        self.assertIn("Current quote: 100 minor units", saved[4])
        (quote,) = self.read(
            self.store,
            "SELECT run_id, account_id, quote_minor_units, observed_at FROM paper_review_quote",
        )
        self.assertEqual(quote[:3], (review_claim["run_id"], "account", 100))
        self.assertLessEqual(quote[3], read[3])
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

    def test_an_unrelated_due_staffing_duty_does_not_reopen_the_founder_order(self):
        self.prepare()
        first = self.operate("--turn-budget", "1")
        self.assertEqual(first.returncode, 0, first.stderr)
        profile = self.success("employee-profile", self.peer)["execution_profile"]
        with hold_company_writer(self.store, self.company["company_id"]) as writer:
            stage_staffing(
                writer,
                StaffingAuthority(
                    self.company["grant_id"], profile, "assessment", 1, 2
                ),
                StaffingNeed("staffing-due", "Review backlog", 4, 0, 1, int(time())),
            )
        resumed = self.operate("--turn-budget", "1")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        result = json.loads(resumed.stdout)
        self.assertIsNone(result["first_trade"])
        self.assertIsNotNone(result["review"])
        self.assertEqual(
            self.read(self.venue, "SELECT count(*) FROM venue_execution"), [(1,)]
        )
        self.assertEqual(
            self.read(self.store, "SELECT state FROM duty WHERE id='staffing-due'"),
            [("due",)],
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
from sourdough.trading import cli as trading_cli

marker = Path(sys.argv.pop(1))
class ReceiptBarrier(FixedQuoteVenue):
    def submit(self, order_id, account_id, intent):
        super().submit(order_id, account_id, intent)
        with marker.open("x") as stream:
            json.dump({"pid":os.getpid(),"order_id":order_id},stream)
        sleep(30)
trading_cli.FixedQuoteVenue = ReceiptBarrier
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
                self.read(self.store, "SELECT COUNT(*) FROM paper_fill"),
                [(0,)],
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

    def stage_observations(self):
        now = int(time())
        source = self.store.with_name("observations.json")
        source.write_text(
            json.dumps(
                [
                    {
                        "observation_id": "earlier",
                        "price_minor_units": 90,
                        "observed_at": now - 100,
                        "available_at": now - 95,
                    },
                    {
                        "observation_id": "latest",
                        "price_minor_units": 100,
                        "observed_at": now - 50,
                        "available_at": now - 45,
                    },
                    {
                        "observation_id": "not_available",
                        "price_minor_units": 110,
                        "observed_at": now - 20,
                        "available_at": now + 300,
                    },
                ]
            )
        )
        self.success("stage-paper-observations", self.store, source, *self.identity)

    def test_opening_research_finding_is_cited_before_order_and_sets_follow_up(self):
        self.prepare(fee_bps=50)
        self.stage_observations()
        command = self.operate()
        self.assertEqual(command.returncode, 0, command.stderr)
        trace = json.loads(command.stdout)
        self.assertIsNone(trace["review"])
        first = trace["first_trade"]
        self.assertEqual(first["order"]["posting"]["quantity"], 2)
        receipt = first["research"]
        (chain,) = self.read(
            self.store,
            "SELECT f.published_at, d.decided_at, f.finding_id, d.decision_id, "
            "o.action_id, w.due_at, f.content FROM paper_research_finding f "
            "JOIN paper_research_decision d ON d.run_id = f.run_id AND d.finding_id = f.finding_id "
            "JOIN paper_order o ON o.run_id = d.run_id JOIN work_follow_up w ON w.previous_duty_id = d.duty_id",
        )
        self.assertLessEqual(chain[0], chain[1])
        self.assertEqual(
            chain[2:5],
            (receipt["finding_id"], receipt["decision_id"], receipt["decision_id"]),
        )
        self.assertEqual(chain[5], chain[1] + 60)
        finding = json.loads(chain[6])
        self.assertEqual(
            (
                finding["gross_change_minor_units"],
                finding["round_trip_fee_minor_units"],
                finding["net_change_minor_units"],
            ),
            (10, 1, 9),
        )
        before = self.store.read_bytes()
        view = self.success("inspect-work", self.store, *self.identity)
        first, follow_up = view["duties"]
        paper_view = view["domains"]["trading"]["paper"]
        (order,) = paper_view["orders"]
        self.assertEqual(first["decision"]["finding_id"], receipt["finding_id"])
        self.assertEqual(first["decision"]["decision_id"], order["action_id"])
        self.assertEqual(follow_up["due_at"], chain[5])
        self.assertEqual(paper_view["booked_position"], 2)
        self.assertEqual(self.store.read_bytes(), before)

    def test_research_investigation_without_order_arms_a_read_only_follow_up(self):
        self.prepare(fee_bps=50, research_action="investigate")
        self.stage_observations()
        completed = self.operate("--repeat-paper")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        first = json.loads(completed.stdout)["first_trade"]
        self.assertIsNone(first["order"])
        result = first["research"]
        self.assertEqual(result["decision"]["action"], "investigate")
        (pending,) = self.read(
            self.store,
            "SELECT result_kind, result_id, purpose, due_at FROM work_follow_up",
        )
        self.assertEqual(
            pending,
            (
                "research_finding",
                result["finding_id"],
                "position_review",
                result["decided_at"] + 60,
            ),
        )
        self.assertEqual(
            self.read(self.venue, "SELECT count(*) FROM venue_execution"), [(0,)]
        )

    def test_research_commit_survives_employee_exit_and_full_unicode_payload(self):
        self.prepare(
            research_action="investigate", research_reply="exit", long_finding=True
        )
        self.stage_observations()
        result = self.operate()
        self.assertEqual(result.returncode, 0, result.stderr)
        (body,) = self.read(self.store, "SELECT content FROM paper_research_finding")
        finding = json.loads(body[0])
        self.assertEqual(finding["hypothesis"], "🧪" * 2000)
        self.assertEqual(finding["uncertainty"], "証" * 2000)
        self.assertEqual(self.read(self.store, "SELECT count(*) FROM duty_run"), [(1,)])
        self.assertEqual(
            self.read(self.store, "SELECT count(*) FROM work_follow_up"), [(1,)]
        )
        self.assertEqual(
            self.read(self.venue, "SELECT count(*) FROM venue_execution"), [(0,)]
        )

    def test_company_reopens_committed_research_without_a_second_agent_turn(self):
        for action, expected_orders in (("investigate", 0), ("buy", 1)):
            with self.subTest(action=action):
                self.prepare(research_action=action, research_reply="kill")
                self.stage_observations()
                cut = self.operate()
                self.assertEqual(cut.returncode, -signal.SIGKILL, cut.stderr)
                original = self.read(
                    self.store,
                    "SELECT run_id, decision_id FROM paper_research_decision",
                )
                self.assertEqual(len(original), 1)
                self.assertEqual(
                    self.read(self.venue, "SELECT count(*) FROM venue_execution"),
                    [(0,)],
                )
                self.success(
                    "operate-paper",
                    self.store,
                    self.venue,
                    *self.identity,
                    "--employee",
                    self.peer,
                    "--turn-budget",
                    "1",
                )
                self.assertEqual(
                    self.read(
                        self.store,
                        "SELECT run_id, decision_id FROM paper_research_decision",
                    ),
                    original,
                )
                self.assertEqual(
                    self.read(self.store, "SELECT count(*) FROM duty_run"), [(1,)]
                )
                self.assertEqual(
                    self.read(self.venue, "SELECT count(*) FROM venue_execution"),
                    [(expected_orders,)],
                )
                self.assertEqual(
                    self.read(self.store, "SELECT count(*) FROM work_follow_up"), [(1,)]
                )
