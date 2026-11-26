"""Transactional core v1 table rebuild and schema migration helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection
from contextlib import closing

from .db import SCHEMA_VERSION, StorageRefused
from .schema import (
    VALID_DOMAINS,
    _execute_sql_script_in_tx,
    canonical_domain_name,
    compute_sqlite_schema_sha256,
    installed_domains,
    load_domain_pack,
    verify_v1_schema,
)

CORE_REBUILD_TABLES: tuple[str, ...] = (
    "installed_domains",
    "decision_question",
    "duty",
    "work_follow_up",
)

_VALID_DUTY_STATES = frozenset(
    {
        "due",
        "scheduled",
        "running",
        "waiting",
        "settled",
        "incomplete",
        "archived",
        "handed_off",
    }
)
_VALID_APPLICATION_STATUSES = frozenset(
    {"pending", "applied", "declined", "not_applicable", "failed"}
)


def extract_table_ddl(
    domain: str, table_names: Collection[str]
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Extract canonical CREATE TABLE and CREATE INDEX SQL statements from a domain pack."""
    wanted = set(table_names)
    pack = load_domain_pack(domain)
    with closing(sqlite3.connect(":memory:", autocommit=True)) as reference:
        if domain != "core":
            core_pack = load_domain_pack("core")
            for _name, sql in core_pack.files:
                _execute_sql_script_in_tx(reference, sql)
        for _name, sql in pack.files:
            _execute_sql_script_in_tx(reference, sql)
        table_ddl: dict[str, str] = {}
        index_ddl: dict[str, list[str]] = {name: [] for name in wanted}
        for row_type, name, tbl_name, sql in reference.execute(
            """SELECT type, name, tbl_name, sql
               FROM sqlite_schema
               WHERE type IN ('table', 'index')
                 AND name NOT LIKE 'sqlite_%'
                 AND sql IS NOT NULL
               ORDER BY rowid"""
        ).fetchall():
            tbl = str(tbl_name)
            if tbl not in wanted:
                continue
            if row_type == "table" and str(name) == tbl:
                table_ddl[tbl] = str(sql)
            elif row_type == "index":
                index_ddl[tbl].append(str(sql))
    missing = wanted - set(table_ddl)
    if missing:
        raise StorageRefused(
            f"Missing canonical DDL in domain '{domain}' for tables: {sorted(missing)}"
        )
    return table_ddl, {name: tuple(stmts) for name, stmts in index_ddl.items()}


def _rebuild_single_table(
    connection: sqlite3.Connection,
    table_name: str,
    create_table_sql: str,
    create_index_sqls: tuple[str, ...],
) -> None:
    if table_name not in CORE_REBUILD_TABLES:
        raise StorageRefused(f"Unsupported core rebuild table: {table_name}")
    columns = [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    ]
    if not columns:
        raise StorageRefused(f"Cannot rebuild missing table '{table_name}'")
    col_list = ", ".join(f'"{col}"' for col in columns)
    staging = f"temp._mig_{table_name}"
    connection.execute(
        f"CREATE TEMP TABLE _mig_{table_name} AS SELECT {col_list} FROM {table_name}"
    )
    connection.execute(f"DROP TABLE {table_name}")
    connection.execute(create_table_sql)
    for idx_sql in create_index_sqls:
        connection.execute(idx_sql)
    connection.execute(
        f"INSERT INTO {table_name} ({col_list}) SELECT {col_list} FROM {staging}"
    )
    connection.execute(f"DROP TABLE {staging}")


def rebuild_core_v1_tables(
    connection: sqlite3.Connection,
    *,
    domain_migrator: Callable[
        [sqlite3.Connection, frozenset[str]], frozenset[str] | None
    ]
    | None = None,
    target_domain: str | None = None,
) -> frozenset[str]:
    """Atomically rebuild updated core v1 tables inside a caller-managed BEGIN IMMEDIATE tx.

    The caller must disable PRAGMA foreign_keys before starting the transaction so
    table drops/recreates do not rewrite child foreign keys via ALTER TABLE RENAME.
    """
    if not connection.in_transaction:
        raise StorageRefused(
            "rebuild_core_v1_tables requires an active BEGIN IMMEDIATE transaction"
        )
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise StorageRefused(
            "rebuild_core_v1_tables requires PRAGMA foreign_keys = OFF before BEGIN IMMEDIATE"
        )
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        raise StorageRefused(
            f"Unsupported company store schema version: {version} (expected {SCHEMA_VERSION})"
        )

    bad_duty = connection.execute(
        "SELECT id, state FROM duty WHERE state NOT IN ({}) LIMIT 1".format(
            ", ".join("?" for _ in _VALID_DUTY_STATES)
        ),
        tuple(sorted(_VALID_DUTY_STATES)),
    ).fetchone()
    if bad_duty is not None:
        raise StorageRefused(
            f"Invalid duty.state '{bad_duty[1]}' on duty '{bad_duty[0]}'"
        )

    bad_app = connection.execute(
        "SELECT question_id, application_status FROM decision_question WHERE application_status NOT IN ({}) LIMIT 1".format(
            ", ".join("?" for _ in _VALID_APPLICATION_STATUSES)
        ),
        tuple(sorted(_VALID_APPLICATION_STATUSES)),
    ).fetchone()
    if bad_app is not None:
        raise StorageRefused(
            f"Invalid decision_question.application_status '{bad_app[1]}' on '{bad_app[0]}'"
        )

    bad_follow_up = connection.execute(
        "SELECT duty_id FROM work_follow_up WHERE purpose IS NULL OR trim(purpose) = '' LIMIT 1"
    ).fetchone()
    if bad_follow_up is not None:
        raise StorageRefused(
            f"work_follow_up '{bad_follow_up[0]}' has empty or null purpose"
        )

    domain_rows = connection.execute(
        "SELECT domain, schema_version FROM installed_domains"
    ).fetchall()
    seen_domains: set[str] = set()
    for raw_domain, raw_dom_version in domain_rows:
        if not isinstance(raw_domain, str) or raw_domain not in VALID_DOMAINS:
            raise StorageRefused(f"Invalid installed_domains.domain '{raw_domain}'")
        if type(raw_dom_version) is not int or raw_dom_version != SCHEMA_VERSION:
            raise StorageRefused(
                f"Installed domain '{raw_domain}' has unsupported schema version {raw_dom_version}"
            )
        seen_domains.add(raw_domain)
    if "core" not in seen_domains or (
        "trading" in seen_domains and "paper" in seen_domains
    ):
        raise StorageRefused(
            f"Invalid installed schema domains in company store: {sorted(seen_domains)}"
        )

    table_ddl, index_ddl = extract_table_ddl("core", CORE_REBUILD_TABLES)
    for table_name in CORE_REBUILD_TABLES:
        _rebuild_single_table(
            connection, table_name, table_ddl[table_name], index_ddl[table_name]
        )

    active = frozenset(installed_domains(connection))
    if target_domain is not None:
        canonical = canonical_domain_name(target_domain)
        if canonical == "trading" and "paper" in active and "trading" not in active:
            connection.execute(
                "UPDATE installed_domains SET domain = 'trading' WHERE domain = 'paper'"
            )
            active = frozenset(installed_domains(connection))

    if domain_migrator is not None:
        migrated_domains = domain_migrator(connection, active)
        if migrated_domains is not None:
            active = frozenset(migrated_domains)

    actual_schema_hash = compute_sqlite_schema_sha256(connection)
    for dom in sorted(active):
        pack = load_domain_pack(dom)
        connection.execute(
            """UPDATE installed_domains
               SET pack_sha256 = ?, sqlite_schema_sha256 = ?
               WHERE domain = ?""",
            (pack.pack_sha256, actual_schema_hash, dom),
        )

    fk_violation = connection.execute("PRAGMA foreign_key_check").fetchone()
    if fk_violation is not None:
        raise StorageRefused(
            f"Migrated company store failed foreign_key_check: {tuple(fk_violation)}"
        )
    return verify_v1_schema(connection)
