import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found, reopen
from sourdough.first_duty import (
    DutyRefused,
    claim_first_duty,
    require_paper_authority,
)


class FirstDutyTests(unittest.TestCase):
    def setUp(self):
        self.store = Path(self.enterContext(TemporaryDirectory())) / "company.sqlite"
        self.receipt = found(
            self.store, FoundingTerms("setup", "charter", "paper", "runtime")
        )

    def claim(self):
        return claim_first_duty(self.store, self.receipt.company_id, "runtime")

    def test_wrong_profile_leaves_duty_due(self):
        with self.assertRaises(DutyRefused):
            claim_first_duty(self.store, self.receipt.company_id, "unbound")
        self.assertEqual(
            reopen(self.store, self.receipt.company_id).due_work_ids,
            (self.receipt.due_work_id,),
        )

    def test_only_one_claim_under_contention(self):
        with ThreadPoolExecutor(2) as executor:
            pending = [executor.submit(self.claim) for index in range(2)]
            claims = []
            for future in pending:
                try:
                    claims.append(future.result())
                except DutyRefused:
                    pass
        self.assertEqual(len(claims), 1)
        with closing(sqlite3.connect(self.store)) as db:
            self.assertEqual(
                db.execute("SELECT duty_id, run_id FROM first_duty_claim").fetchall(),
                [(self.receipt.due_work_id, claims[0].run_id)],
            )

    def test_read_only_grant_cannot_consume_paper_duty(self):
        with closing(sqlite3.connect(self.store)) as db, db:
            db.execute("UPDATE standing_grant SET scope = 'read'")
        with self.assertRaises(DutyRefused):
            self.claim()
        self.assertEqual(
            reopen(self.store, self.receipt.company_id).due_work_ids,
            (self.receipt.due_work_id,),
        )

    def test_authority_is_checked_again_at_use(self):
        claim = self.claim()
        with closing(sqlite3.connect(self.store)) as db, db:
            require_paper_authority(db, claim)
            for wrong in (
                replace(claim, run_id="stale"),
                replace(claim, company_id="foreign"),
            ):
                with self.assertRaises(DutyRefused):
                    require_paper_authority(db, wrong)
            db.execute(
                "UPDATE standing_grant SET scope = 'read' WHERE id = ?",
                (self.receipt.grant_id,),
            )
            with self.assertRaises(DutyRefused):
                require_paper_authority(db, claim)
