import json
import os
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough.board_control import apply_company_restriction, clear_company_restriction
from sourdough.company import FoundingRefused, FoundingTerms, found
from sourdough.trading import TradingDomainPlugin
from sourdough.trading.risk import TRADING_RISK_CAUSE_CATEGORY, TradingRestrictionGuard
from sourdough.work_projection import read_local_board_timeline
from sourdough.writer_lease import hold_company_writer


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
        self.assertEqual(first["domains"], {})
        self.assertNotIn("paper", first)
        self.assertNotIn("risk_control", first)
        self.assertEqual(second["revision"], first["revision"])
        self.assertGreaterEqual(second["as_of_ns"], first["as_of_ns"])
        self.assertEqual(self.store.read_bytes(), before)

    def test_risk_projection_reports_only_the_effective_receipt_without_writes(self):
        from sourdough.storage.db import open_writer, write_tx
        from sourdough.storage.schema import enable_domain_v1

        company_id = self.receipt.company_id
        with closing(open_writer(self.store)) as conn, write_tx(conn):
            enable_domain_v1(conn, "trading", company_id=company_id, actor_id="test")
        plugins = (TradingDomainPlugin(),)
        original = read_local_board_timeline(
            self.store, company_id, domain_plugins=plugins
        )

        def authorize(*args):
            return "verification-principal"

        with hold_company_writer(
            self.store, company_id, lease_directory=self.store.parent / "leases"
        ) as writer:
            applied = apply_company_restriction(
                writer,
                command_id="hold",
                restriction_id="rest-hold",
                charter_revision="charter",
                expected_restriction_revision=0,
                cause_category=TRADING_RISK_CAUSE_CATEGORY,
                scope_ref="execution:all",
                reason="Private review",
                authorize=authorize,
                restriction_guards=(TradingRestrictionGuard(),),
            )
            conflict = apply_company_restriction(
                writer,
                command_id="stale",
                restriction_id="rest-hold",
                charter_revision="charter",
                expected_restriction_revision=0,
                cause_category=TRADING_RISK_CAUSE_CATEGORY,
                scope_ref="execution:all",
                reason="Not applied",
                authorize=authorize,
                restriction_guards=(TradingRestrictionGuard(),),
            )
        self.assertEqual(conflict.disposition, "conflicting_revision")
        before = self.store.read_bytes()
        observed = read_local_board_timeline(
            self.store, company_id, domain_plugins=plugins
        )
        self.assertEqual(
            observed["domains"]["trading"]["risk_control"],
            {
                "state": "suspended",
                "effective_revision": applied.effective_revision,
                "last_applied": {
                    "request_id": "hold",
                    "operation": applied.command_type,
                    "recorded_at_ns": applied.recorded_at_ns,
                },
            },
        )
        self.assertNotEqual(observed["revision"], original["revision"])
        self.assertNotIn("verification-principal", json.dumps(observed))
        self.assertNotIn("Private review", json.dumps(observed))
        self.assertEqual(self.store.read_bytes(), before)
        # CLI read-command-receipt reports applied status without disclosing principal or reason
        receipt_cli = subprocess.run(
            [
                sys.executable,
                "-IB",
                "-m",
                "sourdough.cli",
                "read-command-receipt",
                str(self.store),
                "--company-id",
                company_id,
                "--command-id",
                "hold",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        receipt_body = json.loads(receipt_cli.stdout)
        self.assertEqual(receipt_body["status"], "found")
        self.assertEqual(receipt_body["state"], "applied")
        self.assertNotIn("verification-principal", receipt_cli.stdout)
        self.assertNotIn("Private review", receipt_cli.stdout)
        missing_cli = subprocess.run(
            [
                sys.executable,
                "-IB",
                "-m",
                "sourdough.cli",
                "read-command-receipt",
                str(self.store),
                "--company-id",
                company_id,
                "--command-id",
                "unknown-cmd",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        self.assertEqual(json.loads(missing_cli.stdout)["status"], "not_found")
        with hold_company_writer(
            self.store, company_id, lease_directory=self.store.parent / "leases"
        ) as writer:
            clear_company_restriction(
                writer,
                command_id="clear-hold",
                restriction_id="rest-hold",
                charter_revision="charter",
                expected_restriction_revision=1,
                reason="Resolved",
                authorize=authorize,
                restriction_guards=(TradingRestrictionGuard(),),
            )
        resumed = read_local_board_timeline(
            self.store, company_id, domain_plugins=plugins
        )["domains"]["trading"]["risk_control"]
        self.assertEqual(resumed["state"], "not_suspended")
        self.assertEqual(resumed["last_applied"]["request_id"], "clear-hold")

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
        self.assertFalse((self.store.parent / "absent").exists())
        allowed_files = {
            self.store.name,
            f"{self.store.name}-wal",
            f"{self.store.name}-shm",
        }
        self.assertLessEqual(
            {path.name for path in self.store.parent.iterdir()},
            allowed_files,
        )
        wal = self.store.parent / f"{self.store.name}-wal"
        if wal.exists():
            self.assertEqual(wal.stat().st_size, 0)
