"""Transactional core v1 table rebuild and canonical v1 store migration."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable, Collection
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from .db import (
    BUSY_TIMEOUT_MS,
    RETIRED_SCHEMA_REVISION,
    SCHEMA_VERSION,
    StorageRefused,
    _has_table,
    connect_sqlite,
    read_retirement,
)
from .schema import (
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

_MIGRATABLE_DOMAINS = frozenset({"core", "trading", "paper"})

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

CANONICAL_TRADING_TABLES: tuple[str, ...] = (
    "execution_environment_binding",
    "trading_book_order",
    "trading_book_fill",
    "trading_book_position",
    "trading_book_valuation",
    "venue_qualification_receipt",
    "environment_promotion_record",
    "trading_live_migration_record",
    "trading_operating_window",
    "trading_statement",
    "trading_funding_settlement",
    "trading_fee_delivery",
    "trading_pit_catalog_manifest",
    "trading_strategy_release",
    "trading_challenger_trial",
    "trading_strategy_deployment",
)

RETIRED_TRADING_TABLES: tuple[str, ...] = (
    # 25 consolidated / retired tables
    "paper_binding",
    "paper_binding_change",
    "paper_order",
    "paper_fill",
    "paper_fill_fee",
    "paper_cancellation",
    "paper_position_observation",
    "paper_reconciliation",
    "paper_decision",
    "paper_review_quote",
    "paper_research_plan",
    "paper_research_snapshot",
    "paper_research_finding",
    "paper_research_decision",
    "hired_research_order_request",
    "paper_risk_state",
    "paper_fill_accounting_source",
    "paper_valuation",
    "agent_sandbox_order",
    "agent_sandbox_fill",
    "agent_sandbox_position",
    "company_paper_book",
    "company_paper_order",
    "company_paper_fill",
    "company_paper_position",
    # 9 renamed source tables
    "paper_fee_delivery",
    "paper_operating_window",
    "paper_statement",
    "paper_funding_settlement",
    "paper_pit_catalog_manifest",
    "paper_strategy_release",
    "paper_challenger_trial",
    "paper_strategy_deployment",
    "paper_live_migration_record",
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


def _table_columns(connection: sqlite3.Connection, table_name: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    ]


def _rebuild_single_table(
    connection: sqlite3.Connection,
    table_name: str,
    create_table_sql: str,
    create_index_sqls: tuple[str, ...],
    *,
    normalize_paper_domain: bool = False,
) -> None:
    if table_name not in CORE_REBUILD_TABLES:
        raise StorageRefused(f"Unsupported core rebuild table: {table_name}")
    columns = _table_columns(connection, table_name)
    if not columns:
        raise StorageRefused(f"Cannot rebuild missing table '{table_name}'")
    col_list = ", ".join(f'"{col}"' for col in columns)
    staging = f"temp._mig_{table_name}"
    connection.execute(f"DROP TABLE IF EXISTS {staging}")
    connection.execute(
        f"CREATE TEMP TABLE _mig_{table_name} AS SELECT {col_list} FROM {table_name}"
    )
    if normalize_paper_domain and table_name == "installed_domains":
        connection.execute(
            f"UPDATE {staging} SET domain = 'trading' WHERE domain = 'paper'"
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

    for required_table in CORE_REBUILD_TABLES:
        if not _has_table(connection, required_table):
            raise StorageRefused(
                f"Company store is missing canonical {required_table} table"
            )

    bad_duty = connection.execute(
        "SELECT id, state FROM duty WHERE state IS NULL OR state NOT IN ({}) LIMIT 1".format(
            ", ".join("?" for _ in _VALID_DUTY_STATES)
        ),
        tuple(sorted(_VALID_DUTY_STATES)),
    ).fetchone()
    if bad_duty is not None:
        raise StorageRefused(
            f"Invalid duty.state '{bad_duty[1]}' on duty '{bad_duty[0]}'"
        )

    bad_app = connection.execute(
        "SELECT question_id, application_status FROM decision_question WHERE application_status IS NULL OR application_status NOT IN ({}) LIMIT 1".format(
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
        if not isinstance(raw_domain, str) or raw_domain not in _MIGRATABLE_DOMAINS:
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

    normalize_paper = (
        target_domain is not None and canonical_domain_name(target_domain) == "trading"
    )
    table_ddl, index_ddl = extract_table_ddl("core", CORE_REBUILD_TABLES)
    for table_name in CORE_REBUILD_TABLES:
        _rebuild_single_table(
            connection,
            table_name,
            table_ddl[table_name],
            index_ddl[table_name],
            normalize_paper_domain=normalize_paper,
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


def _to_ns(raw_ts: int | None) -> int:
    if raw_ts is None or raw_ts <= 0:
        return 1
    if raw_ts >= 1_000_000_000_000:
        return int(raw_ts)
    return int(raw_ts) * 1_000_000_000


def _deterministic_binding_id(company_id: str, revision: str, account_id: str) -> str:
    digest = sha256(f"{company_id}:{revision}:{account_id}".encode("utf-8")).hexdigest()
    return f"mig-pb-{digest[:16]}"


def _stage_and_recreate_table(
    connection: sqlite3.Connection,
    table_name: str,
    create_table_sql: str,
    create_index_sqls: tuple[str, ...],
) -> tuple[str, list[str]]:
    """If table_name exists, move its rows to temp._mig_<table> and recreate canonical table."""
    staging = f"temp._mig_{table_name}"
    connection.execute(f"DROP TABLE IF EXISTS {staging}")
    cols: list[str] = []
    if _has_table(connection, table_name):
        cols = _table_columns(connection, table_name)
        col_list = ", ".join(f'"{c}"' for c in cols)
        connection.execute(
            f"CREATE TEMP TABLE _mig_{table_name} AS SELECT {col_list} FROM {table_name}"
        )
        connection.execute(f"DROP TABLE {table_name}")
    connection.execute(create_table_sql)
    for idx_sql in create_index_sqls:
        connection.execute(idx_sql)
    return staging, cols


def _migrate_trading_domain_in_tx(
    connection: sqlite3.Connection,
    active: frozenset[str],
) -> frozenset[str]:
    if "trading" not in active:
        stray = connection.execute(
            """SELECT name FROM sqlite_schema
               WHERE type = 'table'
                 AND (
                     name LIKE 'paper_%'
                     OR name LIKE 'company_paper_%'
                     OR name LIKE 'agent_sandbox_%'
                     OR name LIKE 'trading_%'
                     OR name IN (
                         'execution_environment_binding',
                         'venue_qualification_receipt',
                         'environment_promotion_record',
                         'hired_research_order_request'
                     )
                 )
               LIMIT 1"""
        ).fetchone()
        if stray is not None:
            raise StorageRefused(
                f"Core-only store contains unexpected trading table '{stray[0]}'"
            )
        return active

    table_ddl, index_ddl = extract_table_ddl("trading", CANONICAL_TRADING_TABLES)

    try:
        return _migrate_trading_tables_impl(connection, active, table_ddl, index_ddl)
    except sqlite3.IntegrityError as error:
        raise StorageRefused(
            f"Trading schema migration failed constraint check: {error}"
        ) from error


def _require_mig_text(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise StorageRefused(
            f"Migration column '{field}' must be text, got {type(value).__name__}"
        )
    if not allow_empty and not value.strip():
        raise StorageRefused(f"Migration column '{field}' must not be empty")
    return value


def _optional_mig_text(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> str | None:
    if value is None:
        return None
    return _require_mig_text(value, field, allow_empty=allow_empty)


def _require_mig_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageRefused(
            f"Migration column '{field}' must be an integer, got {type(value).__name__}"
        )
    return value


def _optional_mig_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _require_mig_int(value, field)


def _migrate_trading_tables_impl(
    connection: sqlite3.Connection,
    active: frozenset[str],
    table_ddl: dict[str, str],
    index_ddl: dict[str, tuple[str, ...]],
) -> frozenset[str]:
    # ------------------------------------------------------------------
    # Step 3: Rebuild execution_environment_binding & fold company_paper_book / paper_binding
    # ------------------------------------------------------------------
    binding_staging, binding_cols = _stage_and_recreate_table(
        connection,
        "execution_environment_binding",
        table_ddl["execution_environment_binding"],
        index_ddl["execution_environment_binding"],
    )

    book_meta_by_id: dict[str, tuple[str, str | None, str, str | None, str | None]] = {}
    if _has_table(connection, "company_paper_book"):
        for row in connection.execute(
            """SELECT binding_id, book_name, strategy_release_id, role,
                      shadow_of_live_binding_id, nominated_question_id
               FROM company_paper_book"""
        ).fetchall():
            book_bid = _require_mig_text(row[0], "company_paper_book.binding_id")
            book_meta_by_id[book_bid] = (
                _require_mig_text(row[1], "company_paper_book.book_name"),
                _optional_mig_text(row[2], "company_paper_book.strategy_release_id"),
                _require_mig_text(row[3], "company_paper_book.role"),
                _optional_mig_text(
                    row[4], "company_paper_book.shadow_of_live_binding_id"
                ),
                _optional_mig_text(row[5], "company_paper_book.nominated_question_id"),
            )

    existing_bindings: list[tuple[str, str, str, str, str]] = []
    # (binding_id, company_id, tier, account_id, binding_revision)
    if binding_cols:
        connection.row_factory = sqlite3.Row
        try:
            staged_rows = connection.execute(
                f"SELECT * FROM {binding_staging} ORDER BY created_at_ns ASC, binding_id ASC"
            ).fetchall()
        finally:
            connection.row_factory = None
        active_live_companies: set[str] = set()
        for row in staged_rows:
            row_dict = {col: row[col] for col in binding_cols}
            bid = _require_mig_text(
                row_dict["binding_id"], "execution_environment_binding.binding_id"
            )
            cid = _require_mig_text(
                row_dict["company_id"], "execution_environment_binding.company_id"
            )
            tier = _require_mig_text(
                row_dict["tier"], "execution_environment_binding.tier"
            )
            raw_env = _require_mig_text(
                row_dict["environment"], "execution_environment_binding.environment"
            )
            env = "agent_sandbox" if tier == "agent_sandbox" else raw_env
            owner_agent_id = _optional_mig_text(
                row_dict.get("owner_agent_id"),
                "execution_environment_binding.owner_agent_id",
            )
            if (tier == "agent_sandbox") != (owner_agent_id is not None):
                raise StorageRefused(
                    f"Invalid execution_environment_binding '{bid}': agent_sandbox owner mismatch"
                )
            cred_ref = _optional_mig_text(
                row_dict.get("credential_handle_ref"),
                "execution_environment_binding.credential_handle_ref",
            )
            if env == "live" and not cred_ref:
                raise StorageRefused(
                    f"Invalid execution_environment_binding '{bid}': live binding missing credential_handle_ref"
                )
            status = _require_mig_text(
                row_dict["status"], "execution_environment_binding.status"
            )
            if tier == "company_live" and status == "active":
                if cid in active_live_companies:
                    raise StorageRefused(
                        f"Duplicate active company_live binding for company '{cid}'"
                    )
                active_live_companies.add(cid)

            meta = book_meta_by_id.get(bid)
            book_name = (
                _optional_mig_text(
                    row_dict["book_name"], "execution_environment_binding.book_name"
                )
                if "book_name" in row_dict
                else None
            )
            strategy_release_id = (
                _optional_mig_text(
                    row_dict["strategy_release_id"],
                    "execution_environment_binding.strategy_release_id",
                )
                if "strategy_release_id" in row_dict
                else None
            )
            book_role = (
                _optional_mig_text(
                    row_dict["book_role"], "execution_environment_binding.book_role"
                )
                if "book_role" in row_dict
                else None
            )
            shadow_id = (
                _optional_mig_text(
                    row_dict["shadow_of_live_binding_id"],
                    "execution_environment_binding.shadow_of_live_binding_id",
                )
                if "shadow_of_live_binding_id" in row_dict
                else None
            )
            nom_qid = (
                _optional_mig_text(
                    row_dict["nominated_question_id"],
                    "execution_environment_binding.nominated_question_id",
                )
                if "nominated_question_id" in row_dict
                else None
            )
            if meta is not None:
                book_name = book_name or meta[0]
                strategy_release_id = strategy_release_id or meta[1]
                book_role = book_role or meta[2]
                shadow_id = shadow_id or meta[3]
                nom_qid = nom_qid or meta[4]
            binding_rev = (
                _require_mig_text(
                    row_dict["binding_revision"],
                    "execution_environment_binding.binding_revision",
                )
                if "binding_revision" in row_dict
                else "1"
            )
            acct_id = _require_mig_text(
                row_dict["account_id"], "execution_environment_binding.account_id"
            )
            dest_id = _require_mig_text(
                row_dict["destination_id"],
                "execution_environment_binding.destination_id",
            )
            conn_rev = _require_mig_text(
                row_dict["connector_revision"],
                "execution_environment_binding.connector_revision",
            )
            rules_digest = _require_mig_text(
                row_dict["instrument_rules_digest"],
                "execution_environment_binding.instrument_rules_digest",
            )
            currency = _require_mig_text(
                row_dict["currency"], "execution_environment_binding.currency"
            )
            starting_minor = _require_mig_int(
                row_dict["starting_minor_units"],
                "execution_environment_binding.starting_minor_units",
            )
            exposure_limit_minor = _require_mig_int(
                row_dict["exposure_limit_minor_units"],
                "execution_environment_binding.exposure_limit_minor_units",
            )
            fee_bps = (
                _require_mig_int(
                    row_dict["fee_bps"], "execution_environment_binding.fee_bps"
                )
                if "fee_bps" in row_dict
                else 0
            )
            slippage_bps = (
                _require_mig_int(
                    row_dict["slippage_bps"],
                    "execution_environment_binding.slippage_bps",
                )
                if "slippage_bps" in row_dict
                else 0
            )
            quote_source = (
                _require_mig_text(
                    row_dict["quote_source"],
                    "execution_environment_binding.quote_source",
                )
                if "quote_source" in row_dict
                else "live_read_only"
            )
            created_ns = _require_mig_int(
                row_dict["created_at_ns"],
                "execution_environment_binding.created_at_ns",
            )
            updated_ns = _require_mig_int(
                row_dict["updated_at_ns"],
                "execution_environment_binding.updated_at_ns",
            )

            connection.execute(
                """INSERT INTO execution_environment_binding (
                       binding_id, company_id, tier, environment, owner_agent_id,
                       account_id, destination_id, connector_revision,
                       instrument_rules_digest, currency, starting_minor_units,
                       exposure_limit_minor_units, fee_bps, slippage_bps, quote_source,
                       credential_handle_ref, binding_revision, book_name,
                       strategy_release_id, book_role, shadow_of_live_binding_id,
                       nominated_question_id, status, created_at_ns, updated_at_ns
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    bid,
                    cid,
                    tier,
                    env,
                    owner_agent_id,
                    acct_id,
                    dest_id,
                    conn_rev,
                    rules_digest,
                    currency,
                    starting_minor,
                    exposure_limit_minor,
                    fee_bps,
                    slippage_bps,
                    quote_source,
                    cred_ref,
                    binding_rev,
                    book_name,
                    strategy_release_id,
                    book_role,
                    shadow_id,
                    nom_qid,
                    status,
                    created_ns,
                    updated_ns,
                ),
            )
            existing_bindings.append((bid, cid, tier, acct_id, binding_rev))
        connection.execute(f"DROP TABLE IF EXISTS {binding_staging}")

    # Synthesize / link Gen 1 paper_binding & historical paper_binding_change bindings
    binding_by_rev: dict[str, str] = {}
    binding_by_rev_account: dict[tuple[str, str], str] = {}
    company_paper_binding_by_company: dict[str, str] = {}
    for bid, cid, tier, acct, brev in existing_bindings:
        if tier == "company_paper":
            binding_by_rev.setdefault(brev, bid)
            binding_by_rev_account.setdefault((brev, acct), bid)
            company_paper_binding_by_company.setdefault(cid, bid)

    if _has_table(connection, "paper_binding"):
        pb_rows = connection.execute(
            """SELECT revision, company_id, exposure_limit_minor_units, account_id,
                      connector_revision, instrument_rules_digest, currency,
                      starting_minor_units, environment, destination_id
               FROM paper_binding
               WHERE singleton = 1"""
        ).fetchall()
        for pb in pb_rows:
            pb_rev = _require_mig_text(pb[0], "paper_binding.revision")
            pb_cid = _require_mig_text(pb[1], "paper_binding.company_id")
            pb_limit = _require_mig_int(
                pb[2], "paper_binding.exposure_limit_minor_units"
            )
            pb_acct = _require_mig_text(pb[3], "paper_binding.account_id")
            pb_conn = _require_mig_text(pb[4], "paper_binding.connector_revision")
            pb_rules = _require_mig_text(pb[5], "paper_binding.instrument_rules_digest")
            pb_curr = _require_mig_text(pb[6], "paper_binding.currency")
            pb_start = _require_mig_int(pb[7], "paper_binding.starting_minor_units")
            pb_env = _require_mig_text(pb[8], "paper_binding.environment")
            pb_dest = _require_mig_text(pb[9], "paper_binding.destination_id")
            if (
                pb_env != "paper"
                or pb_start <= 0
                or pb_limit <= 0
                or pb_limit > pb_start
            ):
                raise StorageRefused(
                    f"Invalid legacy paper_binding row for company '{pb_cid}'"
                )
            matched_row = connection.execute(
                """SELECT binding_id, binding_revision
                   FROM execution_environment_binding
                   WHERE company_id = ? AND tier = 'company_paper'
                     AND account_id = ? AND status = 'active'
                   LIMIT 1""",
                (pb_cid, pb_acct),
            ).fetchone()
            if matched_row is not None:
                bid = str(matched_row[0])
                if str(matched_row[1]) == "1":
                    connection.execute(
                        """UPDATE execution_environment_binding
                           SET binding_revision = ?
                           WHERE binding_id = ?""",
                        (pb_rev, bid),
                    )
                binding_by_rev[pb_rev] = bid
                binding_by_rev_account[(pb_rev, pb_acct)] = bid
                company_paper_binding_by_company.setdefault(pb_cid, bid)
            else:
                bid = _deterministic_binding_id(pb_cid, pb_rev, pb_acct)
                connection.execute(
                    """INSERT INTO execution_environment_binding (
                           binding_id, company_id, tier, environment, owner_agent_id,
                           account_id, destination_id, connector_revision,
                           instrument_rules_digest, currency, starting_minor_units,
                           exposure_limit_minor_units, fee_bps, slippage_bps,
                           quote_source, credential_handle_ref, binding_revision,
                           book_name, strategy_release_id, book_role,
                           shadow_of_live_binding_id, nominated_question_id,
                           status, created_at_ns, updated_at_ns
                       ) VALUES (?, ?, 'company_paper', 'paper', NULL, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'legacy_paper_binding', NULL, ?, ?, NULL, 'active', NULL, NULL, 'active', 1, 1)""",
                    (
                        bid,
                        pb_cid,
                        pb_acct,
                        pb_dest,
                        pb_conn,
                        pb_rules,
                        pb_curr,
                        pb_start,
                        pb_limit,
                        pb_rev,
                        pb_acct,
                    ),
                )
                binding_by_rev[pb_rev] = bid
                binding_by_rev_account[(pb_rev, pb_acct)] = bid
                company_paper_binding_by_company.setdefault(pb_cid, bid)

            if _has_table(connection, "paper_binding_change"):
                referenced_revs: set[str] = set()
                if _has_table(connection, "paper_order"):
                    referenced_revs.update(
                        r[0]
                        for r in connection.execute(
                            "SELECT DISTINCT mandate_revision FROM paper_order"
                        ).fetchall()
                        if isinstance(r[0], str)
                    )
                if _has_table(connection, "paper_valuation"):
                    referenced_revs.update(
                        r[0]
                        for r in connection.execute(
                            "SELECT DISTINCT binding_revision FROM paper_valuation"
                        ).fetchall()
                        if isinstance(r[0], str)
                    )
                for chg in connection.execute(
                    """SELECT previous_revision, old_terms, requested_at_ns, effective_at_ns
                       FROM paper_binding_change
                       WHERE effective_at_ns IS NOT NULL"""
                ).fetchall():
                    prev_rev = _require_mig_text(
                        chg[0], "paper_binding_change.previous_revision"
                    )
                    if prev_rev not in referenced_revs or prev_rev in binding_by_rev:
                        continue
                    old_terms_raw = _require_mig_text(
                        chg[1], "paper_binding_change.old_terms", allow_empty=True
                    )
                    try:
                        old_terms = json.loads(old_terms_raw)
                    except json.JSONDecodeError:
                        old_terms = {}
                    if not isinstance(old_terms, dict):
                        old_terms = {}
                    old_acct = (
                        _optional_mig_text(
                            old_terms.get("account_id"),
                            "paper_binding_change.old_terms.account_id",
                        )
                        or pb_acct
                    )
                    old_dest = (
                        _optional_mig_text(
                            old_terms.get("destination_id"),
                            "paper_binding_change.old_terms.destination_id",
                        )
                        or pb_dest
                    )
                    old_conn = (
                        _optional_mig_text(
                            old_terms.get("connector_revision"),
                            "paper_binding_change.old_terms.connector_revision",
                        )
                        or pb_conn
                    )
                    old_rules = (
                        _optional_mig_text(
                            old_terms.get("instrument_rules_digest"),
                            "paper_binding_change.old_terms.instrument_rules_digest",
                        )
                        or pb_rules
                    )
                    old_curr = (
                        _optional_mig_text(
                            old_terms.get("currency"),
                            "paper_binding_change.old_terms.currency",
                        )
                        or pb_curr
                    )
                    raw_old_start = _optional_mig_int(
                        old_terms.get("starting_minor_units"),
                        "paper_binding_change.old_terms.starting_minor_units",
                    )
                    old_start = pb_start if raw_old_start is None else raw_old_start
                    raw_old_limit = _optional_mig_int(
                        old_terms.get("exposure_limit_minor_units"),
                        "paper_binding_change.old_terms.exposure_limit_minor_units",
                    )
                    old_limit = pb_limit if raw_old_limit is None else raw_old_limit
                    if old_start <= 0 or old_limit <= 0 or old_limit > old_start:
                        raise StorageRefused(
                            f"Invalid historical paper_binding_change '{prev_rev}'"
                        )
                    hist_bid = _deterministic_binding_id(pb_cid, prev_rev, old_acct)
                    created_ns = _to_ns(
                        _require_mig_int(chg[2], "paper_binding_change.requested_at_ns")
                    )
                    updated_ns = max(
                        created_ns,
                        _to_ns(
                            _require_mig_int(
                                chg[3], "paper_binding_change.effective_at_ns"
                            )
                        ),
                    )
                    connection.execute(
                        """INSERT INTO execution_environment_binding (
                               binding_id, company_id, tier, environment, owner_agent_id,
                               account_id, destination_id, connector_revision,
                               instrument_rules_digest, currency, starting_minor_units,
                               exposure_limit_minor_units, fee_bps, slippage_bps,
                               quote_source, credential_handle_ref, binding_revision,
                               book_name, strategy_release_id, book_role,
                               shadow_of_live_binding_id, nominated_question_id,
                               status, created_at_ns, updated_at_ns
                           ) VALUES (?, ?, 'company_paper', 'paper', NULL, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'legacy_paper_binding', NULL, ?, ?, NULL, 'retired', NULL, NULL, 'retired', ?, ?)
                           ON CONFLICT (binding_id) DO NOTHING""",
                        (
                            hist_bid,
                            pb_cid,
                            old_acct,
                            old_dest,
                            old_conn,
                            old_rules,
                            old_curr,
                            old_start,
                            old_limit,
                            prev_rev,
                            old_acct,
                            created_ns,
                            updated_ns,
                        ),
                    )
                    binding_by_rev[prev_rev] = hist_bid
                    binding_by_rev_account[(prev_rev, old_acct)] = hist_bid

    # ------------------------------------------------------------------
    # Step 4.1: Unified trading_book_{order,fill,position,valuation}
    # ------------------------------------------------------------------
    tb_order_staging, tb_order_cols = _stage_and_recreate_table(
        connection,
        "trading_book_order",
        table_ddl["trading_book_order"],
        index_ddl["trading_book_order"],
    )
    tb_fill_staging, tb_fill_cols = _stage_and_recreate_table(
        connection,
        "trading_book_fill",
        table_ddl["trading_book_fill"],
        index_ddl["trading_book_fill"],
    )
    tb_pos_staging, tb_pos_cols = _stage_and_recreate_table(
        connection,
        "trading_book_position",
        table_ddl["trading_book_position"],
        index_ddl["trading_book_position"],
    )
    tb_val_staging, tb_val_cols = _stage_and_recreate_table(
        connection,
        "trading_book_valuation",
        table_ddl["trading_book_valuation"],
        index_ddl["trading_book_valuation"],
    )

    valid_work_runs: set[str] = set()
    if _has_table(connection, "work_runs"):
        valid_work_runs = {
            str(r[0])
            for r in connection.execute("SELECT run_id FROM work_runs").fetchall()
        }

    # 4.1a: Re-insert pre-existing trading_book_* rows
    if tb_order_cols:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(f"SELECT * FROM {tb_order_staging}").fetchall()
        finally:
            connection.row_factory = None
        for r in rows:
            d = {c: r[c] for c in tb_order_cols}
            raw_run = d.get("run_id")
            safe_run = (
                str(raw_run)
                if isinstance(raw_run, str) and raw_run in valid_work_runs
                else None
            )
            connection.execute(
                """INSERT INTO trading_book_order (
                       order_id, binding_id, environment, company_id, actor_id,
                       action_id, external_order_id, run_id, decision_id,
                       mandate_revision, cancelled_at_ns, cancel_reason,
                       instrument_id, side, quantity, limit_minor_units,
                       market_price_minor_units, quote_observed_at_ns, quote_source,
                       fee_bps, slippage_bps, status, filled_quantity,
                       fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    d["order_id"],
                    d["binding_id"],
                    d["environment"],
                    d["company_id"],
                    d["actor_id"],
                    d["action_id"],
                    d.get("external_order_id"),
                    safe_run,
                    d.get("decision_id"),
                    d.get("mandate_revision"),
                    d.get("cancelled_at_ns"),
                    d.get("cancel_reason"),
                    d["instrument_id"],
                    d["side"],
                    d["quantity"],
                    d["limit_minor_units"],
                    d["market_price_minor_units"],
                    d["quote_observed_at_ns"],
                    d["quote_source"],
                    d["fee_bps"],
                    d["slippage_bps"],
                    d["status"],
                    d["filled_quantity"],
                    d.get("fill_price_minor_units"),
                    d["fee_minor_units"],
                    d["created_at_ns"],
                    d["updated_at_ns"],
                ),
            )
        connection.execute(f"DROP TABLE IF EXISTS {tb_order_staging}")

    if tb_fill_cols:
        col_list = ", ".join(f'"{c}"' for c in tb_fill_cols)
        connection.execute(
            f"INSERT INTO trading_book_fill ({col_list}) SELECT {col_list} FROM {tb_fill_staging}"
        )
        connection.execute(f"DROP TABLE IF EXISTS {tb_fill_staging}")

    if tb_pos_cols:
        col_list = ", ".join(f'"{c}"' for c in tb_pos_cols)
        connection.execute(
            f"INSERT INTO trading_book_position ({col_list}) SELECT {col_list} FROM {tb_pos_staging}"
        )
        connection.execute(f"DROP TABLE IF EXISTS {tb_pos_staging}")

    if tb_val_cols:
        col_list = ", ".join(f'"{c}"' for c in tb_val_cols)
        connection.execute(
            f"INSERT INTO trading_book_valuation ({col_list}) SELECT {col_list} FROM {tb_val_staging}"
        )
        connection.execute(f"DROP TABLE IF EXISTS {tb_val_staging}")

    # 4.1b: Merge company_paper_{order,fill,position} (deduplicating dual-writes)
    if _has_table(connection, "company_paper_order"):
        connection.execute(
            """INSERT INTO trading_book_order (
                   order_id, binding_id, environment, company_id, actor_id,
                   action_id, external_order_id, run_id, decision_id,
                   mandate_revision, cancelled_at_ns, cancel_reason,
                   instrument_id, side, quantity, limit_minor_units,
                   market_price_minor_units, quote_observed_at_ns, quote_source,
                   fee_bps, slippage_bps, status, filled_quantity,
                   fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
               )
               SELECT order_id, binding_id, 'paper', company_id, actor_id,
                      action_id, NULL, NULL, NULL,
                      NULL, NULL, NULL,
                      instrument_id, side, quantity, limit_minor_units,
                      market_price_minor_units, quote_observed_at_ns, quote_source,
                      fee_bps, slippage_bps, status, filled_quantity,
                      fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
               FROM company_paper_order
               WHERE true
               ON CONFLICT (order_id) DO NOTHING"""
        )
    if _has_table(connection, "company_paper_fill"):
        connection.execute(
            """INSERT INTO trading_book_fill (
                   fill_id, order_id, binding_id, environment, company_id,
                   actor_id, external_fill_id, instrument_id, side, quantity,
                   price_minor_units, fee_minor_units, realized_pnl_minor_units,
                   filled_at_ns
               )
               SELECT fill_id, order_id, binding_id, 'paper', company_id,
                      actor_id, NULL, instrument_id, side, quantity,
                      price_minor_units, fee_minor_units, realized_pnl_minor_units,
                      filled_at_ns
               FROM company_paper_fill
               WHERE true
               ON CONFLICT (fill_id) DO NOTHING"""
        )
    if _has_table(connection, "company_paper_position"):
        connection.execute(
            """INSERT INTO trading_book_position (
                   binding_id, environment, company_id, instrument_id,
                   quantity, avg_entry_minor_units, mark_price_minor_units,
                   realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
               )
               SELECT binding_id, 'paper', company_id, instrument_id,
                      quantity, avg_entry_minor_units, 0,
                      realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
               FROM company_paper_position
               WHERE true
               ON CONFLICT (binding_id, instrument_id) DO NOTHING"""
        )

    # 4.1c: Merge agent_sandbox_{order,fill,position} -> environment = 'agent_sandbox'
    if _has_table(connection, "agent_sandbox_order"):
        connection.execute(
            """INSERT INTO trading_book_order (
                   order_id, binding_id, environment, company_id, actor_id,
                   action_id, external_order_id, run_id, decision_id,
                   mandate_revision, cancelled_at_ns, cancel_reason,
                   instrument_id, side, quantity, limit_minor_units,
                   market_price_minor_units, quote_observed_at_ns, quote_source,
                   fee_bps, slippage_bps, status, filled_quantity,
                   fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
               )
               SELECT order_id, binding_id, 'agent_sandbox', company_id, agent_id,
                      action_id, NULL, NULL, NULL,
                      NULL, NULL, NULL,
                      instrument_id, side, quantity, limit_minor_units,
                      market_price_minor_units, quote_observed_at_ns, quote_source,
                      fee_bps, slippage_bps, status, filled_quantity,
                      fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
               FROM agent_sandbox_order
               WHERE true
               ON CONFLICT DO NOTHING"""
        )
    if _has_table(connection, "agent_sandbox_fill"):
        connection.execute(
            """INSERT INTO trading_book_fill (
                   fill_id, order_id, binding_id, environment, company_id,
                   actor_id, external_fill_id, instrument_id, side, quantity,
                   price_minor_units, fee_minor_units, realized_pnl_minor_units,
                   filled_at_ns
               )
               SELECT fill_id, order_id, binding_id, 'agent_sandbox', company_id,
                      agent_id, NULL, instrument_id, side, quantity,
                      price_minor_units, fee_minor_units, realized_pnl_minor_units,
                      filled_at_ns
               FROM agent_sandbox_fill
               WHERE true
               ON CONFLICT (fill_id) DO NOTHING"""
        )
    if _has_table(connection, "agent_sandbox_position"):
        connection.execute(
            """INSERT INTO trading_book_position (
                   binding_id, environment, company_id, instrument_id,
                   quantity, avg_entry_minor_units, mark_price_minor_units,
                   realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
               )
               SELECT binding_id, 'agent_sandbox', company_id, instrument_id,
                      quantity, avg_entry_minor_units, 0,
                      realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
               FROM agent_sandbox_position
               WHERE true
               ON CONFLICT (binding_id, instrument_id) DO NOTHING"""
        )

    # 4.1d: Migrate Gen 1 paper_order / paper_fill / paper_fill_fee / paper_cancellation
    if _has_table(connection, "paper_order"):
        default_company_row = connection.execute(
            "SELECT id FROM company WHERE singleton = 1"
        ).fetchone()
        if default_company_row is None:
            raise StorageRefused("Cannot migrate paper_order without company row")
        default_company_id = _require_mig_text(default_company_row[0], "company.id")

        has_duty_run = _has_table(connection, "duty_run")
        has_paper_decision = _has_table(connection, "paper_decision")
        has_paper_research_dec = _has_table(connection, "paper_research_decision")
        has_review_quote = _has_table(connection, "paper_review_quote")
        has_paper_fill = _has_table(connection, "paper_fill")
        has_paper_fill_fee = _has_table(connection, "paper_fill_fee")
        has_paper_cancel = _has_table(connection, "paper_cancellation")
        has_pos_obs = _has_table(connection, "paper_position_observation")

        gen1_orders = connection.execute(
            """SELECT order_id, run_id, mandate_revision, account_id, action_id,
                      instrument_rules_digest, quantity, limit_minor_units
               FROM paper_order
               ORDER BY rowid ASC"""
        ).fetchall()

        gen1_position_accum: dict[tuple[str, str], list[int]] = {}
        # (binding_id, instrument_id) -> [qty, cost_sum, fee_sum, max_filled_ns]

        for row in gen1_orders:
            order_id = _require_mig_text(row[0], "paper_order.order_id")
            legacy_run_id = _optional_mig_text(row[1], "paper_order.run_id")
            mandate_rev = _require_mig_text(row[2], "paper_order.mandate_revision")
            account_id = _require_mig_text(row[3], "paper_order.account_id")
            action_id = _require_mig_text(row[4], "paper_order.action_id")
            instrument_id = _require_mig_text(
                row[5], "paper_order.instrument_rules_digest"
            )
            quantity = _require_mig_int(row[6], "paper_order.quantity")
            limit_minor_units = _require_mig_int(
                row[7], "paper_order.limit_minor_units"
            )

            resolved_binding_id = binding_by_rev_account.get(
                (mandate_rev, account_id)
            ) or binding_by_rev.get(mandate_rev)
            if resolved_binding_id is None:
                # Try matching any company_paper binding for that account
                matched = connection.execute(
                    """SELECT binding_id FROM execution_environment_binding
                       WHERE tier = 'company_paper' AND account_id = ?
                       ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END
                       LIMIT 1""",
                    (account_id,),
                ).fetchone()
                if matched is not None:
                    resolved_binding_id = str(matched[0])
            if resolved_binding_id is None:
                raise StorageRefused(
                    f"Cannot resolve execution_environment_binding for paper_order '{order_id}'"
                )

            bind_company_row = connection.execute(
                "SELECT company_id FROM execution_environment_binding WHERE binding_id = ?",
                (resolved_binding_id,),
            ).fetchone()
            company_id = (
                _require_mig_text(
                    bind_company_row[0], "execution_environment_binding.company_id"
                )
                if bind_company_row is not None
                else default_company_id
            )

            actor_id = "system"
            created_at_ns = 1
            if legacy_run_id is not None and legacy_run_id in valid_work_runs:
                wr = connection.execute(
                    "SELECT agent_id, created_at_ns FROM work_runs WHERE run_id = ?",
                    (legacy_run_id,),
                ).fetchone()
                if wr is not None:
                    actor_id = _require_mig_text(wr[0], "work_runs.agent_id")
                    created_at_ns = _to_ns(
                        _require_mig_int(wr[1], "work_runs.created_at_ns")
                    )
            elif legacy_run_id is not None and has_duty_run:
                dr = connection.execute(
                    """SELECT d.agent_id
                       FROM duty_run dr
                       JOIN duty d ON d.id = dr.duty_id
                       WHERE dr.run_id = ?""",
                    (legacy_run_id,),
                ).fetchone()
                if dr is not None:
                    actor_id = _require_mig_text(dr[0], "duty.agent_id")

            decision_id: str | None = None
            if legacy_run_id is not None and has_paper_decision:
                dec = connection.execute(
                    "SELECT decision_id FROM paper_decision WHERE run_id = ? LIMIT 1",
                    (legacy_run_id,),
                ).fetchone()
                if dec is not None:
                    decision_id = _require_mig_text(
                        dec[0], "paper_decision.decision_id"
                    )
            if (
                decision_id is None
                and legacy_run_id is not None
                and has_paper_research_dec
            ):
                rdec = connection.execute(
                    "SELECT decision_id FROM paper_research_decision WHERE run_id = ? LIMIT 1",
                    (legacy_run_id,),
                ).fetchone()
                if rdec is not None:
                    decision_id = _require_mig_text(
                        rdec[0], "paper_research_decision.decision_id"
                    )

            market_price = limit_minor_units
            quote_observed_at_ns = created_at_ns
            quote_source = "legacy_paper_order"
            if legacy_run_id is not None and has_review_quote:
                qrow = connection.execute(
                    """SELECT quote_minor_units, observed_at, connector_revision
                       FROM paper_review_quote
                       WHERE run_id = ?""",
                    (legacy_run_id,),
                ).fetchone()
                if qrow is not None:
                    quote_minor = _optional_mig_int(
                        qrow[0], "paper_review_quote.quote_minor_units"
                    )
                    if quote_minor is not None and quote_minor > 0:
                        market_price = quote_minor
                    quote_observed_at_ns = _to_ns(
                        _require_mig_int(qrow[1], "paper_review_quote.observed_at")
                    )
                    conn_rev = _optional_mig_text(
                        qrow[2],
                        "paper_review_quote.connector_revision",
                        allow_empty=True,
                    )
                    if conn_rev is not None and conn_rev.strip():
                        quote_source = conn_rev

            fills_rows: list[tuple[str, int, int, int, int]] = []
            filled_qty = 0
            total_notional = 0
            total_fee = 0
            if has_paper_fill:
                raw_fills = connection.execute(
                    "SELECT fill_id, quantity, price_minor_units FROM paper_fill WHERE order_id = ? ORDER BY rowid ASC",
                    (order_id,),
                ).fetchall()
                for frow in raw_fills:
                    fid = _require_mig_text(frow[0], "paper_fill.fill_id")
                    fqty = _require_mig_int(frow[1], "paper_fill.quantity")
                    fprice = _require_mig_int(frow[2], "paper_fill.price_minor_units")
                    ffee = 0
                    if has_paper_fill_fee:
                        fee_row = connection.execute(
                            "SELECT fee_minor_units FROM paper_fill_fee WHERE fill_id = ?",
                            (fid,),
                        ).fetchone()
                        if fee_row is not None:
                            ffee = _require_mig_int(
                                fee_row[0], "paper_fill_fee.fee_minor_units"
                            )
                    filled_at_ns = created_at_ns
                    if has_pos_obs:
                        obs_row = connection.execute(
                            "SELECT available_at FROM paper_position_observation WHERE fill_id = ?",
                            (fid,),
                        ).fetchone()
                        if obs_row is not None:
                            obs_at = _require_mig_int(
                                obs_row[0], "paper_position_observation.available_at"
                            )
                            filled_at_ns = max(created_at_ns, _to_ns(obs_at))
                    fills_rows.append((fid, fqty, fprice, ffee, filled_at_ns))
                    filled_qty += fqty
                    total_notional += fqty * fprice
                    total_fee += ffee

            cancelled_at_ns: int | None = None
            cancel_reason: str | None = None
            if has_paper_cancel:
                crow = connection.execute(
                    "SELECT available_at, outcome FROM paper_cancellation WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                if crow is not None:
                    avail_at = _optional_mig_int(
                        crow[0], "paper_cancellation.available_at"
                    )
                    cancelled_at_ns = max(
                        created_at_ns,
                        _to_ns(avail_at) if avail_at is not None else created_at_ns,
                    )
                    cancel_reason = _require_mig_text(
                        crow[1], "paper_cancellation.outcome"
                    )

            if cancelled_at_ns is not None:
                status = "cancelled"
            elif filled_qty >= quantity:
                status = "filled"
            else:
                status = "open"

            avg_fill_price = (
                max(1, total_notional // filled_qty) if filled_qty > 0 else None
            )
            max_fill_ns = max((f[4] for f in fills_rows), default=created_at_ns)
            updated_at_ns = max(
                created_at_ns, max_fill_ns, cancelled_at_ns or created_at_ns
            )
            safe_run_id = (
                legacy_run_id
                if legacy_run_id is not None and legacy_run_id in valid_work_runs
                else None
            )

            connection.execute(
                """INSERT INTO trading_book_order (
                       order_id, binding_id, environment, company_id, actor_id,
                       action_id, external_order_id, run_id, decision_id,
                       mandate_revision, cancelled_at_ns, cancel_reason,
                       instrument_id, side, quantity, limit_minor_units,
                       market_price_minor_units, quote_observed_at_ns, quote_source,
                       fee_bps, slippage_bps, status, filled_quantity,
                       fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
                   ) VALUES (?, ?, 'paper', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'buy', ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    resolved_binding_id,
                    company_id,
                    actor_id,
                    action_id,
                    safe_run_id,
                    decision_id,
                    mandate_rev,
                    cancelled_at_ns,
                    cancel_reason,
                    instrument_id,
                    quantity,
                    limit_minor_units,
                    market_price,
                    quote_observed_at_ns,
                    quote_source,
                    status,
                    min(filled_qty, quantity),
                    avg_fill_price,
                    max(0, total_fee),
                    created_at_ns,
                    updated_at_ns,
                ),
            )

            for fid, fqty, fprice, ffee, filled_at_ns in fills_rows:
                connection.execute(
                    """INSERT INTO trading_book_fill (
                           fill_id, order_id, binding_id, environment, company_id,
                           actor_id, external_fill_id, instrument_id, side,
                           quantity, price_minor_units, fee_minor_units,
                           realized_pnl_minor_units, filled_at_ns
                       ) VALUES (?, ?, ?, 'paper', ?, ?, NULL, ?, 'buy', ?, ?, ?, 0, ?)""",
                    (
                        fid,
                        order_id,
                        resolved_binding_id,
                        company_id,
                        actor_id,
                        instrument_id,
                        fqty,
                        fprice,
                        max(0, ffee),
                        filled_at_ns,
                    ),
                )
                key = (resolved_binding_id, instrument_id)
                acc = gen1_position_accum.setdefault(key, [0, 0, 0, filled_at_ns])
                acc[0] += fqty
                acc[1] += fqty * fprice
                acc[2] += max(0, ffee)
                acc[3] = max(acc[3], filled_at_ns)

        for (bid, inst_id), (
            pos_qty,
            cost_sum,
            fee_sum,
            upd_ns,
        ) in gen1_position_accum.items():
            bind_company_row = connection.execute(
                "SELECT company_id FROM execution_environment_binding WHERE binding_id = ?",
                (bid,),
            ).fetchone()
            cid = (
                str(bind_company_row[0])
                if bind_company_row is not None
                else default_company_id
            )
            avg_entry = cost_sum // pos_qty if pos_qty > 0 else 0
            connection.execute(
                """INSERT INTO trading_book_position (
                       binding_id, environment, company_id, instrument_id,
                       quantity, avg_entry_minor_units, mark_price_minor_units,
                       realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
                   ) VALUES (?, 'paper', ?, ?, ?, ?, ?, 0, ?, ?)""",
                (bid, cid, inst_id, pos_qty, avg_entry, avg_entry, fee_sum, upd_ns),
            )

    # 4.1e: Migrate Gen 1 paper_valuation -> trading_book_valuation
    if _has_table(connection, "paper_valuation"):
        val_rows = connection.execute(
            """SELECT capture_id, binding_revision, policy, evidence,
                      trading_nav_atoms, revision
               FROM paper_valuation
               ORDER BY rowid ASC"""
        ).fetchall()
        for vrow in val_rows:
            capture_id = _require_mig_text(vrow[0], "paper_valuation.capture_id")
            brev = _require_mig_text(vrow[1], "paper_valuation.binding_revision")
            policy = _require_mig_text(vrow[2], "paper_valuation.policy")
            evidence_raw = _require_mig_text(vrow[3], "paper_valuation.evidence")
            nav_atoms = _require_mig_int(vrow[4], "paper_valuation.trading_nav_atoms")
            rev = _require_mig_text(vrow[5], "paper_valuation.revision")

            bid = binding_by_rev.get(brev)
            if bid is None:
                matched = connection.execute(
                    """SELECT binding_id FROM execution_environment_binding
                       WHERE tier = 'company_paper'
                       ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at_ns ASC
                       LIMIT 1"""
                ).fetchone()
                if matched is not None:
                    bid = str(matched[0])
            if bid is None:
                raise StorageRefused(
                    f"Cannot resolve execution_environment_binding for paper_valuation '{capture_id}'"
                )
            cid_row = connection.execute(
                "SELECT company_id FROM execution_environment_binding WHERE binding_id = ?",
                (bid,),
            ).fetchone()
            assert cid_row is not None
            cid = str(cid_row[0])

            try:
                ev = json.loads(evidence_raw)
            except json.JSONDecodeError:
                ev = {}
            if not isinstance(ev, dict):
                ev = {}
            obs = (
                ev.get("observation") if isinstance(ev.get("observation"), dict) else {}
            )
            bal = obs.get("balance") if isinstance(obs.get("balance"), dict) else {}
            raw_cash = bal.get("cash_atoms")
            cash_minor = (
                raw_cash
                if isinstance(raw_cash, int) and not isinstance(raw_cash, bool)
                else nav_atoms
            )
            raw_obs_ns = obs.get("observed_ns")
            recorded_ns = _to_ns(
                raw_obs_ns
                if isinstance(raw_obs_ns, int) and not isinstance(raw_obs_ns, bool)
                else 1
            )
            gross_exposure = max(0, nav_atoms - cash_minor)

            connection.execute(
                """INSERT INTO trading_book_valuation (
                       valuation_id, binding_id, environment, company_id, label,
                       nav_minor_units, cash_minor_units, gross_exposure_minor_units,
                       realized_pnl_minor_units, unrealized_pnl_minor_units,
                       total_fees_minor_units, source, binding_revision, policy,
                       evidence_json, revision, recorded_at_ns
                   ) VALUES (?, ?, 'paper', ?, ?, ?, ?, ?, 0, 0, 0, 'paper_valuation', ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (
                    capture_id,
                    bid,
                    cid,
                    policy,
                    nav_atoms,
                    cash_minor,
                    gross_exposure,
                    brev,
                    policy,
                    evidence_raw,
                    rev,
                    recorded_ns,
                ),
            )

    # ------------------------------------------------------------------
    # Step 4.2: Ensure venue_qualification_receipt & environment_promotion_record
    # ------------------------------------------------------------------
    for tbl in ("venue_qualification_receipt", "environment_promotion_record"):
        stg, cols = _stage_and_recreate_table(
            connection, tbl, table_ddl[tbl], index_ddl[tbl]
        )
        if cols:
            col_list = ", ".join(f'"{c}"' for c in cols)
            connection.execute(
                f"INSERT INTO {tbl} ({col_list}) SELECT {col_list} FROM {stg}"
            )
            connection.execute(f"DROP TABLE IF EXISTS {stg}")

    # ------------------------------------------------------------------
    # Step 4.3: 9 Renamed paper_* -> trading_* tables
    # ------------------------------------------------------------------
    rename_pairs: tuple[tuple[str, str, str], ...] = (
        ("paper_live_migration_record", "trading_live_migration_record", "rowid ASC"),
        ("paper_operating_window", "trading_operating_window", "rowid ASC"),
        ("paper_statement", "trading_statement", "rowid ASC"),
        ("paper_funding_settlement", "trading_funding_settlement", "rowid ASC"),
        ("paper_pit_catalog_manifest", "trading_pit_catalog_manifest", "rowid ASC"),
        ("paper_strategy_release", "trading_strategy_release", "rowid ASC"),
        ("paper_challenger_trial", "trading_challenger_trial", "rowid ASC"),
        ("paper_strategy_deployment", "trading_strategy_deployment", "rowid ASC"),
    )
    for old_tbl, new_tbl, order_by in rename_pairs:
        stg, existing_cols = _stage_and_recreate_table(
            connection, new_tbl, table_ddl[new_tbl], index_ddl[new_tbl]
        )
        if existing_cols:
            col_list = ", ".join(f'"{c}"' for c in existing_cols)
            connection.execute(
                f"INSERT INTO {new_tbl} ({col_list}) SELECT {col_list} FROM {stg}"
            )
            connection.execute(f"DROP TABLE IF EXISTS {stg}")
        if _has_table(connection, old_tbl):
            old_cols = _table_columns(connection, old_tbl)
            col_list = ", ".join(f'"{c}"' for c in old_cols)
            connection.execute(
                f"INSERT INTO {new_tbl} ({col_list}) SELECT {col_list} FROM {old_tbl} ORDER BY {order_by} ON CONFLICT DO NOTHING"
            )

    # trading_fee_delivery (must verify against paper_fill_accounting_source & trading_book_fill)
    fee_stg, fee_cols = _stage_and_recreate_table(
        connection,
        "trading_fee_delivery",
        table_ddl["trading_fee_delivery"],
        index_ddl["trading_fee_delivery"],
    )
    if fee_cols:
        col_list = ", ".join(f'"{c}"' for c in fee_cols)
        connection.execute(
            f"INSERT INTO trading_fee_delivery ({col_list}) SELECT {col_list} FROM {fee_stg}"
        )
        connection.execute(f"DROP TABLE IF EXISTS {fee_stg}")
    if _has_table(connection, "paper_fee_delivery"):
        has_acct_src = _has_table(connection, "paper_fill_accounting_source")
        fee_rows = connection.execute(
            "SELECT fill_id, delivery_digest, acknowledged, journal_id FROM paper_fee_delivery"
        ).fetchall()
        for frow in fee_rows:
            fid = _require_mig_text(frow[0], "paper_fee_delivery.fill_id")
            delivery_digest = _require_mig_text(
                frow[1], "paper_fee_delivery.delivery_digest"
            )
            acknowledged = _require_mig_int(frow[2], "paper_fee_delivery.acknowledged")
            journal_id = _optional_mig_text(frow[3], "paper_fee_delivery.journal_id")
            if not has_acct_src:
                raise StorageRefused(
                    f"Cannot migrate paper_fee_delivery '{fid}': missing paper_fill_accounting_source"
                )
            src_ok = connection.execute(
                "SELECT 1 FROM paper_fill_accounting_source WHERE fill_id = ?",
                (fid,),
            ).fetchone()
            fill_ok = connection.execute(
                "SELECT 1 FROM trading_book_fill WHERE fill_id = ?",
                (fid,),
            ).fetchone()
            if src_ok is None or fill_ok is None:
                raise StorageRefused(
                    f"Orphan paper_fee_delivery row '{fid}' cannot map to trading_book_fill"
                )
            connection.execute(
                """INSERT INTO trading_fee_delivery (
                       fill_id, delivery_digest, acknowledged, journal_id
                   ) VALUES (?, ?, ?, ?)""",
                (fid, delivery_digest, acknowledged, journal_id),
            )

    # ------------------------------------------------------------------
    # Step 4.4: Active paper_risk_state -> restriction_cause
    # ------------------------------------------------------------------
    if _has_table(connection, "paper_risk_state"):
        risk_rows = connection.execute(
            """SELECT company_id, effective_revision, last_command_id, updated_at_ns
               FROM paper_risk_state
               WHERE suspended = 1"""
        ).fetchall()
        for rrow in risk_rows:
            cid = _require_mig_text(rrow[0], "paper_risk_state.company_id")
            eff_rev = _require_mig_int(rrow[1], "paper_risk_state.effective_revision")
            last_cmd = _optional_mig_text(rrow[2], "paper_risk_state.last_command_id")
            updated_ns = max(
                1, _require_mig_int(rrow[3], "paper_risk_state.updated_at_ns")
            )
            existing_cause = connection.execute(
                """SELECT 1 FROM restriction_cause
                   WHERE company_id = ?
                     AND cause_category = 'trading_risk_pause'
                     AND scope_ref = 'execution:all'
                     AND active = 1
                   LIMIT 1""",
                (cid,),
            ).fetchone()
            if existing_cause is None:
                max_rev_row = connection.execute(
                    "SELECT COALESCE(MAX(restriction_revision), 0) FROM restriction_cause WHERE company_id = ?",
                    (cid,),
                ).fetchone()
                next_rev = int(max_rev_row[0]) + 1
                rid = f"mig-risk-{cid[:12]}-{eff_rev}"
                if last_cmd is not None:
                    cmd_exists = connection.execute(
                        "SELECT 1 FROM command_receipt WHERE company_id = ? AND command_id = ?",
                        (cid, last_cmd),
                    ).fetchone()
                    if cmd_exists is None:
                        last_cmd = None
                connection.execute(
                    """INSERT INTO restriction_cause (
                           restriction_id, company_id, cause_category, scope_ref,
                           restriction_revision, active, operator_releasable,
                           reason, recorded_at_ns, cleared_at_ns, last_command_id
                       ) VALUES (?, ?, 'trading_risk_pause', 'execution:all', ?, 1, 1, ?, ?, NULL, ?)""",
                    (
                        rid,
                        cid,
                        next_rev,
                        "Migrated active paper_risk_state suspension",
                        updated_ns,
                        last_cmd,
                    ),
                )

    # ------------------------------------------------------------------
    # Step 5: Drop all 34 retired / renamed source tables
    # ------------------------------------------------------------------
    for retired_table in RETIRED_TRADING_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {retired_table}")

    return active


def migrate_v1_store_to_canonical(store: Path) -> frozenset[str]:
    """Atomically migrate a pre-cleanup v1 company store to canonical v1 core+trading schema."""
    try:
        st = os.lstat(store)
    except OSError as error:
        raise StorageRefused(f"Company store does not exist: {store}") from error
    if not stat.S_ISREG(st.st_mode):
        raise StorageRefused(f"Company store must be a regular file: {store}")

    try:
        connection = connect_sqlite(
            f"{store.absolute().as_uri()}?mode=rw",
            uri=True,
            autocommit=True,
        )
    except sqlite3.OperationalError as error:
        raise StorageRefused(
            "Company store could not be opened for migration"
        ) from error

    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.execute("PRAGMA temp_store = MEMORY")

        if read_retirement(connection) is not None:
            raise StorageRefused("Company store is retired and cannot be migrated")
        if _has_table(connection, "company"):
            row = connection.execute(
                "SELECT schema_revision FROM company WHERE singleton = 1"
            ).fetchone()
            if row is not None and int(row[0]) == RETIRED_SCHEMA_REVISION:
                raise StorageRefused("Company store is retired and cannot be migrated")

        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise StorageRefused(
                f"Unsupported company store schema version: {version} (expected {SCHEMA_VERSION})"
            )

        connection.execute("BEGIN IMMEDIATE")
        try:
            verified = rebuild_core_v1_tables(
                connection,
                target_domain="trading",
                domain_migrator=_migrate_trading_domain_in_tx,
            )
            connection.execute("COMMIT")
            return verified
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
