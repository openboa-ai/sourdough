import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.general_ledger import (
    JournalEntry,
    JournalLine,
    JournalRefused,
    post_journal,
)
from sourdough.writer_lease import WriterRefused, hold_company_writer


class GeneralLedger(unittest.TestCase):
    def setUp(self):
        scratch = Path(self.enterContext(TemporaryDirectory()))
        self.store = scratch / "company.sqlite"
        self.company = found(
            self.store, FoundingTerms("accounting", "charter", "scope", "profile")
        )
        self.leases = scratch / "leases"
        self.entry = JournalEntry(
            self.company.company_id,
            "compute-statement",
            "charge-1",
            "statement-revision",
            date(2026, 6, 19),
            "USD",
            "chart-1",
            (JournalLine("compute-expense", 300), JournalLine("cash", -300)),
        )

    def post(self, entry):
        with hold_company_writer(
            self.store, self.company.company_id, lease_directory=self.leases
        ) as writer:
            return post_journal(writer, entry)

    def balances(self):
        with closing(sqlite3.connect(self.store)) as database:
            return database.execute(
                "SELECT account, sum(amount_minor) FROM journal_line GROUP BY account ORDER BY account"
            ).fetchall()

    def test_reopened_redelivery_posts_one_balanced_receipt(self):
        receipt = self.post(self.entry)
        self.assertEqual(self.post(self.entry), receipt)
        self.assertEqual(self.balances(), [("cash", -300), ("compute-expense", 300)])
        with self.assertRaisesRegex(JournalRefused, "conflicts"):
            self.post(replace(self.entry, evidence_revision="different"))
        with self.assertRaisesRegex(WriterRefused, "another store or company"):
            self.post(replace(self.entry, company_id="different"))
        with self.assertRaisesRegex(JournalRefused, "must balance"):
            replace(
                self.entry,
                lines=(JournalLine("compute-expense", 300), JournalLine("cash", -299)),
            )
        self.assertEqual(self.balances(), [("cash", -300), ("compute-expense", 300)])

    def test_correction_keeps_original_lines_and_requires_exact_reversal(self):
        original = self.post(self.entry)
        reversal = replace(self.entry, source_event="reversal", reverses=original)
        corrected = replace(
            self.entry,
            source_event="replacement",
            evidence_revision="corrected-statement",
            lines=(JournalLine("compute-expense", 250), JournalLine("cash", -250)),
            replaces=original,
        )
        with self.assertRaisesRegex(JournalRefused, "requires one effective"):
            self.post(corrected)
        with self.assertRaisesRegex(JournalRefused, "exactly negate"):
            self.post(reversal)
        reversal = replace(
            reversal,
            lines=(JournalLine("compute-expense", -300), JournalLine("cash", 300)),
        )
        reversed_id = self.post(reversal)
        self.assertEqual(self.post(reversal), reversed_id)
        corrected_id = self.post(corrected)
        with self.assertRaisesRegex(JournalRefused, "requires one effective"):
            self.post(replace(corrected, source_event="another-replacement"))
        self.assertEqual(self.balances(), [("cash", -250), ("compute-expense", 250)])
        with closing(sqlite3.connect(self.store)) as database:
            self.assertEqual(
                dict(database.execute("SELECT id, reverses FROM journal_entry")),
                {original: None, reversed_id: original, corrected_id: None},
            )
            self.assertEqual(
                database.execute(
                    "SELECT account, amount_minor FROM journal_line WHERE entry_id=? ORDER BY ordinal",
                    (original,),
                ).fetchall(),
                [("compute-expense", 300), ("cash", -300)],
            )

    def test_write_failure_rolls_back_header_and_partial_lines(self):
        self.post(self.entry)
        with closing(sqlite3.connect(self.store)) as database, database:
            database.execute(
                """CREATE TRIGGER reject_cash BEFORE INSERT ON journal_line
                   WHEN NEW.account='cash' BEGIN SELECT RAISE(ABORT,'cut'); END"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cut"):
            self.post(replace(self.entry, source_event="charge-2"))
        with closing(sqlite3.connect(self.store)) as database:
            self.assertEqual(
                database.execute("SELECT count(*) FROM journal_entry").fetchone(), (1,)
            )
        self.assertEqual(self.balances(), [("cash", -300), ("compute-expense", 300)])
