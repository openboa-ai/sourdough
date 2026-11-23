import os
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

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

        # Symlink inside Desk must be rejected at checkpoint time
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

    def test_logical_watermark_ignores_wal_physical_layout(self):
        with closing(open_reader(self.store)) as reader, read_tx(reader):
            wm1 = compute_logical_watermark(reader, self.company_id)
        self.assertEqual(wm1.schema_version, MAX_SCHEMA_VERSION)
        snapshot = self.root / "copy.sqlite"
        snapshot_normalized_store(self.store, snapshot)
        with closing(open_reader(snapshot)) as reader, read_tx(reader):
            wm2 = compute_logical_watermark(reader, self.company_id)
        self.assertEqual(wm1, wm2)
