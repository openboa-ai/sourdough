"""Own the company's first persisted transition and its reopen check."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .storage.db import (
    MAX_SCHEMA_VERSION,
    StorageRefused,
    open_reader,
    open_writer,
    read_tx,
    write_tx,
)
from .storage.kernel import append_audit_event
from .storage.migrations import apply_fresh_migrations, migrate_store


LEGACY_SCHEMA_REVISION = 1
CONTINUATION_SCHEMA_REVISION = 2
RUN_SCHEMA_REVISION = 3
PURPOSE_SCHEMA_REVISION = 4
RUN_READ_SCHEMA_REVISION = 5
AGENT_SCHEMA_REVISION = 6
PAPER_CONTROL_SCHEMA_REVISION = 7
STORAGE_KERNEL_SCHEMA_REVISION = 8
WORK_KERNEL_SCHEMA_REVISION = 9
SCHEMA_REVISION = MAX_SCHEMA_VERSION

_SUPPORTED_REVISIONS = tuple(range(LEGACY_SCHEMA_REVISION, SCHEMA_REVISION + 1))

AGENT_SCHEMA = """CREATE TABLE agent (
    id TEXT PRIMARY KEY NOT NULL, company_id TEXT NOT NULL REFERENCES company(id),
    execution_profile TEXT NOT NULL
)"""
DUTY_SCHEMA = """CREATE TABLE duty (
    id TEXT PRIMARY KEY NOT NULL, agent_id TEXT NOT NULL REFERENCES agent(id),
    state TEXT NOT NULL
)"""
FOLLOW_UP_SCHEMA = """CREATE TABLE work_follow_up (
    previous_duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
    duty_id TEXT NOT NULL UNIQUE REFERENCES duty(id),
    grant_id TEXT NOT NULL REFERENCES standing_grant(id),
    grant_scope TEXT NOT NULL,
    due_at INTEGER NOT NULL,
    result_kind TEXT NOT NULL,
    result_id TEXT NOT NULL,
    result_revision TEXT NOT NULL,
    available_at INTEGER NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'position_review'
)"""
DUTY_RUN_SCHEMA = """CREATE TABLE duty_run (
    run_id TEXT PRIMARY KEY NOT NULL,
    duty_id TEXT NOT NULL REFERENCES duty(id),
    grant_id TEXT NOT NULL REFERENCES standing_grant(id),
    grant_scope TEXT NOT NULL,
    purpose TEXT NOT NULL,
    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1))
)"""
CURRENT_RUN_INDEX = (
    "CREATE UNIQUE INDEX current_duty_run ON duty_run(duty_id) WHERE is_current = 1"
)
RESULT_READ_SCHEMA = """CREATE TABLE IF NOT EXISTS work_result_read (
    duty_id TEXT NOT NULL REFERENCES work_follow_up(duty_id),
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
    result_revision TEXT NOT NULL,
    read_at INTEGER NOT NULL
)"""


class FoundingRefused(Exception):
    """The founding or reopen command could not be admitted."""


@dataclass(frozen=True)
class FoundingTerms:
    setup_identity: str
    charter_revision: str
    founder_scope: str
    execution_profile: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.setup_identity,
                self.charter_revision,
                self.founder_scope,
                self.execution_profile,
            )
        ):
            raise FoundingRefused("Founding terms must be nonempty strings")


@dataclass(frozen=True)
class FoundingReceipt:
    company_id: str
    founder_id: str
    grant_id: str
    due_work_id: str


@dataclass(frozen=True)
class OpenCompany:
    company_id: str
    due_work_ids: tuple[str, ...]


def _connect(store: Path, mode: str) -> sqlite3.Connection:
    if mode == "ro":
        return open_reader(store)
    if mode == "rw":
        return open_writer(store, create=False)
    if mode == "rwc":
        return open_writer(store, create=True)
    raise FoundingRefused(f"Unsupported SQLite open mode: {mode}")


def _company_version(connection: sqlite3.Connection) -> tuple[str, int]:
    row = connection.execute(
        "SELECT id, schema_revision FROM company WHERE singleton = 1"
    ).fetchone()
    if row is None or row[1] not in _SUPPORTED_REVISIONS:
        raise FoundingRefused("Unsupported company store schema")
    return str(row[0]), int(row[1])


def _identity(connection: sqlite3.Connection) -> str:
    return _company_version(connection)[0]


def _upgrade_duties(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE duty_next (
            id TEXT PRIMARY KEY NOT NULL,
            founder_id TEXT NOT NULL REFERENCES founder(id),
            state TEXT NOT NULL
        )"""
    )
    connection.execute("INSERT INTO duty_next SELECT id, founder_id, state FROM duty")
    connection.execute("DROP TABLE duty")
    connection.execute("ALTER TABLE duty_next RENAME TO duty")
    connection.execute(FOLLOW_UP_SCHEMA)


def _consolidate_runs(connection: sqlite3.Connection, revision: int) -> None:
    old_claims = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'first_duty_claim'"
    ).fetchone()
    connection.execute(
        DUTY_RUN_SCHEMA.replace(
            "CREATE TABLE duty_run", "CREATE TABLE duty_run_next", 1
        )
    )
    if old_claims:
        connection.execute("ALTER TABLE first_duty_claim RENAME TO duty_run")
        connection.execute(
            """INSERT INTO duty_run_next
               SELECT claim.run_id, claim.duty_id,
                      (SELECT grant_id FROM founding_receipt WHERE due_work_id = claim.duty_id),
                      'paper', 'paper_execution', 1
               FROM duty_run claim"""
        )
    if revision == CONTINUATION_SCHEMA_REVISION:
        connection.execute(
            """INSERT INTO duty_run_next
               SELECT run_id, duty_id, grant_id, grant_scope, 'position_review', 1
               FROM work_follow_up WHERE run_id IS NOT NULL"""
        )
        connection.execute(
            FOLLOW_UP_SCHEMA.replace(
                "CREATE TABLE work_follow_up", "CREATE TABLE follow_up_next", 1
            )
        )
        connection.execute(
            """INSERT INTO follow_up_next (
                      previous_duty_id, duty_id, grant_id, grant_scope, due_at,
                      result_kind, result_id, result_revision, available_at)
               SELECT previous_duty_id, duty_id, grant_id, grant_scope, due_at,
                      result_kind, result_id, result_revision, available_at
               FROM work_follow_up"""
        )
        connection.execute("DROP TABLE work_follow_up")
        connection.execute("ALTER TABLE follow_up_next RENAME TO work_follow_up")
    if old_claims:
        connection.execute("DROP TABLE duty_run")
    connection.execute("ALTER TABLE duty_run_next RENAME TO duty_run")
    connection.execute(CURRENT_RUN_INDEX)


def _upgrade_agent_registry(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE founder RENAME TO agent")
    connection.execute(
        AGENT_SCHEMA.replace("CREATE TABLE agent", "CREATE TABLE agent_next", 1)
    )
    connection.execute(
        "INSERT INTO agent_next SELECT id, company_id, execution_profile FROM agent"
    )
    connection.execute("DROP TABLE agent")
    connection.execute("ALTER TABLE agent_next RENAME TO agent")
    connection.execute(
        "ALTER TABLE standing_grant RENAME COLUMN founder_id TO agent_id"
    )
    connection.execute("ALTER TABLE duty RENAME COLUMN founder_id TO agent_id")


def _upgrade_result_reads(connection: sqlite3.Connection) -> None:
    if not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'work_result_read'"
    ).fetchone():
        return
    connection.execute(
        RESULT_READ_SCHEMA.replace("work_result_read (", "work_result_read_next (", 1)
    )
    connection.execute(
        """INSERT INTO work_result_read_next (duty_id, run_id, result_revision, read_at)
           SELECT duty_id, run_id, result_revision, read_at FROM work_result_read"""
    )
    connection.execute("DROP TABLE work_result_read")
    connection.execute("ALTER TABLE work_result_read_next RENAME TO work_result_read")


def _legacy_upgrade_step(connection: sqlite3.Connection, revision: int) -> None:
    if revision < AGENT_SCHEMA_REVISION:
        if revision == RUN_SCHEMA_REVISION:
            connection.execute(
                "ALTER TABLE work_follow_up ADD COLUMN purpose TEXT NOT NULL DEFAULT 'position_review'"
            )
        elif revision < PURPOSE_SCHEMA_REVISION:
            if revision == LEGACY_SCHEMA_REVISION:
                _upgrade_duties(connection)
            _consolidate_runs(connection, revision)
        if revision < RUN_READ_SCHEMA_REVISION:
            _upgrade_result_reads(connection)
        _upgrade_agent_registry(connection)


def upgrade_work_schema(store: Path, expected_company_id: str) -> None:
    try:
        migrate_store(
            store,
            expected_company_id,
            legacy_upgrade_hook=_legacy_upgrade_step,
        )
    except StorageRefused as error:
        raise FoundingRefused(str(error)) from error


def found(store: Path, terms: FoundingTerms) -> FoundingReceipt:
    """Explicit setup may initialize a store. SQLite serializes setup and replay."""
    try:
        connection = _connect(store, "rwc")
    except StorageRefused as error:
        raise FoundingRefused(str(error)) from error
    with closing(connection), write_tx(connection):
        if connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
            _identity(connection)
            return _replay(connection, terms)

        apply_fresh_migrations(connection)
        receipt = FoundingReceipt(*(uuid4().hex for _ in range(4)))
        connection.execute(
            "INSERT INTO company VALUES (1, ?, ?, ?)",
            (receipt.company_id, SCHEMA_REVISION, terms.charter_revision),
        )
        connection.execute(
            "INSERT INTO agent VALUES (?, ?, ?)",
            (receipt.founder_id, receipt.company_id, terms.execution_profile),
        )
        connection.execute(
            "INSERT INTO standing_grant VALUES (?, ?, ?)",
            (receipt.grant_id, receipt.founder_id, terms.founder_scope),
        )
        connection.execute(
            "INSERT INTO duty VALUES (?, ?, 'due')",
            (receipt.due_work_id, receipt.founder_id),
        )
        connection.execute(
            "INSERT INTO founding_receipt VALUES (?, ?, ?, ?, ?)",
            (
                terms.setup_identity,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                receipt.due_work_id,
            ),
        )
        append_audit_event(
            connection,
            company_id=receipt.company_id,
            domain="org",
            entity_type="company",
            entity_id=receipt.company_id,
            event_type="company.founded",
            actor_id=terms.setup_identity,
            payload={
                "charter_revision": terms.charter_revision,
                "founder_id": receipt.founder_id,
                "grant_id": receipt.grant_id,
                "due_work_id": receipt.due_work_id,
            },
        )
    return receipt


def _replay(connection: sqlite3.Connection, terms: FoundingTerms) -> FoundingReceipt:
    _, revision = _company_version(connection)
    registry = "agent" if revision >= AGENT_SCHEMA_REVISION else "founder"
    row = connection.execute(
        f"""SELECT c.charter_revision, g.scope, f.execution_profile,
                  r.company_id, r.founder_id, r.grant_id, r.due_work_id
           FROM founding_receipt r
           JOIN company c ON c.id = r.company_id
           JOIN {registry} f ON f.id = r.founder_id
           JOIN standing_grant g ON g.id = r.grant_id
           WHERE r.setup_identity = ?""",
        (terms.setup_identity,),
    ).fetchone()
    if row is None or row[:3] != (
        terms.charter_revision,
        terms.founder_scope,
        terms.execution_profile,
    ):
        raise FoundingRefused("Founding terms conflict with the existing company")
    return FoundingReceipt(*row[3:])


def founding_execution_profile(
    connection: sqlite3.Connection, company_id: str
) -> str | None:
    _, revision = _company_version(connection)
    registry = "agent" if revision >= AGENT_SCHEMA_REVISION else "founder"
    row = connection.execute(
        f"""SELECT a.execution_profile FROM founding_receipt r
            JOIN {registry} a ON a.id = r.founder_id AND a.company_id = r.company_id
            WHERE r.company_id = ?""",
        (company_id,),
    ).fetchone()
    return row[0] if row is not None else None


def reopen(store: Path, expected_company_id: str) -> OpenCompany:
    """Check compatibility and inventory due work in one read-only snapshot."""
    try:
        connection = _connect(store, "ro")
    except (sqlite3.OperationalError, StorageRefused) as error:
        raise FoundingRefused(str(error)) from error
    with closing(connection), read_tx(connection):
        if _identity(connection) != expected_company_id:
            raise FoundingRefused("Company identity mismatch")
        rows = connection.execute(
            "SELECT id FROM duty WHERE state = 'due' ORDER BY id"
        ).fetchall()
        return OpenCompany(expected_company_id, tuple(row[0] for row in rows))
