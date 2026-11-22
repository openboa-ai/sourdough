import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from sourdough.company import FoundingTerms, found
from sourdough.company_run import run_paper_company
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_execution import (
    PaperExecution,
    PaperOrderSizing,
    VenueExecution,
    VenuePosition,
    VenueRefused,
)
from sourdough.paper_venue import FixedQuoteVenue


class ExecutionPort(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.company = found(
            self.store, FoundingTerms("setup", "charter", "paper", "employee")
        )

    def test_company_admits_and_books_through_an_independent_adapter(self):
        account = PaperAccount(
            "account", "another-adapter", "rules", "USD", 1000, "paper", "destination"
        )
        execution = None
        reservations_at_send = []
        venue = Mock(spec_set=PaperExecution)
        venue.observe_account.return_value = account
        venue.read_cancellation.return_value = None
        venue.observe_quote.return_value = 100
        sizing = PaperOrderSizing("EXAMPLE", "0.1", "USD", 100, 1, 1, 50)
        venue.observe_order_sizing.return_value = sizing

        def submit(order_id, account_id, intent):
            nonlocal execution
            with closing(
                sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)
            ) as db:
                reservations_at_send.extend(
                    db.execute("SELECT order_id,reserved_minor_units FROM paper_order")
                )
            execution = VenueExecution(
                order_id,
                account_id,
                intent.instrument_rules_digest,
                intent.quantity,
                intent.limit_minor_units,
                "fill-at-destination",
                99,
            )

        venue.submit.side_effect = submit
        venue.read_order_position.side_effect = (
            lambda account_id, order_id: VenuePosition(
                order_id, account_id, account.instrument_rules_digest, 7
            )
        )
        venue.read_fills.side_effect = lambda account_id, order_id, **_: (
            (execution,)
            if execution is not None
            and (execution.account_id, execution.order_id) == (account_id, order_id)
            else ()
        )
        bind(
            self.store,
            PaperMandate("mandate", self.company.company_id, "charter", account, 500),
            venue,
        )
        employee = Mock(profile="employee")
        employee.run.side_effect = lambda context, order: order("action", 1, 100)
        result = run_paper_company(
            self.store, self.company.company_id, venue, employee, turn_budget=1
        )
        venue.require_independent_from.assert_called_with(self.store)
        self.assertEqual(employee.run.call_args.args[0].order_sizing, sizing)
        venue.submit.assert_called_once()
        order = result.first_trade.order
        venue.read_fills.assert_any_call(
            account.account_id, order.order_id, account_sync=False
        )
        self.assertEqual(reservations_at_send, [(order.order_id, 100)])
        self.assertEqual(order.posting.fill_id, "fill-at-destination")
        self.assertEqual(order.posting.price_minor_units, 99)
        self.assertEqual(result.first_trade.position_quantity, 7)
        with closing(sqlite3.connect(self.store)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT fill_id,position_quantity FROM paper_position_observation"
                ).fetchall(),
                [("fill-at-destination", 7)],
            )

    def test_unavailable_position_does_not_finish_the_duty_or_resubmit(self):
        class PendingPosition(FixedQuoteVenue):
            position_offline = True
            sends = 0

            def submit(self, order_id, account_id, intent):
                self.sends += 1
                super().submit(order_id, account_id, intent)

            def read_order_position(self, account_id, order_id):
                if self.position_offline:
                    raise TimeoutError("Destination position lookup is offline")
                return super().read_order_position(account_id, order_id)

        venue = PendingPosition(self.root / "venue.sqlite")
        account = venue.provision(
            PaperAccount("account", "fixture", "rules", "USD", 1000, "paper"), 100
        )
        company_id = self.company.company_id
        bind(
            self.store,
            PaperMandate("mandate", company_id, "charter", account, 500),
            venue,
        )
        employee = Mock(profile="employee")
        employee.run.side_effect = lambda context, order: order("action", 1, 100)

        waiting = run_paper_company(
            self.store, company_id, venue, employee, turn_budget=1
        )
        original = waiting.first_trade
        self.assertIsNotNone(original.order.posting)
        self.assertIsNone(original.position_quantity)
        self.assertEqual(waiting.blocked_work, (original.claim.duty_id,))
        employee.review.assert_not_called()

        venue.position_offline = False
        with (
            patch(
                "sourdough.company_run.finish_first_duty",
                side_effect=OSError("cut before settlement"),
            ),
            self.assertRaisesRegex(OSError, "cut before settlement"),
        ):
            run_paper_company(self.store, company_id, venue, employee, turn_budget=1)
        venue.position_offline = True

        def review(read):
            observation = read()
            self.assertEqual(observation["order_id"], original.order.order_id)
            self.assertEqual(observation["position_quantity"], 1)
            return {
                "tool": "record_review_decision",
                "call_id": "next",
                "action": "hold",
                "observation_revision": observation["revision"],
                "rationale": "The original exposure is confirmed.",
                "next_check_seconds": 60,
            }

        employee.review.side_effect = review
        resumed = run_paper_company(
            self.store, company_id, venue, employee, turn_budget=1
        )
        self.assertEqual(resumed.blocked_work, ())
        self.assertEqual(resumed.review.claim.founder_id, original.claim.founder_id)
        self.assertEqual(venue.sends, 1)
        employee.run.assert_called_once()

    def test_fixture_cannot_share_the_company_store_even_through_an_alias(self):
        symbolic = self.root / "symbolic.sqlite"
        symbolic.symlink_to(self.store)
        hard = self.root / "hard.sqlite"
        os.link(self.store, hard)
        for journal in (self.store, symbolic, hard):
            with (
                self.subTest(journal=journal),
                self.assertRaisesRegex(VenueRefused, "must be separate"),
            ):
                FixedQuoteVenue(journal).require_independent_from(self.store)
