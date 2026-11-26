import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.trading.execution import PaperAccount, PaperIntent
from sourdough.trading.venue import FixedQuoteVenue, VenueRefused


class PaperVenueTests(unittest.TestCase):
    def setUp(self):
        self.journal = Path(self.enterContext(TemporaryDirectory())) / "venue.sqlite"
        self.account = PaperAccount(
            "account", "local-v1", "rules", "USD", 5000, "paper"
        )
        self.venue = FixedQuoteVenue(self.journal)
        self.account = self.venue.provision(self.account, 100)
        self.intent = PaperIntent("action", "rules", 2, 110)

    def test_fill_has_one_durable_destination_identity(self):
        self.venue.submit("order", "account", self.intent)
        reopened = FixedQuoteVenue(self.journal)
        self.assertEqual(reopened.observe_account("account"), self.account)
        reopened.submit("order", "account", self.intent)
        with self.assertRaises(VenueRefused):
            reopened.submit("order", "account", replace(self.intent, quantity=3))
        observed = reopened.observe_execution("order")
        self.assertIsNotNone(observed)
        self.assertTrue(observed.fill_id)
        with closing(
            sqlite3.connect(f"{self.journal.as_uri()}?mode=ro", uri=True)
        ) as db:
            self.assertEqual(
                db.execute(
                    "SELECT order_id, fill_id, quantity, fill_price_minor_units "
                    "FROM venue_execution"
                ).fetchall(),
                [("order", observed.fill_id, 2, 100)],
            )

    def test_below_quote_remains_unfilled(self):
        self.venue.submit(
            "order", "account", replace(self.intent, limit_minor_units=99)
        )
        observed = self.venue.observe_execution("order")
        self.assertIsNotNone(observed)
        self.assertIsNone(observed.fill_id)
        self.assertIsNone(observed.fill_price_minor_units)

    def test_reprovision_cannot_change_price(self):
        self.venue.provision(self.account, 100)
        with self.assertRaises(VenueRefused):
            self.venue.provision(self.account, 99)
