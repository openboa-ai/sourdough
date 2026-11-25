"""Apply board-authorized paper risk controls over core command_receipt and paper_risk_state."""

import json
import os
import stat
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from time import time_ns

from .company import _company_version, _identity
from .storage.db import StorageRefused, open_reader, open_writer, read_tx, write_tx
from .storage.kernel import (
    CommandConflict,
    CommandReceipt,
    CommandUnauthorized,
    admit_command_receipt,
    append_audit_event,
    read_command_receipt,
)
from .storage.schema import enable_domain_v1, is_domain_installed
from .writer_lease import WriterLease


PAPER_RISK_COMMAND = "paper-risk-command-v1"
SUSPEND_NEW_RISK = "suspend_new_risk"
RESUME_NEW_RISK = "resume_new_risk"
_RISK_OPERATIONS = (SUSPEND_NEW_RISK, RESUME_NEW_RISK)
AuthorizeBoard = Callable[[Connection, str, str], str | None]


class PaperRiskRefused(Exception):
    """The control is unsupported or cannot be authorized."""


@dataclass(frozen=True)
class PaperSuspension:
    request_id: str
    charter_revision: str
    expected_revision: int
    reason: str
    contract_revision: str = PAPER_RISK_COMMAND

    def __post_init__(self) -> None:
        if (
            self.contract_revision != PAPER_RISK_COMMAND
            or type(self.expected_revision) is not int
            or not 0 <= self.expected_revision < 2**63
            or not all(
                isinstance(value, str) and value.strip() and len(value) <= limit
                for value, limit in (
                    (self.request_id, 128),
                    (self.charter_revision, 128),
                    (self.reason, 2000),
                )
            )
        ):
            raise PaperRiskRefused("Unsupported paper risk command")


@dataclass(frozen=True)
class PaperRiskReceipt:
    request_id: str
    principal: str
    operation: str
    disposition: str
    effective_revision: int
    recorded_at_ns: int


@dataclass(frozen=True)
class PaperRiskState:
    suspended: bool
    applied: PaperRiskReceipt | None

    @property
    def revision(self) -> int:
        return self.applied.effective_revision if self.applied else 0


def _payload_digest(command: PaperSuspension) -> str:
    canonical = json.dumps(
        {
            "charter_revision": command.charter_revision,
            "expected_revision": command.expected_revision,
            "reason": command.reason,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _preconditions(command: PaperSuspension) -> dict[str, object]:
    return {
        "charter_revision": command.charter_revision,
        "expected_revision": command.expected_revision,
    }


def _receipt_from_command(receipt: CommandReceipt) -> PaperRiskReceipt:
    if receipt.command_type not in _RISK_OPERATIONS:
        raise PaperRiskRefused("Unsupported effective paper risk control")
    if receipt.state not in ("applied", "denied"):
        raise PaperRiskRefused("Unsupported effective paper risk control")
    if receipt.state == "applied" and (
        type(receipt.applied_seq) is not int or receipt.applied_seq <= 0
    ):
        raise PaperRiskRefused("Unsupported effective paper risk control")
    try:
        result = json.loads(receipt.result_json) if receipt.result_json else {}
    except json.JSONDecodeError as error:
        raise PaperRiskRefused("Corrupted paper risk command receipt") from error
    if not isinstance(result, dict):
        raise PaperRiskRefused("Corrupted paper risk command receipt")
    disposition = result.get("disposition")
    effective_revision = result.get("effective_revision")
    recorded_at_ns = result.get("recorded_at_ns")
    if (
        disposition not in ("applied", "conflicting_revision")
        or (receipt.state == "applied" and disposition != "applied")
        or (receipt.state == "denied" and disposition != "conflicting_revision")
        or type(effective_revision) is not int
        or effective_revision < 0
        or (disposition == "applied" and effective_revision < 1)
        or type(recorded_at_ns) is not int
        or recorded_at_ns <= 0
    ):
        raise PaperRiskRefused("Unsupported effective paper risk control")
    return PaperRiskReceipt(
        request_id=receipt.command_id,
        principal=receipt.actor_id,
        operation=receipt.command_type,
        disposition=disposition,
        effective_revision=effective_revision,
        recorded_at_ns=recorded_at_ns,
    )


def _fetch_command(
    connection: Connection, company_id: str, command_id: str
) -> CommandReceipt | None:
    row = connection.execute(
        """SELECT company_id, command_id, actor_id, peer_pid, command_type,
                  contract_version, payload_sha256, preconditions_json, state,
                  denial_code, result_json, applied_seq, created_at_ns, finalized_at_ns
           FROM command_receipt
           WHERE company_id = ? AND command_id = ?""",
        (company_id, command_id),
    ).fetchone()
    return CommandReceipt(*row) if row is not None else None


def effective_paper_risk(connection: Connection) -> PaperRiskState:
    company_id, _ = _company_version(connection)
    latest_row = connection.execute(
        """SELECT company_id, command_id, actor_id, peer_pid, command_type,
                  contract_version, payload_sha256, preconditions_json, state,
                  denial_code, result_json, applied_seq, created_at_ns, finalized_at_ns
           FROM command_receipt
           WHERE company_id = ?
             AND command_type IN ('suspend_new_risk', 'resume_new_risk')
             AND state = 'applied'
           ORDER BY applied_seq DESC, created_at_ns DESC
           LIMIT 1""",
        (company_id,),
    ).fetchone()
    latest_applied = (
        _receipt_from_command(CommandReceipt(*latest_row)) if latest_row else None
    )

    if not is_domain_installed(connection, "paper"):
        if latest_applied is not None:
            raise PaperRiskRefused("Paper risk state is missing for applied receipt")
        return PaperRiskState(False, None)

    state_row = connection.execute(
        """SELECT suspended, effective_revision, last_command_id, updated_at_ns
           FROM paper_risk_state
           WHERE company_id = ?""",
        (company_id,),
    ).fetchone()
    if state_row is None:
        if latest_applied is not None:
            raise PaperRiskRefused("Controlled store has no effective paper risk state")
        return PaperRiskState(False, None)

    suspended_int, effective_revision, last_command_id, updated_at_ns = state_row
    if (
        suspended_int not in (0, 1)
        or type(effective_revision) is not int
        or effective_revision < 0
        or type(updated_at_ns) is not int
        or updated_at_ns <= 0
    ):
        raise PaperRiskRefused("Unsupported effective paper risk control")
    if effective_revision == 0:
        if (
            suspended_int != 0
            or last_command_id is not None
            or latest_applied is not None
        ):
            raise PaperRiskRefused("Inconsistent zero-revision paper risk state")
        return PaperRiskState(False, None)

    if last_command_id is None or latest_applied is None:
        raise PaperRiskRefused("Controlled store has no effective paper risk receipt")
    linked = _fetch_command(connection, company_id, str(last_command_id))
    if linked is None or linked.state != "applied":
        raise PaperRiskRefused("Effective paper risk receipt is missing")
    applied = _receipt_from_command(linked)
    expected_suspended = applied.operation == SUSPEND_NEW_RISK
    if (
        bool(suspended_int) != expected_suspended
        or applied.effective_revision != effective_revision
        or latest_applied.request_id != applied.request_id
    ):
        raise PaperRiskRefused(
            "Effective paper risk state does not match its applied command receipt"
        )
    return PaperRiskState(expected_suspended, applied)


def _principal(
    connection: Connection,
    company_id: str,
    operation: str,
    authorize: AuthorizeBoard | None,
) -> str:
    principal = authorize(connection, company_id, operation) if authorize else None
    if not isinstance(principal, str) or not principal.strip() or len(principal) > 128:
        raise PaperRiskRefused("Current board risk control grant is required")
    return principal


@dataclass(frozen=True)
class _AuthorizedControl:
    principal: str
    operation: str
    command: PaperSuspension
    payload_sha256: str
    preconditions: dict[str, object]
    prior: PaperRiskReceipt | None
    current_revision: int
    eligible: bool


def _authorize_control(
    connection: Connection,
    writer: WriterLease,
    command: PaperSuspension,
    operation: str,
    authorize: AuthorizeBoard | None,
) -> _AuthorizedControl:
    writer.verify()
    principal = _principal(connection, writer.company_id, operation, authorize)
    company_id, _ = _company_version(connection)
    if company_id != writer.company_id:
        raise PaperRiskRefused("Company identity mismatch")
    payload_sha256 = _payload_digest(command)
    preconditions = _preconditions(command)
    preconditions_json = json.dumps(
        preconditions, sort_keys=True, separators=(",", ":")
    )
    existing = _fetch_command(connection, company_id, command.request_id)
    if existing is not None:
        if (
            existing.actor_id != principal
            or existing.command_type != operation
            or existing.contract_version != command.contract_revision
            or existing.payload_sha256 != payload_sha256
            or existing.preconditions_json != preconditions_json
        ):
            raise PaperRiskRefused("Request identity is already in use")
        receipt = _receipt_from_command(existing)
        return _AuthorizedControl(
            principal,
            operation,
            command,
            payload_sha256,
            preconditions,
            receipt,
            receipt.effective_revision,
            False,
        )
    state = effective_paper_risk(connection)
    (current_charter,) = connection.execute(
        "SELECT charter_revision FROM company WHERE singleton=1"
    ).fetchone()
    eligible = (command.charter_revision, command.expected_revision) == (
        current_charter,
        state.revision,
    ) and (operation == SUSPEND_NEW_RISK or state.suspended)
    return _AuthorizedControl(
        principal,
        operation,
        command,
        payload_sha256,
        preconditions,
        None,
        state.revision,
        eligible,
    )


def _append_control(
    connection: Connection,
    company_id: str,
    control: _AuthorizedControl,
    *,
    applied: bool,
    peer_pid: int | None = None,
) -> PaperRiskReceipt:
    if peer_pid is not None and (type(peer_pid) is not int or peer_pid <= 0):
        raise PaperRiskRefused("Invalid peer process identifier")
    effective_peer_pid = os.getpid() if peer_pid is None else peer_pid
    now_ns = time_ns()
    next_revision = control.current_revision + int(applied)
    disposition = "applied" if applied else "conflicting_revision"
    result_payload = {
        "disposition": disposition,
        "effective_revision": next_revision,
        "recorded_at_ns": now_ns,
    }
    applied_seq: int | None = None
    if applied:
        enable_domain_v1(
            connection,
            "paper",
            company_id=company_id,
            actor_id=control.principal,
        )
        applied_seq = append_audit_event(
            connection,
            company_id=company_id,
            domain="paper",
            entity_type="paper_risk_state",
            entity_id=company_id,
            event_type=(
                "paper.risk_suspended"
                if control.operation == SUSPEND_NEW_RISK
                else "paper.risk_resumed"
            ),
            actor_id=control.principal,
            command_id=control.command.request_id,
            payload={
                "operation": control.operation,
                "effective_revision": next_revision,
                "peer_pid": effective_peer_pid,
            },
            recorded_at_ns=now_ns,
        )
    try:
        admit_command_receipt(
            connection,
            company_id=company_id,
            command_id=control.command.request_id,
            actor_id=control.principal,
            peer_pid=effective_peer_pid,
            command_type=control.operation,
            contract_version=control.command.contract_revision,
            payload_sha256=control.payload_sha256,
            preconditions=control.preconditions,
            state="applied" if applied else "denied",
            denial_code=None if applied else "conflicting_revision",
            result=result_payload,
            applied_seq=applied_seq,
            now_ns=now_ns,
        )
    except CommandConflict as error:
        raise PaperRiskRefused("Request identity is already in use") from error
    if applied:
        suspended_int = 1 if control.operation == SUSPEND_NEW_RISK else 0
        connection.execute(
            """INSERT INTO paper_risk_state (
                   company_id, suspended, effective_revision, last_command_id, updated_at_ns
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(company_id) DO UPDATE SET
                   suspended = excluded.suspended,
                   effective_revision = excluded.effective_revision,
                   last_command_id = excluded.last_command_id,
                   updated_at_ns = excluded.updated_at_ns""",
            (
                company_id,
                suspended_int,
                next_revision,
                control.command.request_id,
                now_ns,
            ),
        )
    return PaperRiskReceipt(
        request_id=control.command.request_id,
        principal=control.principal,
        operation=control.operation,
        disposition=disposition,
        effective_revision=next_revision,
        recorded_at_ns=now_ns,
    )


def suspend_paper_risk(
    writer: WriterLease,
    command: PaperSuspension,
    authorize: AuthorizeBoard | None,
    *,
    peer_pid: int | None = None,
) -> PaperRiskReceipt:
    writer.verify()
    with (
        closing(open_writer(writer.store, lease=writer)) as connection,
        write_tx(connection),
    ):
        control = _authorize_control(
            connection, writer, command, SUSPEND_NEW_RISK, authorize
        )
        return control.prior or _append_control(
            connection,
            writer.company_id,
            control,
            applied=control.eligible,
            peer_pid=peer_pid,
        )


def _resume_with_evidence(
    writer: WriterLease,
    command: PaperSuspension,
    authorize: AuthorizeBoard | None,
    observe: Callable[[], Callable[[Connection], None]],
    *,
    peer_pid: int | None = None,
) -> PaperRiskReceipt:
    writer.verify()
    with closing(open_reader(writer.store)) as connection, read_tx(connection):
        initial = _authorize_control(
            connection, writer, command, RESUME_NEW_RISK, authorize
        )
        if initial.prior is not None:
            return initial.prior
    verify_local = observe() if initial.eligible else None
    with (
        closing(open_writer(writer.store, lease=writer)) as connection,
        write_tx(connection),
    ):
        current = _authorize_control(
            connection, writer, command, RESUME_NEW_RISK, authorize
        )
        if (
            current.principal,
            current.operation,
            current.payload_sha256,
            current.preconditions,
        ) != (
            initial.principal,
            initial.operation,
            initial.payload_sha256,
            initial.preconditions,
        ):
            raise PaperRiskRefused("Board authorization changed during observation")
        if current.prior is not None:
            return current.prior
        if current.eligible and verify_local is None:
            raise PaperRiskRefused("Paper risk state changed during observation")
        applied = current.eligible and verify_local is not None
        if applied:
            verify_local(connection)
        return _append_control(
            connection,
            writer.company_id,
            current,
            applied=applied,
            peer_pid=peer_pid,
        )


def read_paper_risk_receipt(
    writer: WriterLease,
    request_id: str,
    authorize: AuthorizeBoard | None,
    *,
    operation: str = SUSPEND_NEW_RISK,
) -> PaperRiskReceipt | None:
    if operation not in _RISK_OPERATIONS:
        raise PaperRiskRefused("Unsupported paper risk operation")
    writer.verify()
    with closing(open_reader(writer.store)) as connection, read_tx(connection):
        writer.verify()
        principal = _principal(connection, writer.company_id, operation, authorize)
        try:
            receipt = read_command_receipt(
                connection,
                writer.company_id,
                request_id,
                principal=principal,
                operation=operation,
            )
        except CommandUnauthorized as error:
            raise PaperRiskRefused(
                "Board receipt is not authorized for this principal or operation"
            ) from error
        except StorageRefused as error:
            raise PaperRiskRefused("Corrupted paper risk command receipt") from error
        if receipt is None:
            return None
        return _receipt_from_command(receipt)


def read_local_command_receipt_status(
    store: Path,
    company_id: str,
    command_id: str,
    *,
    operation: str | None = None,
) -> dict[str, object]:
    """Read a board risk command receipt status read-only without disclosing actor or private reason."""
    if (
        not isinstance(command_id, str)
        or not command_id.strip()
        or len(command_id) > 128
    ):
        raise PaperRiskRefused("Invalid command identifier")
    if operation is not None and operation not in _RISK_OPERATIONS:
        raise PaperRiskRefused("Unsupported board command operation")
    try:
        initial = store.stat(follow_symlinks=False)
    except OSError as error:
        raise PaperRiskRefused("Company store is unavailable") from error
    if not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.geteuid():
        raise PaperRiskRefused(
            "Only the local company store owner can inspect command receipts"
        )
    with closing(open_reader(store)) as connection, read_tx(connection):
        if _identity(connection) != company_id:
            raise PaperRiskRefused("Company identity mismatch")
        receipt = _fetch_command(connection, company_id, command_id)
        if receipt is None:
            response: dict[str, object] = {
                "status": "not_found",
                "company_id": company_id,
                "command_id": command_id,
            }
        else:
            if receipt.command_type not in _RISK_OPERATIONS or (
                operation is not None and receipt.command_type != operation
            ):
                raise PaperRiskRefused(
                    "Command receipt is outside the authorized board control scope"
                )
            effective_revision: int | None = None
            if receipt.state in ("applied", "denied"):
                parsed = _receipt_from_command(receipt)
                effective_revision = parsed.effective_revision
            elif receipt.state != "accepted":
                raise PaperRiskRefused("Unsupported board command receipt state")
            response = {
                "status": "found",
                "company_id": company_id,
                "command_id": command_id,
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
            raise PaperRiskRefused(
                "Company store changed while inspecting command receipt"
            ) from error
        if (initial.st_dev, initial.st_ino, initial.st_uid) != (
            current.st_dev,
            current.st_ino,
            current.st_uid,
        ):
            raise PaperRiskRefused(
                "Company store changed while inspecting command receipt"
            )
    return response
