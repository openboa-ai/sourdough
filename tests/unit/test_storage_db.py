import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.storage.db import (
    MAX_SCHEMA_VERSION,
    RETIRED_SCHEMA_REVISION,
    StorageRefused,
    open_reader,
    open_writer,
    read_tx,
    wal_checkpoint,
    write_tx,
)
from sourdough.storage.kernel import (
    CommandConflict,
    CommandUnauthorized,
    ExternalActionConflict,
    admit_command_receipt,
    append_audit_event,
    finalize_command_receipt,
    max_event_seq,
    prepare_external_action,
    read_command_receipt,
    settle_external_action,
)


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class StorageDatabaseAndKernel(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.receipt = found(
            self.store, FoundingTerms("setup", "charter-v1", "manage", "profile-v1")
        )
        self.company_id = self.receipt.company_id

    def test_writer_and_reader_pragmas_and_query_only_enforcement(self):
        with closing(open_writer(self.store)) as writer:
            self.assertEqual(
                writer.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal"
            )
            self.assertEqual(writer.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(writer.execute("PRAGMA trusted_schema").fetchone()[0], 0)
            self.assertEqual(writer.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(
                writer.execute("PRAGMA user_version").fetchone()[0], MAX_SCHEMA_VERSION
            )
            busy, log_frames, checkpointed = wal_checkpoint(writer, "PASSIVE")
            self.assertEqual(busy, 0)
            self.assertGreaterEqual(log_frames, 0)
            self.assertGreaterEqual(checkpointed, 0)
            busy2, _, _ = wal_checkpoint(writer, "TRUNCATE")
            self.assertEqual(busy2, 0)

        with closing(open_reader(self.store)) as reader, read_tx(reader):
            self.assertEqual(reader.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(reader.execute("PRAGMA trusted_schema").fetchone()[0], 0)
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute(
                    "UPDATE company SET charter_revision = 'tampered' WHERE singleton = 1"
                )

    def test_future_user_version_and_retired_store_fail_closed(self):
        with closing(sqlite3.connect(self.store)) as raw, raw:
            raw.execute(f"PRAGMA user_version = {MAX_SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(StorageRefused, "Unsupported future"):
            open_writer(self.store)
        with self.assertRaisesRegex(StorageRefused, "Unsupported future"):
            open_reader(self.store)

        with closing(sqlite3.connect(self.store)) as raw, raw:
            raw.execute(f"PRAGMA user_version = {MAX_SCHEMA_VERSION}")
            raw.execute(
                "UPDATE company SET schema_revision = ? WHERE singleton = 1",
                (RETIRED_SCHEMA_REVISION,),
            )
        with self.assertRaisesRegex(StorageRefused, "retired"):
            open_writer(self.store)

    def test_audit_event_is_strict_and_append_only(self):
        with closing(open_writer(self.store)) as writer:
            with write_tx(writer):
                seq = append_audit_event(
                    writer,
                    company_id=self.company_id,
                    domain="work",
                    entity_type="duty",
                    entity_id=self.receipt.due_work_id,
                    event_type="duty.observed",
                    actor_id=self.receipt.founder_id,
                    payload={"state": "due"},
                )
            self.assertGreaterEqual(seq, 2)
            self.assertEqual(max_event_seq(writer, self.company_id), seq)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                writer.execute(
                    "UPDATE audit_event SET event_type = 'tampered' WHERE event_seq = ?",
                    (seq,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                writer.execute(
                    "DELETE FROM audit_event WHERE event_seq = ?",
                    (seq,),
                )
            # STRICT table rejects text in INTEGER recorded_at_ns column
            with self.assertRaises(sqlite3.IntegrityError):
                writer.execute(
                    """INSERT INTO audit_event (
                           company_id, domain, entity_type, entity_id, event_type,
                           actor_id, run_id, command_id, payload_json, recorded_at_ns
                       ) VALUES (?, 'd', 'e', 'id', 'ev', 'a', NULL, NULL, '{}', 'not-an-int')""",
                    (self.company_id,),
                )

    def test_command_receipt_idempotency_conflict_scoping_and_finalization(self):
        payload_hash = _digest('{"action":"suspend"}')
        preconditions = {
            "charter_revision": "charter-v1",
            "expected_control_revision": 2,
        }
        with closing(open_writer(self.store)) as writer:
            with write_tx(writer):
                first, replayed = admit_command_receipt(
                    writer,
                    company_id=self.company_id,
                    command_id="cmd-1",
                    actor_id="board-principal-a",
                    peer_pid=1234,
                    command_type="risk.suspend",
                    contract_version="v1",
                    payload_sha256=payload_hash,
                    preconditions=preconditions,
                    state="accepted",
                )
            self.assertFalse(replayed)
            self.assertEqual(first.state, "accepted")

            # Exact retry returns stored receipt
            with write_tx(writer):
                again, replayed2 = admit_command_receipt(
                    writer,
                    company_id=self.company_id,
                    command_id="cmd-1",
                    actor_id="board-principal-a",
                    peer_pid=1234,
                    command_type="risk.suspend",
                    contract_version="v1",
                    payload_sha256=payload_hash,
                    preconditions=preconditions,
                    state="accepted",
                )
            self.assertTrue(replayed2)
            self.assertEqual(again, first)

            # Conflicting payload digest under same command_id is rejected
            with self.assertRaises(CommandConflict):
                with write_tx(writer):
                    admit_command_receipt(
                        writer,
                        company_id=self.company_id,
                        command_id="cmd-1",
                        actor_id="board-principal-a",
                        peer_pid=1234,
                        command_type="risk.suspend",
                        contract_version="v1",
                        payload_sha256=_digest('{"action":"resume"}'),
                        preconditions=preconditions,
                        state="accepted",
                    )

            # Finalize accepted -> applied
            with write_tx(writer):
                applied = finalize_command_receipt(
                    writer,
                    self.company_id,
                    "cmd-1",
                    state="applied",
                    result={"control_revision": 3},
                    applied_seq=5,
                )
            self.assertEqual(applied.state, "applied")
            self.assertEqual(applied.applied_seq, 5)

            # Persisted conflicting_revision denial replays identically
            with write_tx(writer):
                denied, replayed3 = admit_command_receipt(
                    writer,
                    company_id=self.company_id,
                    command_id="cmd-stale",
                    actor_id="board-principal-a",
                    peer_pid=1234,
                    command_type="risk.resume",
                    contract_version="v1",
                    payload_sha256=_digest('{"action":"resume"}'),
                    preconditions={
                        "charter_revision": "charter-v1",
                        "expected_control_revision": 1,
                    },
                    state="denied",
                    denial_code="conflicting_revision",
                    result={"actual_control_revision": 3},
                )
            self.assertFalse(replayed3)
            self.assertEqual(denied.denial_code, "conflicting_revision")

            # Principal and operation scoping on read
            fetched = read_command_receipt(
                writer,
                self.company_id,
                "cmd-1",
                principal="board-principal-a",
                operation="risk.suspend",
            )
            self.assertEqual(fetched, applied)
            with self.assertRaises(CommandUnauthorized):
                read_command_receipt(
                    writer,
                    self.company_id,
                    "cmd-1",
                    principal="board-principal-b",
                    operation="risk.suspend",
                )
            with self.assertRaises(CommandUnauthorized):
                read_command_receipt(
                    writer,
                    self.company_id,
                    "cmd-1",
                    principal="board-principal-a",
                    operation="risk.resume",
                )

    def test_external_action_outbox_idempotency_and_conflict(self):
        req_hash = _digest('{"order":"buy"}')
        with closing(open_writer(self.store)) as writer:
            with write_tx(writer):
                action, replayed = prepare_external_action(
                    writer,
                    idempotency_key="ext-1",
                    company_id=self.company_id,
                    connector="paper-venue",
                    operation="submit_order",
                    request_sha256=req_hash,
                    run_id="run-1",
                )
            self.assertFalse(replayed)
            self.assertEqual(action.status, "prepared")

            with write_tx(writer):
                same, replayed2 = prepare_external_action(
                    writer,
                    idempotency_key="ext-1",
                    company_id=self.company_id,
                    connector="paper-venue",
                    operation="submit_order",
                    request_sha256=req_hash,
                    run_id="run-1",
                )
            self.assertTrue(replayed2)
            self.assertEqual(same, action)

            with self.assertRaises(ExternalActionConflict):
                with write_tx(writer):
                    prepare_external_action(
                        writer,
                        idempotency_key="ext-1",
                        company_id=self.company_id,
                        connector="paper-venue",
                        operation="submit_order",
                        request_sha256=_digest('{"order":"sell"}'),
                    )

            with write_tx(writer):
                settled = settle_external_action(
                    writer,
                    "ext-1",
                    status="confirmed",
                    receipt={"fill_id": "f-1"},
                )
            self.assertEqual(settled.status, "confirmed")
            self.assertIn("f-1", settled.receipt_json)

    def test_concurrent_fresh_found_handles_wal_activation_contention(self):
        for trial in range(15):
            fresh = self.store.with_name(f"concurrent-{trial}.sqlite")
            terms = FoundingTerms("setup", "charter-v1", "manage", "profile-v1")
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(found, fresh, terms) for _ in range(4)]
                receipts = [future.result() for future in futures]
            self.assertEqual(len(set(receipts)), 1)
