"""Single-node SQLite connection factory, transaction guards, and WAL controls."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..writer_lease import WriterLease


MAX_SCHEMA_VERSION = 8
RETIRED_SCHEMA_REVISION = -1
BUSY_TIMEOUT_MS = 5000
JOURNAL_SIZE_LIMIT_BYTES = 67_108_864


class StorageRefused(Exception):
    """The company store could not be opened or safely mutated."""


@dataclass(frozen=True)
class StoreRetirement:
    request_revision: str
    original_schema: int


class ManagedConnection(sqlite3.Connection):
    """Autocommit connection that closes open explicit transactions on context exit."""

    def __exit__(self, exc_type, exc_val, exc_tb) -> Literal[False]:
        if self.in_transaction:
            if exc_type is None:
                self.execute("COMMIT")
            else:
                self.execute("ROLLBACK")
        return False


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def read_retirement(connection: sqlite3.Connection) -> StoreRetirement | None:
    if not _has_table(connection, "company_store_retired"):
        return None
    row = connection.execute(
        "SELECT request_revision, original_schema FROM company_store_retired WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return None
    return StoreRetirement(str(row[0]), int(row[1]))


def _check_user_version(connection: sqlite3.Connection) -> int:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version < 0 or version > MAX_SCHEMA_VERSION:
        raise StorageRefused(
            f"Unsupported future company store schema version: {version}"
        )
    return version


def _check_writable_state(
    connection: sqlite3.Connection, *, allow_retired: bool
) -> None:
    if not allow_retired and read_retirement(connection) is not None:
        raise StorageRefused("Company store is retired and cannot accept writes")
    if _has_table(connection, "company"):
        row = connection.execute(
            "SELECT schema_revision FROM company WHERE singleton = 1"
        ).fetchone()
        if row is not None:
            revision = int(row[0])
            if not allow_retired and revision == RETIRED_SCHEMA_REVISION:
                raise StorageRefused(
                    "Company store is retired and cannot accept writes"
                )
            if revision > MAX_SCHEMA_VERSION:
                raise StorageRefused(
                    f"Unsupported future company store schema revision: {revision}"
                )


def open_writer(
    store: Path,
    *,
    create: bool = False,
    lease: "WriterLease | None" = None,
    journal_mode: Literal["WAL", "DELETE", "PRESERVE"] = "WAL",
    allow_retired: bool = False,
    foreign_keys: bool = True,
) -> ManagedConnection:
    """Open the company SQLite store for serialized writing."""
    if lease is not None:
        lease.verify()
    mode = "rwc" if create else "rw"
    try:
        connection = sqlite3.connect(
            f"{store.absolute().as_uri()}?mode={mode}",
            uri=True,
            autocommit=True,
            factory=ManagedConnection,
        )
    except sqlite3.OperationalError as error:
        raise StorageRefused("Company store could not be opened for writing") from error
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA trusted_schema = OFF")
        _check_user_version(connection)
        _check_writable_state(connection, allow_retired=allow_retired)
        if journal_mode == "WAL":
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT_BYTES}"
            )
        elif journal_mode == "DELETE":
            connection.execute("PRAGMA journal_mode = DELETE")
        elif journal_mode != "PRESERVE":
            raise StorageRefused(f"Unsupported journal mode: {journal_mode}")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        connection.execute("PRAGMA temp_store = MEMORY")
    except BaseException:
        connection.close()
        raise
    return connection


def open_reader(store: Path) -> ManagedConnection:
    """Open the company SQLite store strictly read-only with query_only enforced."""
    try:
        connection = sqlite3.connect(
            f"{store.absolute().as_uri()}?mode=ro",
            uri=True,
            autocommit=True,
            factory=ManagedConnection,
        )
    except sqlite3.OperationalError as error:
        raise StorageRefused("Expected company store could not be opened") from error
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA foreign_keys = ON")
        _check_user_version(connection)
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def write_tx(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run an atomic BEGIN IMMEDIATE write transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    else:
        if connection.in_transaction:
            connection.execute("COMMIT")


@contextmanager
def read_tx(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a bounded read-only snapshot transaction and promptly release its WAL lock."""
    connection.execute("BEGIN DEFERRED")
    try:
        yield connection
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")


def wal_checkpoint(
    connection: sqlite3.Connection,
    mode: Literal["PASSIVE", "TRUNCATE"] = "PASSIVE",
) -> tuple[int, int, int]:
    """Run an explicit WAL checkpoint and return (busy, log_frames, checkpointed)."""
    if mode not in ("PASSIVE", "TRUNCATE"):
        raise StorageRefused(f"Unsupported WAL checkpoint mode: {mode}")
    row = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    if row is None or len(row) != 3:
        raise StorageRefused("Unexpected WAL checkpoint response")
    return int(row[0]), int(row[1]), int(row[2])


def verify_no_wal_sidecars(store: Path) -> None:
    """Ensure neither -wal nor -shm sidecar files exist alongside a staged store."""
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{store}{suffix}")
        if os.path.lexists(sidecar):
            raise StorageRefused(f"Unexpected SQLite sidecar present: {sidecar.name}")
