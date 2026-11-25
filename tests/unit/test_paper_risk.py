import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import (
    SCHEMA_REVISION,
    FoundingTerms,
    found,
)
from sourdough.company_run import run_paper_company
from sourdough.duty_run import PAPER_EXECUTION, POSITION_REVIEW
from sourdough.first_duty import claim_first_duty
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import capture_position, post_confirmed_fill
from sourdough.paper_execution import PaperIntent
from sourdough.paper_order import OrderRefused, OrderReplyUnknown, submit_paper_order
from sourdough.paper_risk import (
    RESUME_NEW_RISK,
    SUSPEND_NEW_RISK,
    PaperRiskRefused,
    PaperSuspension,
    _resume_with_evidence,
    read_local_command_receipt_status,
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
        self.assertEqual(
            self.rows(self.store, "SELECT COUNT(*) FROM paper_order"), [(0,)]
        )
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
            self.assertEqual(
                self.rows(
                    self.store,
                    "SELECT COUNT(*) FROM command_receipt WHERE command_type IN ('suspend_new_risk','resume_new_risk')",
                ),
                [(0,)],
            )
            self.assertEqual(
                self.rows(self.store, "SELECT COUNT(*) FROM paper_risk_state"), [(0,)]
            )
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
        self.assertEqual(
            self.rows(self.store, "SELECT schema_revision FROM company"),
            [(SCHEMA_REVISION,)],
        )
        self.assertEqual(self.rows(self.store, "PRAGMA user_version"), [(1,)])
        self.assertEqual(
            self.rows(
                self.store,
                "SELECT suspended, effective_revision, last_command_id FROM paper_risk_state",
            ),
            [(1, 1, "suspend")],
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
            self.rows(
                self.store,
                "SELECT count(*) FROM command_receipt WHERE command_type IN ('suspend_new_risk','resume_new_risk')",
            ),
            [(2,)],
        )
        self.assertEqual(
            self.rows(
                self.store,
                "SELECT count(*) FROM audit_event WHERE event_type='paper.risk_suspended'",
            ),
            [(1,)],
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

    def test_command_receipt_status_authorization_redaction_and_resume_toctou(self):
        with self.writer() as writer:
            applied = suspend_paper_risk(writer, self.command, self.authorize)
            # Wrong principal or wrong operation on read_paper_risk_receipt must raise, not return None.
            with self.assertRaisesRegex(PaperRiskRefused, "not authorized"):
                read_paper_risk_receipt(
                    writer,
                    self.command.request_id,
                    lambda db, cid, op: "other-board-principal",
                    operation=SUSPEND_NEW_RISK,
                )
            with self.assertRaisesRegex(PaperRiskRefused, "not authorized"):
                read_paper_risk_receipt(
                    writer,
                    self.command.request_id,
                    lambda db, cid, op: applied.principal,
                    operation=RESUME_NEW_RISK,
                )

        # read_local_command_receipt_status must redact principal/reason and enforce operation scope & non-symlink store.
        status = read_local_command_receipt_status(
            self.store,
            self.company.company_id,
            self.command.request_id,
            operation=SUSPEND_NEW_RISK,
        )
        self.assertEqual(status["status"], "found")
        self.assertEqual(status["state"], "applied")
        self.assertEqual(status["effective_revision"], 1)
        self.assertNotIn("principal", status)
        self.assertNotIn("reason", status)
        self.assertNotIn("result", status)
        with self.assertRaisesRegex(PaperRiskRefused, "authorized board control scope"):
            read_local_command_receipt_status(
                self.store,
                self.company.company_id,
                self.command.request_id,
                operation=RESUME_NEW_RISK,
            )
        symlink = self.root / "store-link.sqlite"
        symlink.symlink_to(self.store)
        with self.assertRaisesRegex(PaperRiskRefused, "store owner"):
            read_local_command_receipt_status(
                symlink, self.company.company_id, self.command.request_id
            )

        # TOCTOU: if resume starts BEFORE a suspension commits (not suspended -> ineligible ->
        # verify_local is None), and a real suspension commits before Phase 2 so resume is now
        # eligible, _resume_with_evidence must refuse without poisoning request_id!
        with self.writer() as writer:
            # First resume rev 1 -> rev 2 (not suspended), then test a rev 3 resume racing with a rev 3 suspension.
            _resume_with_evidence(
                writer,
                PaperSuspension("resume-ok", "charter", 1, "Clean resume"),
                lambda _db, _cid, _op: "verified-test-board",
                lambda: (lambda _conn: None),
            )
            stale_cmd = PaperSuspension("resume-race", "charter", 3, "Resume race")
            phase = 0

            def racing_auth(_db, _cid, _op):
                nonlocal phase
                phase += 1
                return "verified-test-board"

            original_open_writer = None
            from sourdough import paper_risk as pr_mod

            original_open_writer = pr_mod.open_writer

            def suspend_before_phase2(*args, **kwargs):
                if phase == 1:
                    pr_mod.open_writer = original_open_writer
                    suspend_paper_risk(
                        writer,
                        PaperSuspension("suspend-rev3", "charter", 2, "Late suspend"),
                        self.authorize,
                    )
                return original_open_writer(*args, **kwargs)

            pr_mod.open_writer = suspend_before_phase2
            try:
                with self.assertRaisesRegex(
                    PaperRiskRefused, "changed during observation"
                ):
                    _resume_with_evidence(
                        writer,
                        stale_cmd,
                        racing_auth,
                        lambda: (lambda _conn: None),
                    )
            finally:
                pr_mod.open_writer = original_open_writer
            # Request ID must NOT have been poisoned with a conflicting_revision denial receipt!
            self.assertEqual(
                read_local_command_receipt_status(
                    self.store, self.company.company_id, stale_cmd.request_id
                )["status"],
                "not_found",
            )
