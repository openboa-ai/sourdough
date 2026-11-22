import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough.accounting_close import (
    CloseRefused,
    CloseRequest,
    ExpectedCost,
    close_period,
    read_period_close,
)
from sourdough.company import FoundingTerms, found
from sourdough.general_ledger import JournalEntry, JournalLine, post_journal
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_fee_bridge import PAPER_FILL_FEE_SOURCE
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.paper_window import WindowPlan, register_window
from sourdough.window_costs import CostInventory, read_window_costs
from sourdough.writer_lease import hold_company_writer


DAY_NS = 86_400_000_000_000
EPOCH = date(1970, 1, 1)


class WindowCostsTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.store, self.leases = root / "company.sqlite", root / "leases"
        self.company = found(
            self.store, FoundingTerms("company", "charter", "paper", "profile")
        ).company_id
        venue = FixedQuoteVenue(root / "venue.sqlite")
        account = venue.provision(
            PaperAccount("paper", "fixture", "instrument", "USD", 1000, "paper"), 10
        )
        bind(
            self.store,
            PaperMandate("binding", self.company, "charter", account, 500),
            venue,
        )
        self.day = date(2027, 1, 15)
        self.compute = ExpectedCost("compute", "jan-15", "expense-compute", "billing")
        self.request = CloseRequest(
            request_id="original",
            company_id=self.company,
            from_date=self.day,
            through_date=self.day,
            cash_account="operating-cash",
            currency="USD",
            mapping_revision="operating-chart",
            statement_revision="independent-statement-0",
            opening_cash_minor=1000,
            statement_cash_minor=1000,
            expense_accounts=("expense-compute",),
            expected_costs=(self.compute,),
            owner="accounting",
            recheck_at_ns=(self.day - EPOCH).days * DAY_NS + 2 * DAY_NS,
        )

    def register(self, window_id, request, *, shift_ns=0):
        opens = (request.from_date - EPOCH).days * DAY_NS
        closes = (request.through_date - EPOCH).days * DAY_NS + DAY_NS - 1
        plan = WindowPlan(
            window_id=window_id,
            binding_revision="binding",
            operating_revision="incumbent",
            evaluation_mandate_revision="mandate",
            source_revision="market",
            valuation_revision="valuation",
            cost_inventory_revision=CostInventory.for_close(
                window_id, request
            ).revision,
            execution_assumptions_revision="execution",
            cash_reference_revision="benchmark",
            contingency_revision="contingency",
            opens_ns=opens + shift_ns,
            closes_ns=closes,
            cadence_ns=10**9,
            minimum_observations=2,
            max_exposure_minor=500,
            max_spend_minor=50,
            max_drawdown_bps=250,
            hurdle_bps=10,
        )
        with (
            patch("sourdough.paper_window.time_ns", return_value=opens - 1),
            self.writer() as writer,
        ):
            return register_window(writer, plan)

    def writer(self):
        return hold_company_writer(
            self.store, self.company, lease_directory=self.leases
        )

    def close(self, request, assessed_ns):
        with (
            self.writer() as writer,
            patch("sourdough.accounting_close.time_ns", return_value=assessed_ns),
        ):
            return close_period(writer, request)

    def read(self, close_id, window="first"):
        return read_window_costs(self.store, self.company, window, close_id)

    def test_missing_actual_becomes_verified_only_in_the_linked_close(self):
        window = self.register("first", self.request)
        cutoff = window.plan.closes_ns
        self.assertEqual(self.read("unknown").missing, ("accounting-close",))
        original = self.close(self.request, cutoff + 1)
        pending = self.read(original.request_id)
        self.assertIsNone(pending.amount_minor)
        self.assertEqual(pending.missing, ("missing-cost-receipt",))
        self.assertEqual(pending.accounting.receipt.issues[0].owner, "billing")
        self.assertEqual(
            pending.accounting.receipt.recheck_at_ns, self.request.recheck_at_ns
        )
        with self.writer() as writer:
            post_journal(
                writer,
                JournalEntry(
                    self.company,
                    self.compute.source,
                    self.compute.event_id,
                    "issued-invoice",
                    self.day,
                    "USD",
                    "operating-chart",
                    (
                        JournalLine("expense-compute", 30),
                        JournalLine("operating-cash", -30),
                    ),
                ),
            )
        repaired = replace(
            self.request,
            request_id="corrected",
            supersedes=original.request_id,
            statement_revision="independent-statement-1",
            statement_cash_minor=970,
            expected_costs=(
                replace(
                    self.compute, evidence_revision="issued-invoice", amount_minor=30
                ),
            ),
        )
        self.close(repaired, cutoff + 2)
        corrected = self.read(repaired.request_id)
        self.assertEqual(
            (corrected.window_revision, corrected.amount_minor), (window.revision, 30)
        )
        self.assertEqual(corrected.accounting.request, repaired)
        self.assertEqual(self.read(original.request_id), pending)

    def test_refuses_incompatible_scope_and_assessment_before_close(self):
        window = self.register("first", self.request)
        cutoff = window.plan.closes_ns
        self.close(self.request, cutoff)
        self.assertEqual(self.read("original").missing, ("accounting-as-of",))

        wrong_currency = replace(self.request, request_id="fx", currency="EUR")
        self.close(wrong_currency, cutoff + 1)
        self.assertEqual(self.read("fx").missing, ("accounting-currency",))

        short_request = replace(
            self.request,
            request_id="partial-day",
            from_date=self.day + timedelta(days=1),
            through_date=self.day + timedelta(days=1),
        )
        short = self.register("short", short_request, shift_ns=10**9)
        self.close(short_request, short.plan.closes_ns + 1)
        self.assertEqual(
            self.read("partial-day", "short").missing, ("accounting-cutoff",)
        )

        changed_sources = replace(
            self.request,
            request_id="changed-sources",
            supersedes="original",
            expected_costs=(replace(self.compute, owner="unregistered-owner"),),
        )
        self.close(changed_sources, cutoff + 1)
        self.assertEqual(
            self.read("changed-sources").missing, ("operating-cost-inventory",)
        )

    def test_fees_already_in_venue_cash_are_never_an_operating_cost(self):
        fee_only = replace(
            self.request,
            expected_costs=(
                replace(
                    self.compute,
                    source=PAPER_FILL_FEE_SOURCE,
                    evidence_revision="explicit-zero",
                    amount_minor=0,
                ),
            ),
        )
        window = self.register("first", fee_only)
        self.assertTrue(self.close(fee_only, window.plan.closes_ns + 1).complete)
        coverage = self.read(fee_only.request_id)
        self.assertEqual(coverage.missing, ("operating-cost-inventory",))
        self.assertIsNone(coverage.amount_minor)

    def test_legacy_close_remains_readable_without_fabricating_an_assessment_time(self):
        window = self.register("first", self.request)
        original = self.close(self.request, window.plan.closes_ns + 1)
        with closing(sqlite3.connect(self.store)) as database, database:
            database.execute("ALTER TABLE accounting_close DROP COLUMN assessed_ns")
        close = read_period_close(self.store, self.company, self.request.request_id)
        self.assertEqual(close.receipt, original)
        self.assertIsNone(close.assessed_ns)
        self.assertEqual(self.read("original").missing, ("accounting-as-of",))
        with self.assertRaisesRegex(CloseRefused, "another company"):
            read_period_close(self.store, "unrelated", self.request.request_id)
        corrected = replace(self.request, request_id="new", supersedes="original")
        self.close(corrected, window.plan.closes_ns + 2)
        self.assertEqual(self.read("new").missing, ("missing-cost-receipt",))
        self.assertIsNone(
            read_period_close(self.store, self.company, "original").assessed_ns
        )
