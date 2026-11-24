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

    def test_tampered_desk_bytes_are_refused_with_or_without_manifest_rewrite(self):
        desk = self.root / "desk-auth"
        desk.mkdir()
        (desk / "state.md").write_text("trusted-context\n", encoding="utf-8")
        archive = self.root / "checkpoint-auth.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            desk_root=desk,
            lease_directory=self.leases,
        )
        with ZipFile(archive, "r") as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            db_bytes = zf.read("company.sqlite")

        # Attack 1: tamper desk/state.md while leaving manifest.json and company.sqlite untouched
        tampered_1 = self.root / "tampered-desk-1.zip"
        with ZipFile(tampered_1, "w", compression=ZIP_STORED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            zf.writestr("company.sqlite", db_bytes)
            zf.writestr("desk/state.md", b"poisoned-context\n")

        with self.assertRaisesRegex(CheckpointRefused, "Desk contents failed"):
            inspect_checkpoint(tampered_1, self.company_id)
        with self.assertRaisesRegex(CheckpointRefused, "Desk contents failed"):
            restore_store(
                self.store,
                self.company_id,
                tampered_1,
                self.root / "never-desk-1.sqlite",
                desk_destination=self.root / "never-desk-1",
                lease_directory=self.leases,
            )

        # Attack 2: tamper desk/state.md AND recompute manifest.desk_digest to match tampered bytes
        poisoned = b"poisoned-context-2\n"
        forged_digest = sha256(
            json.dumps(
                [["state.md", sha256(poisoned).hexdigest()]], separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        manifest["desk_digest"] = forged_digest
        tampered_2 = self.root / "tampered-desk-2.zip"
        with ZipFile(tampered_2, "w", compression=ZIP_STORED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            zf.writestr("company.sqlite", db_bytes)
            zf.writestr("desk/state.md", poisoned)

        with self.assertRaisesRegex(CheckpointRefused, "authoritative audit digest"):
            inspect_checkpoint(tampered_2, self.company_id)
        with self.assertRaisesRegex(CheckpointRefused, "authoritative audit digest"):
            restore_store(
                self.store,
                self.company_id,
                tampered_2,
                self.root / "never-desk-2.sqlite",
                desk_destination=self.root / "never-desk-2",
                lease_directory=self.leases,
            )
        # Original store remains live and writable
        with closing(open_writer(self.store)) as writer:
            self.assertEqual(
                writer.execute("SELECT schema_revision FROM company").fetchone(),
                (MAX_SCHEMA_VERSION,),
            )

    def test_interrupted_post_retirement_publication_resumes_cleanly(self):
        from unittest.mock import patch

        desk = self.root / "desk-recover"
        desk.mkdir()
        (desk / "state.md").write_text("durable-desk\n", encoding="utf-8")
        archive = self.root / "checkpoint-recover.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            desk_root=desk,
            lease_directory=self.leases,
        )

        replacement = self.root / "recovered.sqlite"
        desk_dest = self.root / "desk-recovered"

        def reconcile_shadow(shadow_db: Path) -> None:
            with closing(open_writer(shadow_db)) as conn, write_tx(conn):
                append_audit_event(
                    conn,
                    company_id=self.company_id,
                    domain="ops",
                    entity_type="restore",
                    entity_id="rec-io",
                    event_type="restore.reconciled_before_crash",
                    actor_id="reconciler",
                    payload={"ok": True},
                )

        real_link = os.link

        def fail_only_final_publish(src, dst, *args, **kwargs):
            if dst == replacement.name or Path(dst) == replacement.resolve(
                strict=False
            ):
                raise OSError("simulated publish I/O error")
            return real_link(src, dst, *args, **kwargs)

        # First attempt fails on final os.link AFTER retirement commits
        with patch(
            "sourdough.storage.checkpoint.os.link", side_effect=fail_only_final_publish
        ):
            with self.assertRaisesRegex(OSError, "simulated publish I/O error"):
                restore_store(
                    self.store,
                    self.company_id,
                    archive,
                    replacement,
                    desk_destination=desk_dest,
                    reconcile=reconcile_shadow,
                    lease_directory=self.leases,
                )

        # Original store remains permanently retired; destination not yet published;
        # durable staged replacement has st_nlink == 1 (temporary shadow unlinked before retirement)
        self.assertFalse(replacement.exists())
        self.assertFalse(desk_dest.exists())
        staged_dbs = list(self.root.glob(".sourdough-restore-*.sqlite"))
        self.assertEqual(len(staged_dbs), 1)
        self.assertEqual(staged_dbs[0].stat(follow_symlinks=False).st_nlink, 1)
        with self.assertRaisesRegex(StorageRefused, "retired"):
            open_writer(self.store)

        # If an unrelated directory occupies desk_dest before retry, resumed restore MUST refuse!
        desk_dest.mkdir()
        (desk_dest / "unrelated.txt").write_text("rogue\n", encoding="utf-8")
        with self.assertRaisesRegex(CheckpointRefused, "Desk restore destination"):
            restore_store(
                self.store,
                self.company_id,
                archive,
                replacement,
                desk_destination=desk_dest,
                lease_directory=self.leases,
            )
        self.assertFalse(replacement.exists())
        (desk_dest / "unrelated.txt").unlink()
        desk_dest.rmdir()

        # A different restore destination against the retired store must fail closed
        with self.assertRaisesRegex(CheckpointRefused, "different restore request"):
            restore_store(
                self.store,
                self.company_id,
                archive,
                self.root / "wrong-dest.sqlite",
                desk_destination=self.root / "wrong-desk",
                lease_directory=self.leases,
            )

        # Retrying the exact restore resumes from durable staged artifacts cleanly!
        receipt = restore_store(
            self.store,
            self.company_id,
            archive,
            replacement,
            desk_destination=desk_dest,
            lease_directory=self.leases,
        )
        verify_no_wal_sidecars(replacement)
        self.assertEqual(replacement.stat(follow_symlinks=False).st_nlink, 1)
        from sourdough.writer_lease import hold_company_writer

        with hold_company_writer(
            replacement, self.company_id, lease_directory=self.leases
        ) as active_lease:
            active_lease.verify()
        self.assertEqual(
            (desk_dest / "state.md").read_text(encoding="utf-8"),
            "durable-desk\n",
        )
        with closing(open_reader(replacement)) as reader, read_tx(reader):
            self.assertEqual(
                reader.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete"
            )
            self.assertEqual(
                reader.execute(
                    "SELECT event_type FROM audit_event WHERE event_type = 'restore.reconciled_before_crash'"
                ).fetchall(),
                [("restore.reconciled_before_crash",)],
            )
            self.assertEqual(
                reader.execute(
                    "SELECT request_revision FROM company_restore_receipt WHERE request_revision = ?",
                    (receipt.request_revision,),
                ).fetchone(),
                (receipt.request_revision,),
            )
        # Durable staging files are cleaned up and original store stays retired
        self.assertEqual(list(self.root.glob(".sourdough-restore-*")), [])
        self.assertEqual(list(self.root.glob(".sourdough-desk-stage-*")), [])
        with self.assertRaisesRegex(StorageRefused, "retired"):
            open_writer(self.store)

    def test_pre_retirement_desk_staging_failure_cleans_up_for_retry(self):
        from unittest.mock import patch

        desk = self.root / "desk-fail"
        desk.mkdir()
        (desk / "state.md").write_text("desk-body\n", encoding="utf-8")
        archive = self.root / "checkpoint-desk-fail.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            desk_root=desk,
            lease_directory=self.leases,
        )

        replacement = self.root / "recovered-retry.sqlite"
        desk_dest = self.root / "desk-retry"

        with patch(
            "sourdough.storage.checkpoint._stage_pinned_desk",
            side_effect=OSError("simulated desk copy failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated desk copy failure"):
                restore_store(
                    self.store,
                    self.company_id,
                    archive,
                    replacement,
                    desk_destination=desk_dest,
                    lease_directory=self.leases,
                )

        # Pre-retirement failure leaves zero durable staging artifacts and keeps store live.
        self.assertEqual(list(self.root.glob(".sourdough-restore-*")), [])
        self.assertEqual(list(self.root.glob(".sourdough-desk-stage-*")), [])
        self.assertEqual(list(self.root.glob(".sourdough-desk-tmp-*")), [])
        self.assertFalse(replacement.exists())
        self.assertFalse(desk_dest.exists())

        receipt = restore_store(
            self.store,
            self.company_id,
            archive,
            replacement,
            desk_destination=desk_dest,
            lease_directory=self.leases,
        )
        self.assertEqual(receipt.company_id, self.company_id)
        self.assertEqual(
            (desk_dest / "state.md").read_text(encoding="utf-8"),
            "desk-body\n",
        )
        self.assertEqual(replacement.stat(follow_symlinks=False).st_nlink, 1)

    def test_tampered_staged_database_refuses_resumed_restore(self):
        from unittest.mock import patch

        archive = self.root / "checkpoint-tamper-stage.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            lease_directory=self.leases,
        )
        replacement = self.root / "recovered-tamper.sqlite"
        real_link = os.link

        def fail_final_publish(src, dst, *args, **kwargs):
            if dst == replacement.name or Path(dst) == replacement.resolve(
                strict=False
            ):
                raise OSError("simulated publish I/O error")
            return real_link(src, dst, *args, **kwargs)

        with patch(
            "sourdough.storage.checkpoint.os.link", side_effect=fail_final_publish
        ):
            with self.assertRaisesRegex(OSError, "simulated publish I/O error"):
                restore_store(
                    self.store,
                    self.company_id,
                    archive,
                    replacement,
                    lease_directory=self.leases,
                )

        staged_dbs = list(self.root.glob(".sourdough-restore-*.sqlite"))
        self.assertEqual(len(staged_dbs), 1)
        # Tamper a domain row in the staged DB while leaving company_restore_receipt intact
        with closing(
            open_writer(staged_dbs[0], create=False, journal_mode="DELETE")
        ) as conn:
            with write_tx(conn):
                conn.execute("UPDATE standing_grant SET scope = 'escalated'")

        with self.assertRaisesRegex(CheckpointRefused, "digest does not match"):
            restore_store(
                self.store,
                self.company_id,
                archive,
                replacement,
                lease_directory=self.leases,
            )
        self.assertFalse(replacement.exists())

    def test_pre_retirement_crash_leftover_stages_recover_cleanly(self):
        from unittest.mock import patch

        desk = self.root / "desk-crash"
        desk.mkdir()
        (desk / "state.md").write_text("pre-retirement-desk\n", encoding="utf-8")
        archive = self.root / "checkpoint-pre-crash.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            desk_root=desk,
            lease_directory=self.leases,
        )
        replacement = self.root / "recovered-after-crash.sqlite"
        desk_dest = self.root / "desk-after-crash"

        # Simulate a process crash after durable DB + Desk staging (skipping finally
        # cleanup) by capturing the staged artifacts and aborting before retirement.
        preserved_db: bytes | None = None
        staged_db_name: str | None = None
        staged_desk_name: str | None = None
        real_open_writer = open_writer

        def crash_before_retirement(path, *args, **kwargs):
            nonlocal preserved_db, staged_db_name, staged_desk_name
            if Path(path).resolve(strict=False) == self.store.resolve(strict=False):
                staged_dbs = list(self.root.glob(".sourdough-restore-*.sqlite"))
                staged_desks = list(self.root.glob(".sourdough-desk-stage-*"))
                self.assertEqual(len(staged_dbs), 1)
                self.assertEqual(len(staged_desks), 1)
                preserved_db = staged_dbs[0].read_bytes()
                staged_db_name = staged_dbs[0].name
                staged_desk_name = staged_desks[0].name
                raise RuntimeError("simulated pre-retirement crash")
            return real_open_writer(path, *args, **kwargs)

        with patch(
            "sourdough.storage.checkpoint.open_writer",
            side_effect=crash_before_retirement,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated pre-retirement crash"):
                restore_store(
                    self.store,
                    self.company_id,
                    archive,
                    replacement,
                    desk_destination=desk_dest,
                    lease_directory=self.leases,
                )

        # Recreate the orphaned deterministic stages that a SIGKILL would leave behind
        # (including an orphaned shadow hard link giving st_nlink == 2).
        assert preserved_db is not None and staged_db_name is not None
        assert staged_desk_name is not None
        orphaned_shadow = self.root / "orphaned-shadow.sqlite"
        orphaned_shadow.write_bytes(preserved_db)
        os.link(orphaned_shadow, self.root / staged_db_name)
        leftover_desk = self.root / staged_desk_name
        leftover_desk.mkdir()
        (leftover_desk / "state.md").write_text(
            "pre-retirement-desk\n", encoding="utf-8"
        )

        # Retry under the live writer lease verifies, discards uncommitted stages,
        # and completes cleanly with a fresh single-link replacement.
        receipt = restore_store(
            self.store,
            self.company_id,
            archive,
            replacement,
            desk_destination=desk_dest,
            lease_directory=self.leases,
        )
        self.assertEqual(receipt.company_id, self.company_id)
        self.assertEqual(replacement.stat(follow_symlinks=False).st_nlink, 1)
        self.assertEqual(
            (desk_dest / "state.md").read_text(encoding="utf-8"),
            "pre-retirement-desk\n",
        )
        self.assertEqual(list(self.root.glob(".sourdough-restore-*")), [])
        self.assertEqual(list(self.root.glob(".sourdough-desk-stage-*")), [])

    def test_interrupted_final_fsync_after_staging_unlink_recovers_receipt(self):
        from unittest.mock import patch

        from sourdough.storage import checkpoint as cp_mod

        desk = self.root / "desk-fsync"
        desk.mkdir()
        (desk / "state.md").write_text("fsync-desk\n", encoding="utf-8")
        archive = self.root / "checkpoint-fsync.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            desk_root=desk,
            lease_directory=self.leases,
        )
        replacement = self.root / "recovered-fsync.sqlite"
        desk_dest = self.root / "desk-fsync-out"

        real_fsync_dir = cp_mod._fsync_dir

        def fail_after_staging_unlink(directory: Path) -> None:
            # Trigger failure strictly on the final directory fsync after target and
            # desk_dest exist and the DB staging link has already been unlinked.
            if (
                directory == replacement.parent.resolve(strict=False)
                and replacement.exists()
                and desk_dest.exists()
                and not list(self.root.glob(".sourdough-restore-*.sqlite"))
            ):
                raise OSError("simulated final fsync error")
            real_fsync_dir(directory)

        with patch(
            "sourdough.storage.checkpoint._fsync_dir",
            side_effect=fail_after_staging_unlink,
        ):
            with self.assertRaisesRegex(OSError, "simulated final fsync error"):
                restore_store(
                    self.store,
                    self.company_id,
                    archive,
                    replacement,
                    desk_destination=desk_dest,
                    lease_directory=self.leases,
                )

        # Both destinations are published and no staging link remains.
        self.assertTrue(replacement.exists())
        self.assertTrue(desk_dest.exists())
        self.assertEqual(list(self.root.glob(".sourdough-restore-*.sqlite")), [])

        # Matching retry verifies the authenticated replacement and recovers the receipt.
        receipt = restore_store(
            self.store,
            self.company_id,
            archive,
            replacement,
            desk_destination=desk_dest,
            lease_directory=self.leases,
        )
        self.assertEqual(receipt.company_id, self.company_id)

        # Once the replacement diverges (or is an unrelated file), retry must refuse.
        with closing(
            open_writer(replacement, create=False, journal_mode="DELETE")
        ) as conn:
            with write_tx(conn):
                append_audit_event(
                    conn,
                    company_id=self.company_id,
                    domain="ops",
                    entity_type="restore",
                    entity_id="post-use",
                    event_type="restore.post_use_mutation",
                    actor_id="operator",
                    payload={"used": True},
                )
        with self.assertRaisesRegex(CheckpointRefused, "digest does not match"):
            restore_store(
                self.store,
                self.company_id,
                archive,
                replacement,
                desk_destination=desk_dest,
                lease_directory=self.leases,
            )

    def test_swapping_staged_database_after_verification_cannot_publish_tampered_db(
        self,
    ):
        from unittest.mock import patch

        from sourdough.storage import checkpoint as cp_mod

        archive = self.root / "checkpoint-swap-stage.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            lease_directory=self.leases,
        )
        replacement = self.root / "recovered-swap.sqlite"
        real_link = os.link

        def fail_final_publish(src, dst, *args, **kwargs):
            if dst == replacement.name or Path(dst) == replacement.resolve(
                strict=False
            ):
                raise OSError("simulated publish I/O error")
            return real_link(src, dst, *args, **kwargs)

        with patch(
            "sourdough.storage.checkpoint.os.link", side_effect=fail_final_publish
        ):
            with self.assertRaisesRegex(OSError, "simulated publish I/O error"):
                restore_store(
                    self.store,
                    self.company_id,
                    archive,
                    replacement,
                    lease_directory=self.leases,
                )

        staged_dbs = list(self.root.glob(".sourdough-restore-*.sqlite"))
        self.assertEqual(len(staged_dbs), 1)
        staged_db = staged_dbs[0]

        # Prepare a tampered DB with escalated standing_grant.scope
        tampered = self.root / "tampered-swap.sqlite"
        tampered.write_bytes(staged_db.read_bytes())
        with closing(
            open_writer(tampered, create=False, journal_mode="DELETE")
        ) as conn:
            with write_tx(conn):
                conn.execute("UPDATE standing_grant SET scope = 'escalated'")

        # Simulate a local attacker swapping staged_db immediately after
        # _copy_and_verify_replacement completes and before _publish_file links target.
        real_copy_and_verify = cp_mod._copy_and_verify_replacement

        def swap_stage_right_after_verify(*args, **kwargs):
            result = real_copy_and_verify(*args, **kwargs)
            os.replace(tampered, staged_db)
            return result

        with patch(
            "sourdough.storage.checkpoint._copy_and_verify_replacement",
            side_effect=swap_stage_right_after_verify,
        ):
            receipt = restore_store(
                self.store,
                self.company_id,
                archive,
                replacement,
                lease_directory=self.leases,
            )

        self.assertEqual(receipt.company_id, self.company_id)
        self.assertEqual(replacement.stat(follow_symlinks=False).st_nlink, 1)
        # Published replacement MUST come from the private verified copy, NOT tampered!
        with closing(open_reader(replacement)) as reader, read_tx(reader):
            scopes = [
                str(row[0])
                for row in reader.execute("SELECT scope FROM standing_grant").fetchall()
            ]
        self.assertNotIn("escalated", scopes)
        self.assertIn("manage", scopes)

    def test_swapping_private_copy_before_link_is_detected_and_unlinked(self):
        from unittest.mock import patch

        from sourdough.storage import checkpoint as cp_mod

        archive = self.root / "checkpoint-swap-private.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            lease_directory=self.leases,
        )
        replacement = self.root / "never-publish-tampered.sqlite"
        real_link = os.link

        def fail_final_publish(src, dst, *args, **kwargs):
            if dst == replacement.name or Path(dst) == replacement.resolve(
                strict=False
            ):
                raise OSError("simulated publish I/O error")
            return real_link(src, dst, *args, **kwargs)

        with patch(
            "sourdough.storage.checkpoint.os.link", side_effect=fail_final_publish
        ):
            with self.assertRaisesRegex(OSError, "simulated publish I/O error"):
                restore_store(
                    self.store,
                    self.company_id,
                    archive,
                    replacement,
                    lease_directory=self.leases,
                )

        staged_db = next(self.root.glob(".sourdough-restore-*.sqlite"))
        tampered = self.root / "tampered-private.sqlite"
        tampered.write_bytes(staged_db.read_bytes())
        with closing(
            open_writer(tampered, create=False, journal_mode="DELETE")
        ) as conn:
            with write_tx(conn):
                conn.execute("UPDATE standing_grant SET scope = 'escalated'")

        # Simulate a same-UID attacker replacing the private copy itself after
        # _copy_and_verify_replacement returns and before os.link executes.
        real_copy_and_verify = cp_mod._copy_and_verify_replacement

        def swap_private_copy(candidate, private_dir_fd, private_copy, *args, **kwargs):
            result = real_copy_and_verify(
                candidate, private_dir_fd, private_copy, *args, **kwargs
            )
            os.replace(tampered, private_copy)
            return result

        with patch(
            "sourdough.storage.checkpoint._copy_and_verify_replacement",
            side_effect=swap_private_copy,
        ):
            with self.assertRaisesRegex(
                CheckpointRefused, "inode or digest changed during publication"
            ):
                restore_store(
                    self.store,
                    self.company_id,
                    archive,
                    replacement,
                    lease_directory=self.leases,
                )
        # Tampered replacement MUST be unlinked and never left published!
        self.assertFalse(replacement.exists())

    def test_restore_refuses_non_private_destination_parent_directory(self):
        archive = self.root / "checkpoint-perms.zip"
        checkpoint_store(
            self.store,
            self.company_id,
            archive,
            lease_directory=self.leases,
        )
        shared_dir = self.root / "shared-dest"
        shared_dir.mkdir(mode=0o755)
        os.chmod(shared_dir, 0o755)
        with self.assertRaisesRegex(CheckpointRefused, "owner-only"):
            restore_store(
                self.store,
                self.company_id,
                archive,
                shared_dir / "restored.sqlite",
                lease_directory=self.leases,
            )
        # Original store remains live and writable
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
