import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.evidence_review import (
    EvidenceClass,
    ReviewCase,
    ReviewRule,
    ReviewRuleRefused,
)
from sourdough.review_qualification_store import (
    PublishedReviewRule,
    read_review_qualification,
    record_review_qualification,
)
from sourdough.writer_lease import hold_company_writer


SOURCE = EvidenceClass.SOURCE_REFERENCE
VERIFICATION = EvidenceClass.INDEPENDENT_VERIFICATION


class QualificationStore(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.company = found(
            self.store, FoundingTerms("setup", "charter", "company", "runtime")
        )
        self.assets = {
            "original": PublishedReviewRule(
                self.company.company_id, "original", ReviewRule((SOURCE,))
            ),
            "new": PublishedReviewRule(
                self.company.company_id, "new", ReviewRule((SOURCE, VERIFICATION))
            ),
            "other": PublishedReviewRule(
                "unrelated-company", "other", ReviewRule((SOURCE,))
            ),
        }
        self.cases = (
            ReviewCase((SOURCE,), (VERIFICATION,)),
            ReviewCase((SOURCE, VERIFICATION), ()),
        )

    def resolve(self, database, company_id, asset_id):
        self.assertTrue(database.in_transaction)
        self.assertEqual(company_id, self.company.company_id)
        return self.assets.get(asset_id)

    def record(self, candidate, baseline=None, cases=None):
        with hold_company_writer(
            self.store, self.company.company_id, lease_directory=self.root / "leases"
        ) as writer:
            return record_review_qualification(
                writer,
                candidate,
                baseline,
                resolve=self.resolve,
                cases=self.cases if cases is None else cases,
            )

    def test_reopened_receipts_retain_both_verdicts_and_all_pins(self):
        rejected = self.record("original")
        accepted = self.record("new", "original")
        self.assertFalse(rejected.qualification.passed)
        self.assertTrue(accepted.qualification.passed)
        self.assertEqual(accepted, self.record("new", "original"))
        changed = self.record("new", "original", (ReviewCase((SOURCE,), ()),))
        self.assertFalse(changed.qualification.passed)
        self.assertNotEqual(
            accepted.qualification.criteria_digest,
            changed.qualification.criteria_digest,
        )
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            self.assertEqual(
                read_review_qualification(db, rejected.receipt_id), rejected
            )
            self.assertEqual(
                read_review_qualification(db, accepted.receipt_id), accepted
            )
            self.assertEqual(read_review_qualification(db, changed.receipt_id), changed)
            self.assertEqual(
                db.execute("SELECT count(*) FROM review_qualification").fetchone(), (3,)
            )
        self.assertEqual(accepted.qualification.outcomes[0].present, (SOURCE,))
        self.assertEqual(
            accepted.qualification.baseline_digest, self.assets["original"].rule.digest
        )

    def test_asset_owner_scope_and_receipt_integrity_are_checked(self):
        for candidate, baseline in (
            ("other", None),
            ("new", "other"),
            ("unknown", None),
        ):
            with self.subTest(candidate=candidate, baseline=baseline):
                with self.assertRaisesRegex(ReviewRuleRefused, "unavailable"):
                    self.record(candidate, baseline)
        receipt = self.record("new")
        with closing(sqlite3.connect(self.store)) as db, db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM review_qualification").fetchone(), (1,)
            )
            db.execute("UPDATE review_qualification SET evidence='{}'")
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            with self.assertRaisesRegex(ReviewRuleRefused, "integrity"):
                read_review_qualification(db, receipt.receipt_id)
        with self.assertRaisesRegex(ReviewRuleRefused, "integrity"):
            self.record("new")
