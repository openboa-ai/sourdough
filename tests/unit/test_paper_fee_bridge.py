import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time_ns
from unittest.mock import patch

from sourdough.company import FoundingTerms, found
from sourdough.general_ledger import JournalRefused
from sourdough.storage.db import open_writer, write_tx
from sourdough.trading.accounts import register_company_paper_tier
from sourdough.trading.duty import claim_paper_first_duty as claim_first_duty
from sourdough.trading.execution import PaperIntent, VenueRefused
from sourdough.trading.fee_bridge import (
    FeeChart,
    VerifiedFee,
    deliver_managed_paper_fee,
    deliver_paper_fee,
)
from sourdough.trading.ledger import submit_company_paper_book_order
from sourdough.trading.market_worker import SpotInstrumentTerms
from sourdough.trading.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.writer_lease import hold_company_writer


class PaperFeeBridge(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.company = found(
            self.store, FoundingTerms("fees", "charter", "paper", "employee")
        ).company_id
        self.chart = FeeChart(
            "chart", "paper-quote-fee-v1", "USD", 100, "fees", "paper-cash"
        )

    def admitted_fill(self, fee_bps):
        claim = claim_first_duty(self.store, self.company, "employee")
        with hold_company_writer(
            self.store, self.company, lease_directory=self.root / "leases"
        ) as writer:
            binding = register_company_paper_tier(
                writer,
                account_id="paper",
                destination_id="fixture-dest",
                connector_revision="fixture",
                instrument_rules_digest="instrument",
                currency="USD",
                starting_minor_units=5000,
                exposure_limit_minor_units=1000,
                fee_bps=fee_bps,
                slippage_bps=0,
            )
            order = submit_company_paper_book_order(
                writer,
                binding_id=binding.binding_id,
                actor_id=claim.founder_id,
                action_id="original",
                instrument_id="instrument",
                side="buy",
                quantity=2,
                limit_minor_units=110,
                market_price_minor_units=100,
                fill_mode="immediate",
                run_id=claim.run_id,
            )
        rows = self.rows(
            self.store,
            f"SELECT fill_id, fee_minor_units FROM trading_book_fill WHERE order_id = '{order['order_id']}'",
        )
        fill_id, fee_minor = rows[0]
        return order["order_id"], VerifiedFee(
            binding.destination_id,
            binding.account_id,
            str(fill_id),
            "venue-fee-cut",
            date(2026, 11, 22),
            "USD",
            100,
            int(fee_minor),
        )

    def rows(self, store, query):
        with closing(
            sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)
        ) as database:
            return database.execute(query).fetchall()

    def deliver(self, verified, chart=None):
        with hold_company_writer(
            self.store, self.company, lease_directory=self.root / "leases"
        ) as writer:
            return deliver_paper_fee(writer, verified, chart or self.chart)

    def test_one_fee_posting_is_atomic_and_idempotent_across_failure(self):
        _order_id, verified = self.admitted_fill(fee_bps=200)
        self.assertEqual(verified.fee_minor_units, 4)
        with self.assertRaisesRegex(JournalRefused, "original paper source"):
            self.deliver(replace(verified, destination_id="wrong-dest"))
        with (
            patch(
                "sourdough.trading.fee_bridge.append_audit_event",
                side_effect=OSError("cut before commit"),
            ),
            self.assertRaisesRegex(OSError, "before commit"),
        ):
            self.deliver(verified)
        self.assertEqual(self.rows(self.store, "SELECT id FROM journal_entry"), [])
        self.assertEqual(
            self.rows(
                self.store, "SELECT acknowledged,journal_id FROM trading_fee_delivery"
            ),
            [],
        )
        receipt = self.deliver(verified)
        [(journal_id,)] = self.rows(self.store, "SELECT id FROM journal_entry")
        self.assertEqual((receipt.journal_id, receipt.amount_minor), (journal_id, 4))
        self.assertEqual(self.deliver(verified), receipt)
        with self.assertRaisesRegex(JournalRefused, "original terms"):
            self.deliver(
                replace(verified, units_per_whole=1000),
                replace(self.chart, units_per_whole=1000),
            )
        self.assertEqual(
            self.rows(
                self.store,
                "SELECT account,amount_minor FROM journal_line ORDER BY ordinal",
            ),
            [("fees", 4), ("paper-cash", -4)],
        )
        self.assertEqual(
            self.rows(
                self.store,
                "SELECT source_event,evidence_revision,source FROM journal_entry",
            ),
            [(receipt.source_event, verified.revision, "paper-fill-fee")],
        )

    def test_explicit_zero_and_unverified_scope_are_distinct(self):
        _order_id, verified = self.admitted_fill(fee_bps=0)
        with self.assertRaisesRegex(JournalRefused, "original paper source"):
            self.deliver(replace(verified, fee_minor_units=1))
        with self.assertRaisesRegex(JournalRefused, "matching chart units"):
            self.deliver(verified, replace(self.chart, units_per_whole=1000))
        self.assertEqual(
            self.rows(self.store, "SELECT COUNT(*) FROM trading_fee_delivery"),
            [(0,)],
        )
        receipt = self.deliver(verified)
        self.assertEqual((receipt.amount_minor, receipt.journal_id), (0, None))
        self.assertEqual(self.deliver(verified), receipt)
        self.assertEqual(
            self.rows(self.store, "SELECT COUNT(*) FROM journal_entry"),
            [(0,)],
        )
        self.assertEqual(
            self.rows(
                self.store, "SELECT acknowledged,journal_id FROM trading_fee_delivery"
            ),
            [(1, None)],
        )

    def test_managed_fee_uses_original_fill_day_and_atoms(self):
        terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        venue = NautilusPaperVenue(self.root / "sandbox.sqlite")
        dest_id = venue.provision(
            SandboxPlan(self.company, "paper", terms, 100_000_000_000, 0)
        )
        gen = venue.start_generation()
        venue.prove_account("original", "BINANCE-001", "USDT", gen)
        with hold_company_writer(
            self.store, self.company, lease_directory=self.root / "leases"
        ) as writer:
            binding = register_company_paper_tier(
                writer,
                account_id="paper",
                destination_id=dest_id,
                connector_revision="nautilus-spot-sandbox-v1",
                instrument_rules_digest=terms.revision,
                currency="USDT",
                starting_minor_units=100_000_000_000,
                exposure_limit_minor_units=10_000_000_000,
                fee_bps=0,
                slippage_bps=0,
            )
            binding_id = binding.binding_id
            company_id = binding.company_id
            order = submit_company_paper_book_order(
                writer,
                binding_id=binding_id,
                actor_id="founder",
                action_id="managed-fee-act-1",
                instrument_id=terms.revision,
                side="buy",
                quantity=20,
                limit_minor_units=60_000_000,
                market_price_minor_units=59_000_000,
                fill_mode="resting",
            )
            order_id = order["order_id"]
        price = 59_000_000
        quote_ns = time_ns()
        venue.publish_quote("original", price, quote_ns, quote_ns)
        effective_ns = int(datetime(2026, 11, 22, tzinfo=UTC).timestamp()) * 10**9 - 1
        with ThreadPoolExecutor(max_workers=1) as pool:
            submission = pool.submit(
                venue.submit,
                order_id,
                "paper",
                PaperIntent(
                    "managed-fee-act-1",
                    terms.revision,
                    20,
                    price + 1_000_000,
                ),
            )
            deadline = monotonic() + 5
            command = None
            while monotonic() < deadline and command is None:
                command = venue.claim_command("original")
                if command is None:
                    sleep(0.02)
            self.assertIsNotNone(command)
            venue.record_fill("original", order_id, "legacy", 8, price, 0)
            fill = ("original", order_id, "timed", 12, price, 4)
            venue.record_fill(*fill, effective_ns=effective_ns)
            venue.record_fill(*fill, effective_ns=effective_ns)
            with self.assertRaisesRegex(VenueRefused, "changed"):
                venue.record_fill(*fill, effective_ns=effective_ns + 1)
            submission.result()
        with hold_company_writer(
            self.store, self.company, lease_directory=self.root / "leases"
        ) as writer:
            with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
                db.execute(
                    """INSERT INTO trading_book_fill (
                           fill_id, order_id, binding_id, environment, company_id,
                           actor_id, external_fill_id, instrument_id, side,
                           quantity, price_minor_units, fee_minor_units,
                           realized_pnl_minor_units, filled_at_ns
                       ) VALUES (?, ?, ?, 'paper', ?, 'founder', ?, ?, 'buy', 12, ?, 4, 0, ?)""",
                    (
                        "timed",
                        order_id,
                        binding_id,
                        company_id,
                        "timed",
                        terms.revision,
                        price,
                        effective_ns,
                    ),
                )
        reopened = NautilusPaperVenue(venue.journal)
        chart = replace(self.chart, currency="USDT", units_per_whole=100_000_000)
        with hold_company_writer(self.store, self.company) as writer:
            with self.assertRaisesRegex(JournalRefused, "independently timed"):
                deliver_managed_paper_fee(writer, reopened, "paper", "legacy", chart)
            with self.assertRaisesRegex(JournalRefused, "matching chart units"):
                deliver_managed_paper_fee(
                    writer,
                    reopened,
                    "paper",
                    "timed",
                    replace(chart, units_per_whole=100),
                )
            receipt = deliver_managed_paper_fee(
                writer, reopened, "paper", "timed", chart
            )
            self.assertEqual(
                deliver_managed_paper_fee(writer, reopened, "paper", "timed", chart),
                receipt,
            )
        self.assertEqual(receipt.amount_minor, 4)
        self.assertEqual(
            self.rows(self.store, "SELECT effective_date,currency FROM journal_entry"),
            [("2026-11-21", "USDT")],
        )
        self.assertEqual(
            self.rows(
                reopened.journal, "SELECT fill_id,effective_ns FROM sandbox_fill_time"
            ),
            [("timed", effective_ns)],
        )
