import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from sourdough.company import FoundingTerms, found
from sourdough.company_run import run_paper_company
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_execution import (
    PaperExecution,
    PaperOrderSizing,
    VenueExecution,
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
        self.assertEqual(result.first_trade.position_quantity, 1)

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
