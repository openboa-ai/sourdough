import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from sourdough.company import SCHEMA_REVISION

TABLES = (
    "company",
    "agent",
    "standing_grant",
    "duty",
    "founding_receipt",
    "work_follow_up",
    "duty_run",
    "work_result_read",
    "schema_migrations",
    "audit_event",
    "command_receipt",
    "external_actions",
    "company_restore_receipt",
    "company_store_retired",
)


class Founding(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Path(directory) / "company.sqlite"
        self.terms = {
            "setup-identity": "board-setup",
            "charter-revision": "charter-v1",
            "founder-scope": "manage-company",
            "execution-profile": "profile-v1",
        }

    def command(self, action, **options):
        flags = [
            argument
            for name, value in options.items()
            for argument in (f"--{name}", value)
        ]
        return subprocess.run(
            [sys.executable, "-m", "sourdough.cli", action, str(self.store), *flags],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def refusal(self, result, reason):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(reason, result.stderr)
        self.assertEqual(result.stdout, "")

    def inventory(self):
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            names = db.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            self.assertCountEqual(names, [(table,) for table in TABLES])
            return {
                table: db.execute(f"SELECT * FROM {table}").fetchall()
                for table in TABLES
            }

    def test_reopen_and_lost_reply_replay(self):
        # Discard the first reply. A separate process must reconstruct the original receipt.
        self.assertEqual(self.command("found", **self.terms).returncode, 0)
        original = self.inventory()
        [[setup, company, founder, grant, duty]] = original["founding_receipt"]
        self.assertEqual(setup, "board-setup")
        self.assertEqual(
            original["company"], [(1, company, SCHEMA_REVISION, "charter-v1")]
        )
        self.assertEqual(original["agent"], [(founder, company, "profile-v1")])
        self.assertEqual(
            original["standing_grant"], [(grant, founder, "manage-company")]
        )
        self.assertEqual(original["duty"], [(duty, founder, "due")])
        self.assertEqual(
            self.success(self.command("found", **self.terms)),
            {
                "company_id": company,
                "founder_id": founder,
                "grant_id": grant,
                "due_work_id": duty,
            },
        )
        self.assertEqual(
            self.success(self.command("reopen", **{"company-id": company})),
            {"company_id": company, "due_work_ids": [duty]},
        )
        for field in self.terms:
            with self.subTest(field=field):
                changed = self.terms | {field: "different"}
                self.refusal(self.command("found", **changed), "conflict")
        self.assertEqual(self.inventory(), original)

    def test_reopen_refuses_missing_or_incompatible_store(self):
        self.refusal(
            self.command("reopen", **{"company-id": "expected"}), "could not be opened"
        )
        self.assertEqual(list(self.store.parent.iterdir()), [])
        receipt = self.success(self.command("found", **self.terms))
        original = self.store.read_bytes()
        self.refusal(
            self.command("reopen", **{"company-id": "other"}), "identity mismatch"
        )
        self.assertEqual(self.store.read_bytes(), original)

        with closing(sqlite3.connect(self.store)) as db, db:
            db.execute("UPDATE company SET schema_revision = 999")
        incompatible = self.store.read_bytes()
        self.refusal(
            self.command("reopen", **{"company-id": receipt["company_id"]}),
            "Unsupported",
        )
        self.assertEqual(self.store.read_bytes(), incompatible)
        files = {path.name for path in self.store.parent.iterdir()}
        self.assertIn(self.store.name, files)
        self.assertTrue(
            files
            <= {
                self.store.name,
                f"{self.store.name}-wal",
                f"{self.store.name}-shm",
            }
        )

    def test_concurrent_setup_converges_on_one_receipt(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [
                pool.submit(self.command, "found", **self.terms) for _ in range(2)
            ]
        first, second = [self.success(attempt.result()) for attempt in attempts]
        self.assertEqual(first, second)
        inv = self.inventory()
        self.assertEqual(
            [
                len(inv[t])
                for t in (
                    "company",
                    "agent",
                    "standing_grant",
                    "duty",
                    "founding_receipt",
                    "work_follow_up",
                    "duty_run",
                )
            ],
            [1, 1, 1, 1, 1, 0, 0],
        )
        self.assertEqual(len(inv["audit_event"]), 1)
        self.assertGreaterEqual(len(inv["schema_migrations"]), 2)
