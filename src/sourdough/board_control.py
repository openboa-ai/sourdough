"""Authoritative Board Client authority, CEO direct messaging, approvals, and restrictions.

Owns board_binding, ceo_message, decision_question, restriction_cause, and agent_profile.
All state-mutating Board commands persist idempotent command_receipt rows and redacted
append-only audit_event entries inside a single BEGIN IMMEDIATE SQLite transaction.
Routine employee retirement remains strictly CEO-delegated (no retire_employee command).
"""

from __future__ import annotations

import hmac
import json
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from time import time_ns
from typing import Any
from uuid import uuid4

from .company import _identity
from .runtime.domains import (
    RestrictionClearContext,
    RestrictionClearHook,
    RestrictionGuard,
)
from .runtime.work_kernel import (
    WorkKernelRefused,
    cancel_work_item,
    interrupt_work_run,
)
from .storage.checkpoint import CheckpointRefused, checkpoint_store
from .storage.db import scoped_reader, write_uow
from .storage.kernel import (
    CommandConflict,
    CommandReceipt,
    CommandUnauthorized,
    admit_command_receipt,
    append_audit_event,
    read_command_receipt,
)
from .writer_lease import WriterLease

AuthorizeBoard = Callable[[Connection, str, str], str | None]

CEO_CHAT_COMMAND = "ceo-chat-command-v1"
SEND_CEO_MESSAGE = "send_ceo_message"

DECISION_COMMAND = "decision-command-v1"
SUBMIT_DECISION_ANSWER = "submit_decision_answer"

RESTRICTION_COMMAND = "restriction-command-v1"
PAUSE_COMPANY = "pause_company"
RESUME_COMPANY = "resume_company"

WORK_COMMAND = "work-command-v1"
INTERRUPT_RUN = "interrupt_run"
CANCEL_WORK = "cancel_work"

CHECKPOINT_COMMAND = "checkpoint-command-v1"
CUT_CHECKPOINT = "cut_checkpoint"

RBAC_COMMAND = "rbac-command-v1"
REGISTER_OWNER = "register_owner"
BIND_BOARD_PRINCIPAL = "bind_board_principal"

BOARD_VIEWER_ROLE = "board_viewer"
BOARD_CONTROL_ROLE = "board_control"
_VALID_ROLES = frozenset({BOARD_VIEWER_ROLE, BOARD_CONTROL_ROLE})
VALID_APPLICATION_STATUSES = frozenset(
    {"pending", "applied", "declined", "not_applicable", "failed"}
)
VALID_DELIVERY_STATUSES = frozenset({"pending", "delivered", "acknowledged"})

ALLOWED_AVATAR_VARIANTS = frozenset(
    {
        "wide-oval-top",
        "wide-oval-bottom",
        "wide-oval-right",
        "wide-oval-left",
        "low-dome-bottom",
        "low-dome-left",
        "low-dome-right",
        "round-triangle-bottom",
        "round-triangle-right",
        "round-triangle-left",
        "tall-capsule-top",
        "tall-capsule-bottom",
        "tall-capsule-right",
        "tall-capsule-left",
        "round-diamond-left",
        "round-diamond-right",
        "wide-base-top",
        "wide-base-bottom",
        "wide-base-right",
        "wide-crown-top",
        "wide-crown-left",
        "wide-crown-right",
        "tilted-oval-top",
        "tilted-oval-bottom",
        "tilted-oval-left",
        "drop-bottom",
        "drop-right",
        "drop-left",
        "round-star-right",
        "round-star-left",
        "bean-top",
        "bean-bottom",
        "bean-left",
        "round-pentagon-bottom",
        "round-pentagon-right",
        "round-pentagon-left",
        "round-heart-right",
        "round-heart-left",
    }
)
_ALLOWED_AVATAR_VARIANTS = ALLOWED_AVATAR_VARIANTS


class BoardControlRefused(Exception):
    """A Board Client governance or control request was invalid or refused."""


class BoardControlForbidden(BoardControlRefused):
    """The caller lacks required board_control authority or targeted a disallowed entity."""


class BoardControlConflict(BoardControlRefused):
    """A command_id was reused with conflicting payload or preconditions."""


@dataclass(frozen=True)
class BoardAccess:
    bootstrap_locked: bool
    principal_id: str | None
    role: str | None
    binding_revision: int


@dataclass(frozen=True)
class BoardCommandOutcome:
    command_id: str
    command_type: str
    state: str
    disposition: str
    denial_code: str | None
    effective_revision: int
    recorded_at_ns: int
    peer_pid: int | None
    result: dict[str, Any]


@dataclass(frozen=True)
class _AuditSpec:
    domain: str
    entity_type: str
    entity_id: str
    event_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class _CommandExecution:
    state: str
    effective_revision: int | None = None
    denial_code: str | None = None
    applied_seq: int | None = None
    extra_result: dict[str, Any] | None = None
    audit: _AuditSpec | None = None


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _require_text(value: Any, field: str, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise BoardControlRefused(f"Invalid or missing {field}")
    return value.strip()


def _require_nonneg_int(value: Any, field: str) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise BoardControlRefused(f"Invalid {field}")
    return value


def _current_charter_revision(connection: Connection, company_id: str) -> str:
    row = connection.execute(
        "SELECT charter_revision FROM company WHERE id = ?",
        (company_id,),
    ).fetchone()
    if row is None:
        raise BoardControlRefused("Company identity not found")
    return str(row[0])


def _founder_agent_id(connection: Connection, company_id: str) -> str:
    row = connection.execute(
        "SELECT founder_id FROM founding_receipt WHERE company_id = ?",
        (company_id,),
    ).fetchone()
    if row is None:
        raise BoardControlRefused("Company founder record is missing")
    return str(row[0])


def _mark_inbound_ceo_message_delivered_in_tx(
    db: Connection,
    *,
    company_id: str,
    message_id: str,
    timestamp: int,
    founder_id: str | None = None,
) -> bool:
    cursor = db.execute(
        """UPDATE ceo_message
           SET delivery_state = 'delivered'
           WHERE company_id = ? AND message_id = ?
             AND sender_kind = 'board' AND delivery_state != 'delivered'""",
        (company_id, message_id),
    )
    updated = cursor.rowcount > 0
    if updated:
        actor = _founder_agent_id(db, company_id) if founder_id is None else founder_id
        append_audit_event(
            db,
            company_id=company_id,
            domain="messages",
            entity_type="ceo_message",
            entity_id=message_id,
            event_type="ceo.message_delivered",
            actor_id=actor,
            payload={"message_id": message_id},
            recorded_at_ns=timestamp,
        )
    return updated


def _upsert_active_restriction_in_tx(
    db: Connection,
    *,
    exists: bool,
    company_id: str,
    restriction_id: str,
    cause_category: str,
    scope_ref: str,
    next_revision: int,
    operator_releasable: bool,
    reason: str,
    timestamp: int,
    last_command_id: str | None,
) -> None:
    if not exists:
        db.execute(
            """INSERT INTO restriction_cause (
                   restriction_id, company_id, cause_category, scope_ref,
                   restriction_revision, active, operator_releasable, reason,
                   recorded_at_ns, cleared_at_ns, last_command_id
               ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, ?)""",
            (
                restriction_id,
                company_id,
                cause_category,
                scope_ref,
                next_revision,
                int(operator_releasable),
                reason,
                timestamp,
                last_command_id,
            ),
        )
    else:
        db.execute(
            """UPDATE restriction_cause
               SET cause_category = ?, scope_ref = ?, restriction_revision = ?,
                   active = 1, operator_releasable = ?, reason = ?,
                   recorded_at_ns = ?, cleared_at_ns = NULL, last_command_id = ?
               WHERE company_id = ? AND restriction_id = ?""",
            (
                cause_category,
                scope_ref,
                next_revision,
                int(operator_releasable),
                reason,
                timestamp,
                last_command_id,
                company_id,
                restriction_id,
            ),
        )


def _record_board_receipt(
    db: Connection,
    *,
    company_id: str,
    command_id: str,
    actor_id: str,
    peer_pid: int | None,
    command_type: str,
    contract_version: str,
    payload_sha256: str,
    preconditions: dict[str, Any],
    timestamp: int,
    execution: _CommandExecution,
) -> BoardCommandOutcome:
    applied_seq = execution.applied_seq
    if execution.audit is not None:
        applied_seq = append_audit_event(
            db,
            company_id=company_id,
            domain=execution.audit.domain,
            entity_type=execution.audit.entity_type,
            entity_id=execution.audit.entity_id,
            event_type=execution.audit.event_type,
            actor_id=actor_id,
            command_id=command_id,
            payload=execution.audit.payload,
            recorded_at_ns=timestamp,
        )
    disposition = (
        "applied"
        if execution.state == "applied"
        else (execution.denial_code or "denied")
    )
    effective_revision = (
        (applied_seq or 0)
        if execution.effective_revision is None
        else execution.effective_revision
    )
    result: dict[str, Any] = {
        "disposition": disposition,
        "effective_revision": effective_revision,
        "recorded_at_ns": timestamp,
    }
    if execution.extra_result:
        result.update(execution.extra_result)
    try:
        receipt, _ = admit_command_receipt(
            db,
            company_id=company_id,
            command_id=command_id,
            actor_id=actor_id,
            peer_pid=peer_pid,
            command_type=command_type,
            contract_version=contract_version,
            payload_sha256=payload_sha256,
            preconditions=preconditions,
            state=execution.state,
            denial_code=execution.denial_code,
            result=result,
            applied_seq=applied_seq,
            now_ns=timestamp,
        )
    except CommandConflict as error:
        raise BoardControlConflict(
            "Command ID is already in use with conflicting terms"
        ) from error
    return _outcome_from_receipt(receipt)


def _run_idempotent_board_command(
    writer: WriterLease,
    *,
    command_id: str,
    command_type: str,
    contract_version: str,
    preconditions: dict[str, Any],
    payload: dict[str, Any],
    authorize: AuthorizeBoard | None,
    principal_hint: str | None,
    peer_pid: int | None,
    now_ns: int | None,
    execute: Callable[[Connection, str, int], _CommandExecution],
    expected_charter_revision: str | None = None,
    before_replay: Callable[[Connection, str], None] | None = None,
    prepare_outside_tx: Callable[[], None] | None = None,
) -> BoardCommandOutcome:
    """Authorize, check idempotent replay/conflicts, execute, audit, and record a Board command."""
    payload_hash = _canonical_sha256(payload)
    timestamp = time_ns() if now_ns is None else now_ns
    conflict_exec = _CommandExecution(
        state="denied",
        denial_code="conflicting_revision",
        effective_revision=0,
    )

    def _authorize(db: Connection) -> str:
        return _require_board_control(
            db,
            writer.company_id,
            authorize_board=authorize,
            operation=command_type,
            principal_hint=principal_hint,
        )

    def _check_replay(db: Connection, principal: str) -> BoardCommandOutcome | None:
        if before_replay is not None:
            before_replay(db, principal)
        return _maybe_replay_receipt(
            db,
            company_id=writer.company_id,
            command_id=command_id,
            actor_id=principal,
            command_type=command_type,
            contract_version=contract_version,
            payload_sha256=payload_hash,
            preconditions=preconditions,
        )

    def _charter_mismatched(db: Connection) -> bool:
        return (
            expected_charter_revision is not None
            and _current_charter_revision(db, writer.company_id)
            != expected_charter_revision
        )

    def _admit(
        db: Connection, principal: str, execution: _CommandExecution
    ) -> BoardCommandOutcome:
        return _record_board_receipt(
            db,
            company_id=writer.company_id,
            command_id=command_id,
            actor_id=principal,
            peer_pid=peer_pid,
            command_type=command_type,
            contract_version=contract_version,
            payload_sha256=payload_hash,
            preconditions=preconditions,
            timestamp=timestamp,
            execution=execution,
        )

    if prepare_outside_tx is not None:
        writer.verify()
        with scoped_reader(writer.store) as db:
            principal = _authorize(db)
            replayed = _check_replay(db, principal)
            if replayed is not None:
                return replayed
            charter_mismatch = _charter_mismatched(db)
        if charter_mismatch:
            with write_uow(writer) as db:
                return _admit(db, principal, conflict_exec)
        prepare_outside_tx()
        with write_uow(writer) as db:
            principal = _authorize(db)
            execution = (
                conflict_exec
                if _charter_mismatched(db)
                else execute(db, principal, timestamp)
            )
            return _admit(db, principal, execution)

    with write_uow(writer) as db:
        principal = _authorize(db)
        replayed = _check_replay(db, principal)
        if replayed is not None:
            return replayed
        execution = (
            conflict_exec
            if _charter_mismatched(db)
            else execute(db, principal, timestamp)
        )
        return _admit(db, principal, execution)


def resolve_board_access(
    connection: Connection,
    company_id: str,
    *,
    authorize_board: AuthorizeBoard | None = None,
    operation: str = "inspect",
    principal_hint: str | None = None,
) -> BoardAccess:
    """Resolve current board principal and role fail-closed inside a SQLite snapshot."""
    if _identity(connection) != company_id:
        raise BoardControlForbidden("Company identity mismatch")

    if authorize_board is not None:
        principal = authorize_board(connection, company_id, operation)
        if isinstance(principal, str) and principal.strip():
            return BoardAccess(
                bootstrap_locked=False,
                principal_id=principal.strip(),
                role=BOARD_CONTROL_ROLE,
                binding_revision=1,
            )
        return BoardAccess(
            bootstrap_locked=False,
            principal_id=None,
            role=None,
            binding_revision=0,
        )

    total_ever_bound = int(
        connection.execute(
            "SELECT COUNT(*) FROM board_binding WHERE company_id = ?",
            (company_id,),
        ).fetchone()[0]
    )
    if total_ever_bound == 0:
        return BoardAccess(
            bootstrap_locked=True,
            principal_id=None,
            role=None,
            binding_revision=0,
        )

    rows = connection.execute(
        """SELECT principal_id, role_name, binding_revision
           FROM board_binding
           WHERE company_id = ? AND revoked_at_ns IS NULL
           ORDER BY CASE role_name WHEN 'board_viewer' THEN 0 ELSE 1 END,
                    bound_at_ns ASC, principal_id ASC""",
        (company_id,),
    ).fetchall()
    if not rows:
        return BoardAccess(
            bootstrap_locked=False,
            principal_id=None,
            role=None,
            binding_revision=0,
        )
    if principal_hint is not None:
        for row in rows:
            if str(row[0]) == principal_hint:
                return BoardAccess(
                    bootstrap_locked=False,
                    principal_id=str(row[0]),
                    role=str(row[1]),
                    binding_revision=int(row[2]),
                )
        return BoardAccess(
            bootstrap_locked=False,
            principal_id=None,
            role=None,
            binding_revision=0,
        )
    # Least-privilege default when principal_hint is omitted: if a single principal
    # is bound, resolve it; if multiple roles exist, prefer board_viewer over board_control.
    top = rows[0]
    return BoardAccess(
        bootstrap_locked=False,
        principal_id=str(top[0]),
        role=str(top[1]),
        binding_revision=int(top[2]),
    )


def _require_board_control(
    connection: Connection,
    company_id: str,
    *,
    authorize_board: AuthorizeBoard | None,
    operation: str,
    principal_hint: str | None = None,
) -> str:
    access = resolve_board_access(
        connection,
        company_id,
        authorize_board=authorize_board,
        operation=operation,
        principal_hint=principal_hint,
    )
    if access.bootstrap_locked:
        raise BoardControlForbidden(
            "Company is bootstrap-locked until an initial owner is bound"
        )
    if access.role != BOARD_CONTROL_ROLE or not access.principal_id:
        raise BoardControlForbidden(
            "Current board_control authority is required for this command"
        )
    return access.principal_id


def make_board_authorizer(
    *,
    authorize_board: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
) -> AuthorizeBoard:
    """Create an AuthorizeBoard callback backed by board_binding."""

    def _authorize(
        connection: Connection, company_id: str, operation: str
    ) -> str | None:
        try:
            return _require_board_control(
                connection,
                company_id,
                authorize_board=authorize_board,
                operation=operation,
                principal_hint=principal_hint,
            )
        except BoardControlForbidden:
            return None

    return _authorize


def bind_board_principal(
    writer: WriterLease,
    *,
    principal_id: str,
    role_name: str,
    scope_ref: str = "company:all",
    actor_id: str = "system",
    now_ns: int | None = None,
) -> BoardAccess:
    """Bind or update a Board principal role directly under WriterLease."""
    safe_principal = _require_text(principal_id, "principal_id", max_len=128)
    safe_scope = _require_text(scope_ref, "scope_ref", max_len=128)
    safe_actor = _require_text(actor_id, "actor_id", max_len=128)
    if role_name not in _VALID_ROLES:
        raise BoardControlRefused("Unsupported board role")
    timestamp = time_ns() if now_ns is None else now_ns
    with write_uow(writer) as db:
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        existing = db.execute(
            """SELECT binding_revision FROM board_binding
               WHERE company_id = ? AND principal_id = ?""",
            (writer.company_id, safe_principal),
        ).fetchone()
        if existing is None:
            revision = 1
            db.execute(
                """INSERT INTO board_binding (
                       principal_id, company_id, role_name, scope_ref,
                       binding_revision, bound_at_ns, revoked_at_ns
                   ) VALUES (?, ?, ?, ?, 1, ?, NULL)""",
                (safe_principal, writer.company_id, role_name, safe_scope, timestamp),
            )
        else:
            revision = int(existing[0]) + 1
            db.execute(
                """UPDATE board_binding
                   SET role_name = ?, scope_ref = ?, binding_revision = ?,
                       bound_at_ns = ?, revoked_at_ns = NULL
                   WHERE company_id = ? AND principal_id = ?""",
                (
                    role_name,
                    safe_scope,
                    revision,
                    timestamp,
                    writer.company_id,
                    safe_principal,
                ),
            )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="governance",
            entity_type="board_binding",
            entity_id=sha256(safe_principal.encode("utf-8")).hexdigest()[:16],
            event_type="governance.board_principal_bound",
            actor_id=safe_actor,
            payload={"role_name": role_name, "binding_revision": revision},
            recorded_at_ns=timestamp,
        )
    return BoardAccess(
        bootstrap_locked=False,
        principal_id=safe_principal,
        role=role_name,
        binding_revision=revision,
    )


def register_owner_command(
    writer: WriterLease,
    *,
    command_id: str,
    principal_id: str,
    challenge: str,
    proof: str,
    expected_challenge: str,
    peer_pid: int | None = None,
    now_ns: int | None = None,
) -> BoardCommandOutcome:
    """Bind the initial board_control owner while the company is bootstrap_locked."""
    safe_cmd = _require_text(command_id, "command_id", max_len=128)
    safe_principal = _require_text(principal_id, "principal_id", max_len=128)
    safe_challenge = _require_text(challenge, "challenge", max_len=128)
    safe_proof = _require_text(proof, "proof", max_len=128)
    expected_proof = sha256(
        f"{expected_challenge}:{writer.company_id}".encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(
        safe_challenge, expected_challenge
    ) or not hmac.compare_digest(safe_proof, expected_proof):
        raise BoardControlForbidden("Owner bootstrap proof verification failed")

    preconditions = {"bootstrap_state": "locked"}
    payload_hash = _canonical_sha256({"principal_id": safe_principal})
    timestamp = time_ns() if now_ns is None else now_ns
    with write_uow(writer) as db:
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        prior = _fetch_command_receipt(db, writer.company_id, safe_cmd)
        if prior is not None:
            if (
                prior.actor_id != safe_principal
                or prior.command_type != REGISTER_OWNER
                or prior.contract_version != RBAC_COMMAND
                or prior.payload_sha256 != payload_hash
            ):
                raise BoardControlConflict(
                    "Command ID is already in use with conflicting terms"
                )
            return _outcome_from_receipt(prior)

        bound_count = int(
            db.execute(
                "SELECT COUNT(*) FROM board_binding WHERE company_id = ?",
                (writer.company_id,),
            ).fetchone()[0]
        )
        if bound_count > 0:
            raise BoardControlForbidden("Company owner is already registered")

        db.execute(
            """INSERT INTO board_binding (
                   principal_id, company_id, role_name, scope_ref,
                   binding_revision, bound_at_ns, revoked_at_ns
               ) VALUES (?, ?, 'board_control', 'company:all', 1, ?, NULL)""",
            (safe_principal, writer.company_id, timestamp),
        )
        return _record_board_receipt(
            db,
            company_id=writer.company_id,
            command_id=safe_cmd,
            actor_id=safe_principal,
            peer_pid=peer_pid,
            command_type=REGISTER_OWNER,
            contract_version=RBAC_COMMAND,
            payload_sha256=payload_hash,
            preconditions=preconditions,
            timestamp=timestamp,
            execution=_CommandExecution(
                state="applied",
                effective_revision=1,
                extra_result={"role_name": BOARD_CONTROL_ROLE},
                audit=_AuditSpec(
                    domain="governance",
                    entity_type="board_binding",
                    entity_id=sha256(safe_principal.encode("utf-8")).hexdigest()[:16],
                    event_type="governance.owner_registered",
                    payload={"role_name": BOARD_CONTROL_ROLE, "binding_revision": 1},
                ),
            ),
        )


def upsert_agent_profile(
    writer: WriterLease,
    *,
    agent_id: str,
    display_name: str,
    role_label: str,
    avatar_variant: str = "wide-oval-top",
    monthly_budget_minor: int = 0,
    now_ns: int | None = None,
) -> None:
    """Persist display metadata and monthly operating budget for an existing agent."""
    safe_agent = _require_text(agent_id, "agent_id", max_len=128)
    safe_name = _require_text(display_name, "display_name", max_len=120)
    safe_role = _require_text(role_label, "role_label", max_len=80)
    if avatar_variant not in _ALLOWED_AVATAR_VARIANTS:
        raise BoardControlRefused("Unsupported agent avatar variant")
    safe_budget = _require_nonneg_int(monthly_budget_minor, "monthly_budget_minor")
    timestamp = time_ns() if now_ns is None else now_ns
    with write_uow(writer) as db:
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        exists = db.execute(
            "SELECT 1 FROM agent WHERE id = ? AND company_id = ?",
            (safe_agent, writer.company_id),
        ).fetchone()
        if exists is None:
            raise BoardControlRefused("Agent does not belong to this company")
        db.execute(
            """INSERT INTO agent_profile (
                   agent_id, company_id, display_name, role_label,
                   avatar_variant, monthly_budget_minor, updated_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                   display_name = excluded.display_name,
                   role_label = excluded.role_label,
                   avatar_variant = excluded.avatar_variant,
                   monthly_budget_minor = excluded.monthly_budget_minor,
                   updated_at_ns = excluded.updated_at_ns""",
            (
                safe_agent,
                writer.company_id,
                safe_name,
                safe_role,
                avatar_variant,
                safe_budget,
                timestamp,
            ),
        )


def post_ceo_message(
    writer: WriterLease,
    *,
    command_id: str,
    target_agent_id: str,
    charter_revision: str,
    body: str,
    expected_ceo_agent_id: str | None = None,
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
    context_kind: str | None = None,
    context_id: str | None = None,
    context_label: str | None = None,
    now_ns: int | None = None,
) -> BoardCommandOutcome:
    """Record an advisory Board -> CEO 1:1 message. Strictly refuses non-CEO targets."""
    safe_cmd = _require_text(command_id, "command_id", max_len=128)
    safe_target = _require_text(target_agent_id, "target_agent_id", max_len=128)
    safe_expected_ceo = (
        _require_text(expected_ceo_agent_id, "ceo_agent_id", max_len=128)
        if expected_ceo_agent_id is not None
        else safe_target
    )
    if safe_expected_ceo != safe_target:
        raise BoardControlForbidden(
            "Precondition ceo_agent_id must match payload target_agent_id"
        )
    safe_charter = _require_text(charter_revision, "charter_revision", max_len=128)
    safe_body = _require_text(body, "body", max_len=4000)
    safe_ctx_kind = (
        _require_text(context_kind, "context_kind", max_len=64)
        if context_kind is not None
        else None
    )
    safe_ctx_id = (
        _require_text(context_id, "context_id", max_len=128)
        if context_id is not None
        else None
    )
    safe_ctx_label = (
        _require_text(context_label, "context_label", max_len=240)
        if context_label is not None
        else None
    )

    def _require_ceo_target(db: Connection, _principal: str) -> None:
        if safe_target != _founder_agent_id(db, writer.company_id):
            raise BoardControlForbidden(
                "Advisory messages may only target the designated CEO agent"
            )

    def _execute(db: Connection, principal: str, timestamp: int) -> _CommandExecution:
        message_id = f"msg-{uuid4().hex[:16]}"
        db.execute(
            """INSERT INTO ceo_message (
                   message_id, company_id, sender_kind, sender_id, target_agent_id,
                   body, context_kind, context_id, context_label, command_id,
                   delivery_state, recorded_at_ns
               ) VALUES (?, ?, 'board', ?, ?, ?, ?, ?, ?, ?, 'recorded', ?)""",
            (
                message_id,
                writer.company_id,
                principal,
                safe_target,
                safe_body,
                safe_ctx_kind,
                safe_ctx_id,
                safe_ctx_label,
                safe_cmd,
                timestamp,
            ),
        )
        msg_count = int(
            db.execute(
                "SELECT COUNT(*) FROM ceo_message WHERE company_id = ?",
                (writer.company_id,),
            ).fetchone()[0]
        )
        return _CommandExecution(
            state="applied",
            effective_revision=msg_count,
            extra_result={"message_id": message_id},
            audit=_AuditSpec(
                domain="messages",
                entity_type="ceo_message",
                entity_id=message_id,
                event_type="board.ceo_message_sent",
                payload={
                    "message_id": message_id,
                    "target_agent_id": safe_target,
                    "has_context": safe_ctx_id is not None,
                },
            ),
        )

    return _run_idempotent_board_command(
        writer,
        command_id=safe_cmd,
        command_type=SEND_CEO_MESSAGE,
        contract_version=CEO_CHAT_COMMAND,
        preconditions={
            "ceo_agent_id": safe_target,
            "charter_revision": safe_charter,
        },
        payload={
            "target_agent_id": safe_target,
            "body": safe_body,
            "context_kind": safe_ctx_kind,
            "context_id": safe_ctx_id,
            "context_label": safe_ctx_label,
        },
        authorize=authorize,
        principal_hint=principal_hint,
        peer_pid=peer_pid,
        now_ns=now_ns,
        expected_charter_revision=safe_charter,
        before_replay=_require_ceo_target,
        execute=_execute,
    )


def record_ceo_reply(
    writer: WriterLease,
    *,
    body: str,
    context_kind: str | None = None,
    context_id: str | None = None,
    context_label: str | None = None,
    command_id: str | None = None,
    inbound_message_id: str | None = None,
    now_ns: int | None = None,
) -> str:
    """Record a CEO reply in the Board <-> CEO direct thread (idempotently when command_id is set)."""
    safe_body = _require_text(body, "body", max_len=4000)
    safe_command_id = (
        None
        if command_id is None
        else _require_text(command_id, "command_id", max_len=128)
    )
    safe_inbound_id = (
        None
        if inbound_message_id is None
        else _require_text(inbound_message_id, "inbound_message_id", max_len=128)
    )
    timestamp = time_ns() if now_ns is None else now_ns
    message_id = f"msg-{uuid4().hex[:16]}"
    with write_uow(writer) as db:
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        founder_id = _founder_agent_id(db, writer.company_id)
        if safe_command_id is not None:
            existing = db.execute(
                """SELECT message_id FROM ceo_message
                   WHERE company_id = ? AND command_id = ? AND sender_kind = 'ceo'
                   LIMIT 1""",
                (writer.company_id, safe_command_id),
            ).fetchone()
            if existing is not None:
                if safe_inbound_id is not None:
                    _mark_inbound_ceo_message_delivered_in_tx(
                        db,
                        company_id=writer.company_id,
                        message_id=safe_inbound_id,
                        timestamp=timestamp,
                        founder_id=founder_id,
                    )
                return str(existing[0])
        db.execute(
            """INSERT INTO ceo_message (
                   message_id, company_id, sender_kind, sender_id, target_agent_id,
                   body, context_kind, context_id, context_label, command_id,
                   delivery_state, recorded_at_ns
               ) VALUES (?, ?, 'ceo', ?, ?, ?, ?, ?, ?, ?, 'delivered', ?)""",
            (
                message_id,
                writer.company_id,
                founder_id,
                founder_id,
                safe_body,
                context_kind,
                context_id,
                context_label,
                safe_command_id,
                timestamp,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="messages",
            entity_type="ceo_message",
            entity_id=message_id,
            event_type="ceo.message_replied",
            actor_id=founder_id,
            payload={"message_id": message_id},
            recorded_at_ns=timestamp,
        )
        if safe_inbound_id is not None:
            _mark_inbound_ceo_message_delivered_in_tx(
                db,
                company_id=writer.company_id,
                message_id=safe_inbound_id,
                timestamp=timestamp,
                founder_id=founder_id,
            )
    return message_id


def mark_ceo_message_delivered(
    writer: WriterLease,
    *,
    message_id: str,
    now_ns: int | None = None,
) -> bool:
    """Mark an inbound Board -> CEO message as delivered once the CEO shift has handled it."""
    safe_mid = _require_text(message_id, "message_id", max_len=128)
    timestamp = time_ns() if now_ns is None else now_ns
    with write_uow(writer) as db:
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        return _mark_inbound_ceo_message_delivered_in_tx(
            db,
            company_id=writer.company_id,
            message_id=safe_mid,
            timestamp=timestamp,
        )


def record_decision_application_result(
    writer: WriterLease,
    *,
    question_id: str,
    application_status: str,
    delivery_status: str = "delivered",
    effect_summary: str = "",
    now_ns: int | None = None,
) -> None:
    """Record the durable application/delivery outcome for a resolved Board decision."""
    safe_qid = _require_text(question_id, "question_id", max_len=128)
    safe_app = _require_text(application_status, "application_status", max_len=64)
    safe_deliv = _require_text(delivery_status, "delivery_status", max_len=64)
    if not isinstance(effect_summary, str):
        raise BoardControlRefused("effect_summary must be a string")
    if safe_app not in VALID_APPLICATION_STATUSES:
        raise BoardControlRefused(f"Unsupported application_status: {safe_app}")
    if safe_deliv not in VALID_DELIVERY_STATUSES:
        raise BoardControlRefused(f"Unsupported delivery_status: {safe_deliv}")
    timestamp = time_ns() if now_ns is None else now_ns
    with write_uow(writer) as db:
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        row = db.execute(
            """SELECT state FROM decision_question
               WHERE company_id = ? AND question_id = ?""",
            (writer.company_id, safe_qid),
        ).fetchone()
        if row is None:
            raise BoardControlRefused("Decision question not found")
        db.execute(
            """UPDATE decision_question
               SET application_status = ?, delivery_status = ?
               WHERE company_id = ? AND question_id = ?""",
            (safe_app, safe_deliv, writer.company_id, safe_qid),
        )
        founder_id = _founder_agent_id(db, writer.company_id)
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="governance",
            entity_type="decision_question",
            entity_id=safe_qid,
            event_type="governance.decision_effect_applied",
            actor_id=founder_id,
            payload={
                "question_id": safe_qid,
                "application_status": safe_app,
                "delivery_status": safe_deliv,
                "effect_summary": effect_summary[:240],
            },
            recorded_at_ns=timestamp,
        )


def record_decision_question(
    writer: WriterLease,
    *,
    question_id: str,
    kind: str,
    title: str,
    summary: str,
    impact: dict[str, Any],
    options: tuple[str, ...] = ("approve", "decline"),
    created_by_agent_id: str,
    respond_by_ns: int | None = None,
    now_ns: int | None = None,
) -> str:
    """Create a pending Board decision question and return its canonical payload_hash."""
    safe_qid = _require_text(question_id, "question_id", max_len=128)
    safe_kind = _require_text(kind, "kind", max_len=64)
    safe_title = _require_text(title, "title", max_len=240)
    safe_summary = _require_text(summary, "summary", max_len=2000)
    safe_creator = _require_text(
        created_by_agent_id, "created_by_agent_id", max_len=128
    )
    if not isinstance(impact, dict) or not options:
        raise BoardControlRefused(
            "Decision question requires impact object and options"
        )
    impact_json = json.dumps(impact, sort_keys=True, separators=(",", ":"))
    options_json = json.dumps(list(options), separators=(",", ":"))
    payload_hash = _canonical_sha256(
        {
            "question_id": safe_qid,
            "kind": safe_kind,
            "title": safe_title,
            "summary": safe_summary,
            "impact": impact,
            "options": list(options),
        }
    )
    timestamp = time_ns() if now_ns is None else now_ns
    with write_uow(writer) as db:
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        db.execute(
            """INSERT INTO decision_question (
                   question_id, company_id, kind, title, summary, impact_json,
                   options_json, revision, payload_hash, state, respond_by_ns,
                   created_by_agent_id, created_at_ns, resolved_at_ns,
                   resolved_command_id, resolution_choice, resolution_reason,
                   application_status, delivery_status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 'open', ?, ?, ?, NULL, NULL, NULL, NULL, 'pending', 'pending')""",
            (
                safe_qid,
                writer.company_id,
                safe_kind,
                safe_title,
                safe_summary,
                impact_json,
                options_json,
                payload_hash,
                respond_by_ns,
                safe_creator,
                timestamp,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="governance",
            entity_type="decision_question",
            entity_id=safe_qid,
            event_type="governance.decision_opened",
            actor_id=safe_creator,
            payload={"kind": safe_kind, "revision": 1},
            recorded_at_ns=timestamp,
        )
    return payload_hash


def submit_decision_answer(
    writer: WriterLease,
    *,
    command_id: str,
    question_id: str,
    expected_revision: int,
    expected_payload_hash: str,
    choice: str,
    reason: str,
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
    now_ns: int | None = None,
) -> BoardCommandOutcome:
    """Resolve an open Board DecisionQuestion with optimistic revision & payload_hash check."""
    safe_cmd = _require_text(command_id, "command_id", max_len=128)
    safe_qid = _require_text(question_id, "question_id", max_len=128)
    safe_rev = _require_nonneg_int(expected_revision, "expected_revision")
    safe_hash = _require_text(
        expected_payload_hash, "expected_payload_hash", max_len=128
    )
    safe_choice = _require_text(choice, "choice", max_len=64)
    safe_reason = _require_text(reason, "reason", max_len=2000)

    def _execute(db: Connection, _principal: str, timestamp: int) -> _CommandExecution:
        row = db.execute(
            """SELECT revision, payload_hash, state, options_json, respond_by_ns
               FROM decision_question
               WHERE company_id = ? AND question_id = ?""",
            (writer.company_id, safe_qid),
        ).fetchone()
        if row is None:
            raise BoardControlRefused("Decision question not found")
        current_rev, current_hash, state, options_json, respond_by_ns = (
            int(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            None if row[4] is None else int(row[4]),
        )
        allowed_options = json.loads(options_json)
        if safe_choice not in allowed_options:
            raise BoardControlRefused("Unsupported decision choice")

        expired = respond_by_ns is not None and timestamp > respond_by_ns
        if (
            state != "open"
            or current_rev != safe_rev
            or current_hash != safe_hash
            or expired
        ):
            denial = (
                "expired" if (state == "open" and expired) else "conflicting_revision"
            )
            if state == "open" and expired:
                db.execute(
                    """UPDATE decision_question
                       SET state = 'expired', application_status = 'not_applicable'
                       WHERE company_id = ? AND question_id = ? AND state = 'open'""",
                    (writer.company_id, safe_qid),
                )
            return _CommandExecution(
                state="denied",
                denial_code=denial,
                effective_revision=current_rev,
            )

        next_rev = current_rev + 1
        next_state = "declined" if safe_choice == "decline" else "applied"
        app_status = "declined" if safe_choice == "decline" else "applied"
        db.execute(
            """UPDATE decision_question
               SET state = ?, revision = ?, resolved_at_ns = ?,
                   resolved_command_id = ?, resolution_choice = ?,
                   resolution_reason = ?, application_status = ?,
                   delivery_status = 'delivered'
               WHERE company_id = ? AND question_id = ? AND state = 'open'
                 AND revision = ?""",
            (
                next_state,
                next_rev,
                timestamp,
                safe_cmd,
                safe_choice,
                safe_reason,
                app_status,
                writer.company_id,
                safe_qid,
                current_rev,
            ),
        )
        return _CommandExecution(
            state="applied",
            effective_revision=next_rev,
            extra_result={
                "question_id": safe_qid,
                "decision_state": next_state,
            },
            audit=_AuditSpec(
                domain="governance",
                entity_type="decision_question",
                entity_id=safe_qid,
                event_type="governance.decision_answered",
                payload={
                    "choice": safe_choice,
                    "state": next_state,
                    "effective_revision": next_rev,
                },
            ),
        )

    return _run_idempotent_board_command(
        writer,
        command_id=safe_cmd,
        command_type=SUBMIT_DECISION_ANSWER,
        contract_version=DECISION_COMMAND,
        preconditions={
            "question_id": safe_qid,
            "expected_revision": safe_rev,
            "expected_payload_hash": safe_hash,
        },
        payload={"choice": safe_choice, "reason": safe_reason},
        authorize=authorize,
        principal_hint=principal_hint,
        peer_pid=peer_pid,
        now_ns=now_ns,
        execute=_execute,
    )


def apply_company_restriction(
    writer: WriterLease,
    *,
    command_id: str,
    restriction_id: str,
    charter_revision: str,
    expected_restriction_revision: int,
    cause_category: str,
    scope_ref: str,
    reason: str,
    operator_releasable: bool = True,
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
    now_ns: int | None = None,
    restriction_guards: Sequence[RestrictionGuard] = (),
) -> BoardCommandOutcome:
    """Activate a distinct cause-scoped restriction without altering other active stops."""
    safe_cmd = _require_text(command_id, "command_id", max_len=128)
    safe_rid = _require_text(restriction_id, "restriction_id", max_len=128)
    safe_charter = _require_text(charter_revision, "charter_revision", max_len=128)
    safe_rev = _require_nonneg_int(
        expected_restriction_revision, "expected_restriction_revision"
    )
    safe_cat = _require_text(cause_category, "cause_category", max_len=64).lower()
    safe_scope = _require_text(scope_ref, "scope_ref", max_len=128).lower()
    safe_reason = _require_text(reason, "reason", max_len=2000)
    if not isinstance(operator_releasable, bool):
        raise BoardControlRefused("operator_releasable must be a boolean")

    def _execute(db: Connection, _principal: str, timestamp: int) -> _CommandExecution:
        current_charter = _current_charter_revision(db, writer.company_id)
        existing = db.execute(
            """SELECT restriction_revision, active
               FROM restriction_cause
               WHERE company_id = ? AND restriction_id = ?""",
            (writer.company_id, safe_rid),
        ).fetchone()
        current_rev = 0 if existing is None else int(existing[0])
        if current_charter != safe_charter or current_rev != safe_rev:
            return _CommandExecution(
                state="denied",
                denial_code="conflicting_revision",
                effective_revision=current_rev,
            )

        next_rev = current_rev + 1
        matching_guards = [
            guard for guard in restriction_guards if guard.handles_scope(safe_scope)
        ]
        if (
            safe_scope.startswith("execution:") or safe_cat == "trading_risk_pause"
        ) and not matching_guards:
            raise BoardControlRefused(
                f"No domain guard registered for scoped restriction '{safe_scope}'"
            )
        for guard in matching_guards:
            guard.validate_apply(
                db,
                company_id=writer.company_id,
                scope_ref=safe_scope,
                cause_category=safe_cat,
            )

        _upsert_active_restriction_in_tx(
            db,
            exists=existing is not None,
            company_id=writer.company_id,
            restriction_id=safe_rid,
            cause_category=safe_cat,
            scope_ref=safe_scope,
            next_revision=next_rev,
            operator_releasable=operator_releasable,
            reason=safe_reason,
            timestamp=timestamp,
            last_command_id=safe_cmd,
        )
        return _CommandExecution(
            state="applied",
            effective_revision=next_rev,
            extra_result={"restriction_id": safe_rid},
            audit=_AuditSpec(
                domain="governance",
                entity_type="restriction_cause",
                entity_id=safe_rid,
                event_type="governance.restriction_applied",
                payload={
                    "cause_category": safe_cat,
                    "scope_ref": safe_scope,
                    "effective_revision": next_rev,
                },
            ),
        )

    return _run_idempotent_board_command(
        writer,
        command_id=safe_cmd,
        command_type=PAUSE_COMPANY,
        contract_version=RESTRICTION_COMMAND,
        preconditions={
            "charter_revision": safe_charter,
            "restriction_id": safe_rid,
            "expected_restriction_revision": safe_rev,
        },
        payload={
            "cause_category": safe_cat,
            "scope_ref": safe_scope,
            "reason": safe_reason,
            "operator_releasable": operator_releasable,
        },
        authorize=authorize,
        principal_hint=principal_hint,
        peer_pid=peer_pid,
        now_ns=now_ns,
        execute=_execute,
    )


def record_system_restriction(
    writer: WriterLease,
    *,
    restriction_id: str,
    cause_category: str,
    scope_ref: str,
    reason: str,
    operator_releasable: bool = True,
    actor_id: str = "system_safety",
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Idempotently activate a system-owned safety restriction (e.g., live venue drift).

    Records `last_command_id = NULL` and explicit system `actor_id` without spoofing
    a Board principal, surfacing immediately in Board Client Controls until cleared.
    """
    writer.verify()
    safe_rid = _require_text(restriction_id, "restriction_id", max_len=128)
    safe_cat = _require_text(cause_category, "cause_category", max_len=64)
    safe_scope = _require_text(scope_ref, "scope_ref", max_len=128)
    safe_reason = _require_text(reason, "reason", max_len=1000)
    safe_actor = _require_text(actor_id, "actor_id", max_len=128)
    timestamp = time_ns() if now_ns is None else now_ns

    with write_uow(writer) as db:
        if _identity(db) != writer.company_id:
            raise BoardControlRefused("Company identity mismatch")
        existing = db.execute(
            """SELECT restriction_revision, active, cause_category, scope_ref, reason
               FROM restriction_cause
               WHERE company_id = ? AND restriction_id = ?""",
            (writer.company_id, safe_rid),
        ).fetchone()
        if existing is not None and int(existing[1]) == 1:
            return {
                "restriction_id": safe_rid,
                "effective_revision": int(existing[0]),
                "active": True,
                "replayed": True,
                "scope_ref": str(existing[3]),
                "reason": str(existing[4]),
            }
        next_rev = (int(existing[0]) if existing is not None else 0) + 1
        _upsert_active_restriction_in_tx(
            db,
            exists=existing is not None,
            company_id=writer.company_id,
            restriction_id=safe_rid,
            cause_category=safe_cat,
            scope_ref=safe_scope,
            next_revision=next_rev,
            operator_releasable=operator_releasable,
            reason=safe_reason,
            timestamp=timestamp,
            last_command_id=None,
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="governance",
            entity_type="restriction_cause",
            entity_id=safe_rid,
            event_type="governance.system_restriction_applied",
            actor_id=safe_actor,
            payload={
                "cause_category": safe_cat,
                "scope_ref": safe_scope,
                "effective_revision": next_rev,
                "reason": safe_reason,
            },
            recorded_at_ns=timestamp,
        )
        return {
            "restriction_id": safe_rid,
            "effective_revision": next_rev,
            "active": True,
            "replayed": False,
            "scope_ref": safe_scope,
            "reason": safe_reason,
        }


def clear_company_restriction(
    writer: WriterLease,
    *,
    command_id: str,
    restriction_id: str,
    charter_revision: str,
    expected_restriction_revision: int,
    reason: str,
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
    now_ns: int | None = None,
    restriction_guards: Sequence[RestrictionGuard] = (),
    pre_clear_hooks: Sequence[RestrictionClearHook] = (),
    engine_directory: Path | None = None,
) -> BoardCommandOutcome:
    """Clear strictly one restriction_id, leaving all other active stops untouched."""
    safe_cmd = _require_text(command_id, "command_id", max_len=128)
    safe_rid = _require_text(restriction_id, "restriction_id", max_len=128)
    safe_charter = _require_text(charter_revision, "charter_revision", max_len=128)
    safe_rev = _require_nonneg_int(
        expected_restriction_revision, "expected_restriction_revision"
    )
    safe_reason = _require_text(reason, "reason", max_len=2000)

    def _execute(db: Connection, _principal: str, timestamp: int) -> _CommandExecution:
        current_charter = _current_charter_revision(db, writer.company_id)
        existing = db.execute(
            """SELECT restriction_revision, active, operator_releasable,
                      cause_category, scope_ref
               FROM restriction_cause
               WHERE company_id = ? AND restriction_id = ?""",
            (writer.company_id, safe_rid),
        ).fetchone()
        if existing is None:
            raise BoardControlRefused("Restriction cause not found")
        current_rev, active, releasable = (
            int(existing[0]),
            int(existing[1]),
            int(existing[2]),
        )
        cause_cat = str(existing[3]).strip().lower()
        scope_ref = str(existing[4]).strip().lower()
        if releasable == 0:
            raise BoardControlForbidden(
                "This restriction cause is system-held and cannot be cleared directly"
            )
        if current_charter != safe_charter or active != 1 or current_rev != safe_rev:
            return _CommandExecution(
                state="denied",
                denial_code="conflicting_revision",
                effective_revision=current_rev,
            )

        matching_guards = [
            guard for guard in restriction_guards if guard.handles_scope(scope_ref)
        ]
        if (
            (scope_ref.startswith("execution:") or cause_cat == "trading_risk_pause")
            and not matching_guards
            and not pre_clear_hooks
        ):
            raise BoardControlRefused(
                f"No domain guard loaded for scoped restriction '{scope_ref}'"
            )
        for guard in matching_guards:
            guard.validate_clear(
                db,
                company_id=writer.company_id,
                scope_ref=scope_ref,
                cause_category=cause_cat,
                now_ns=timestamp,
                engine_directory=engine_directory,
            )
        if pre_clear_hooks:
            ctx = RestrictionClearContext(
                connection=db,
                company_id=writer.company_id,
                restriction_id=safe_rid,
                cause_category=cause_cat,
                scope_ref=scope_ref,
                engine_directory=engine_directory,
                now_ns=timestamp,
            )
            for hook in pre_clear_hooks:
                hook(ctx)

        next_rev = current_rev + 1
        db.execute(
            """UPDATE restriction_cause
               SET active = 0, restriction_revision = ?,
                   cleared_at_ns = ?, last_command_id = ?
               WHERE company_id = ? AND restriction_id = ? AND active = 1""",
            (next_rev, timestamp, safe_cmd, writer.company_id, safe_rid),
        )
        remaining_active = int(
            db.execute(
                """SELECT COUNT(*) FROM restriction_cause
                   WHERE company_id = ? AND active = 1""",
                (writer.company_id,),
            ).fetchone()[0]
        )
        return _CommandExecution(
            state="applied",
            effective_revision=next_rev,
            extra_result={
                "restriction_id": safe_rid,
                "remaining_active_causes": remaining_active,
            },
            audit=_AuditSpec(
                domain="governance",
                entity_type="restriction_cause",
                entity_id=safe_rid,
                event_type="governance.restriction_cleared",
                payload={
                    "effective_revision": next_rev,
                    "remaining_active_causes": remaining_active,
                },
            ),
        )

    return _run_idempotent_board_command(
        writer,
        command_id=safe_cmd,
        command_type=RESUME_COMPANY,
        contract_version=RESTRICTION_COMMAND,
        preconditions={
            "charter_revision": safe_charter,
            "restriction_id": safe_rid,
            "expected_restriction_revision": safe_rev,
        },
        payload={"reason": safe_reason},
        authorize=authorize,
        principal_hint=principal_hint,
        peer_pid=peer_pid,
        now_ns=now_ns,
        execute=_execute,
    )


def execute_work_control_command(
    writer: WriterLease,
    *,
    command_id: str,
    command_type: str,
    preconditions: dict[str, Any],
    payload: dict[str, Any],
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
    now_ns: int | None = None,
) -> BoardCommandOutcome:
    """Interrupt a running work_run or cancel a pending work_item via work_kernel."""
    safe_cmd = _require_text(command_id, "command_id", max_len=128)
    safe_reason = _require_text(payload.get("reason"), "reason", max_len=2000)
    if command_type == INTERRUPT_RUN:
        if set(preconditions.keys()) != {"run_id", "expected_fencing_token"}:
            raise BoardControlRefused(
                "interrupt_run requires strictly run_id and expected_fencing_token preconditions"
            )
        canonical_pre: dict[str, Any] = {
            "run_id": _require_text(preconditions.get("run_id"), "run_id", max_len=128),
            "expected_fencing_token": _require_nonneg_int(
                preconditions.get("expected_fencing_token"),
                "expected_fencing_token",
            ),
        }
    elif command_type == CANCEL_WORK:
        if set(preconditions.keys()) != {"work_item_id", "expected_state"}:
            raise BoardControlRefused(
                "cancel_work requires strictly work_item_id and expected_state preconditions"
            )
        canonical_pre = {
            "work_item_id": _require_text(
                preconditions.get("work_item_id"), "work_item_id", max_len=128
            ),
            "expected_state": _require_text(
                preconditions.get("expected_state"), "expected_state", max_len=32
            ),
        }
    else:
        raise BoardControlRefused("Unsupported work control command")

    def _execute(db: Connection, principal: str, timestamp: int) -> _CommandExecution:
        try:
            if command_type == INTERRUPT_RUN:
                run_id = str(canonical_pre["run_id"])
                token = int(canonical_pre["expected_fencing_token"])
                applied, effective = interrupt_work_run(
                    db,
                    company_id=writer.company_id,
                    run_id=run_id,
                    expected_fencing_token=token,
                    actor_id=principal,
                    reason=safe_reason,
                    command_id=safe_cmd,
                    now_ns=timestamp,
                )
                result_extra: dict[str, Any] = {"run_id": run_id}
            else:
                work_item_id = str(canonical_pre["work_item_id"])
                expected_state = str(canonical_pre["expected_state"])
                applied, current_state = cancel_work_item(
                    db,
                    company_id=writer.company_id,
                    work_item_id=work_item_id,
                    expected_state=expected_state,
                    actor_id=principal,
                    reason=safe_reason,
                    command_id=safe_cmd,
                    now_ns=timestamp,
                )
                effective = 1 if applied else 0
                result_extra = {
                    "work_item_id": work_item_id,
                    "work_state": current_state,
                }
        except WorkKernelRefused as error:
            raise BoardControlRefused(str(error)) from error

        if not applied:
            return _CommandExecution(
                state="denied",
                denial_code="conflicting_revision",
                effective_revision=effective,
                extra_result=result_extra,
            )
        seq = int(
            db.execute(
                "SELECT COALESCE(MAX(event_seq), 1) FROM audit_event WHERE company_id = ?",
                (writer.company_id,),
            ).fetchone()[0]
        )
        return _CommandExecution(
            state="applied",
            effective_revision=max(effective, 1),
            applied_seq=seq,
            extra_result=result_extra,
        )

    return _run_idempotent_board_command(
        writer,
        command_id=safe_cmd,
        command_type=command_type,
        contract_version=WORK_COMMAND,
        preconditions=canonical_pre,
        payload={"reason": safe_reason},
        authorize=authorize,
        principal_hint=principal_hint,
        peer_pid=peer_pid,
        now_ns=now_ns,
        execute=_execute,
    )


def execute_cut_checkpoint_command(
    writer: WriterLease,
    *,
    command_id: str,
    charter_revision: str,
    reason: str,
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
    now_ns: int | None = None,
) -> BoardCommandOutcome:
    """Cut a verified checkpoint under <store.parent>/checkpoints/ with receipt tracking."""
    safe_cmd = _require_text(command_id, "command_id", max_len=128)
    safe_charter = _require_text(charter_revision, "charter_revision", max_len=128)
    safe_reason = _require_text(reason, "reason", max_len=2000)

    archive_path: Path | None = None
    manifest: Any = None

    def _prepare_checkpoint() -> None:
        nonlocal archive_path, manifest
        checkpoints_dir = writer.store.resolve(strict=False).parent / "checkpoints"
        if os.path.lexists(checkpoints_dir):
            st = os.lstat(checkpoints_dir)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise BoardControlRefused(
                    "Checkpoints directory must be a regular directory"
                )
        else:
            checkpoints_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_name = sha256(safe_cmd.encode("utf-8")).hexdigest()[:16]
        archive_path = checkpoints_dir / f"checkpoint-{safe_name}.zip"
        if os.path.lexists(archive_path) and stat.S_ISLNK(
            os.lstat(archive_path).st_mode
        ):
            raise BoardControlRefused("Refusing to write checkpoint through symlink")
        try:
            from .agent_desk import desks_root

            desks = desks_root(writer.store)
            manifest = checkpoint_store(
                writer.store,
                writer.company_id,
                archive_path,
                desk_root=desks if os.path.lexists(desks) else None,
                writer=writer,
            )
        except CheckpointRefused as error:
            raise BoardControlRefused(str(error)) from error

    def _execute(
        _db: Connection, _principal: str, _timestamp: int
    ) -> _CommandExecution:
        assert archive_path is not None and manifest is not None
        cut_payload = {
            "archive_name": archive_path.name,
            "sha256": manifest.sha256,
            "cut_id": manifest.cut_id,
        }
        return _CommandExecution(
            state="applied",
            effective_revision=None,
            extra_result=cut_payload,
            audit=_AuditSpec(
                domain="ops",
                entity_type="checkpoint",
                entity_id=manifest.cut_id,
                event_type="ops.checkpoint_cut",
                payload=cut_payload,
            ),
        )

    return _run_idempotent_board_command(
        writer,
        command_id=safe_cmd,
        command_type=CUT_CHECKPOINT,
        contract_version=CHECKPOINT_COMMAND,
        preconditions={"charter_revision": safe_charter},
        payload={"reason": safe_reason},
        authorize=authorize,
        principal_hint=principal_hint,
        peer_pid=peer_pid,
        now_ns=now_ns,
        expected_charter_revision=safe_charter,
        prepare_outside_tx=_prepare_checkpoint,
        execute=_execute,
    )


def read_scoped_command_receipt(
    store: Path,
    company_id: str,
    command_id: str,
    *,
    operation: str | None = None,
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
) -> dict[str, Any]:
    """Read a redacted command receipt strictly scoped to (principal, operation)."""
    safe_cmd = _require_text(command_id, "command_id", max_len=128)
    with scoped_reader(store) as db:
        if _identity(db) != company_id:
            raise BoardControlForbidden("Company identity mismatch")
        access = resolve_board_access(
            db,
            company_id,
            authorize_board=authorize,
            operation=operation or "read_receipt",
            principal_hint=principal_hint,
        )
        if operation is not None and access.principal_id is not None:
            try:
                receipt = read_command_receipt(
                    db,
                    company_id,
                    safe_cmd,
                    principal=access.principal_id,
                    operation=operation,
                )
            except CommandUnauthorized as error:
                raise BoardControlForbidden(str(error)) from error
        else:
            receipt = _fetch_command_receipt(db, company_id, safe_cmd)
            if receipt is not None:
                # Fail closed: an existing receipt MUST NOT be disclosed to an
                # unauthorized/unbound principal or without an explicit operation match.
                if access.principal_id is None or operation is None:
                    raise BoardControlForbidden(
                        "Authenticated principal and matching operation are required to inspect a command receipt"
                    )
                if (receipt.actor_id, receipt.command_type) != (
                    access.principal_id,
                    operation,
                ):
                    raise BoardControlForbidden(
                        "Command receipt does not belong to this principal and operation"
                    )
    if receipt is None:
        return {
            "status": "not_found",
            "company_id": company_id,
            "command_id": safe_cmd,
            "operation": operation,
        }
    outcome = _outcome_from_receipt(receipt)
    return {
        "status": "found",
        "company_id": company_id,
        "command_id": outcome.command_id,
        "command_type": outcome.command_type,
        "state": outcome.state,
        "disposition": outcome.disposition,
        "denial_code": outcome.denial_code,
        "effective_revision": outcome.effective_revision,
        "recorded_at_ns": outcome.recorded_at_ns,
        "peer_pid": outcome.peer_pid,
    }


def read_local_command_receipt_status(
    store: Path,
    company_id: str,
    command_id: str,
    *,
    operation: str | None = None,
) -> dict[str, object]:
    """Read a board command receipt status read-only without disclosing actor or private reason."""
    safe_cmd = _require_text(command_id, "command_id", max_len=128)
    safe_op = (
        _require_text(operation, "operation", max_len=128)
        if operation is not None
        else None
    )
    try:
        initial = store.stat(follow_symlinks=False)
    except OSError as error:
        raise BoardControlRefused("Company store is unavailable") from error
    if not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.geteuid():
        raise BoardControlRefused(
            "Only the local company store owner can inspect command receipts"
        )
    with scoped_reader(store) as connection:
        if _identity(connection) != company_id:
            raise BoardControlRefused("Company identity mismatch")
        receipt = _fetch_command_receipt(connection, company_id, safe_cmd)
        if receipt is None:
            response: dict[str, object] = {
                "status": "not_found",
                "company_id": company_id,
                "command_id": safe_cmd,
            }
        else:
            if safe_op is not None and receipt.command_type != safe_op:
                raise BoardControlRefused(
                    "Command receipt does not match the requested operation"
                )
            effective_revision: int | None = None
            if receipt.state in ("applied", "denied"):
                effective_revision = _outcome_from_receipt(receipt).effective_revision
            elif receipt.state != "accepted":
                raise BoardControlRefused("Unsupported board command receipt state")
            response = {
                "status": "found",
                "company_id": company_id,
                "command_id": safe_cmd,
                "command_type": receipt.command_type,
                "state": receipt.state,
                "denial_code": receipt.denial_code,
                "effective_revision": effective_revision,
                "applied_seq": receipt.applied_seq,
                "created_at_ns": int(receipt.created_at_ns),
                "finalized_at_ns": (
                    None
                    if receipt.finalized_at_ns is None
                    else int(receipt.finalized_at_ns)
                ),
            }
        try:
            current = store.stat(follow_symlinks=False)
        except OSError as error:
            raise BoardControlRefused(
                "Company store changed while inspecting command receipt"
            ) from error
        if (initial.st_dev, initial.st_ino, initial.st_uid) != (
            current.st_dev,
            current.st_ino,
            current.st_uid,
        ):
            raise BoardControlRefused(
                "Company store changed while inspecting command receipt"
            )
    return response


def _fetch_command_receipt(
    db: Connection, company_id: str, command_id: str
) -> CommandReceipt | None:
    row = db.execute(
        """SELECT company_id, command_id, actor_id, peer_pid, command_type,
                  contract_version, payload_sha256, preconditions_json, state,
                  denial_code, result_json, applied_seq, created_at_ns, finalized_at_ns
           FROM command_receipt
           WHERE company_id = ? AND command_id = ?""",
        (company_id, command_id),
    ).fetchone()
    return None if row is None else CommandReceipt(*row)


def _maybe_replay_receipt(
    db: Connection,
    *,
    company_id: str,
    command_id: str,
    actor_id: str,
    command_type: str,
    contract_version: str,
    payload_sha256: str,
    preconditions: dict[str, Any],
) -> BoardCommandOutcome | None:
    prior = _fetch_command_receipt(db, company_id, command_id)
    if prior is None:
        return None
    expected_pre = json.dumps(preconditions, sort_keys=True, separators=(",", ":"))
    if (
        prior.actor_id != actor_id
        or prior.command_type != command_type
        or prior.contract_version != contract_version
        or prior.payload_sha256 != payload_sha256
        or prior.preconditions_json != expected_pre
    ):
        raise BoardControlConflict(
            "Command ID is already in use with conflicting terms"
        )
    return _outcome_from_receipt(prior)


def _outcome_from_receipt(receipt: CommandReceipt) -> BoardCommandOutcome:
    parsed: dict[str, Any] = {}
    if receipt.result_json:
        try:
            loaded = json.loads(receipt.result_json)
            if isinstance(loaded, dict):
                parsed = loaded
        except json.JSONDecodeError:
            parsed = {}
    disposition = str(
        parsed.get(
            "disposition",
            "applied"
            if receipt.state == "applied"
            else (receipt.denial_code or "denied"),
        )
    )
    raw_rev = parsed.get("effective_revision")
    effective_rev = (
        raw_rev if type(raw_rev) is int and raw_rev >= 0 else (receipt.applied_seq or 0)
    )
    raw_ts = parsed.get("recorded_at_ns")
    recorded_ts = (
        raw_ts
        if type(raw_ts) is int and raw_ts > 0
        else (receipt.finalized_at_ns or receipt.created_at_ns)
    )
    return BoardCommandOutcome(
        command_id=receipt.command_id,
        command_type=receipt.command_type,
        state=receipt.state,
        disposition=disposition,
        denial_code=receipt.denial_code,
        effective_revision=effective_rev,
        recorded_at_ns=recorded_ts,
        peer_pid=receipt.peer_pid,
        result={
            k: v
            for k, v in parsed.items()
            if k not in ("principal", "reason", "actor_id")
        },
    )
