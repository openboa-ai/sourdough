import json
import os
import subprocess
import sys
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

    def test_client_query_checks_contract_separately_and_leaves_store_unchanged(self):
        before = self.store.read_bytes()
        command = (
            sys.executable,
            "-IB",
            "-m",
            "sourdough.cli",
            "inspect-work",
        )
        scope = ("--company-id", self.receipt.company_id, "--contract-revision")
        accepted = subprocess.run(
            [*command, str(self.store), *scope, "work-timeline-v1"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        observed = json.loads(accepted.stdout)
        self.assertEqual(observed["contract_revision"], "work-timeline-v1")
        self.assertEqual(observed["company_id"], self.receipt.company_id)
        self.assertEqual(observed["duties"][0]["duty_id"], self.receipt.due_work_id)
        self.assertNotEqual(observed["revision"], observed["contract_revision"])
        self.assertGreater(observed["as_of_ns"], 0)
        unknown = subprocess.run(
            [*command, str(self.store.parent / "absent"), *scope, "future"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("Unsupported work timeline contract", unknown.stderr)
        self.assertEqual(unknown.stdout, "")
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(list(self.store.parent.iterdir()), [self.store])
