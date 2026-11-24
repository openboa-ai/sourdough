import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingRefused, FoundingTerms, found, reopen
from sourdough.storage.db import (
    SCHEMA_VERSION,
    StorageRefused,
    open_reader,
    open_writer,
    read_tx,
    write_tx,
)
from sourdough.storage.schema import (
    enable_domain_v1,
    installed_domains,
    is_domain_installed,
    load_domain_pack,
    verify_v1_schema,
)


class CanonicalV1Schema(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.terms = FoundingTerms("setup", "charter-v1", "paper", "profile-v1")

    def test_fresh_store_installs_only_strict_core_v1_with_user_version_one(self):
        found(self.store, self.terms)
        with closing(open_reader(self.store)) as reader, read_tx(reader):
            self.assertEqual(verify_v1_schema(reader), frozenset({"core"}))
            self.assertEqual(
                reader.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )
            self.assertEqual(SCHEMA_VERSION, 1)
            domains = installed_domains(reader)
            self.assertEqual(set(domains), {"core"})
            self.assertEqual(domains["core"][0], 1)
            self.assertEqual(domains["core"][1], load_domain_pack("core").pack_sha256)
            self.assertTrue(is_domain_installed(reader, "core"))
            self.assertFalse(is_domain_installed(reader, "paper"))
            # Company-only founding must not create quant paper_* tables
            paper_tables = reader.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name LIKE 'paper_%'"
            ).fetchall()
            self.assertEqual(paper_tables, [])
            # Every user table must be STRICT
            non_strict = reader.execute(
                """SELECT name FROM pragma_table_list
                   WHERE schema = 'main' AND type = 'table'
                     AND name NOT LIKE 'sqlite_%' AND strict != 1"""
            ).fetchall()
            self.assertEqual(non_strict, [])

    def test_paper_domain_activates_idempotently_and_audits_once(self):
        receipt = found(self.store, self.terms)
        with closing(open_writer(self.store)) as writer:
            with write_tx(writer):
                first = enable_domain_v1(
                    writer, "paper", company_id=receipt.company_id, actor_id="test"
                )
            with write_tx(writer):
                second = enable_domain_v1(
                    writer, "paper", company_id=receipt.company_id, actor_id="test"
                )
        self.assertTrue(first)
        self.assertFalse(second)

        with closing(open_reader(self.store)) as reader, read_tx(reader):
            self.assertEqual(verify_v1_schema(reader), frozenset({"core", "paper"}))
            self.assertTrue(is_domain_installed(reader, "paper"))
            non_strict = reader.execute(
                """SELECT name FROM pragma_table_list
                   WHERE schema = 'main' AND type = 'table'
                     AND name NOT LIKE 'sqlite_%' AND strict != 1"""
            ).fetchall()
            self.assertEqual(non_strict, [])
            events = reader.execute(
                "SELECT entity_id, event_type FROM audit_event WHERE event_type = 'domain.activated'"
            ).fetchall()
            self.assertEqual(events, [("paper", "domain.activated")])

    def test_non_canonical_user_versions_fail_closed_across_apis(self):
        receipt = found(self.store, self.terms)
        for bad_version in (0, 2, -1):
            with self.subTest(user_version=bad_version):
                with closing(sqlite3.connect(self.store)) as raw, raw:
                    raw.execute(f"PRAGMA user_version = {bad_version}")
                with self.assertRaisesRegex(StorageRefused, "Unsupported"):
                    open_reader(self.store)
                with self.assertRaisesRegex(StorageRefused, "Unsupported"):
                    open_writer(self.store)
                with self.assertRaisesRegex(FoundingRefused, "Unsupported"):
                    reopen(self.store, receipt.company_id)
                with self.assertRaisesRegex(FoundingRefused, "Unsupported"):
                    found(self.store, self.terms)

        # Empty file stamped with non-zero user_version also fails closed on found()
        corrupt_empty = self.store.with_name("empty-v2.sqlite")
        with closing(sqlite3.connect(corrupt_empty)) as raw, raw:
            raw.execute("PRAGMA user_version = 2")
        with self.assertRaisesRegex(FoundingRefused, "unexpected schema version"):
            found(corrupt_empty, self.terms)

    def test_tampered_installed_domains_or_sqlite_schema_fails_closed(self):
        found(self.store, self.terms)

        # Tamper pack_sha256 in installed_domains
        copy_pack = self.store.with_name("tamper-pack.sqlite")
        copy_pack.write_bytes(self.store.read_bytes())
        with closing(sqlite3.connect(copy_pack)) as raw, raw:
            raw.execute(
                "UPDATE installed_domains SET pack_sha256 = ? WHERE domain = 'core'",
                ("0" * 64,),
            )
        with self.assertRaisesRegex(StorageRefused, "checksum mismatch"):
            open_reader(copy_pack)
        with self.assertRaisesRegex(StorageRefused, "checksum mismatch"):
            open_writer(copy_pack)

        # Drop an append-only audit trigger out-of-band
        copy_trigger = self.store.with_name("tamper-trigger.sqlite")
        copy_trigger.write_bytes(self.store.read_bytes())
        with closing(sqlite3.connect(copy_trigger)) as raw, raw:
            raw.execute("DROP TRIGGER tr_audit_event_no_update")
        with self.assertRaisesRegex(StorageRefused, "triggers or views"):
            open_reader(copy_trigger)
        with self.assertRaisesRegex(StorageRefused, "triggers or views"):
            open_writer(copy_trigger)

        # Drop a canonical index out-of-band
        copy_index = self.store.with_name("tamper-index.sqlite")
        copy_index.write_bytes(self.store.read_bytes())
        with closing(sqlite3.connect(copy_index)) as raw, raw:
            raw.execute("DROP INDEX idx_audit_event_company_seq")
        with self.assertRaisesRegex(StorageRefused, "sqlite_schema"):
            open_reader(copy_index)
        with self.assertRaisesRegex(StorageRefused, "sqlite_schema"):
            open_writer(copy_index)

    def test_enable_domain_refuses_outside_tx_or_unsupported_domain(self):
        found(self.store, self.terms)
        with closing(open_writer(self.store)) as writer:
            with self.assertRaisesRegex(StorageRefused, "BEGIN IMMEDIATE"):
                enable_domain_v1(writer, "paper")
            with write_tx(writer):
                with self.assertRaisesRegex(StorageRefused, "Unsupported"):
                    enable_domain_v1(writer, "core")
                with self.assertRaisesRegex(StorageRefused, "Unsupported"):
                    enable_domain_v1(writer, "unknown")
