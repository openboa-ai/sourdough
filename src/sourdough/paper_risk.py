"""Apply a board-authorized suspension at the paper reservation boundary."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import astuple, dataclass
from sqlite3 import Connection
from time import time_ns

from .company import (
    CURRENT_WORK_SCHEMA_REVISIONS,
    PAPER_CONTROL_SCHEMA_REVISION,
    _company_version,
    _connect,
)
from .writer_lease import WriterLease


PAPER_RISK_COMMAND = "paper-risk-command-v1"
SUSPEND_NEW_RISK = "suspend_new_risk"
RESUME_NEW_RISK = "resume_new_risk"
AuthorizeBoard = Callable[[Connection, str, str], str | None]

_SELECT = """
SELECT principal, operation, charter_revision, expected_revision, reason, contract_revision,
       disposition, effective_revision, recorded_at_ns
FROM paper_risk_command WHERE request_id=?
"""


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


def _has_commands(connection: Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='paper_risk_command'"
        ).fetchone()
        is not None
    )


def effective_paper_risk(connection: Connection) -> PaperRiskState:
    _, schema = _company_version(connection)
    if _has_commands(connection):
        row = connection.execute(
            """SELECT request_id,principal,operation,disposition,effective_revision,recorded_at_ns
               FROM paper_risk_command
               WHERE disposition='applied' ORDER BY effective_revision DESC LIMIT 1"""
        ).fetchone()
        if row is not None:
            applied = PaperRiskReceipt(*row)
            if (
                applied.operation not in (SUSPEND_NEW_RISK, RESUME_NEW_RISK)
                or type(applied.effective_revision) is not int
                or applied.effective_revision < 1
                or type(applied.recorded_at_ns) is not int
                or applied.recorded_at_ns <= 0
            ):
                raise PaperRiskRefused("Unsupported effective paper risk control")
            return PaperRiskState(applied.operation == SUSPEND_NEW_RISK, applied)
    if schema == PAPER_CONTROL_SCHEMA_REVISION:
        raise PaperRiskRefused("Controlled store has no effective paper risk receipt")
    return PaperRiskState(False, None)


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


def _prior(connection: Connection, request_id: str) -> tuple | None:
    return (
        connection.execute(_SELECT, (request_id,)).fetchone()
        if _has_commands(connection)
        else None
    )


@dataclass(frozen=True)
class _AuthorizedControl:
    terms: tuple
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
    company_id, schema = _company_version(connection)
    if company_id != writer.company_id:
        raise PaperRiskRefused("Company identity mismatch")
    if schema not in CURRENT_WORK_SCHEMA_REVISIONS:
        raise PaperRiskRefused("Company work schema must be upgraded before control")
    terms = (principal, operation, *astuple(command)[1:])
    if prior := _prior(connection, command.request_id):
        if prior[:6] != terms:
            raise PaperRiskRefused("Request identity is already in use")
        receipt = PaperRiskReceipt(command.request_id, *prior[:2], *prior[6:])
        return _AuthorizedControl(terms, receipt, receipt.effective_revision, False)
    state = effective_paper_risk(connection)
    (current_charter,) = connection.execute(
        "SELECT charter_revision FROM company WHERE singleton=1"
    ).fetchone()
    eligible = (command.charter_revision, command.expected_revision) == (
        current_charter,
        state.revision,
    ) and (operation == SUSPEND_NEW_RISK or state.suspended)
    return _AuthorizedControl(terms, None, state.revision, eligible)


def _append_control(
    connection: Connection,
    request_id: str,
    control: _AuthorizedControl,
    *,
    applied: bool,
) -> PaperRiskReceipt:
    principal, operation, *_ = control.terms
    receipt = PaperRiskReceipt(
        request_id,
        principal,
        operation,
        "applied" if applied else "conflicting_revision",
        control.current_revision + int(applied),
        time_ns(),
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS paper_risk_command (
               request_id TEXT PRIMARY KEY NOT NULL, principal TEXT NOT NULL,
               operation TEXT NOT NULL,
               charter_revision TEXT NOT NULL, expected_revision INTEGER NOT NULL,
               reason TEXT NOT NULL, contract_revision TEXT NOT NULL,
               disposition TEXT NOT NULL CHECK(disposition IN ('applied','conflicting_revision')),
               effective_revision INTEGER NOT NULL, recorded_at_ns INTEGER NOT NULL
           )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS paper_risk_effective_revision
           ON paper_risk_command(effective_revision) WHERE disposition='applied'"""
    )
    connection.execute(
        "INSERT INTO paper_risk_command VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (request_id, *control.terms, *astuple(receipt)[3:]),
    )
    if applied:
        connection.execute(
            "UPDATE company SET schema_revision=? WHERE singleton=1",
            (PAPER_CONTROL_SCHEMA_REVISION,),
        )
    return receipt


def suspend_paper_risk(
    writer: WriterLease,
    command: PaperSuspension,
    authorize: AuthorizeBoard | None,
) -> PaperRiskReceipt:
    writer.verify()
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        control = _authorize_control(
            connection, writer, command, SUSPEND_NEW_RISK, authorize
        )
        return control.prior or _append_control(
            connection, command.request_id, control, applied=control.eligible
        )


def _resume_with_evidence(
    writer: WriterLease,
    command: PaperSuspension,
    authorize: AuthorizeBoard | None,
    observe: Callable[[], Callable[[Connection], None]],
) -> PaperRiskReceipt:
    writer.verify()
    with closing(_connect(writer.store, "ro")) as connection:
        connection.execute("BEGIN")
        initial = _authorize_control(
            connection, writer, command, RESUME_NEW_RISK, authorize
        )
        if initial.prior is not None:
            return initial.prior
    verify_local = observe() if initial.eligible else None
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _authorize_control(
            connection, writer, command, RESUME_NEW_RISK, authorize
        )
        if current.terms != initial.terms:
            raise PaperRiskRefused("Board authorization changed during observation")
        if current.prior is not None:
            return current.prior
        applied = current.eligible and verify_local is not None
        if applied:
            verify_local(connection)
        return _append_control(connection, command.request_id, current, applied=applied)


def read_paper_risk_receipt(
    writer: WriterLease,
    request_id: str,
    authorize: AuthorizeBoard | None,
    *,
    operation: str = SUSPEND_NEW_RISK,
) -> PaperRiskReceipt | None:
    writer.verify()
    with closing(_connect(writer.store, "ro")) as connection:
        connection.execute("BEGIN")
        writer.verify()
        principal = _principal(connection, writer.company_id, operation, authorize)
        prior = _prior(connection, request_id)
        if prior is None or prior[:2] != (principal, operation):
            return None
        return PaperRiskReceipt(request_id, *prior[:2], *prior[6:])
