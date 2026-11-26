"""Canonical v1 SQLite schema packs, opt-in domain activation, and tamper detection."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from time import time_ns

from .db import SCHEMA_VERSION, StorageRefused
from .kernel import append_audit_event

VALID_DOMAINS: tuple[str, ...] = ("core", "trading", "paper")
_SCHEMA_ROOT = Path(__file__).resolve().parent / "schema" / "v1"


def canonical_domain_name(domain: str) -> str:
    """Map legacy domain aliases to their canonical schema domain name."""
    if domain == "core":
        return "core"
    if domain in ("trading", "paper"):
        return "trading"
    raise StorageRefused(f"Unsupported schema domain: {domain}")


@dataclass(frozen=True)
class SchemaPack:
    domain: str
    files: tuple[tuple[str, str], ...]
    pack_sha256: str


def _split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            stripped = candidate.strip()
            if stripped:
                statements.append(stripped)
            buffer.clear()
    remainder = "".join(buffer).strip()
    if remainder:
        raise StorageRefused("Incomplete SQL statement in canonical schema pack")
    return statements


def _execute_sql_script_in_tx(connection: sqlite3.Connection, sql: str) -> None:
    """Execute DDL statements inside the caller's active transaction."""
    for statement in _split_sql_statements(sql):
        connection.execute(statement)


@lru_cache(maxsize=8)
def load_domain_pack(domain: str) -> SchemaPack:
    """Load the bundled canonical v1 SQL files for a domain."""
    if domain not in VALID_DOMAINS:
        raise StorageRefused(f"Unsupported schema domain: {domain}")
    domain_dir = _SCHEMA_ROOT / domain
    if domain == "trading" and not domain_dir.is_dir():
        fallback_dir = _SCHEMA_ROOT / "paper"
        if fallback_dir.is_dir():
            domain_dir = fallback_dir
    if not domain_dir.is_dir():
        raise StorageRefused(f"Missing canonical v1 schema directory for {domain}")
    files: list[tuple[str, str]] = []
    for sql_file in sorted(domain_dir.glob("*.sql")):
        files.append((sql_file.name, sql_file.read_text(encoding="utf-8")))
    if not files:
        raise StorageRefused(f"No canonical v1 SQL files found for {domain}")
    digest = sha256(
        json.dumps(
            [[name, sql] for name, sql in files],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return SchemaPack(domain=domain, files=tuple(files), pack_sha256=digest)


def compute_sqlite_schema_sha256(connection: sqlite3.Connection) -> str:
    """Hash canonical user-defined sqlite_schema definitions (tables, indexes, triggers, views)."""
    objects = [
        [
            str(row[0]),
            str(row[1]),
            str(row[2]),
            "" if row[3] is None else str(row[3]),
        ]
        for row in connection.execute(
            """SELECT type, name, tbl_name, sql FROM sqlite_schema
               WHERE name NOT LIKE 'sqlite_%'
               ORDER BY type, name, tbl_name, COALESCE(sql, '')"""
        ).fetchall()
    ]
    return sha256(
        json.dumps(objects, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_domain_order(domains: frozenset[str]) -> tuple[str, ...]:
    invalid = set(domains) - set(VALID_DOMAINS)
    if (
        invalid
        or "core" not in domains
        or ("trading" in domains and "paper" in domains)
    ):
        raise StorageRefused(f"Invalid active schema domain set: {sorted(domains)}")
    return tuple(domain for domain in VALID_DOMAINS if domain in domains)


@lru_cache(maxsize=8)
def _build_reference_schema(
    domains: frozenset[str],
) -> tuple[str, tuple[tuple[str, str, str, str], ...]]:
    ordered = _canonical_domain_order(domains)
    with closing(sqlite3.connect(":memory:", autocommit=True)) as reference:
        reference.execute("PRAGMA foreign_keys = ON")
        reference.execute("PRAGMA trusted_schema = OFF")
        for domain in ordered:
            pack = load_domain_pack(domain)
            for _name, sql in pack.files:
                _execute_sql_script_in_tx(reference, sql)
        schema_hash = compute_sqlite_schema_sha256(reference)
        triggers_and_views = tuple(
            (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                "" if row[3] is None else str(row[3]),
            )
            for row in reference.execute(
                """SELECT type, name, tbl_name, sql FROM sqlite_schema
                   WHERE type IN ('trigger', 'view')
                   ORDER BY type, name, tbl_name, COALESCE(sql, '')"""
            ).fetchall()
        )
    return schema_hash, triggers_and_views


def expected_sqlite_schema_sha256(domains: frozenset[str]) -> str:
    """Return the canonical sqlite_schema SHA-256 for the given active domain set."""
    return _build_reference_schema(domains)[0]


def trusted_triggers_and_views(
    domains: frozenset[str],
) -> tuple[tuple[str, str, str, str], ...]:
    """Return the canonical trigger and view definitions bundled for the active domains."""
    return _build_reference_schema(domains)[1]


def has_user_schema_objects(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        is not None
    )


def installed_domains(
    connection: sqlite3.Connection,
) -> dict[str, tuple[int, str, str]]:
    """Return {domain: (schema_version, pack_sha256, sqlite_schema_sha256)}."""
    has_table = (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'installed_domains'"
        ).fetchone()
        is not None
    )
    if not has_table:
        raise StorageRefused("Company store is missing canonical installed_domains")
    rows = connection.execute(
        """SELECT domain, schema_version, pack_sha256, sqlite_schema_sha256
           FROM installed_domains
           ORDER BY domain"""
    ).fetchall()
    return {str(row[0]): (int(row[1]), str(row[2]), str(row[3])) for row in rows}


def is_domain_installed(connection: sqlite3.Connection, domain: str) -> bool:
    canonical = canonical_domain_name(domain)
    installed = installed_domains(connection)
    if canonical == "core":
        return "core" in installed
    return "trading" in installed or "paper" in installed


def require_domain(connection: sqlite3.Connection, domain: str) -> None:
    canonical = canonical_domain_name(domain)
    if not is_domain_installed(connection, canonical):
        raise StorageRefused(f"Required schema domain '{canonical}' is not installed")


def verify_v1_schema(connection: sqlite3.Connection) -> frozenset[str]:
    """Verify PRAGMA user_version == 1, installed_domains digests, and live sqlite_schema."""
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        raise StorageRefused(
            f"Unsupported company store schema version: {version} (expected {SCHEMA_VERSION})"
        )
    records = installed_domains(connection)
    active = frozenset(records)
    if (
        "core" not in active
        or not active.issubset(set(VALID_DOMAINS))
        or ("trading" in active and "paper" in active)
    ):
        raise StorageRefused(
            f"Invalid installed schema domains in company store: {sorted(active)}"
        )
    expected_schema_hash = expected_sqlite_schema_sha256(active)
    for domain, (dom_version, pack_hash, recorded_schema_hash) in records.items():
        if dom_version != SCHEMA_VERSION:
            raise StorageRefused(
                f"Installed domain '{domain}' has unsupported schema version {dom_version}"
            )
        expected_pack_hash = load_domain_pack(domain).pack_sha256
        if pack_hash != expected_pack_hash:
            raise StorageRefused(f"Installed domain '{domain}' pack checksum mismatch")
        if recorded_schema_hash != expected_schema_hash:
            raise StorageRefused(
                f"Installed domain '{domain}' recorded schema digest mismatch"
            )
    actual_triggers = tuple(
        (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
        for r in connection.execute(
            """SELECT type, name, tbl_name, COALESCE(sql, '')
               FROM sqlite_schema
               WHERE type IN ('trigger', 'view') AND name NOT LIKE 'sqlite_%'
               ORDER BY type, name, tbl_name, COALESCE(sql, '')"""
        ).fetchall()
    )
    if actual_triggers != trusted_triggers_and_views(active):
        raise StorageRefused(
            "Company store contains unexpected, altered, or missing triggers or views"
        )
    actual_schema_hash = compute_sqlite_schema_sha256(connection)
    if actual_schema_hash != expected_schema_hash:
        raise StorageRefused(
            "Company store sqlite_schema does not match canonical v1 schema"
        )
    return active


def apply_core_v1(connection: sqlite3.Connection) -> None:
    """Install canonical core v1 schema into an empty store inside an active write_tx."""
    if not connection.in_transaction:
        raise StorageRefused(
            "apply_core_v1 requires an active BEGIN IMMEDIATE transaction"
        )
    if has_user_schema_objects(connection):
        raise StorageRefused("Cannot apply core v1 schema to a non-empty store")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != 0:
        raise StorageRefused(
            f"Empty store has unexpected PRAGMA user_version: {version}"
        )
    pack = load_domain_pack("core")
    for _name, sql in pack.files:
        _execute_sql_script_in_tx(connection, sql)
    actual_schema_hash = compute_sqlite_schema_sha256(connection)
    expected_schema_hash = expected_sqlite_schema_sha256(frozenset({"core"}))
    if actual_schema_hash != expected_schema_hash:
        raise StorageRefused("Applied core v1 schema failed canonical digest check")
    connection.execute(
        """INSERT INTO installed_domains (
               domain, schema_version, pack_sha256, sqlite_schema_sha256, activated_at_ns
           ) VALUES (?, ?, ?, ?, ?)""",
        ("core", SCHEMA_VERSION, pack.pack_sha256, actual_schema_hash, time_ns()),
    )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def enable_domain_v1(
    connection: sqlite3.Connection,
    domain: str,
    *,
    company_id: str | None = None,
    actor_id: str = "system",
) -> bool:
    """Idempotently install an opt-in canonical v1 domain pack inside an active write_tx."""
    if not connection.in_transaction:
        raise StorageRefused(
            "enable_domain_v1 requires an active BEGIN IMMEDIATE transaction"
        )
    if domain not in VALID_DOMAINS or domain == "core":
        raise StorageRefused(f"Unsupported opt-in schema domain: {domain}")
    target_domain = (
        canonical_domain_name(domain) if (_SCHEMA_ROOT / "trading").is_dir() else domain
    )
    active = verify_v1_schema(connection)
    pack = load_domain_pack(target_domain)
    if canonical_domain_name(target_domain) == "trading" and (
        "trading" in active or "paper" in active
    ):
        return False
    for _name, sql in pack.files:
        _execute_sql_script_in_tx(connection, sql)
    new_active = frozenset(active | {target_domain})
    actual_schema_hash = compute_sqlite_schema_sha256(connection)
    expected_schema_hash = expected_sqlite_schema_sha256(new_active)
    if actual_schema_hash != expected_schema_hash:
        raise StorageRefused(
            f"Activated domain '{target_domain}' failed canonical sqlite_schema check"
        )
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise StorageRefused(
            f"Activated domain '{target_domain}' failed foreign_key_check"
        )
    now = time_ns()
    connection.execute(
        "UPDATE installed_domains SET sqlite_schema_sha256 = ?",
        (actual_schema_hash,),
    )
    connection.execute(
        """INSERT INTO installed_domains (
               domain, schema_version, pack_sha256, sqlite_schema_sha256, activated_at_ns
           ) VALUES (?, ?, ?, ?, ?)""",
        (target_domain, SCHEMA_VERSION, pack.pack_sha256, actual_schema_hash, now),
    )
    resolved_company_id = company_id
    if resolved_company_id is None:
        row = connection.execute(
            "SELECT id FROM company WHERE singleton = 1"
        ).fetchone()
        if row is not None:
            resolved_company_id = str(row[0])
    if resolved_company_id is not None:
        append_audit_event(
            connection,
            company_id=resolved_company_id,
            domain="ops",
            entity_type="domain",
            entity_id=target_domain,
            event_type="domain.activated",
            actor_id=actor_id,
            payload={"domain": target_domain, "pack_sha256": pack.pack_sha256},
        )
    return True
