import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import claim_first_duty
from sourdough.general_ledger import JournalRefused
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import post_confirmed_fill
from sourdough.paper_fee_bridge import FeeChart, VerifiedFee, deliver_paper_fee
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

    def test_one_fee_posting_survives_lost_delivery_acknowledgement(self):
        order_id, verified = self.admitted_fill(fee_bps=200)
        self.assertEqual(verified.fee_minor_units, 4)
        with self.assertRaisesRegex(JournalRefused, "original paper source"):
            self.deliver(verified)
        post_confirmed_fill(self.store, self.company, order_id, self.venue)
        with (
            patch(
                "sourdough.paper_fee_bridge._acknowledge",
                side_effect=OSError("cut before acknowledgement"),
            ),
            self.assertRaisesRegex(OSError, "before acknowledgement"),
        ):
            self.deliver(verified)
        [(journal_id,)] = self.rows(self.store, "SELECT id FROM journal_entry")
        self.assertEqual(
            self.rows(
                self.store, "SELECT acknowledged,journal_id FROM paper_fee_delivery"
            ),
            [(0, None)],
        )
        with self.assertRaisesRegex(JournalRefused, "original terms"):
            self.deliver(
                replace(verified, units_per_whole=1000),
                replace(self.chart, units_per_whole=1000),
            )
        receipt = self.deliver(verified)
        self.assertEqual((receipt.journal_id, receipt.amount_minor), (journal_id, 4))
        self.assertEqual(self.deliver(verified), receipt)
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
            self.rows(
                self.store,
                "SELECT name FROM sqlite_schema WHERE name='paper_fee_delivery'",
            ),
            [],
        )
        receipt = self.deliver(verified)
        self.assertEqual((receipt.amount_minor, receipt.journal_id), (0, None))
        self.assertEqual(self.deliver(verified), receipt)
        self.assertEqual(
            self.rows(
                self.store, "SELECT name FROM sqlite_schema WHERE name='journal_entry'"
            ),
            [],
        )
        self.assertEqual(
            self.rows(
                self.store, "SELECT acknowledged,journal_id FROM paper_fee_delivery"
            ),
            [(1, None)],
        )
