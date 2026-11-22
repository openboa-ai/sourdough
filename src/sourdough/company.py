"""Own the company's first persisted transition and its reopen check."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


LEGACY_SCHEMA_REVISION = 1
CONTINUATION_SCHEMA_REVISION = 2
RUN_SCHEMA_REVISION = 3
PURPOSE_SCHEMA_REVISION = 4
RUN_READ_SCHEMA_REVISION = 5
SCHEMA_REVISION = 6
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
SCHEMA = (
    """CREATE TABLE company (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        id TEXT NOT NULL UNIQUE, schema_revision INTEGER NOT NULL,
        charter_revision TEXT NOT NULL
    )""",
    AGENT_SCHEMA,
    """CREATE TABLE standing_grant (
        id TEXT PRIMARY KEY NOT NULL, agent_id TEXT NOT NULL UNIQUE REFERENCES agent(id),
        scope TEXT NOT NULL
    )""",
    DUTY_SCHEMA,
    """CREATE TABLE founding_receipt (
        setup_identity TEXT PRIMARY KEY NOT NULL,
        company_id TEXT NOT NULL UNIQUE REFERENCES company(id),
        founder_id TEXT NOT NULL UNIQUE REFERENCES agent(id),
        grant_id TEXT NOT NULL UNIQUE REFERENCES standing_grant(id),
        due_work_id TEXT NOT NULL UNIQUE REFERENCES duty(id)
    )""",
    FOLLOW_UP_SCHEMA,
    DUTY_RUN_SCHEMA,
    CURRENT_RUN_INDEX,
)


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
    connection = sqlite3.connect(
        f"{store.absolute().as_uri()}?mode={mode}", uri=True, timeout=3
    )
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if mode != "ro":
            _require_writable_store(connection)
    except BaseException:
        connection.close()
        raise
    return connection


_RETIRED_SCHEMA_REVISION = -1


@dataclass(frozen=True)
class StoreRetirement:
    request_revision: str
    original_schema: int


def _retired_store(connection: sqlite3.Connection) -> StoreRetirement | None:
    if not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='company_store_retired'"
    ).fetchone():
        return None
    result = connection.execute(
        "SELECT request_revision,original_schema FROM company_store_retired WHERE singleton=1"
    ).fetchone()
    if result is None or not result[0]:
        raise FoundingRefused("Company store retirement receipt is invalid")
    return StoreRetirement(*result)


def _require_writable_store(connection: sqlite3.Connection) -> None:
    if _retired_store(connection) is not None:
        raise FoundingRefused("The original company store is retired")


def _company_version(connection: sqlite3.Connection) -> tuple[str, int]:
    row = connection.execute(
        "SELECT id, schema_revision FROM company WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise FoundingRefused("Unsupported company store schema")
    company_id, schema = row
    if schema == _RETIRED_SCHEMA_REVISION:
        retirement = _retired_store(connection)
        if retirement is None:
            raise FoundingRefused("Company store retirement receipt is missing")
        schema = retirement.original_schema
    if schema not in (
        LEGACY_SCHEMA_REVISION,
        CONTINUATION_SCHEMA_REVISION,
        RUN_SCHEMA_REVISION,
        PURPOSE_SCHEMA_REVISION,
        RUN_READ_SCHEMA_REVISION,
        SCHEMA_REVISION,
    ):
        raise FoundingRefused("Unsupported company store schema")
    return company_id, schema


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


def upgrade_work_schema(store: Path, expected_company_id: str) -> None:
    with closing(_connect(store, "rw")) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            company_id, revision = _company_version(connection)
            if company_id != expected_company_id:
                raise FoundingRefused("Company identity mismatch")
            if revision == SCHEMA_REVISION:
                return
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
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise FoundingRefused("Company store failed foreign-key validation")
            connection.execute(
                "UPDATE company SET schema_revision = ? WHERE singleton = 1",
                (SCHEMA_REVISION,),
            )


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


def found(store: Path, terms: FoundingTerms) -> FoundingReceipt:
    """Explicit setup may initialize a store. SQLite serializes setup and replay."""
    with closing(_connect(store, "rwc")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
            _identity(connection)
            return _replay(connection, terms)

        for statement in SCHEMA:
            connection.execute(statement)
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
    return receipt


def _replay(connection: sqlite3.Connection, terms: FoundingTerms) -> FoundingReceipt:
    _, revision = _company_version(connection)
    registry = "agent" if revision >= SCHEMA_REVISION else "founder"
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
    registry = "agent" if revision >= SCHEMA_REVISION else "founder"
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
    except sqlite3.OperationalError as error:
        raise FoundingRefused("Expected company store could not be opened") from error
    with closing(connection), connection:
        connection.execute("BEGIN")
        if _identity(connection) != expected_company_id:
            raise FoundingRefused("Company identity mismatch")
        rows = connection.execute(
            "SELECT id FROM duty WHERE state = 'due' ORDER BY id"
        ).fetchall()
        return OpenCompany(expected_company_id, tuple(row[0] for row in rows))
