"""Kernel persistence helpers for audit events, command receipts, and outbox actions."""

import json
import sqlite3
from dataclasses import dataclass
from time import time_ns
from typing import Any

from .db import StorageRefused


class CommandConflict(StorageRefused):
    """A command_id was reused with conflicting payload terms."""


class CommandUnauthorized(StorageRefused):
    """A caller attempted to inspect another principal's or operation's receipt."""


class ExternalActionConflict(StorageRefused):
    """An external action idempotency key was reused with conflicting terms."""


@dataclass(frozen=True)
class AuditEvent:
    event_seq: int
    company_id: str
    domain: str
    entity_type: str
    entity_id: str
    event_type: str
    actor_id: str
    run_id: str | None
    command_id: str | None
    payload_json: str
    recorded_at_ns: int


@dataclass(frozen=True)
class CommandReceipt:
    company_id: str
    command_id: str
    actor_id: str
    peer_pid: int | None
    command_type: str
    contract_version: str
    payload_sha256: str
    preconditions_json: str
    state: str
    denial_code: str | None
    result_json: str | None
    applied_seq: int | None
    created_at_ns: int
    finalized_at_ns: int | None


@dataclass(frozen=True)
class ExternalAction:
    idempotency_key: str
    company_id: str
    run_id: str | None
    command_id: str | None
    connector: str
    operation: str
    request_sha256: str
    status: str
    receipt_json: str | None
    created_at_ns: int
    updated_at_ns: int


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _require_nonempty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorageRefused(f"{field} must be a nonempty string")
    return value


def _require_sha256(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise StorageRefused(f"{field} must be a 64-character lowercase hex SHA-256")
    return value


def append_audit_event(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    domain: str,
    entity_type: str,
    entity_id: str,
    event_type: str,
    actor_id: str,
    payload: dict[str, Any],
    run_id: str | None = None,
    command_id: str | None = None,
    recorded_at_ns: int | None = None,
) -> int:
    """Append an immutable audit event and return its monotonic event_seq."""
    for value, name in (
        (company_id, "company_id"),
        (domain, "domain"),
        (entity_type, "entity_type"),
        (entity_id, "entity_id"),
        (event_type, "event_type"),
        (actor_id, "actor_id"),
    ):
        _require_nonempty(value, name)
    timestamp = time_ns() if recorded_at_ns is None else recorded_at_ns
    cursor = connection.execute(
        """INSERT INTO audit_event (
               company_id, domain, entity_type, entity_id, event_type,
               actor_id, run_id, command_id, payload_json, recorded_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            company_id,
            domain,
            entity_type,
            entity_id,
            event_type,
            actor_id,
            run_id,
            command_id,
            _canonical_json(payload),
            timestamp,
        ),
    )
    if cursor.lastrowid is None:
        raise StorageRefused("Failed to allocate audit_event sequence number")
    return int(cursor.lastrowid)


def max_event_seq(connection: sqlite3.Connection, company_id: str) -> int:
    """Return the highest committed audit_event sequence number for the company."""
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'audit_event'"
    ).fetchone()
    if has_table is None:
        return 0
    row = connection.execute(
        "SELECT COALESCE(MAX(event_seq), 0) FROM audit_event WHERE company_id = ?",
        (company_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def admit_command_receipt(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    command_id: str,
    actor_id: str,
    peer_pid: int | None,
    command_type: str,
    contract_version: str,
    payload_sha256: str,
    preconditions: dict[str, Any],
    state: str,
    denial_code: str | None = None,
    result: dict[str, Any] | None = None,
    applied_seq: int | None = None,
    now_ns: int | None = None,
) -> tuple[CommandReceipt, bool]:
    """Record or idempotently replay a typed control-plane command receipt.

    Returns (receipt, replayed). Reusing command_id with a different payload digest,
    actor, command_type, or preconditions raises CommandConflict.
    """
    for value, name in (
        (company_id, "company_id"),
        (command_id, "command_id"),
        (actor_id, "actor_id"),
        (command_type, "command_type"),
        (contract_version, "contract_version"),
    ):
        _require_nonempty(value, name)
    _require_sha256(payload_sha256, "payload_sha256")
    if state not in ("accepted", "applied", "denied"):
        raise StorageRefused(f"Unsupported command receipt state: {state}")
    preconditions_json = _canonical_json(preconditions)

    existing = connection.execute(
        """SELECT company_id, command_id, actor_id, peer_pid, command_type,
                  contract_version, payload_sha256, preconditions_json, state,
                  denial_code, result_json, applied_seq, created_at_ns, finalized_at_ns
           FROM command_receipt
           WHERE company_id = ? AND command_id = ?""",
        (company_id, command_id),
    ).fetchone()
    if existing is not None:
        prior = CommandReceipt(*existing)
        if (
            prior.actor_id != actor_id
            or prior.command_type != command_type
            or prior.contract_version != contract_version
            or prior.payload_sha256 != payload_sha256
            or prior.preconditions_json != preconditions_json
        ):
            raise CommandConflict(
                "Command ID was already recorded with conflicting terms"
            )
        return prior, True

    timestamp = time_ns() if now_ns is None else now_ns
    finalized_at = timestamp if state in ("applied", "denied") else None
    result_json = _canonical_json(result) if result is not None else None
    connection.execute(
        """INSERT INTO command_receipt (
               company_id, command_id, actor_id, peer_pid, command_type,
               contract_version, payload_sha256, preconditions_json, state,
               denial_code, result_json, applied_seq, created_at_ns, finalized_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            company_id,
            command_id,
            actor_id,
            peer_pid,
            command_type,
            contract_version,
            payload_sha256,
            preconditions_json,
            state,
            denial_code,
            result_json,
            applied_seq,
            timestamp,
            finalized_at,
        ),
    )
    receipt = CommandReceipt(
        company_id,
        command_id,
        actor_id,
        peer_pid,
        command_type,
        contract_version,
        payload_sha256,
        preconditions_json,
        state,
        denial_code,
        result_json,
        applied_seq,
        timestamp,
        finalized_at,
    )
    return receipt, False


def finalize_command_receipt(
    connection: sqlite3.Connection,
    company_id: str,
    command_id: str,
    *,
    state: str,
    denial_code: str | None = None,
    result: dict[str, Any] | None = None,
    applied_seq: int | None = None,
    now_ns: int | None = None,
) -> CommandReceipt:
    """Transition an accepted command receipt to applied or denied exactly once."""
    if state not in ("applied", "denied"):
        raise StorageRefused(f"Final receipt state must be applied or denied: {state}")
    timestamp = time_ns() if now_ns is None else now_ns
    result_json = _canonical_json(result) if result is not None else None
    changed = connection.execute(
        """UPDATE command_receipt
           SET state = ?, denial_code = ?, result_json = ?, applied_seq = ?, finalized_at_ns = ?
           WHERE company_id = ? AND command_id = ? AND state = 'accepted'""",
        (
            state,
            denial_code,
            result_json,
            applied_seq,
            timestamp,
            company_id,
            command_id,
        ),
    )
    if changed.rowcount != 1:
        raise StorageRefused("Command receipt is missing or already finalized")
    row = connection.execute(
        """SELECT company_id, command_id, actor_id, peer_pid, command_type,
                  contract_version, payload_sha256, preconditions_json, state,
                  denial_code, result_json, applied_seq, created_at_ns, finalized_at_ns
           FROM command_receipt
           WHERE company_id = ? AND command_id = ?""",
        (company_id, command_id),
    ).fetchone()
    if row is None:
        raise StorageRefused("Command receipt vanished after finalization")
    return CommandReceipt(*row)


def read_command_receipt(
    connection: sqlite3.Connection,
    company_id: str,
    command_id: str,
    *,
    principal: str,
    operation: str,
) -> CommandReceipt | None:
    """Read a command receipt only for the authenticated principal and operation."""
    _require_nonempty(company_id, "company_id")
    _require_nonempty(command_id, "command_id")
    _require_nonempty(principal, "principal")
    _require_nonempty(operation, "operation")
    row = connection.execute(
        """SELECT company_id, command_id, actor_id, peer_pid, command_type,
                  contract_version, payload_sha256, preconditions_json, state,
                  denial_code, result_json, applied_seq, created_at_ns, finalized_at_ns
           FROM command_receipt
           WHERE company_id = ? AND command_id = ?""",
        (company_id, command_id),
    ).fetchone()
    if row is None:
        return None
    receipt = CommandReceipt(*row)
    if (receipt.actor_id, receipt.command_type) != (principal, operation):
        raise CommandUnauthorized(
            "Command receipt does not belong to this principal and operation"
        )
    return receipt


def prepare_external_action(
    connection: sqlite3.Connection,
    *,
    idempotency_key: str,
    company_id: str,
    connector: str,
    operation: str,
    request_sha256: str,
    run_id: str | None = None,
    command_id: str | None = None,
    now_ns: int | None = None,
) -> tuple[ExternalAction, bool]:
    """Persist an idempotent external outbox intent prior to connector dispatch."""
    for value, name in (
        (idempotency_key, "idempotency_key"),
        (company_id, "company_id"),
        (connector, "connector"),
        (operation, "operation"),
    ):
        _require_nonempty(value, name)
    _require_sha256(request_sha256, "request_sha256")
    existing = connection.execute(
        """SELECT idempotency_key, company_id, run_id, command_id, connector,
                  operation, request_sha256, status, receipt_json,
                  created_at_ns, updated_at_ns
           FROM external_actions
           WHERE idempotency_key = ?""",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        prior = ExternalAction(*existing)
        if (
            prior.company_id != company_id
            or prior.connector != connector
            or prior.operation != operation
            or prior.request_sha256 != request_sha256
        ):
            raise ExternalActionConflict(
                "External action idempotency key reused with conflicting terms"
            )
        return prior, True
    timestamp = time_ns() if now_ns is None else now_ns
    connection.execute(
        """INSERT INTO external_actions (
               idempotency_key, company_id, run_id, command_id, connector,
               operation, request_sha256, status, receipt_json,
               created_at_ns, updated_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, ?, ?)""",
        (
            idempotency_key,
            company_id,
            run_id,
            command_id,
            connector,
            operation,
            request_sha256,
            timestamp,
            timestamp,
        ),
    )
    return (
        ExternalAction(
            idempotency_key,
            company_id,
            run_id,
            command_id,
            connector,
            operation,
            request_sha256,
            "prepared",
            None,
            timestamp,
            timestamp,
        ),
        False,
    )


def settle_external_action(
    connection: sqlite3.Connection,
    idempotency_key: str,
    *,
    status: str,
    receipt: dict[str, Any] | None = None,
    now_ns: int | None = None,
) -> ExternalAction:
    """Update an external outbox action's status and receipt."""
    if status not in ("in_flight", "confirmed", "rejected", "unknown"):
        raise StorageRefused(f"Unsupported external action status: {status}")
    timestamp = time_ns() if now_ns is None else now_ns
    receipt_json = _canonical_json(receipt) if receipt is not None else None
    changed = connection.execute(
        """UPDATE external_actions
           SET status = ?, receipt_json = COALESCE(?, receipt_json), updated_at_ns = ?
           WHERE idempotency_key = ?""",
        (status, receipt_json, timestamp, idempotency_key),
    )
    if changed.rowcount != 1:
        raise StorageRefused("External action not found")
    row = connection.execute(
        """SELECT idempotency_key, company_id, run_id, command_id, connector,
                  operation, request_sha256, status, receipt_json,
                  created_at_ns, updated_at_ns
           FROM external_actions
           WHERE idempotency_key = ?""",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        raise StorageRefused("External action vanished after update")
    return ExternalAction(*row)
