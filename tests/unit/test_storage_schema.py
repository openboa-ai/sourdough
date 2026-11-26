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
from sourdough.storage.migration import rebuild_core_v1_tables
from sourdough.storage.schema import (
    canonical_domain_name,
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

    def test_canonical_domain_name_and_trading_activation_aliases(self):
        self.assertEqual(canonical_domain_name("core"), "core")
        self.assertEqual(canonical_domain_name("trading"), "trading")
        self.assertEqual(canonical_domain_name("paper"), "trading")
        with self.assertRaisesRegex(StorageRefused, "Unsupported"):
            canonical_domain_name("unknown")

        receipt = found(
            self.store, FoundingTerms("setup", "charter-v1", "manage", "profile-v1")
        )
        with closing(open_writer(self.store)) as writer, write_tx(writer):
            self.assertTrue(
                enable_domain_v1(
                    writer, "trading", company_id=receipt.company_id, actor_id="test"
                )
            )
            self.assertFalse(
                enable_domain_v1(
                    writer, "paper", company_id=receipt.company_id, actor_id="test"
                )
            )
        with closing(open_reader(self.store)) as reader, read_tx(reader):
            self.assertTrue(is_domain_installed(reader, "trading"))
            self.assertTrue(is_domain_installed(reader, "paper"))
            self.assertEqual(verify_v1_schema(reader), frozenset({"core", "trading"}))

    def test_core_v1_constraints_and_rebuild_core_v1_tables(self):
        receipt = found(self.store, self.terms)
        with closing(open_writer(self.store)) as writer, write_tx(writer):
            with self.assertRaises(sqlite3.IntegrityError):
                writer.execute(
                    "UPDATE duty SET state = 'bogus' WHERE id = ?",
                    (receipt.due_work_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                writer.execute(
                    """INSERT INTO work_follow_up (
                           duty_id, previous_duty_id, due_at, result_kind,
                           result_id, result_revision, available_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ("fu-1", receipt.due_work_id, 10, "r", "id", "rev", 1),
                )
            writer.execute(
                """INSERT INTO decision_question (
                       question_id, company_id, kind, title, summary,
                       impact_json, options_json, revision, payload_hash,
                       state, created_by_agent_id, created_at_ns,
                       application_status, delivery_status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "q-failed",
                    receipt.company_id,
                    "advisory",
                    "Test",
                    "Summary",
                    "{}",
                    "[]",
                    1,
                    "h1",
                    "applied",
                    receipt.founder_id,
                    100,
                    "failed",
                    "delivered",
                ),
            )

        mig_store = self.store.with_name("mig.sqlite")
        mig_store.write_bytes(self.store.read_bytes())
        with closing(sqlite3.connect(mig_store)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            with raw:
                raw.execute("DROP TABLE work_follow_up")
                raw.execute(
                    """CREATE TABLE work_follow_up (
                           duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
                           previous_duty_id TEXT NOT NULL UNIQUE REFERENCES duty(id),
                           due_at INTEGER NOT NULL,
                           purpose TEXT NOT NULL DEFAULT 'position_review',
                           result_kind TEXT NOT NULL,
                           result_id TEXT NOT NULL,
                           result_revision TEXT NOT NULL,
                           available_at INTEGER NOT NULL
                       ) STRICT"""
                )
        with self.assertRaisesRegex(StorageRefused, "sqlite_schema"):
            open_reader(mig_store)
        with closing(sqlite3.connect(mig_store)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("BEGIN IMMEDIATE")
            domains = rebuild_core_v1_tables(raw)
            raw.commit()
        self.assertEqual(domains, frozenset({"core"}))
        with closing(open_reader(mig_store)) as reader, read_tx(reader):
            self.assertEqual(verify_v1_schema(reader), frozenset({"core"}))

    def test_rebuild_core_v1_tables_rejects_invalid_installed_domains_with_storage_refused(
        self,
    ):
        found(self.store, self.terms)
        bad_store = self.store.with_name("bad-domain.sqlite")
        bad_store.write_bytes(self.store.read_bytes())
        with closing(sqlite3.connect(bad_store)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            with raw:
                raw.execute("DROP TABLE installed_domains")
                raw.execute(
                    """CREATE TABLE installed_domains (
                           domain TEXT PRIMARY KEY,
                           schema_version INTEGER NOT NULL,
                           pack_sha256 TEXT NOT NULL,
                           sqlite_schema_sha256 TEXT NOT NULL,
                           activated_at_ns INTEGER NOT NULL
                       ) STRICT"""
                )
                raw.execute(
                    "INSERT INTO installed_domains VALUES ('core', 1, 'a', 'b', 1)"
                )
                raw.execute(
                    "INSERT INTO installed_domains VALUES ('bogus', 1, 'a', 'b', 1)"
                )
            before_rows = raw.execute(
                "SELECT domain, schema_version FROM installed_domains ORDER BY domain"
            ).fetchall()
            raw.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(StorageRefused, "installed_domains.domain"):
                rebuild_core_v1_tables(raw)
            raw.rollback()
            after_rows = raw.execute(
                "SELECT domain, schema_version FROM installed_domains ORDER BY domain"
            ).fetchall()
            self.assertEqual(after_rows, before_rows)
