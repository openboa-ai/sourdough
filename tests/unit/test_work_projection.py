import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough.company import FoundingRefused, FoundingTerms, found
from sourdough.work_projection import read_local_board_timeline


class BoardTimeline(unittest.TestCase):
    def setUp(self):
        self.store = Path(self.enterContext(TemporaryDirectory())) / "company.sqlite"
        self.receipt = found(
            self.store, FoundingTerms("setup", "charter", "manage", "profile")
        )

    def test_core_only_timeline_distinguishes_due_work_from_missing_decision(self):
        before = self.store.read_bytes()
        first = read_local_board_timeline(self.store, self.receipt.company_id)
        second = read_local_board_timeline(self.store, self.receipt.company_id)
        self.assertEqual(
            first["duties"],
            [
                {
                    "duty_id": self.receipt.due_work_id,
                    "owner_id": self.receipt.founder_id,
                    "state": "due",
                    "due_at": None,
                    "run_id": None,
                    "decision": None,
                }
            ],
        )
        self.assertIsNone(first["paper"])
        self.assertEqual(second["revision"], first["revision"])
        self.assertGreaterEqual(second["as_of_ns"], first["as_of_ns"])
        self.assertEqual(self.store.read_bytes(), before)

    def test_unrelated_company_and_nonowner_are_refused_without_mutation(self):
        before = self.store.read_bytes()
        with self.assertRaisesRegex(FoundingRefused, "identity mismatch"):
            read_local_board_timeline(self.store, "another-company")
        with patch(
            "sourdough.work_projection.os.geteuid", return_value=os.geteuid() + 1
        ):
            with self.assertRaisesRegex(FoundingRefused, "store owner"):
                read_local_board_timeline(self.store, self.receipt.company_id)
        self.assertEqual(self.store.read_bytes(), before)
