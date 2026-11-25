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
from sourdough.first_duty import claim_first_duty
from sourdough.general_ledger import JournalRefused
from sourdough.market_worker import SpotInstrumentTerms
from sourdough.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import post_confirmed_fill
from sourdough.paper_execution import VenueRefused
from sourdough.paper_fee_bridge import (
    FeeChart,
    VerifiedFee,
    deliver_managed_paper_fee,
    deliver_paper_fee,
)
from sourdough.paper_order import PaperIntent, submit_paper_order
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.writer_lease import hold_company_writer


class PaperFeeBridge(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.venue = FixedQuoteVenue(self.root / "venue.sqlite")
        self.company = found(
            self.store, FoundingTerms("fees", "charter", "paper", "employee")
        ).company_id
        self.chart = FeeChart(
            "chart", "paper-quote-fee-v1", "USD", 100, "fees", "paper-cash"
        )

    def admitted_fill(self, fee_bps):
        account = self.venue.provision(
            PaperAccount("paper", "fixture", "instrument", "USD", 5000, "paper"),
            100,
            fee_bps=fee_bps,
        )
        bind(
            self.store,
            PaperMandate("scope", self.company, "charter", account, 1000),
            self.venue,
        )
        claim = claim_first_duty(self.store, self.company, "employee")
        order = submit_paper_order(
            self.store, claim, PaperIntent("original", "instrument", 2, 110), self.venue
        )
        observed = self.venue.observe_execution(order.order_id)
        return order.order_id, VerifiedFee(
            account.destination_id,
            account.account_id,
            observed.fill_id,
            "venue-fee-cut",
            date(2026, 11, 22),
            "USD",
            100,
            observed.fee_minor_units,
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
        order_id, verified = self.admitted_fill(fee_bps=200)
        self.assertEqual(verified.fee_minor_units, 4)
        with self.assertRaisesRegex(JournalRefused, "original paper source"):
            self.deliver(verified)
        post_confirmed_fill(self.store, self.company, order_id, self.venue)
        with (
            patch(
                "sourdough.paper_fee_bridge.append_audit_event",
                side_effect=OSError("cut before commit"),
            ),
            self.assertRaisesRegex(OSError, "before commit"),
        ):
            self.deliver(verified)
        self.assertEqual(self.rows(self.store, "SELECT id FROM journal_entry"), [])
        self.assertEqual(
            self.rows(
                self.store, "SELECT acknowledged,journal_id FROM paper_fee_delivery"
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
        self.assertEqual(
            self.rows(
                self.venue.journal, "SELECT order_id,fill_id FROM venue_execution"
            ),
            [(order_id, verified.fill_id)],
        )

    def test_explicit_zero_and_unverified_scope_are_distinct(self):
        order_id, verified = self.admitted_fill(fee_bps=0)
        post_confirmed_fill(self.store, self.company, order_id, self.venue)
        with self.assertRaisesRegex(JournalRefused, "original paper source"):
            self.deliver(replace(verified, fee_minor_units=1))
        with self.assertRaisesRegex(JournalRefused, "matching chart units"):
            self.deliver(verified, replace(self.chart, units_per_whole=1000))
        self.assertEqual(
            self.rows(self.store, "SELECT COUNT(*) FROM paper_fee_delivery"),
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
                self.store, "SELECT acknowledged,journal_id FROM paper_fee_delivery"
            ),
            [(1, None)],
        )

    def test_managed_fee_uses_original_fill_day_and_atoms(self):
        terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        venue = NautilusPaperVenue(self.root / "sandbox.sqlite")
        venue.provision(SandboxPlan(self.company, "paper", terms, 100_000_000_000, 0))
        venue.prove_account("original", "BINANCE-001", "USDT", venue.start_generation())
        account = venue.observe_account("paper")
        bind(
            self.store,
            PaperMandate("scope", self.company, "charter", account, 10_000_000_000),
            venue,
        )
        claim = claim_first_duty(self.store, self.company, "employee")
        price = 59_000_000
        quote_ns = time_ns()
        venue.publish_quote("original", price, quote_ns, quote_ns)
        effective_ns = int(datetime(2026, 11, 22, tzinfo=UTC).timestamp()) * 10**9 - 1
        with ThreadPoolExecutor(max_workers=1) as pool:
            submission = pool.submit(
                submit_paper_order,
                self.store,
                claim,
                PaperIntent("managed", terms.revision, 20, price),
                venue,
            )
            command = None
            deadline = monotonic() + 3
            while command is None and monotonic() < deadline:
                command = venue.claim_command("original")
                sleep(0.01)
            self.assertIsNotNone(command)
            order_id = command.order_id
            venue.record_fill("original", order_id, "legacy", 8, price, 0)
            fill = ("original", order_id, "timed", 12, price, 4)
            venue.record_fill(*fill, effective_ns=effective_ns)
            venue.record_fill(*fill, effective_ns=effective_ns)
            with self.assertRaisesRegex(VenueRefused, "changed"):
                venue.record_fill(*fill, effective_ns=effective_ns + 1)
            self.assertEqual(submission.result(timeout=2).order_id, order_id)
        post_confirmed_fill(self.store, self.company, order_id, venue)
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
