import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import (
    FoundingRefused,
    FoundingTerms,
    found,
    upgrade_work_schema,
)
from sourdough.storage.db import MAX_SCHEMA_VERSION, StorageRefused, open_reader
from sourdough.storage.migrations import (
    load_migrations,
    migrate_store,
    verify_applied_migrations,
)


class StorageMigrations(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.terms = FoundingTerms("setup", "charter-v1", "paper", "profile-v1")

    def test_fresh_store_records_verified_migration_checksums_and_user_version(self):
        found(self.store, self.terms)
        specs = load_migrations()
        with closing(open_reader(self.store)) as reader:
            applied = verify_applied_migrations(reader)
            self.assertEqual(applied, [spec.version for spec in specs])
            self.assertEqual(
                reader.execute("PRAGMA user_version").fetchone()[0], MAX_SCHEMA_VERSION
            )
            # Company-only founding must not eagerly create quant paper_* tables
            paper_tables = reader.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name LIKE 'paper_%'"
            ).fetchall()
            self.assertEqual(paper_tables, [])

    def test_tampered_migration_checksum_is_rejected(self):
        receipt = found(self.store, self.terms)
        with closing(sqlite3.connect(self.store)) as raw, raw:
            raw.execute(
                "UPDATE schema_migrations SET sha256 = ? WHERE version = ?",
                ("0" * 64, MAX_SCHEMA_VERSION),
            )
        with closing(open_reader(self.store)) as reader:
            with self.assertRaisesRegex(StorageRefused, "checksum mismatch"):
                verify_applied_migrations(reader)
        with self.assertRaisesRegex(FoundingRefused, "checksum mismatch"):
            upgrade_work_schema(self.store, receipt.company_id)

    def test_upgrade_from_v6_and_v7_legacy_stores_preserves_rows_and_sets_user_version(
        self,
    ):
        for legacy_version in (6, 7):
            with self.subTest(legacy_version=legacy_version):
                legacy_store = self.store.with_name(f"legacy-v{legacy_version}.sqlite")
                receipt = found(legacy_store, self.terms)
                # Strip unified kernel metadata to emulate a pre-v8 store
                with closing(sqlite3.connect(legacy_store)) as raw, raw:
                    raw.execute("PRAGMA journal_mode = DELETE")
                    for table in (
                        "schema_migrations",
                        "audit_event",
                        "command_receipt",
                        "external_actions",
                        "company_restore_receipt",
                        "company_store_retired",
                    ):
                        raw.execute(f"DROP TABLE IF EXISTS {table}")
                    raw.execute(
                        "UPDATE company SET schema_revision = ? WHERE singleton = 1",
                        (legacy_version,),
                    )
                    raw.execute("PRAGMA user_version = 0")

                upgrade_work_schema(legacy_store, receipt.company_id)
                # Second call is idempotent
                upgrade_work_schema(legacy_store, receipt.company_id)

                with closing(open_reader(legacy_store)) as reader:
                    self.assertEqual(
                        reader.execute("PRAGMA user_version").fetchone()[0],
                        MAX_SCHEMA_VERSION,
                    )
                    self.assertEqual(
                        reader.execute(
                            "SELECT schema_revision FROM company"
                        ).fetchone(),
                        (MAX_SCHEMA_VERSION,),
                    )
                    self.assertEqual(
                        reader.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                        "wal",
                    )
                    self.assertEqual(
                        reader.execute("PRAGMA foreign_key_check").fetchall(), []
                    )
                    self.assertEqual(
                        verify_applied_migrations(reader),
                        [spec.version for spec in load_migrations()],
                    )
                    self.assertEqual(
                        reader.execute(
                            "SELECT company_id, founder_id FROM founding_receipt"
                        ).fetchall(),
                        [(receipt.company_id, receipt.founder_id)],
                    )

    def test_upgrade_refuses_future_user_version(self):
        receipt = found(self.store, self.terms)
        with closing(sqlite3.connect(self.store)) as raw, raw:
            raw.execute(f"PRAGMA user_version = {MAX_SCHEMA_VERSION + 2}")
        with self.assertRaisesRegex(FoundingRefused, "Unsupported future"):
            upgrade_work_schema(self.store, receipt.company_id)

    def test_migrate_store_refuses_legacy_v5_without_legacy_upgrade_hook(self):
        receipt = found(self.store, self.terms)
        with closing(sqlite3.connect(self.store)) as raw, raw:
            raw.execute("UPDATE company SET schema_revision = 5 WHERE singleton = 1")
            raw.execute("PRAGMA user_version = 0")
        with self.assertRaisesRegex(StorageRefused, "legacy upgrade hook"):
            migrate_store(self.store, receipt.company_id)
