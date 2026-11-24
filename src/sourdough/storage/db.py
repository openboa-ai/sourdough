"""Single-node SQLite connection factory, transaction guards, and WAL controls."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..writer_lease import WriterLease


SCHEMA_VERSION = 1
RETIRED_SCHEMA_REVISION = -1
BUSY_TIMEOUT_MS = 5000
JOURNAL_SIZE_LIMIT_BYTES = 67_108_864


class StorageRefused(Exception):
    """The company store could not be opened or safely mutated."""


@dataclass(frozen=True)
class StoreRetirement:
    request_revision: str
    original_schema: int
    replacement_sha256: str = ""


class ManagedConnection(sqlite3.Connection):
    """Autocommit connection that closes open explicit transactions on context exit."""

    _allow_retired: bool = False
    _allow_initialize: bool = False

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
        """SELECT request_revision, original_schema, replacement_sha256
           FROM company_store_retired WHERE singleton = 1"""
    ).fetchone()
    if row is None:
        return None
    return StoreRetirement(str(row[0]), int(row[1]), str(row[2]))


def _verify_store_schema(connection: sqlite3.Connection, *, allow_empty: bool) -> None:
    from .schema import has_user_schema_objects, verify_v1_schema

    if allow_empty and not has_user_schema_objects(connection):
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != 0:
            raise StorageRefused(
                f"Empty company store has unexpected schema version: {version}"
            )
        return
    verify_v1_schema(connection)


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
            if revision != SCHEMA_VERSION and not (
                allow_retired and revision == RETIRED_SCHEMA_REVISION
            ):
                raise StorageRefused(
                    f"Unsupported company store schema revision: {revision}"
                )


def _set_journal_mode_with_retry(connection: sqlite3.Connection, mode: str) -> None:
    """Retry journal_mode transition across concurrent cold-start initialization."""
    deadline = monotonic() + (BUSY_TIMEOUT_MS / 1000.0)
    delay = 0.01
    while True:
        try:
            connection.execute(f"PRAGMA journal_mode = {mode}")
            return
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            if (
                "locked" not in message and "busy" not in message
            ) or monotonic() >= deadline:
                raise
            sleep(min(delay, max(0.0, deadline - monotonic())))
            delay = min(delay * 1.5, 0.1)


def open_writer(
    store: Path,
    *,
    create: bool = False,
    initialize: bool = False,
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
        with read_tx(connection):
            _verify_store_schema(connection, allow_empty=initialize)
            _check_writable_state(connection, allow_retired=allow_retired)
        if journal_mode == "WAL":
            _set_journal_mode_with_retry(connection, "WAL")
            connection.execute(
                f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT_BYTES}"
            )
        elif journal_mode == "DELETE":
            _set_journal_mode_with_retry(connection, "DELETE")
        elif journal_mode != "PRESERVE":
            raise StorageRefused(f"Unsupported journal mode: {journal_mode}")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection._allow_retired = allow_retired
        connection._allow_initialize = initialize
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
        with read_tx(connection):
            _verify_store_schema(connection, allow_empty=False)
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def write_tx(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run an atomic BEGIN IMMEDIATE write transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        _verify_store_schema(
            connection,
            allow_empty=bool(getattr(connection, "_allow_initialize", False)),
        )
        _check_writable_state(
            connection,
            allow_retired=bool(getattr(connection, "_allow_retired", False)),
        )
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
