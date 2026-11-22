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
from unittest.mock import patch

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import claim_first_duty
from sourdough.market_worker import SpotInstrumentTerms
from sourdough.nautilus_paper import NautilusPaperVenue, SandboxBalance, SandboxPlan
from sourdough.paper_account import PaperMandate, bind
from sourdough.paper_books import post_confirmed_fill, read_booked_no_fill
from sourdough.paper_execution import PaperIntent, VenueRefused
from sourdough.paper_order import OrderRefused, OrderReplyUnknown, submit_paper_order
from sourdough.paper_reconcile import reconcile_paper_orders
from sourdough.spot_cash import SpotCashUnits
from sourdough.work_projection import read_local_board_timeline


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

    def test_replay_quote_requires_sealed_cut_but_not_fresh_wall_time(self):
        revision = "1" * 64
        self.venue.provision(replace(self.plan, tape_revision=revision))
        balance = self.venue.start_generation()
        self.venue.prove_account("offline", "BINANCE-001", "USDT", balance)
        self.venue.publish_quote("offline", 50_000_000, 100, 90)
        with self.assertRaisesRegex(VenueRefused, "sealed availability cut"):
            self.venue.observe_quote("paper")
        self.venue.publish_replay_cut("offline", 50_000_000, 90, 101)
        with patch("sourdough.nautilus_paper.time_ns", return_value=time_ns() + 10**12):
            price, cut = self.venue.observe_research_quote("paper")
        self.assertEqual((price, cut.tape_revision), (50_000_000, revision))
        self.assertEqual((cut.event_ns, cut.available_ns), (90, 101))

    def test_current_quote_cannot_be_refreshed_by_callback_or_new_trade(self):
        self.venue.provision(self.plan)
        self.venue.prove_account(
            "current", "BINANCE-001", "USDT", self.venue.start_generation()
        )
        now = time_ns()
        price = 50_000_000
        with patch("sourdough.nautilus_paper.time_ns", return_value=now):
            self.venue.publish_quote(
                "current", price, now, now, event_ns=now - 60 * 10**9
            )
            with self.assertRaisesRegex(VenueRefused, "no fresh quote"):
                self.venue.observe_quote("paper")
            self.venue.publish_quote(
                "current", price, now - 2, now, event_ns=now - 10 * 10**9
            )
            self.assertEqual(self.venue.observe_quote("paper"), price)
        with patch("sourdough.nautilus_paper.time_ns", return_value=now + 6 * 10**9):
            with self.assertRaisesRegex(VenueRefused, "no fresh quote"):
                self.venue.observe_quote("paper")
        with closing(sqlite3.connect(self.venue.journal)) as journal:
            self.assertEqual(
                journal.execute(
                    "SELECT event_ns,received_ns,exchange_trade_ns FROM sandbox_quote"
                ).fetchone(),
                (now - 10 * 10**9, now - 2, now),
            )

    def test_rejected_order_releases_once_only_after_the_original_balance(self):
        store = self.venue.journal.with_name("company.sqlite")
        founded = found(store, FoundingTerms("refusal", "charter", "paper", "runtime"))
        self.venue.provision(replace(self.plan, company_id=founded.company_id))
        balance = self.venue.start_generation()
        self.venue.prove_account("current", "BINANCE-001", "USDT", balance)
        account = self.venue.observe_account("paper")
        bind(
            store,
            PaperMandate(
                "mandate", founded.company_id, "charter", account, 10_000_000_000
            ),
            self.venue,
        )
        claim = claim_first_duty(store, founded.company_id, "runtime")
        price = 59_000_000
        stamp = time_ns()
        self.venue.publish_quote("current", price, stamp, stamp)
        intent = PaperIntent("action", self.terms.revision, 20, price)
        with ThreadPoolExecutor(max_workers=1) as pool:
            submission = pool.submit(
                submit_paper_order, store, claim, intent, self.venue
            )
            deadline = monotonic() + 3
            command = None
            while command is None and monotonic() < deadline:
                command = self.venue.claim_command("current")
                sleep(0.01)
            self.assertIsNotNone(command)
            order_id = command.order_id
            self.venue.mark_unresolved("current", order_id)
            self.assertEqual(submission.result(timeout=2).order_id, order_id)
        self.venue.invalidate_quote()
        self.assertIsNone(self.venue.read_cancellation("paper", order_id))
        self.venue.record_cancellation(
            "current", order_id, "original-refusal", 0, outcome="rejected"
        )
        with self.assertRaisesRegex(VenueRefused, "changed its terms"):
            self.venue.record_cancellation(
                "current", order_id, "original-refusal", 0, outcome="denied"
            )
        self.assertFalse(
            self.venue.record_account("current", order_id, 0, balance.cash_atoms - 1)
        )
        self.assertIsNone(self.venue.read_cancellation("paper", order_id))
        self.assertIsNone(
            post_confirmed_fill(store, founded.company_id, order_id, self.venue)
        )
        [order] = read_local_board_timeline(store, founded.company_id)["paper"][
            "orders"
        ]
        self.assertEqual(
            (order["status"], order["open_reservation_minor_units"]),
            ("unresolved", 20 * price),
        )
        self.assertTrue(
            self.venue.record_account("current", order_id, 0, balance.cash_atoms)
        )
        for account_sync in (False, True):
            terminal = self.venue.read_cancellation(
                "paper", order_id, account_sync=account_sync
            )
            self.assertEqual(
                (terminal.event_id, terminal.outcome, terminal.filled_quantity),
                ("original-refusal", "rejected", 0),
            )
            self.assertIsNone(
                post_confirmed_fill(
                    store,
                    founded.company_id,
                    order_id,
                    self.venue,
                    account_sync=account_sync,
                )
            )
        result = read_booked_no_fill(store, founded.company_id, order_id)
        self.assertEqual((result.status, result.position_quantity), ("rejected", 0))
        [order] = read_local_board_timeline(store, founded.company_id)["paper"][
            "orders"
        ]
        self.assertEqual(
            (order["status"], order["open_reservation_minor_units"]), ("rejected", 0)
        )
        self.assertEqual(
            NautilusPaperVenue(self.venue.journal).start_generation(), balance
        )

    def test_invalid_managed_terms_never_reserve_or_reach_the_venue(self):
        store = self.venue.journal.with_name("company.sqlite")
        founded = found(store, FoundingTerms("setup", "charter", "paper", "runtime"))
        plan = replace(self.plan, company_id=founded.company_id)
        self.venue.provision(plan)
        self.venue.prove_account(
            "current", "BINANCE-001", "USDT", self.venue.start_generation()
        )
        account = self.venue.observe_account("paper")
        bind(
            store,
            PaperMandate(
                "mandate", founded.company_id, "charter", account, 10_000_000_000
            ),
            self.venue,
        )
        claim = claim_first_duty(store, founded.company_id, "runtime")
        limit = self.units.price_per_lot(Decimal("59000"))
        supported = PaperIntent("action", self.terms.revision, 20, limit)
        for invalid in (
            replace(supported, quantity=1),
            replace(supported, limit_minor_units=limit + 1),
            replace(supported, instrument_rules_digest="changed"),
        ):
            with self.subTest(intent=invalid):
                with self.assertRaisesRegex(OrderRefused, "refused the order terms"):
                    submit_paper_order(store, claim, invalid, self.venue)
        self.assertEqual(self.venue.check_order_terms("paper", supported), account)
        with closing(sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)) as company:
            self.assertIsNone(
                company.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='paper_order'"
                ).fetchone()
            )
        with closing(
            sqlite3.connect(self.venue.journal.as_uri() + "?mode=ro", uri=True)
        ) as venue:
            self.assertEqual(
                venue.execute("SELECT count(*) FROM sandbox_order").fetchone(), (0,)
            )

    def test_admitted_order_waits_for_a_fresh_worker_without_losing_its_identity(self):
        store = self.venue.journal.with_name("company.sqlite")
        founded = found(store, FoundingTerms("setup", "charter", "paper", "runtime"))
        plan = replace(self.plan, company_id=founded.company_id)
        self.venue.provision(plan)
        balance = self.venue.start_generation()
        self.venue.prove_account("departed", "BINANCE-001", "USDT", balance)
        account = self.venue.observe_account("paper")
        bind(
            store,
            PaperMandate(
                "mandate", founded.company_id, "charter", account, 10_000_000_000
            ),
            self.venue,
        )
        price = self.units.price_per_lot(Decimal("59000"))
        stamp = time_ns()
        self.venue.publish_quote("departed", price, stamp, stamp)
        claim = claim_first_duty(store, founded.company_id, "runtime")
        intent = PaperIntent("resume-original", self.terms.revision, 20, price)
        self.venue.invalidate_quote()
        with (
            patch("sourdough.nautilus_paper.monotonic", side_effect=(0, 13)),
            self.assertRaises(OrderReplyUnknown) as awaiting,
        ):
            submit_paper_order(store, claim, intent, self.venue)
        order_id = awaiting.exception.order_id
        with self.assertRaisesRegex(VenueRefused, "no fresh quote"):
            self.venue.claim_command("departed")
        with closing(sqlite3.connect(self.venue.journal)) as journal:
            self.assertEqual(
                journal.execute(
                    "SELECT order_id,state,generation FROM sandbox_order"
                ).fetchall(),
                [(order_id, "queued", None)],
            )
        with closing(sqlite3.connect(store)) as book:
            self.assertEqual(
                book.execute("SELECT reserved_minor_units FROM paper_order").fetchall(),
                [(20 * price,)],
            )
        pending = reconcile_paper_orders(store, founded.company_id, self.venue, now=100)
        self.assertEqual(pending.unresolved, 1)
        self.assertEqual(pending.recovered, ())

        replacement = NautilusPaperVenue(self.venue.journal)
        self.assertEqual(replacement.start_generation(), balance)
        replacement.prove_account("current", "BINANCE-001", "USDT", balance)
        stamp = time_ns()
        replacement.publish_quote("current", price, stamp, stamp)
        command = replacement.claim_command("current")
        self.assertIsNotNone(command)
        self.assertEqual(
            (command.order_id, command.action_id), (order_id, intent.action_id)
        )
        self.assertIsNone(replacement.claim_command("current"))
        replacement.record_fill("current", order_id, "original-fill", 20, price, 0)
        self.assertTrue(
            replacement.record_account(
                "current", order_id, 20, plan.starting_atoms - 20 * price
            )
        )
        receipt = reconcile_paper_orders(
            store, founded.company_id, replacement, now=102
        )
        self.assertEqual(receipt.unresolved, 0)
        [recovered] = receipt.recovered
        self.assertEqual(recovered.posting.order_id, order_id)
        self.assertEqual(recovered.posting.fill_id, "original-fill")
        self.assertEqual(recovered.original_claim, claim)
        again = submit_paper_order(store, claim, intent, replacement)
        self.assertEqual(again.order_id, order_id)
        self.assertFalse(again.dispatched_now)

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
