import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.accounting_close import (
    CloseRefused,
    CloseRequest,
    ExpectedCost,
    close_period,
)
from sourdough.company import FoundingTerms, found
from sourdough.general_ledger import JournalEntry, JournalLine, post_journal
from sourdough.writer_lease import hold_company_writer


class AccountingClose(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.leases = directory / "leases"
        founded = found(self.store, FoundingTerms("close", "charter", "scope", "agent"))
        self.cost = ExpectedCost("compute", "charge", "compute-expense", "billing")
        self.request = CloseRequest(
            request_id="period-1",
            company_id=founded.company_id,
            from_date=date(2026, 6, 19),
            through_date=date(2026, 6, 20),
            cash_account="cash",
            currency="USD",
            mapping_revision="chart-1",
            statement_revision="bank-1",
            opening_cash_minor=1000,
            statement_cash_minor=1000,
            expense_accounts=("compute-expense",),
            expected_costs=(self.cost,),
            owner="accounting",
            recheck_at_ns=1_784_000_000_000_000_000,
        )

    def post_cost(self, **overrides):
        entry = JournalEntry(
            self.request.company_id,
            *self.cost.key[:2],
            "invoice-1",
            self.request.through_date,
            "USD",
            "chart-1",
            (JournalLine("compute-expense", 30), JournalLine("cash", -30)),
        )
        with hold_company_writer(
            self.store, self.request.company_id, lease_directory=self.leases
        ) as writer:
            return post_journal(writer, replace(entry, **overrides))

    def close(self, request):
        with hold_company_writer(
            self.store, self.request.company_id, lease_directory=self.leases
        ) as writer:
            return close_period(writer, request)

    def rows(self, query):
        with closing(
            sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)
        ) as database:
            return database.execute(query).fetchall()

    def test_matches_independent_cash_and_expense_without_adding_journal_lines(self):
        entry_id = self.post_cost()
        self.post_cost(source_event="next-period", effective_date=date(2026, 6, 21))
        request = replace(
            self.request,
            statement_cash_minor=970,
            expected_costs=(
                replace(self.cost, evidence_revision="invoice-1", amount_minor=30),
            ),
        )
        receipt = self.close(request)
        self.assertTrue(receipt.complete)
        self.assertEqual(receipt.journal_ids, (entry_id,))
        self.assertEqual(
            (receipt.book_cash_minor, receipt.cash_difference_minor), (970, 0)
        )
        self.assertEqual(receipt.verified_cost_minor, 30)
        self.assertEqual(self.close(request), receipt)
        self.assertEqual(self.rows("SELECT count(*) FROM accounting_close"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM journal_line"), [(4,)])
        with self.assertRaisesRegex(CloseRefused, "conflicts"):
            self.close(replace(request, statement_cash_minor=960))

    def test_missing_cost_requires_a_linked_close_after_late_actual(self):
        pending = self.close(self.request)
        [missing] = pending.issues
        self.assertEqual(
            (missing.reason, missing.owner, missing.reference),
            (
                "missing-cost-receipt",
                "billing",
                self.cost.key,
            ),
        )
        self.assertIsNone(pending.verified_cost_minor)
        self.assertEqual(pending.recheck_at_ns, self.request.recheck_at_ns)
        self.post_cost()
        self.assertEqual(self.close(self.request), pending)
        revised = replace(
            self.request,
            request_id="period-2",
            supersedes=pending.request_id,
            statement_revision="bank-2",
            statement_cash_minor=970,
            expected_costs=(
                replace(self.cost, evidence_revision="invoice-1", amount_minor=30),
            ),
        )
        with self.assertRaisesRegex(CloseRefused, "remove cost sources"):
            self.close(replace(revised, expected_costs=()))
        completed = self.close(revised)
        self.assertTrue(completed.complete)
        self.assertEqual(
            (completed.supersedes, completed.verified_cost_minor),
            (pending.request_id, 30),
        )
        self.assertEqual(self.rows("SELECT count(*) FROM journal_entry"), [(1,)])
        with self.assertRaisesRegex(CloseRefused, "supersede the latest"):
            self.close(replace(revised, request_id="stale"))

    def test_unexplained_cash_and_unlisted_expenses_never_close_cleanly(self):
        self.post_cost()
        receipt = self.close(
            replace(self.request, statement_cash_minor=960, expected_costs=())
        )
        self.assertEqual(receipt.cash_difference_minor, 10)
        self.assertEqual(
            [issue.reason for issue in receipt.issues],
            ["cash-mismatch", "undeclared-cost"],
        )
        self.assertIsNone(receipt.verified_cost_minor)
        self.assertEqual(self.rows("SELECT count(*) FROM journal_entry"), [(1,)])

    def test_explicit_scale_does_not_accept_same_currency_different_units(self):
        self.post_cost(units_per_whole=100)
        request = replace(
            self.request,
            statement_cash_minor=970,
            units_per_whole=100,
            expected_costs=(
                replace(self.cost, evidence_revision="invoice-1", amount_minor=30),
            ),
        )
        original = self.close(request)
        self.assertEqual(original.verified_cost_minor, 30)
        self.post_cost(source_event="different-unit", units_per_whole=1000)
        correction = replace(
            request, request_id="correction", supersedes=original.request_id
        )
        with self.assertRaisesRegex(CloseRefused, "change the chart or scale"):
            self.close(replace(correction, units_per_whole=1000))
        receipt = self.close(correction)
        self.assertEqual([issue.reason for issue in receipt.issues], ["unit-mismatch"])
        self.assertIsNone(receipt.verified_cost_minor)
        self.assertEqual(self.close(request), original)

    def test_explicit_zero_and_mismatched_evidence_are_distinct(self):
        zero = replace(self.cost, evidence_revision="no-charge", amount_minor=0)
        self.assertEqual(
            self.close(
                replace(self.request, expected_costs=(zero,))
            ).verified_cost_minor,
            0,
        )
        self.post_cost(evidence_revision="unrelated")
        wrong_evidence = self.close(
            replace(
                self.request,
                request_id="period-2",
                supersedes=self.request.request_id,
                statement_cash_minor=970,
                expected_costs=(
                    replace(self.cost, evidence_revision="invoice-1", amount_minor=30),
                ),
            )
        )
        self.assertEqual(
            [issue.reason for issue in wrong_evidence.issues], ["unmatched-cost"]
        )
        self.assertIsNone(wrong_evidence.verified_cost_minor)
