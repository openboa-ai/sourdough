import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time_ns

from sourdough.market_worker import SpotInstrumentTerms
from sourdough.nautilus_paper import NautilusPaperVenue, SandboxBalance, SandboxPlan
from sourdough.paper_execution import PaperIntent, VenueRefused
from sourdough.spot_cash import SpotCashUnits


class NautilusPaperPort(unittest.TestCase):
    def setUp(self):
        self.terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        self.units = SpotCashUnits(self.terms)
        self.plan = SandboxPlan("company", "paper", self.terms, 1000 * 10**8, 0)
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.venue = NautilusPaperVenue(directory / "sandbox.sqlite")

    def test_cash_units_require_exact_size_price_and_order_minima(self):
        ask = self.units.price_per_lot(Decimal("59148.49"))
        self.assertEqual(ask, 59_148_490)
        self.assertEqual(
            self.units.limit_order(20, ask), (Decimal("0.0002"), Decimal("59148.49"))
        )
        self.assertEqual(self.units.lots(Decimal("0.0002")), 20)
        self.assertEqual(self.units.quote_atoms(Decimal("0.00001001")), 1001)
        with self.assertRaisesRegex(VenueRefused, "price increment"):
            self.units.limit_order(20, ask + 1)
        with self.assertRaisesRegex(VenueRefused, "spot minima"):
            self.units.limit_order(1, ask)
        with self.assertRaisesRegex(VenueRefused, "integer units"):
            self.units.lots(Decimal("0.00001001"))

    def test_pre_replay_plan_recognizes_same_live_paper_account(self):
        destination = self.venue.provision(self.plan)
        with closing(sqlite3.connect(self.venue.journal)) as database, database:
            (body,) = database.execute("SELECT plan FROM sandbox_config").fetchone()
            legacy = json.loads(body)
            del legacy["tape_revision"]
            database.execute("UPDATE sandbox_config SET plan=?", (json.dumps(legacy),))
        reopened = NautilusPaperVenue(self.venue.journal)
        self.assertEqual(reopened.provision(self.plan), destination)
        self.assertEqual(reopened.configuration(), (destination, self.plan))
        with self.assertRaisesRegex(VenueRefused, "original account"):
            reopened.provision(replace(self.plan, tape_revision="1" * 64))

    def test_fill_replays_once_and_survives_the_sender(self):
        destination = self.venue.provision(self.plan)
        self.assertEqual(destination, self.venue.provision(self.plan))
        with self.assertRaisesRegex(VenueRefused, "witnessed"):
            self.venue.observe_account("paper")
        opening = self.venue.start_generation()
        self.assertEqual(opening, SandboxBalance(0, self.plan.starting_atoms))
        self.venue.prove_account("current", "BINANCE-001", "USDT", opening)
        stamp = time_ns()
        price = self.units.price_per_lot(Decimal("59000"))
        first_price = price - 10
        cash_after = self.plan.starting_atoms - 8 * first_price - 12 * price
        self.venue.publish_quote("current", price, stamp, stamp)
        account = self.venue.observe_account("paper")
        self.assertEqual(account.destination_id, destination)
        intent = PaperIntent("action", self.terms.revision, 20, price)
        with ThreadPoolExecutor(max_workers=1) as pool:
            submission = pool.submit(
                self.venue.submit, "order", account.account_id, intent
            )
            deadline = monotonic() + 4
            command = None
            while command is None and monotonic() < deadline:
                command = self.venue.claim_command("current")
                sleep(0.01)
            self.assertEqual(command.order_id, "order")
            self.assertIsNone(self.venue.claim_command("current"))
            with self.assertRaisesRegex(VenueRefused, "original sender"):
                self.venue.record_fill("departed", "order", "fill", 20, price, 0)
            self.venue.record_fill("current", "order", "first", 8, first_price, 0)
            self.venue.record_fill("current", "order", "first", 8, first_price, 0)
            [partial] = self.venue.read_fills(account.account_id, "order")
            self.assertEqual((partial.quantity, partial.filled_quantity), (20, 8))
            self.assertFalse(submission.done())
            self.venue.record_cancellation("current", "order", "unconfirmed", None)
            self.assertIsNone(self.venue.read_cancellation(account.account_id, "order"))
            self.assertEqual(
                self.venue.read_fills(account.account_id, "order"), (partial,)
            )
            with self.assertRaisesRegex(VenueRefused, "exceed the original order"):
                self.venue.record_fill("current", "order", "overfill", 13, price, 0)
            self.venue.record_fill("current", "order", "final", 12, price, 0)
            self.venue.record_fill("current", "order", "final", 12, price, 0)
            self.venue.record_cancellation("current", "order", "late", None)
            self.assertIsNone(self.venue.read_cancellation(account.account_id, "order"))
            with self.assertRaisesRegex(VenueRefused, "cancellation conflicts"):
                self.venue.record_cancellation("departed", "order", "stale", None)
            with self.assertRaisesRegex(VenueRefused, "cancellation conflicts"):
                self.venue.record_cancellation("current", "order", "contradictory", 8)
            submission.result(timeout=2)
        sender = NautilusPaperVenue(self.venue.journal)
        history = sender.read_fills(account.account_id, "order")
        self.assertEqual(
            [
                (fill.fill_id, fill.filled_quantity, fill.fill_price_minor_units)
                for fill in history
            ],
            [("first", 8, first_price), ("final", 12, price)],
        )
        self.assertEqual(
            sender.read_fills(account.account_id, "order", account_sync=True), ()
        )
        with self.assertRaisesRegex(VenueRefused, "missing a corroborated"):
            sender.start_generation()
        with self.assertRaisesRegex(VenueRefused, "no fresh quote"):
            sender.observe_quote(account.account_id)
        self.assertFalse(sender.record_account("departed", "order", 20, cash_after))
        self.assertFalse(
            sender.record_account(
                "current", "order", 20, self.plan.starting_atoms - 20 * price
            )
        )
        self.assertTrue(sender.record_account("current", "order", 20, cash_after))
        self.assertEqual(
            sender.read_fills(account.account_id, "order", account_sync=True), history
        )
        checkpoint = sender.start_generation()
        self.assertEqual(checkpoint, SandboxBalance(20, cash_after))
        with self.assertRaisesRegex(VenueRefused, "original verified balances"):
            sender.prove_account("replacement", "BINANCE-001", "USDT", opening)
        sender.prove_account("replacement", "BINANCE-001", "USDT", checkpoint)
        self.assertEqual(sender.observe_account("paper"), account)
        with closing(sqlite3.connect(sender.journal)) as database:
            self.assertEqual(
                database.execute("SELECT count(*) FROM sandbox_order").fetchone(), (1,)
            )
            self.assertEqual(
                database.execute(
                    "SELECT count(*) FROM sandbox_fill_observation"
                ).fetchone(),
                (2,),
            )
            self.assertEqual(
                database.execute(
                    "SELECT position_lots,cash_atoms FROM sandbox_activation ORDER BY rowid"
                ).fetchall(),
                [
                    (0, opening.cash_atoms),
                    (checkpoint.position_lots, checkpoint.cash_atoms),
                ],
            )
