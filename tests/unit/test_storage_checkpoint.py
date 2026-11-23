import json
import os
import sqlite3
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_STORED, ZipFile

from sourdough.company import FoundingTerms, found
from sourdough.storage.checkpoint import (
    CheckpointRefused,
    checkpoint_store,
    compute_logical_watermark,
    inspect_checkpoint,
    restore_store,
    snapshot_normalized_store,
)
from sourdough.storage.db import (
    MAX_SCHEMA_VERSION,
    StorageRefused,
    open_reader,
    open_writer,
    read_tx,
    verify_no_wal_sidecars,
    wal_checkpoint,
    write_tx,
)
from sourdough.storage.kernel import append_audit_event


class StorageCheckpointAndRestore(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.leases = self.root / "leases"
        self.store = self.root / "company.sqlite"
        self.receipt = found(
            self.store, FoundingTerms("setup", "charter-v1", "manage", "profile-v1")
        )
        self.company_id = self.receipt.company_id

    def test_snapshot_normalizes_live_wal_store_to_delete_with_zero_sidecars(self):
        # Leave an active writer open with uncommitted WAL frames to prove online backup
        # captures WAL writes and normalizes destination to DELETE mode with zero sidecars.
        with closing(open_writer(self.store)) as writer:
            with write_tx(writer):
                seq = append_audit_event(
                    writer,
                    company_id=self.company_id,
                    domain="work",
                    entity_type="duty",
                    entity_id=self.receipt.due_work_id,
                    event_type="duty.live_wal",
                    actor_id=self.receipt.founder_id,
                    payload={"in_wal": True},
                )
            snapshot = self.root / "snapshot.sqlite"
            snapshot_normalized_store(self.store, snapshot)
            verify_no_wal_sidecars(snapshot)
            with closing(open_reader(snapshot)) as reader, read_tx(reader):
                self.assertEqual(
                    reader.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "delete",
                )
                row = reader.execute(
                    "SELECT event_type FROM audit_event WHERE event_seq = ?", (seq,)
                ).fetchone()
                self.assertEqual(row, ("duty.live_wal",))

    def test_restore_preserves_reconciled_writes_and_receipt_across_wal_normalization(
        self,
    ):
        desk = self.root / "desk-src"
        (desk / "agent-1").mkdir(parents=True)
        (desk / "agent-1/state.md").write_text(
            "checkpointed-desk-state\n", encoding="utf-8"
        )

        archive = self.root / "checkpoint.zip"
        manifest = checkpoint_store(
            self.store,
            self.company_id,
            archive,
            desk_root=desk,
            lease_directory=self.leases,
        )
        self.assertEqual(manifest.desk_files, ("agent-1/state.md",))
        inspected = inspect_checkpoint(archive, self.company_id)
        self.assertEqual(inspected, manifest)

        # Mutate physical WAL layout (checkpoint + PRAGMA optimize) WITHOUT changing
        # logical domain rows; logical watermark cut verification must still succeed!
        with closing(open_writer(self.store)) as writer:
            wal_checkpoint(writer, "TRUNCATE")
            writer.execute("PRAGMA optimize")

        def reconcile_shadow(shadow_db: Path) -> None:
            # Even if a reconciler writes via normal WAL open_writer, restore_store
            # must normalize shadow back to DELETE mode so zero rows are lost on link!
            with closing(open_writer(shadow_db)) as conn, write_tx(conn):
                append_audit_event(
                    conn,
                    company_id=self.company_id,
                    domain="ops",
                    entity_type="restore",
                    entity_id="reconcile-1",
                    event_type="restore.reconciled_fill",
                    actor_id="reconciler",
                    payload={"fill_id": "fill-99"},
                )

        restored = self.root / "restored.sqlite"
        desk_dest = self.root / "desk-restored"
        restore_receipt = restore_store(
            self.store,
            self.company_id,
            archive,
            restored,
            desk_destination=desk_dest,
            reconcile=reconcile_shadow,
            lease_directory=self.leases,
        )
        verify_no_wal_sidecars(restored)

        # Verify restored DB retained BOTH reconciled audit event and restore receipt!
        with closing(open_reader(restored)) as reader, read_tx(reader):
            self.assertEqual(
                reader.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete"
            )
            reconciled_rows = reader.execute(
                "SELECT event_type FROM audit_event WHERE event_type = 'restore.reconciled_fill'"
            ).fetchall()
            self.assertEqual(reconciled_rows, [("restore.reconciled_fill",)])
            receipt_row = reader.execute(
                "SELECT checkpoint_sha256 FROM company_restore_receipt WHERE request_revision = ?",
                (restore_receipt.request_revision,),
            ).fetchone()
            self.assertEqual(receipt_row, (manifest.sha256,))

        # Verify bundled Desk snapshot was restored intact
        self.assertEqual(
            (desk_dest / "agent-1/state.md").read_text(encoding="utf-8"),
            "checkpointed-desk-state\n",
        )

        # Verify original store is retired and rejects any new writer
        with self.assertRaisesRegex(StorageRefused, "retired"):
            open_writer(self.store)

    def test_restore_refuses_drifted_logical_watermark_and_desk_symlinks(self):
        archive = self.root / "checkpoint.zip"
        checkpoint_store(
            self.store, self.company_id, archive, lease_directory=self.leases
        )
        # Perform a real domain mutation after checkpoint
        with closing(open_writer(self.store)) as writer, write_tx(writer):
            append_audit_event(
                writer,
                company_id=self.company_id,
                domain="work",
                entity_type="duty",
                entity_id=self.receipt.due_work_id,
                event_type="duty.post_checkpoint",
                actor_id=self.receipt.founder_id,
                payload={"changed": True},
            )
        with self.assertRaisesRegex(CheckpointRefused, "logical watermark"):
            restore_store(
                self.store,
                self.company_id,
                archive,
                self.root / "should-not-exist.sqlite",
                lease_directory=self.leases,
            )
        self.assertFalse((self.root / "should-not-exist.sqlite").exists())

        # Symlink inside Desk (file, subdir, or root) must be rejected at checkpoint time
        bad_desk = self.root / "bad-desk"
        bad_desk.mkdir()
        os.symlink(self.store, bad_desk / "escape.sqlite")
        with self.assertRaisesRegex(CheckpointRefused, "Symlink"):
            checkpoint_store(
                self.store,
                self.company_id,
                self.root / "bad.zip",
                desk_root=bad_desk,
                lease_directory=self.leases,
            )

        bad_subdir_desk = self.root / "bad-subdir-desk"
        bad_subdir_desk.mkdir()
        os.symlink(self.root, bad_subdir_desk / "escape-dir")
        with self.assertRaisesRegex(CheckpointRefused, "Symlink"):
            checkpoint_store(
                self.store,
                self.company_id,
                self.root / "bad-subdir.zip",
                desk_root=bad_subdir_desk,
                lease_directory=self.leases,
            )

        real_desk = self.root / "real-desk"
        real_desk.mkdir()
        symlink_root = self.root / "symlink-desk-root"
        os.symlink(real_desk, symlink_root)
        with self.assertRaisesRegex(CheckpointRefused, "Symlink"):
            checkpoint_store(
                self.store,
                self.company_id,
                self.root / "bad-root.zip",
                desk_root=symlink_root,
                lease_directory=self.leases,
            )

    def test_existing_desk_destination_refuses_before_retiring_original_store(self):
        desk = self.root / "desk-src"
        desk.mkdir()
        (desk / "notes.md").write_text("draft\n", encoding="utf-8")
        archive = self.root / "checkpoint-desk.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            desk_root=desk,
            lease_directory=self.leases,
        )
        occupied_desk = self.root / "occupied-desk"
        occupied_desk.mkdir()
        replacement = self.root / "never-published.sqlite"
        # Omitting desk_destination for a Desk-bundled checkpoint must refuse cleanly
        with self.assertRaisesRegex(CheckpointRefused, "desk_destination is required"):
            restore_store(
                self.store,
                self.company_id,
                archive,
                replacement,
                lease_directory=self.leases,
            )
        self.assertFalse(replacement.exists())
        with self.assertRaisesRegex(CheckpointRefused, "Desk restore destination"):
            restore_store(
                self.store,
                self.company_id,
                archive,
                replacement,
                desk_destination=occupied_desk,
                lease_directory=self.leases,
            )
        self.assertFalse(replacement.exists())
        # Original store MUST remain unretired and writable!
        with closing(open_writer(self.store)) as writer:
            self.assertEqual(
                writer.execute("SELECT schema_revision FROM company").fetchone(),
                (MAX_SCHEMA_VERSION,),
            )

    def test_intervening_write_during_reconcile_refuses_before_retirement(self):
        archive = self.root / "checkpoint-race.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            lease_directory=self.leases,
        )
        replacement = self.root / "should-not-publish.sqlite"

        def concurrent_mutation(_shadow: Path) -> None:
            # Simulate a caller mutating the original store without WriterLease
            # after the initial watermark check succeeded.
            with closing(open_writer(self.store)) as writer, write_tx(writer):
                append_audit_event(
                    writer,
                    company_id=self.company_id,
                    domain="work",
                    actor_id=self.receipt.founder_id,
                    entity_type="duty",
                    entity_id="duty-race",
                    event_type="duty.claimed",
                    payload={"source": "intervening-writer"},
                )

        with self.assertRaisesRegex(CheckpointRefused, "logical watermark"):
            restore_store(
                self.store,
                self.company_id,
                archive,
                replacement,
                reconcile=concurrent_mutation,
                lease_directory=self.leases,
            )
        self.assertFalse(replacement.exists())
        # Original store must stay live and retain the intervening write.
        with closing(open_writer(self.store)) as writer:
            self.assertEqual(
                writer.execute("SELECT schema_revision FROM company").fetchone(),
                (MAX_SCHEMA_VERSION,),
            )
            count = writer.execute(
                "SELECT COUNT(*) FROM audit_event WHERE entity_id = 'duty-race'"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_forged_archive_with_injected_trigger_is_refused(self):
        archive = self.root / "checkpoint-clean.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            lease_directory=self.leases,
        )
        tampered_dir = self.root / "tampered"
        tampered_dir.mkdir()
        with ZipFile(archive, "r") as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            db_path = tampered_dir / "company.sqlite"
            db_path.write_bytes(zf.read("company.sqlite"))

        # Inject a rogue SQL trigger and forge size, sha256, and even watermark
        with closing(sqlite3.connect(db_path, autocommit=True)) as conn:
            conn.execute(
                """CREATE TRIGGER tr_rogue_escalate AFTER INSERT ON duty
                   BEGIN
                       UPDATE standing_grant SET scope = 'escalated';
                   END"""
            )
            forged_wm = compute_logical_watermark(conn, self.company_id)

        raw = db_path.read_bytes()
        manifest["size"] = len(raw)
        manifest["sha256"] = sha256(raw).hexdigest()
        manifest["watermark"] = {
            "company_id": forged_wm.company_id,
            "schema_version": forged_wm.schema_version,
            "max_event_seq": forged_wm.max_event_seq,
            "domain_digest": forged_wm.domain_digest,
        }
        forged_zip = self.root / "forged-trigger.zip"
        with ZipFile(forged_zip, "w", compression=ZIP_STORED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            zf.write(db_path, "company.sqlite")

        # Both standalone inspect_checkpoint and restore_store must refuse the trigger
        with self.assertRaisesRegex(CheckpointRefused, "triggers or views"):
            inspect_checkpoint(forged_zip, self.company_id)
        replacement = self.root / "never-restored-trigger.sqlite"
        with self.assertRaisesRegex(CheckpointRefused, "triggers or views"):
            restore_store(
                self.store,
                self.company_id,
                forged_zip,
                replacement,
                lease_directory=self.leases,
            )
        self.assertFalse(replacement.exists())

    def test_forged_archive_with_altered_index_is_refused_by_restore(self):
        archive = self.root / "checkpoint-index.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            lease_directory=self.leases,
        )
        tampered_dir = self.root / "tampered-index"
        tampered_dir.mkdir()
        with ZipFile(archive, "r") as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            db_path = tampered_dir / "company.sqlite"
            db_path.write_bytes(zf.read("company.sqlite"))

        # Alter schema without adding triggers (drop an index) and re-sign manifest
        with closing(sqlite3.connect(db_path, autocommit=True)) as conn:
            conn.execute("DROP INDEX idx_audit_event_company_seq")
            forged_wm = compute_logical_watermark(conn, self.company_id)

        raw = db_path.read_bytes()
        manifest["size"] = len(raw)
        manifest["sha256"] = sha256(raw).hexdigest()
        manifest["watermark"] = {
            "company_id": forged_wm.company_id,
            "schema_version": forged_wm.schema_version,
            "max_event_seq": forged_wm.max_event_seq,
            "domain_digest": forged_wm.domain_digest,
        }
        forged_zip = self.root / "forged-index.zip"
        with ZipFile(forged_zip, "w", compression=ZIP_STORED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            zf.write(db_path, "company.sqlite")

        # Live store logical watermark includes full sqlite_schema, so restore refuses
        replacement = self.root / "never-restored-index.sqlite"
        with self.assertRaisesRegex(CheckpointRefused, "logical watermark"):
            restore_store(
                self.store,
                self.company_id,
                forged_zip,
                replacement,
                lease_directory=self.leases,
            )
        self.assertFalse(replacement.exists())
        with closing(open_writer(self.store)) as writer:
            self.assertEqual(
                writer.execute("SELECT schema_revision FROM company").fetchone(),
                (MAX_SCHEMA_VERSION,),
            )

    def test_logical_watermark_ignores_wal_physical_layout(self):
        with closing(open_reader(self.store)) as reader, read_tx(reader):
            wm1 = compute_logical_watermark(reader, self.company_id)
        self.assertEqual(wm1.schema_version, MAX_SCHEMA_VERSION)
        snapshot = self.root / "copy.sqlite"
        snapshot_normalized_store(self.store, snapshot)
        with closing(open_reader(snapshot)) as reader, read_tx(reader):
            wm2 = compute_logical_watermark(reader, self.company_id)
        self.assertEqual(wm1, wm2)
