"""Ordered, checksum-verified SQL migrations and legacy store upgrades."""

import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from importlib import resources
from pathlib import Path
from time import time_ns

from .db import (
    MAX_SCHEMA_VERSION,
    RETIRED_SCHEMA_REVISION,
    StorageRefused,
    open_writer,
    write_tx,
)

_FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

MIGRATIONS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    domain TEXT NOT NULL,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    applied_at_ns INTEGER NOT NULL
) STRICT"""


@dataclass(frozen=True)
class MigrationSpec:
    version: int
    name: str
    domain: str
    sql: str
    sha256: str


def _infer_domain(name: str) -> str:
    if "core" in name:
        return "core"
    if "kernel" in name:
        return "kernel"
    if "work" in name:
        return "work"
    return "shared"


def load_migrations() -> tuple[MigrationSpec, ...]:
    """Load and validate all packaged ordered SQL migrations."""
    directory = resources.files("sourdough.storage").joinpath("migrations")
    specs: list[MigrationSpec] = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if not entry.is_file() or not entry.name.endswith(".sql"):
            continue
        match = _FILENAME_RE.fullmatch(entry.name)
        if match is None:
            raise StorageRefused(f"Invalid migration filename: {entry.name}")
        version = int(match.group(1))
        name = match.group(2)
        sql = entry.read_text(encoding="utf-8")
        digest = sha256(sql.encode("utf-8")).hexdigest()
        specs.append(MigrationSpec(version, name, _infer_domain(name), sql, digest))
    if not specs:
        raise StorageRefused("No packaged SQL migrations found")
    versions = [spec.version for spec in specs]
    if len(set(versions)) != len(versions) or versions != sorted(versions):
        raise StorageRefused("SQL migrations must have unique ascending versions")
    if versions[-1] != MAX_SCHEMA_VERSION:
        raise StorageRefused(
            f"Latest migration version {versions[-1]} does not match MAX_SCHEMA_VERSION {MAX_SCHEMA_VERSION}"
        )
    return tuple(specs)


def _execute_sql_script_in_tx(connection: sqlite3.Connection, sql: str) -> None:
    """Execute SQL statements sequentially without ending the active transaction."""
    buffer: list[str] = []
    in_trigger = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer.append(line)
        upper = stripped.upper()
        if "CREATE TRIGGER" in upper:
            in_trigger = True
        if in_trigger:
            if upper == "END;":
                statement = "\n".join(buffer)
                connection.execute(statement)
                buffer.clear()
                in_trigger = False
        elif sqlite3.complete_statement("\n".join(buffer) + "\n"):
            statement = "\n".join(buffer)
            connection.execute(statement)
            buffer.clear()
    if buffer:
        remainder = "\n".join(buffer).strip()
        if remainder:
            connection.execute(remainder)


def verify_applied_migrations(connection: sqlite3.Connection) -> list[int]:
    """Verify every recorded migration checksum matches its packaged SQL file."""
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if has_table is None:
        return []
    specs_by_version = {spec.version: spec for spec in load_migrations()}
    rows = connection.execute(
        "SELECT version, name, sha256 FROM schema_migrations ORDER BY version"
    ).fetchall()
    applied: list[int] = []
    for version, name, recorded_sha in rows:
        spec = specs_by_version.get(int(version))
        if spec is None:
            raise StorageRefused(f"Unknown applied schema migration version: {version}")
        if spec.name != name or spec.sha256 != recorded_sha:
            raise StorageRefused(
                f"Applied schema migration {version} ({name}) checksum mismatch"
            )
        applied.append(int(version))
    return applied


def apply_fresh_migrations(
    connection: sqlite3.Connection, *, now_ns: int | None = None
) -> None:
    """Initialize a brand-new company store inside an active write transaction."""
    if not connection.in_transaction:
        raise StorageRefused("Fresh migrations must run inside an active transaction")
    timestamp = time_ns() if now_ns is None else now_ns
    connection.execute(MIGRATIONS_TABLE_DDL)
    for spec in load_migrations():
        _execute_sql_script_in_tx(connection, spec.sql)
        connection.execute(
            """INSERT INTO schema_migrations (version, domain, name, sha256, applied_at_ns)
               VALUES (?, ?, ?, ?, ?)""",
            (spec.version, spec.domain, spec.name, spec.sha256, timestamp),
        )
    connection.execute(f"PRAGMA user_version = {MAX_SCHEMA_VERSION}")


def migrate_store(
    store: Path,
    expected_company_id: str,
    *,
    legacy_upgrade_hook: Callable[[sqlite3.Connection, int], None] | None = None,
) -> int:
    """Upgrade an existing v1-v7 or partially migrated store to MAX_SCHEMA_VERSION.

    Preserves the existing journal mode until the migration commits so a failed
    upgrade rolls back without mutating a single byte of a legacy store.
    """
    specs = load_migrations()
    with closing(
        open_writer(store, create=False, journal_mode="PRESERVE", foreign_keys=False)
    ) as connection:
        with write_tx(connection):
            row = connection.execute(
                "SELECT id, schema_revision FROM company WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise StorageRefused("Unsupported company store schema")
            company_id, revision = str(row[0]), int(row[1])
            if company_id != expected_company_id:
                raise StorageRefused("Company identity mismatch")
            if revision == RETIRED_SCHEMA_REVISION or revision > MAX_SCHEMA_VERSION:
                raise StorageRefused("Unsupported company store schema")
            if revision < 1:
                raise StorageRefused("Unsupported company store schema")

            applied = set(verify_applied_migrations(connection))
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])

            if (
                revision == MAX_SCHEMA_VERSION
                and user_version == MAX_SCHEMA_VERSION
                and all(spec.version in applied for spec in specs)
            ):
                return MAX_SCHEMA_VERSION

            if revision < 6 and legacy_upgrade_hook is None:
                raise StorageRefused(
                    f"Legacy v{revision} company store requires an explicit legacy upgrade hook"
                )
            if revision < MAX_SCHEMA_VERSION and legacy_upgrade_hook is not None:
                legacy_upgrade_hook(connection, revision)

            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StorageRefused("Company store failed foreign-key validation")

            connection.execute(MIGRATIONS_TABLE_DDL)
            now = time_ns()
            for spec in specs:
                if spec.version in applied:
                    continue
                _execute_sql_script_in_tx(connection, spec.sql)
                connection.execute(
                    """INSERT INTO schema_migrations (version, domain, name, sha256, applied_at_ns)
                       VALUES (?, ?, ?, ?, ?)""",
                    (spec.version, spec.domain, spec.name, spec.sha256, now),
                )

            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StorageRefused("Company store failed foreign-key validation")

            connection.execute(
                "UPDATE company SET schema_revision = ? WHERE singleton = 1",
                (MAX_SCHEMA_VERSION,),
            )
            connection.execute(f"PRAGMA user_version = {MAX_SCHEMA_VERSION}")

        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
    return MAX_SCHEMA_VERSION
