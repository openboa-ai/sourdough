import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import (
    PAPER_CONTROL_SCHEMA_REVISION,
    FoundingTerms,
    found,
    upgrade_work_schema,
)
from sourdough.company_run import run_paper_company
from sourdough.duty_run import PAPER_EXECUTION, POSITION_REVIEW
from sourdough.first_duty import claim_first_duty
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import capture_position, post_confirmed_fill
from sourdough.paper_execution import PaperIntent
from sourdough.paper_order import OrderRefused, OrderReplyUnknown, submit_paper_order
from sourdough.paper_risk import (
    SUSPEND_NEW_RISK,
    PaperRiskRefused,
    PaperSuspension,
    read_paper_risk_receipt,
    suspend_paper_risk,
)
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.work import ResultReference, finish_first_duty
from sourdough.writer_lease import hold_company_writer


class ReviewingEmployee:
    profile = "runtime"

    def review(self, read_result):
        self.observed = read_result()
        return {
            "tool": "record_review_decision",
            "call_id": "suspended-position-review",
            "action": "hold",
            "observation_revision": self.observed["revision"],
            "rationale": "Review the original position without new risk.",
            "next_check_seconds": 60,
        }


class PaperRiskControl(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.journal = self.root / "venue.sqlite"
        self.company = found(
            self.store, FoundingTerms("setup", "charter", "paper", "runtime")
        )
        self.venue = FixedQuoteVenue(self.journal)
        account = self.venue.provision(
            PaperAccount("account", "fixed-quote-v1", "rules", "USD", 5000, "paper"),
            100,
        )
        bind(
            self.store,
            PaperMandate("mandate", self.company.company_id, "charter", account, 1000),
            self.venue,
        )
        self.claim = claim_first_duty(self.store, self.company.company_id, "runtime")
        self.intent = PaperIntent("first", "rules", 2, 110)
        self.command = PaperSuspension("suspend", "charter", 0, "Risk review")
        self.allowed = True

    def authorize(self, database, company_id, operation):
        self.assertEqual(operation, SUSPEND_NEW_RISK)
        self.assertEqual(
            database.execute("SELECT id FROM company").fetchone(), (company_id,)
        )
        return "verified-test-board" if self.allowed else None

    def writer(self):
        return hold_company_writer(
            self.store, self.company.company_id, lease_directory=self.root / "leases"
        )

    def rows(self, path, sql, params=()):
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
            return database.execute(sql, params).fetchall()

    def company_table(self, name):
        return self.rows(
            self.store, "SELECT name FROM sqlite_schema WHERE name=?", (name,)
        )

    def test_applied_suspension_wins_against_completed_destination_preflight(self):
        original_check = self.venue.check_order_terms
        with self.writer() as writer:

            def race(account_id, intent):
                account = original_check(account_id, intent)
                receipt = suspend_paper_risk(writer, self.command, self.authorize)
                self.assertEqual(
                    (receipt.disposition, receipt.effective_revision), ("applied", 1)
                )
                return account

            self.venue.check_order_terms = race
            with self.assertRaisesRegex(OrderRefused, "risk is suspended"):
                submit_paper_order(
                    self.store, self.claim, self.intent, self.venue, writer=writer
                )
        self.assertEqual(self.company_table("paper_order"), [])
        self.assertEqual(
            self.rows(self.journal, "SELECT order_id FROM venue_execution"), []
        )

    def test_original_order_can_settle_after_suspension(self):
        original_send = self.venue.submit

        def lose_send(*args):
            raise TimeoutError

        self.venue.submit = lose_send
        with self.writer() as writer:
            with self.assertRaises(OrderReplyUnknown) as pending:
                submit_paper_order(
                    self.store, self.claim, self.intent, self.venue, writer=writer
                )
            original_id = pending.exception.order_id
            suspend_paper_risk(writer, self.command, self.authorize)
            replay = submit_paper_order(
                self.store, self.claim, self.intent, self.venue, writer=writer
            )
            self.assertEqual(
                (replay.order_id, replay.dispatched_now), (original_id, False)
            )
            original_send(original_id, "account", self.intent)
            with self.assertRaisesRegex(OrderRefused, "risk is suspended"):
                submit_paper_order(
                    self.store,
                    self.claim,
                    replace(self.intent, action_id="new"),
                    self.venue,
                    writer=writer,
                )
        employee = ReviewingEmployee()
        result = run_paper_company(
            self.store, self.company.company_id, self.venue, employee, repeat_paper=True
        )
        self.assertEqual(result.reconciliation.unresolved, 0)
        self.assertEqual(result.blocked_work, ())
        self.assertEqual(result.review.claim.founder_id, self.claim.founder_id)
        self.assertNotEqual(result.review.claim.duty_id, self.claim.duty_id)
        self.assertEqual(result.review.decision.action, "hold")
        self.assertEqual(employee.observed["position_quantity"], self.intent.quantity)
        self.assertEqual(
            self.rows(self.store, "SELECT order_id,fill_id,quantity FROM paper_fill"),
            self.rows(
                self.journal, "SELECT order_id,fill_id,quantity FROM venue_execution"
            ),
        )
        self.assertEqual(
            self.rows(self.journal, "SELECT order_id FROM venue_execution"),
            [(original_id,)],
        )

    def test_stale_request_and_exact_replay_do_not_reapply_or_grant_access(self):
        with self.writer() as writer:
            with self.assertRaises(PaperRiskRefused):
                suspend_paper_risk(writer, self.command, None)
            self.assertEqual(self.company_table("paper_risk_command"), [])
            future = replace(self.command, request_id="future", expected_revision=1)
            conflict = suspend_paper_risk(writer, future, self.authorize)
            self.assertEqual(
                (conflict.disposition, conflict.effective_revision),
                ("conflicting_revision", 0),
            )
            original = suspend_paper_risk(writer, self.command, self.authorize)
            self.assertEqual(
                suspend_paper_risk(writer, future, self.authorize), conflict
            )
        upgrade_work_schema(self.store, self.company.company_id)
        self.assertEqual(
            self.rows(self.store, "SELECT schema_revision FROM company"),
            [(PAPER_CONTROL_SCHEMA_REVISION,)],
        )
        with self.writer() as writer:
            self.assertEqual(
                read_paper_risk_receipt(
                    writer, self.command.request_id, self.authorize
                ),
                original,
            )
            self.assertEqual(
                suspend_paper_risk(writer, self.command, self.authorize), original
            )
            with self.assertRaises(PaperRiskRefused):
                suspend_paper_risk(
                    writer, replace(self.command, reason="changed"), self.authorize
                )
            self.allowed = False
            with self.assertRaises(PaperRiskRefused):
                suspend_paper_risk(writer, self.command, self.authorize)
            with self.assertRaises(PaperRiskRefused):
                read_paper_risk_receipt(writer, self.command.request_id, self.authorize)
        self.assertEqual(
            self.rows(self.store, "SELECT count(*) FROM paper_risk_command"), [(2,)]
        )

    def test_previously_scheduled_execution_duty_continues_as_review_only(self):
        with self.writer() as writer:
            order = submit_paper_order(
                self.store, self.claim, self.intent, self.venue, writer=writer
            )
            fill = post_confirmed_fill(
                self.store,
                self.company.company_id,
                order.order_id,
                self.venue,
                writer=writer,
            )
            position = capture_position(
                self.store,
                self.company.company_id,
                fill.fill_id,
                self.venue.read_order_position("account", order.order_id),
                now=100,
            )
            scheduled = finish_first_duty(
                self.store,
                self.claim,
                ResultReference("paper_position", fill.fill_id, position.revision, 100),
                due_at=100,
                purpose=PAPER_EXECUTION,
            )
            suspend_paper_risk(writer, self.command, self.authorize)
        employee = ReviewingEmployee()
        result = run_paper_company(
            self.store, self.company.company_id, self.venue, employee, repeat_paper=True
        )
        self.assertEqual(result.review.claim.duty_id, scheduled.duty_id)
        self.assertEqual(result.review.next_duty.purpose, POSITION_REVIEW)
        self.assertEqual(employee.observed["fill_id"], fill.fill_id)
        self.assertEqual(result.later_trades, ())
        self.assertEqual(result.blocked_work, ())
        self.assertEqual(
            self.rows(self.journal, "SELECT order_id FROM venue_execution"),
            [(order.order_id,)],
        )
