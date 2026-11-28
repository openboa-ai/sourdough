"""Own the company's first persisted transition and its reopen check."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .storage.db import (
    RETIRED_SCHEMA_REVISION,
    SCHEMA_VERSION,
    StorageRefused,
    StoreRetirement,
    open_reader,
    open_writer,
    read_retirement,
    read_tx,
    write_tx,
)
from .storage.kernel import append_audit_event
from .storage.schema import apply_core_v1, has_user_schema_objects


SCHEMA_REVISION = SCHEMA_VERSION
_RETIRED_SCHEMA_REVISION = RETIRED_SCHEMA_REVISION


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


def _retired_store(connection: sqlite3.Connection) -> StoreRetirement | None:
    try:
        return read_retirement(connection)
    except StorageRefused as error:
        raise FoundingRefused(str(error)) from error


def _require_writable_store(connection: sqlite3.Connection) -> None:
    if _retired_store(connection) is not None:
        raise FoundingRefused("The original company store is retired")


def _connect(store: Path, mode: str, *, initialize: bool = False) -> sqlite3.Connection:
    try:
        if mode == "ro":
            return open_reader(store)
        if mode == "rw":
            return open_writer(store, create=False)
        if mode == "rwc":
            return open_writer(store, create=True, initialize=initialize)
    except StorageRefused as error:
        if "retired" in str(error).lower():
            raise FoundingRefused("The original company store is retired") from error
        raise
    raise FoundingRefused(f"Unsupported SQLite open mode: {mode}")


def _company_version(connection: sqlite3.Connection) -> tuple[str, int]:
    row = connection.execute(
        "SELECT id, schema_revision FROM company WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise FoundingRefused("Unsupported company store schema")
    company_id = str(row[0])
    schema = int(row[1])
    if schema == _RETIRED_SCHEMA_REVISION:
        retirement = _retired_store(connection)
        if retirement is None:
            raise FoundingRefused("Company store retirement receipt is missing")
        schema = retirement.original_schema
    if schema != SCHEMA_REVISION:
        raise FoundingRefused("Unsupported company store schema")
    return company_id, schema


def _identity(connection: sqlite3.Connection) -> str:
    return _company_version(connection)[0]


def found(store: Path, terms: FoundingTerms) -> FoundingReceipt:
    """Explicit setup may initialize a store. SQLite serializes setup and replay."""
    try:
        connection = _connect(store, "rwc", initialize=True)
    except StorageRefused as error:
        raise FoundingRefused(str(error)) from error
    try:
        with closing(connection), write_tx(connection):
            if has_user_schema_objects(connection):
                _identity(connection)
                return _replay(connection, terms)

            apply_core_v1(connection)
            receipt = FoundingReceipt(*(uuid4().hex for _ in range(4)))
            connection.execute(
                "INSERT INTO company VALUES (1, ?, ?, ?)",
                (receipt.company_id, SCHEMA_REVISION, terms.charter_revision),
            )
            connection.execute(
                "INSERT INTO agent VALUES (?, ?, ?)",
                (receipt.founder_id, receipt.company_id, terms.execution_profile),
            )
            from .agent_desk import ensure_private_desk

            ensure_private_desk(
                connection,
                receipt.company_id,
                receipt.founder_id,
                terms.execution_profile,
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
    except StorageRefused as error:
        raise FoundingRefused(str(error)) from error
    return receipt


def _replay(connection: sqlite3.Connection, terms: FoundingTerms) -> FoundingReceipt:
    _company_version(connection)
    row = connection.execute(
        """SELECT c.charter_revision, g.scope, f.execution_profile,
                  r.company_id, r.founder_id, r.grant_id, r.due_work_id
           FROM founding_receipt r
           JOIN company c ON c.id = r.company_id
           JOIN agent f ON f.id = r.founder_id
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
    _company_version(connection)
    row = connection.execute(
        """SELECT a.execution_profile FROM founding_receipt r
           JOIN agent a ON a.id = r.founder_id AND a.company_id = r.company_id
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
