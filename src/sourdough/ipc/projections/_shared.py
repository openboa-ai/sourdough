"""Shared constants, read-only SQLite snapshot helpers, and common query helpers for projections."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any

from ...agent_desk import _check_no_symlinks, _drafts_directory
from ...board_control import ALLOWED_AVATAR_VARIANTS
from ...duty_run import DutyRefused
from ...runtime.domains import DomainPlugin, load_domain_plugins
from ...storage.db import open_reader, read_tx, scoped_reader
from ...storage.schema import installed_domains

PROJECTION_AGENTS_GRAPH = "agents-graph-v1"
PROJECTION_AGENT_DETAIL = "agent-detail-v1"
PROJECTION_MESSAGES = "messages-v1"
PROJECTION_APPROVALS = "approvals-v1"
PROJECTION_FINANCE = "finance-v1"
PROJECTION_ORG = "org-v1"
PROJECTION_RESTRICTIONS = "restrictions-v1"
PROJECTION_SYSTEM_SAFETY = "system-safety-v1"
PROJECTION_SYSTEM_EVALS = "system-evals-v1"
PROJECTION_SYSTEM_AUDIT = "system-audit-v1"

PROJECTION_ORDER_TRACE = "order-trace-v1"
PROJECTION_STRATEGIES = "strategies-v1"
PROJECTION_TRADING_DESK = "trading-desk-v1"

CORE_CLIENT_PROJECTIONS: tuple[str, ...] = (
    PROJECTION_AGENTS_GRAPH,
    PROJECTION_AGENT_DETAIL,
    PROJECTION_MESSAGES,
    PROJECTION_APPROVALS,
    PROJECTION_FINANCE,
    PROJECTION_ORG,
    PROJECTION_RESTRICTIONS,
    PROJECTION_SYSTEM_SAFETY,
    PROJECTION_SYSTEM_EVALS,
    PROJECTION_SYSTEM_AUDIT,
)

ALL_CLIENT_PROJECTIONS: tuple[str, ...] = (
    *CORE_CLIENT_PROJECTIONS,
    PROJECTION_ORDER_TRACE,
    PROJECTION_STRATEGIES,
    PROJECTION_TRADING_DESK,
)

_DEFAULT_AVATAR_VARIANTS: tuple[str, ...] = (
    "wide-oval-top",
    "tall-capsule-right",
    "round-triangle-bottom",
    "round-diamond-left",
    "low-dome-bottom",
    "wide-crown-top",
    "bean-top",
    "round-pentagon-bottom",
)


@contextmanager
def _open_projection_snapshot(store: Path) -> Iterator[sqlite3.Connection]:
    """Open a bounded read-only SQLite snapshot via scoped_reader."""
    pkg = sys.modules.get(__package__ or "")
    pkg_open_reader = getattr(pkg, "open_reader", None) if pkg is not None else None
    pkg_read_tx = getattr(pkg, "read_tx", None) if pkg is not None else None
    if (pkg_open_reader is not None and pkg_open_reader is not open_reader) or (
        pkg_read_tx is not None and pkg_read_tx is not read_tx
    ):
        reader_fn = pkg_open_reader if pkg_open_reader is not None else open_reader
        tx_fn = pkg_read_tx if pkg_read_tx is not None else read_tx
        with closing(reader_fn(store)) as db, tx_fn(db):
            yield db
    else:
        with scoped_reader(store) as db:
            yield db


def _count_private_drafts_for_projection(store: Path, agent_id: str) -> int:
    """Count private drafts via the projections package symbol so test hooks remain observable."""
    pkg = sys.modules.get(__package__ or "")
    fn = (
        getattr(pkg, "_count_private_drafts", _count_private_drafts)
        if pkg is not None
        else _count_private_drafts
    )
    return int(fn(store, agent_id))


def _apply_core_enrichers(
    projection_name: str,
    db: sqlite3.Connection,
    company_id: str,
    payload: dict[str, Any],
    domain_plugins: Sequence[DomainPlugin] | None,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    domains = set(installed_domains(db))
    non_core = domains - {"core"}
    if not non_core:
        return
    plugins = (
        tuple(domain_plugins) if domain_plugins is not None else load_domain_plugins()
    )
    for plugin in plugins:
        if plugin.domain_name in domains:
            plugin.enrich_core_projection(
                projection_name, db, company_id, payload, context=context
            )


def _json_loads_safe(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _count_private_drafts(store: Path, agent_id: str) -> int:
    """Count unique private Desk draft stems without opening, reading, or naming any file."""
    try:
        root = store.resolve(strict=False).parent
        drafts_dir = _drafts_directory(store, agent_id)
        if not _check_no_symlinks(drafts_dir, root):
            return 0
        dir_st = os.lstat(drafts_dir)
        if stat.S_ISLNK(dir_st.st_mode) or not stat.S_ISDIR(dir_st.st_mode):
            return 0
        stems: set[str] = set()
        for entry in drafts_dir.iterdir():
            name = entry.name
            if name.startswith("."):
                continue
            st = os.lstat(entry)
            if (
                stat.S_ISLNK(st.st_mode)
                or not stat.S_ISREG(st.st_mode)
                or st.st_nlink != 1
            ):
                continue
            stems.add(entry.stem if entry.suffix else name)
        return len(stems)
    except (OSError, DutyRefused):
        return 0


def _agent_directory(
    db: sqlite3.Connection, company_id: str
) -> dict[str, dict[str, Any]]:
    founder_row = db.execute(
        "SELECT founder_id FROM founding_receipt WHERE company_id = ?",
        (company_id,),
    ).fetchone()
    founder_id = str(founder_row[0]) if founder_row else None

    rows = db.execute(
        """SELECT a.id, a.execution_profile,
                  g.id, g.scope,
                  p.display_name, p.role_label, p.avatar_variant, p.monthly_budget_minor,
                  ar.cutoff, ar.reason,
                  ho.state, ho.purpose, ho.attempts,
                  hr.allocated_turns
           FROM agent a
           LEFT JOIN standing_grant g ON g.agent_id = a.id
           LEFT JOIN agent_profile p ON p.agent_id = a.id
           LEFT JOIN agent_archive ar ON ar.agent_id = a.id
           LEFT JOIN hire_onboarding ho ON ho.agent_id = a.id
           LEFT JOIN hire_receipt hr ON hr.agent_id = a.id
           WHERE a.company_id = ?
           ORDER BY CASE WHEN a.id = ? THEN 0 ELSE 1 END, a.rowid ASC""",
        (company_id, founder_id or ""),
    ).fetchall()

    directory: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(rows):
        agent_id = str(row[0])
        is_founder = agent_id == founder_id
        scope = str(row[3]) if row[3] is not None else ""
        default_role = "CEO" if is_founder else "Operations & QA"
        default_name = (
            "Founder · CEO" if is_founder else agent_id.replace("-", " ").title()
        )
        raw_avatar = str(row[6]).strip() if row[6] is not None else ""
        avatar_variant = (
            raw_avatar
            if raw_avatar in ALLOWED_AVATAR_VARIANTS
            else _DEFAULT_AVATAR_VARIANTS[idx % len(_DEFAULT_AVATAR_VARIANTS)]
        )
        directory[agent_id] = {
            "agent_id": agent_id,
            "is_ceo": is_founder,
            "execution_profile": str(row[1]),
            "grant_id": None if row[2] is None else str(row[2]),
            "grant_scope": scope,
            "display_name": str(row[4]) if row[4] else default_name,
            "role_label": str(row[5]) if row[5] else default_role,
            "avatar_variant": avatar_variant,
            "monthly_budget_minor": int(row[7]) if row[7] is not None else 0,
            "archived": row[8] is not None,
            "archive_reason": None if row[9] is None else str(row[9]),
            "onboarding_state": None if row[10] is None else str(row[10]),
            "onboarding_purpose": None if row[11] is None else str(row[11]),
            "onboarding_attempts": int(row[12]) if row[12] is not None else 0,
            "allocated_turns": int(row[13]) if row[13] is not None else None,
        }
    return directory


def _active_restrictions_rows(
    db: sqlite3.Connection, company_id: str
) -> list[dict[str, Any]]:
    rows = db.execute(
        """SELECT restriction_id, cause_category, scope_ref, restriction_revision,
                  active, operator_releasable, reason, recorded_at_ns, cleared_at_ns,
                  last_command_id
           FROM restriction_cause
           WHERE company_id = ?
           ORDER BY active DESC, recorded_at_ns DESC""",
        (company_id,),
    ).fetchall()
    return [
        {
            "restriction_id": str(r[0]),
            "cause_category": str(r[1]),
            "scope_ref": str(r[2]),
            "restriction_revision": int(r[3]),
            "active": bool(r[4]),
            "operator_releasable": bool(r[5]),
            "reason": str(r[6]),
            "recorded_at_ns": int(r[7]),
            "cleared_at_ns": None if r[8] is None else int(r[8]),
            "last_command_id": None if r[9] is None else str(r[9]),
        }
        for r in rows
    ]


def _sum_gl_accounts(db: sqlite3.Connection) -> dict[str, int]:
    rows = db.execute(
        """SELECT account, SUM(amount_minor)
           FROM journal_line
           GROUP BY account"""
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _close_issues_summary(
    db: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = db.execute(
        """SELECT request_id, scope, supersedes, request, receipt, assessed_ns
           FROM accounting_close
           ORDER BY rowid DESC"""
    ).fetchall()
    closes: list[dict[str, Any]] = []
    open_issues: list[dict[str, Any]] = []
    for r in rows:
        req = _json_loads_safe(str(r[3]), {})
        rec = _json_loads_safe(str(r[4]), {})
        issues = rec.get("issues", []) if isinstance(rec, dict) else []
        if not isinstance(issues, list):
            issues = []
        status = (
            str(rec.get("status", "unknown")) if isinstance(rec, dict) else "unknown"
        )
        entry = {
            "request_id": str(r[0]),
            "scope": str(r[1]),
            "supersedes": None if r[2] is None else str(r[2]),
            "status": status,
            "assessed_ns": None if r[5] is None else int(r[5]),
            "issues": issues,
            "period": req.get("period") if isinstance(req, dict) else None,
        }
        closes.append(entry)
    if closes:
        for issue in closes[0]["issues"]:
            if isinstance(issue, dict):
                open_issues.append(issue)
            else:
                open_issues.append({"detail": str(issue)})
    return closes, open_issues


def projection_etag(payload: dict[str, Any], event_seq: int) -> str:
    """Compute a stable ETag over projection payload excluding volatile as_of_ns."""
    stable = {k: v for k, v in payload.items() if k != "as_of_ns"}
    digest = sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f'"{digest}:{event_seq}"'
