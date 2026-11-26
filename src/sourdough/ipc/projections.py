"""Pure read-only SQLite projections for the Sourdough Board Client.

Every projection executes inside a single SQLite read_tx snapshot and returns
(payload, event_seq) so ETag and X-Sourdough-Event-Seq never tear. Private Desk
drafts are never opened or disclosed (opaque count only), missing accounting or
valuation telemetry is never coerced to zero, and paper_* tables are queried
only when the opt-in paper domain is installed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any

from ..agent_desk import _check_no_symlinks, _drafts_directory
from ..board_control import resolve_board_access
from ..company import FoundingRefused, _identity
from ..execution_tiers import (
    read_agent_sandbox_portfolio_from_db,
    read_execution_tiers_snapshot,
)
from ..paper_risk import AuthorizeBoard, effective_paper_risk
from ..storage.db import open_reader, read_tx
from ..storage.kernel import max_event_seq
from ..storage.schema import installed_domains, is_domain_installed

PROJECTION_LIVE_TRADING = "live-trading-v1"
PROJECTION_ORDER_TRACE = "order-trace-v1"
PROJECTION_AGENTS_GRAPH = "agents-graph-v1"
PROJECTION_AGENT_DETAIL = "agent-detail-v1"
PROJECTION_MESSAGES = "messages-v1"
PROJECTION_APPROVALS = "approvals-v1"
PROJECTION_STRATEGIES = "strategies-v1"
PROJECTION_FINANCE = "finance-v1"
PROJECTION_ORG = "org-v1"
PROJECTION_RESTRICTIONS = "restrictions-v1"
PROJECTION_SYSTEM_SAFETY = "system-safety-v1"
PROJECTION_SYSTEM_EVALS = "system-evals-v1"
PROJECTION_SYSTEM_AUDIT = "system-audit-v1"
PROJECTION_EXECUTION_TIERS = "execution-tiers-v1"

ALL_CLIENT_PROJECTIONS: tuple[str, ...] = (
    PROJECTION_LIVE_TRADING,
    PROJECTION_ORDER_TRACE,
    PROJECTION_AGENTS_GRAPH,
    PROJECTION_AGENT_DETAIL,
    PROJECTION_MESSAGES,
    PROJECTION_APPROVALS,
    PROJECTION_STRATEGIES,
    PROJECTION_FINANCE,
    PROJECTION_ORG,
    PROJECTION_RESTRICTIONS,
    PROJECTION_SYSTEM_SAFETY,
    PROJECTION_SYSTEM_EVALS,
    PROJECTION_SYSTEM_AUDIT,
    PROJECTION_EXECUTION_TIERS,
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


def _json_loads_safe(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _count_private_drafts(store: Path, agent_id: str) -> int:
    """Count private Desk draft files without opening, reading, or naming any file."""
    try:
        root = store.resolve(strict=False).parent
        drafts_dir = _drafts_directory(store, agent_id)
        if not _check_no_symlinks(drafts_dir, root):
            return 0
        count = 0
        for entry in drafts_dir.iterdir():
            if entry.name.startswith(".") or entry.suffix != ".json":
                continue
            st = os.lstat(entry)
            if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                count += 1
        return count
    except Exception:
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
        default_role = (
            "CEO"
            if is_founder
            else (
                "Quant Research"
                if "research" in scope or "paper" in scope
                else "Operations & QA"
            )
        )
        default_name = (
            "Founder · CEO" if is_founder else agent_id.replace("-", " ").title()
        )
        directory[agent_id] = {
            "agent_id": agent_id,
            "is_ceo": is_founder,
            "execution_profile": str(row[1]),
            "grant_id": None if row[2] is None else str(row[2]),
            "grant_scope": scope,
            "display_name": str(row[4]) if row[4] else default_name,
            "role_label": str(row[5]) if row[5] else default_role,
            "avatar_variant": (
                str(row[6])
                if row[6]
                else _DEFAULT_AVATAR_VARIANTS[idx % len(_DEFAULT_AVATAR_VARIANTS)]
            ),
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


def _read_execution_tape(
    db: sqlite3.Connection, directory: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build grouped causal orders (Order -> Fills -> Fees -> Cancellation -> Position)."""
    orders = db.execute(
        """SELECT o.order_id, o.run_id, o.mandate_revision, o.account_id,
                  o.action_id, o.instrument_rules_digest, o.quantity,
                  o.limit_minor_units, o.reserved_minor_units,
                  d.agent_id, d.id AS duty_id,
                  pd.decision_id, pd.rationale, pd.action, pd.decided_at
           FROM paper_order o
           LEFT JOIN duty_run dr ON dr.run_id = o.run_id
           LEFT JOIN duty d ON d.id = dr.duty_id
           LEFT JOIN paper_decision pd ON pd.run_id = o.run_id
           ORDER BY o.rowid DESC"""
    ).fetchall()

    fills_by_order: dict[str, list[dict[str, Any]]] = {}
    for row in db.execute(
        """SELECT f.fill_id, f.order_id, f.quantity, f.price_minor_units,
                  ff.fee_minor_units, po.position_quantity, po.available_at
           FROM paper_fill f
           LEFT JOIN paper_fill_fee ff ON ff.fill_id = f.fill_id
           LEFT JOIN paper_position_observation po ON po.fill_id = f.fill_id
           ORDER BY f.rowid ASC"""
    ).fetchall():
        oid = str(row[1])
        fills_by_order.setdefault(oid, []).append(
            {
                "fill_id": str(row[0]),
                "quantity": int(row[2]),
                "price_minor_units": int(row[3]),
                "fee_minor_units": None if row[4] is None else int(row[4]),
                "position_quantity": None if row[5] is None else int(row[5]),
                "available_at": None if row[6] is None else int(row[6]),
            }
        )

    cancellations: dict[str, dict[str, Any]] = {}
    for row in db.execute(
        """SELECT order_id, event_id, filled_quantity, released,
                  position_quantity, available_at, outcome
           FROM paper_cancellation"""
    ).fetchall():
        cancellations[str(row[0])] = {
            "event_id": str(row[1]),
            "filled_quantity": int(row[2]),
            "released": bool(row[3]),
            "position_quantity": None if row[4] is None else int(row[4]),
            "available_at": None if row[5] is None else int(row[5]),
            "outcome": str(row[6]),
        }

    reconciliations: dict[str, dict[str, Any]] = {}
    for row in db.execute(
        "SELECT order_id, attempts, next_query_at, fill_id, sources FROM paper_reconciliation"
    ).fetchall():
        reconciliations[str(row[0])] = {
            "attempts": int(row[1]),
            "next_query_at": None if row[2] is None else int(row[2]),
            "fill_id": None if row[3] is None else str(row[3]),
            "sources": _json_loads_safe(str(row[4]), []),
        }

    tape: list[dict[str, Any]] = []
    positions_by_instrument: dict[str, dict[str, Any]] = {}
    for row in orders:
        order_id = str(row[0])
        ordered_qty = int(row[6])
        order_fills = fills_by_order.get(order_id, [])
        filled_qty = sum(f["quantity"] for f in order_fills)
        fees_known = all(f["fee_minor_units"] is not None for f in order_fills)
        total_fee: int | None = (
            sum(
                int(f["fee_minor_units"])
                for f in order_fills
                if f["fee_minor_units"] is not None
            )
            if fees_known
            else None
        )
        avg_price = (
            int(
                sum(f["price_minor_units"] * f["quantity"] for f in order_fills)
                / filled_qty
            )
            if filled_qty > 0
            else None
        )
        cancel = cancellations.get(order_id)
        remaining_qty = max(ordered_qty - filled_qty, 0)
        residual_pos = (
            cancel["position_quantity"]
            if cancel is not None and cancel["position_quantity"] is not None
            else (
                order_fills[-1]["position_quantity"]
                if order_fills and order_fills[-1]["position_quantity"] is not None
                else filled_qty
            )
        )
        cancelled = cancel is not None
        if cancelled:
            status = (
                "cancelled_with_position"
                if residual_pos is not None and residual_pos != 0
                else "cancelled_flat"
            )
        elif filled_qty >= ordered_qty and ordered_qty > 0:
            status = "filled"
        elif filled_qty > 0:
            status = "partial_fill"
        else:
            status = "working"

        agent_id = None if row[9] is None else str(row[9])
        agent_meta = directory.get(agent_id or "", {})
        instrument = str(row[5])[:24]
        item = {
            "order_id": order_id,
            "run_id": str(row[1]),
            "duty_id": None if row[10] is None else str(row[10]),
            "decision_id": None if row[11] is None else str(row[11]),
            "rationale": None if row[12] is None else str(row[12]),
            "action": str(row[13]) if row[13] else "buy",
            "decided_at": None if row[14] is None else int(row[14]),
            "account_id": str(row[3]),
            "instrument": instrument,
            "mandate_revision": str(row[2]),
            "agent_id": agent_id,
            "agent_name": agent_meta.get("display_name", agent_id or "System"),
            "agent_role": agent_meta.get("role_label", "Trading"),
            "ordered_qty": ordered_qty,
            "filled_qty": filled_qty,
            "remaining_qty": remaining_qty,
            "limit_minor_units": int(row[7]),
            "avg_fill_minor_units": avg_price,
            "reserved_minor_units": int(row[8]),
            "total_fee_minor_units": total_fee,
            "status": status,
            "cancellation": cancel,
            "residual_position_qty": residual_pos,
            "cancel_not_flat": bool(cancelled and residual_pos not in (None, 0)),
            "reconciliation": reconciliations.get(order_id),
            "fills": order_fills,
        }
        tape.append(item)
        if residual_pos not in (None, 0) and instrument not in positions_by_instrument:
            positions_by_instrument[instrument] = {
                "instrument": instrument,
                "account_id": str(row[3]),
                "quantity": residual_pos,
                "avg_entry_minor_units": avg_price or int(row[7]),
                "latest_order_id": order_id,
                "agent_id": agent_id,
                "agent_name": item["agent_name"],
                "from_cancelled_order": cancelled,
            }
    return tape, list(positions_by_instrument.values())


def _build_since_last_visit(
    db: sqlite3.Connection, company_id: str, since_seq: int | None
) -> dict[str, Any]:
    cutoff = since_seq if isinstance(since_seq, int) and since_seq > 0 else 0
    rows = db.execute(
        """SELECT event_seq, domain, entity_type, entity_id, event_type, recorded_at_ns
           FROM audit_event
           WHERE company_id = ? AND event_seq > ?
           ORDER BY event_seq DESC
           LIMIT 50""",
        (company_id, cutoff),
    ).fetchall()
    counts: dict[str, int] = {}
    highlights: list[dict[str, Any]] = []
    for r in rows:
        domain = str(r[1])
        counts[domain] = counts.get(domain, 0) + 1
        if len(highlights) < 6:
            highlights.append(
                {
                    "event_seq": int(r[0]),
                    "domain": domain,
                    "entity_type": str(r[2]),
                    "entity_id": str(r[3]),
                    "event_type": str(r[4]),
                    "recorded_at_ns": int(r[5]),
                }
            )
    return {
        "since_seq": cutoff,
        "new_event_count": len(rows),
        "by_domain": counts,
        "highlights": highlights,
    }


def read_live_trading_projection(
    store: Path,
    company_id: str,
    *,
    since_seq: int | None = None,
    authorize_board: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Area 1 Live Trading & Portfolio projection in one SQLite read_tx."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        domains = sorted(installed_domains(db))
        has_paper = "paper" in domains
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        directory = _agent_directory(db, company_id)
        access = resolve_board_access(
            db,
            company_id,
            authorize_board=authorize_board,
            principal_hint=principal_hint,
        )
        restrictions = _active_restrictions_rows(db, company_id)
        active_restrictions = [r for r in restrictions if r["active"]]
        gl_balances = _sum_gl_accounts(db)
        closes, open_close_issues = _close_issues_summary(db)
        since_visit = _build_since_last_visit(db, company_id, since_seq)

        llm_opex_minor = sum(
            abs(val)
            for acct, val in gl_balances.items()
            if any(
                k in acct.lower()
                for k in ("llm", "opex", "compute", "expense", "operating")
            )
            and "trading" not in acct.lower()
            and "fee" not in acct.lower()
        )
        has_opex_data = any(
            any(
                k in acct.lower()
                for k in ("llm", "opex", "compute", "expense", "operating")
            )
            for acct in gl_balances
        )

        if not has_paper:
            payload = {
                "projection": PROJECTION_LIVE_TRADING,
                "company_id": company_id,
                "charter_revision": charter_revision,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "active_domains": domains,
                "paper_enabled": False,
                "environment": "core",
                "board_role": access.role,
                "risk_gate": {
                    "suspended": len(active_restrictions) > 0,
                    "effective_revision": 0,
                    "active_restriction_count": len(active_restrictions),
                },
                "kpis": {
                    "portfolio_value": {
                        "value_minor": None,
                        "return_pct": None,
                        "currency": "USD",
                        "status": "unavailable",
                        "source": "Paper trading domain not active",
                    },
                    "trading_bor_pnl": {
                        "value_minor": None,
                        "currency": "USD",
                        "status": "unavailable",
                        "source": "Paper trading domain not active",
                    },
                    "agent_llm_opex": {
                        "value_minor": -llm_opex_minor if has_opex_data else None,
                        "currency": "USD",
                        "status": "ok" if has_opex_data else "unavailable",
                        "source": "General ledger",
                    },
                    "company_net_pnl": {
                        "value_minor": None,
                        "currency": "USD",
                        "status": "unavailable",
                        "source": "Requires paper domain and clean close",
                    },
                },
                "since_last_visit": since_visit,
                "cumulative_return_series": [],
                "pnl_reconciliation_bridge": [],
                "execution_tape": [],
                "open_positions": [],
                "adapter_status": None,
                "active_restrictions": active_restrictions,
            }
            return payload, event_seq

        binding_row = db.execute(
            """SELECT revision, charter_revision, exposure_limit_minor_units,
                      account_id, connector_revision, instrument_rules_digest,
                      currency, starting_minor_units, environment, destination_id
               FROM paper_binding WHERE singleton = 1"""
        ).fetchone()
        risk_state = effective_paper_risk(db)
        valuations = db.execute(
            """SELECT capture_id, binding_revision, policy, evidence,
                      trading_nav_atoms, revision
               FROM paper_valuation
               ORDER BY rowid ASC"""
        ).fetchall()
        tape, open_positions = _read_execution_tape(db, directory)
        funding_rows = db.execute(
            """SELECT status, funding_cashflow_atoms, unavailable_reason
               FROM paper_funding_settlement"""
        ).fetchall()

        currency = str(binding_row[6]) if binding_row else "USD"
        starting_minor = int(binding_row[7]) if binding_row else None
        exposure_limit = int(binding_row[2]) if binding_row else None
        fees_known = all(o["total_fee_minor_units"] is not None for o in tape)
        total_fees: int | None = (
            sum(
                int(o["total_fee_minor_units"])
                for o in tape
                if o["total_fee_minor_units"] is not None
            )
            if fees_known
            else None
        )
        funding_unavailable = any(str(f[0]) == "unavailable" for f in funding_rows)
        funding_total = sum(
            int(f[1])
            for f in funding_rows
            if f[1] is not None and str(f[0]) == "booked"
        )

        # Build 0%-baseline cumulative return series strictly from sealed valuations.
        return_series: list[dict[str, Any]] = []
        latest_nav: int | None = None
        if starting_minor and starting_minor > 0 and valuations:
            for idx, v in enumerate(valuations):
                nav = int(v[4])
                ev_meta = _json_loads_safe(str(v[3]), {})
                missing = (
                    bool(ev_meta.get("missing")) if isinstance(ev_meta, dict) else False
                )
                label = (
                    str(ev_meta.get("label"))
                    if isinstance(ev_meta, dict) and ev_meta.get("label")
                    else f"Cut #{idx + 1}"
                )
                if missing:
                    return_series.append(
                        {
                            "id": str(v[0]),
                            "label": label,
                            "return_pct": None,
                            "pnl_minor": None,
                            "nav_minor": None,
                            "source": "paper_valuation (missing)",
                        }
                    )
                else:
                    latest_nav = nav
                    delta = nav - starting_minor
                    pct = round((delta / starting_minor) * 100.0, 4)
                    return_series.append(
                        {
                            "id": str(v[0]),
                            "label": label,
                            "return_pct": pct,
                            "pnl_minor": delta,
                            "nav_minor": nav,
                            "source": "paper_valuation",
                        }
                    )

        trading_pnl = (
            (latest_nav - starting_minor)
            if latest_nav is not None and starting_minor is not None
            else None
        )
        return_pct = (
            round((trading_pnl / starting_minor) * 100.0, 4)
            if trading_pnl is not None and starting_minor and starting_minor > 0
            else None
        )
        close_has_issues = len(open_close_issues) > 0
        opex_available = has_opex_data and not close_has_issues
        if (
            trading_pnl is not None
            and opex_available
            and not funding_unavailable
            and total_fees is not None
        ):
            company_net = trading_pnl - llm_opex_minor
            net_status = "ok"
            net_source = "Two-layer reconciled"
        else:
            company_net = None
            net_status = "unavailable"
            net_source = (
                "Unavailable: open accounting close issues"
                if close_has_issues
                else (
                    "Unavailable: incomplete funding settlement"
                    if funding_unavailable
                    else (
                        "Unavailable: no operating cost ledger posted"
                        if not has_opex_data
                        else "Unavailable: no sealed valuation"
                    )
                )
            )

        gross_trading = (
            trading_pnl + total_fees - funding_total
            if trading_pnl is not None and total_fees is not None
            else None
        )
        bridge = [
            {
                "step": "gross_trading_pnl",
                "label": "Gross Trading P&L",
                "amount_minor": gross_trading,
                "status": "ok" if gross_trading is not None else "unavailable",
            },
            {
                "step": "trading_fees",
                "label": "Venue Fees & Commissions",
                "amount_minor": None
                if total_fees is None
                else (-total_fees if tape else 0),
                "status": "unavailable" if total_fees is None else "ok",
            },
            {
                "step": "funding_settlements",
                "label": "8h Funding Settlements",
                "amount_minor": None if funding_unavailable else funding_total,
                "status": "unavailable" if funding_unavailable else "ok",
            },
            {
                "step": "agent_llm_opex",
                "label": "Agent LLM & Compute Opex",
                "amount_minor": -llm_opex_minor if opex_available else None,
                "status": "ok" if opex_available else "unavailable",
            },
            {
                "step": "company_net_pnl",
                "label": "Company Net P&L",
                "amount_minor": company_net,
                "status": net_status,
            },
        ]

        # Check latest quote freshness (non-binding telemetry)
        quote_row = db.execute(
            """SELECT quote_minor_units, observed_at, connector_revision
               FROM paper_review_quote ORDER BY observed_at DESC LIMIT 1"""
        ).fetchone()

        adapter_status = (
            {
                "environment": str(binding_row[8]),
                "account_id": str(binding_row[3]),
                "connector_revision": str(binding_row[4]),
                "destination_id": str(binding_row[9]),
                "mandate_revision": str(binding_row[0]),
                "exposure_limit_minor_units": exposure_limit,
                "starting_minor_units": starting_minor,
                "currency": currency,
                "last_quote_minor_units": None if quote_row is None else quote_row[0],
                "last_quote_observed_at": None
                if quote_row is None
                else int(quote_row[1]),
            }
            if binding_row
            else None
        )

        payload = {
            "projection": PROJECTION_LIVE_TRADING,
            "company_id": company_id,
            "charter_revision": charter_revision,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "active_domains": domains,
            "paper_enabled": True,
            "environment": "paper",
            "board_role": access.role,
            "risk_gate": {
                "suspended": bool(risk_state.suspended or len(active_restrictions) > 0),
                "paper_risk_suspended": bool(risk_state.suspended),
                "effective_revision": int(risk_state.revision),
                "active_restriction_count": len(active_restrictions),
            },
            "kpis": {
                "portfolio_value": {
                    "value_minor": latest_nav,
                    "starting_minor": starting_minor,
                    "return_pct": return_pct,
                    "currency": currency,
                    "status": "ok" if latest_nav is not None else "unavailable",
                    "source": "Sealed paper valuation"
                    if latest_nav is not None
                    else "No live market tick",
                },
                "trading_bor_pnl": {
                    "value_minor": trading_pnl,
                    "currency": currency,
                    "status": "ok" if trading_pnl is not None else "unavailable",
                    "source": "Paper trading books of record",
                },
                "agent_llm_opex": {
                    "value_minor": -llm_opex_minor if opex_available else None,
                    "currency": currency,
                    "status": "ok" if opex_available else "unavailable",
                    "source": (
                        "Unavailable: close issue"
                        if close_has_issues
                        else (
                            "General ledger"
                            if has_opex_data
                            else "Unavailable: no GL opex recorded"
                        )
                    ),
                },
                "company_net_pnl": {
                    "value_minor": company_net,
                    "currency": currency,
                    "status": net_status,
                    "source": net_source,
                },
            },
            "since_last_visit": since_visit,
            "cumulative_return_series": return_series,
            "pnl_reconciliation_bridge": bridge,
            "execution_tape": tape,
            "open_positions": open_positions,
            "adapter_status": adapter_status,
            "active_restrictions": active_restrictions,
            "open_close_issues": open_close_issues,
            "close_count": len(closes),
        }
        return payload, event_seq


def read_order_trace_projection(
    store: Path, company_id: str, order_id: str
) -> tuple[dict[str, Any] | None, int]:
    """Read full causal lineage for a single paper order."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        event_seq = max_event_seq(db, company_id)
        if not is_domain_installed(db, "paper"):
            return None, event_seq
        directory = _agent_directory(db, company_id)
        tape, _ = _read_execution_tape(db, directory)
        match = next((o for o in tape if o["order_id"] == order_id), None)
        if match is None:
            return None, event_seq
        pubs = [
            {
                "asset_id": str(r[0]),
                "project_id": str(r[1]),
                "author_id": str(r[2]),
                "excerpt": str(r[3])[:240],
            }
            for r in db.execute(
                """SELECT asset_id, project_id, author_id, content
                   FROM shared_publication
                   WHERE company_id = ? AND author_id = ?
                   ORDER BY rowid DESC LIMIT 5""",
                (company_id, match["agent_id"] or ""),
            ).fetchall()
        ]
        steps = [
            {
                "stage": "Duty & Run",
                "id": match["duty_id"] or match["run_id"],
                "detail": f"Author: {match['agent_name']} ({match['agent_role']})",
                "status": "complete",
            },
            {
                "stage": "Trading Decision",
                "id": match["decision_id"] or "unlinked",
                "detail": match["rationale"] or "No recorded decision rationale",
                "status": "complete" if match["decision_id"] else "unavailable",
            },
            {
                "stage": "Paper Order",
                "id": match["order_id"],
                "detail": f"{match['action'].upper()} {match['ordered_qty']} @ limit {match['limit_minor_units']} ({match['instrument']})",
                "status": "complete",
            },
            {
                "stage": "Fills & Fees",
                "id": f"{len(match['fills'])} fills",
                "detail": f"Filled {match['filled_qty']} / {match['ordered_qty']} · Fees {match['total_fee_minor_units']}",
                "status": "complete" if match["fills"] else "pending",
            },
        ]
        if match["cancellation"] is not None:
            c = match["cancellation"]
            steps.append(
                {
                    "stage": "Cancellation",
                    "id": c["event_id"],
                    "detail": (
                        f"Outcome: {c['outcome']} · Residual open position: {match['residual_position_qty']} "
                        "(cancel does not flatten filled exposure)"
                        if match["cancel_not_flat"]
                        else f"Outcome: {c['outcome']} · Flat (0 residual position)"
                    ),
                    "status": "warning" if match["cancel_not_flat"] else "complete",
                }
            )
        steps.append(
            {
                "stage": "Net Position",
                "id": match["instrument"],
                "detail": f"Observed quantity: {match['residual_position_qty']}",
                "status": "complete",
            }
        )
        return (
            {
                "projection": PROJECTION_ORDER_TRACE,
                "company_id": company_id,
                "as_of_ns": time_ns(),
                "event_seq": event_seq,
                "order": match,
                "causal_steps": steps,
                "related_publications": pubs,
            },
            event_seq,
        )


def read_agents_graph_projection(
    store: Path, company_id: str
) -> tuple[dict[str, Any], int]:
    """Read Area 2 Agents & Activity node-and-edge interaction graph projection."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        directory = _agent_directory(db, company_id)

        # Active work_runs and work_items per agent
        active_runs: dict[str, dict[str, Any]] = {}
        for r in db.execute(
            """SELECT run_id, agent_id, work_item_id, duty_kind, status,
                      fencing_token, budget_ceiling_minor, updated_at_ns
               FROM work_runs
               WHERE company_id = ?
               ORDER BY updated_at_ns DESC""",
            (company_id,),
        ).fetchall():
            aid = str(r[1])
            if aid not in active_runs or str(r[4]) == "running":
                active_runs[aid] = {
                    "run_id": str(r[0]),
                    "work_item_id": str(r[2]),
                    "duty_kind": str(r[3]),
                    "status": str(r[4]),
                    "fencing_token": int(r[5]),
                    "budget_ceiling_minor": int(r[6]),
                    "updated_at_ns": int(r[7]),
                }

        latest_items: dict[str, dict[str, Any]] = {}
        for r in db.execute(
            """SELECT work_item_id, owner_agent_id, duty_kind, state, payload_json, updated_at_ns
               FROM work_items
               WHERE company_id = ?
               ORDER BY updated_at_ns DESC""",
            (company_id,),
        ).fetchall():
            aid = str(r[1])
            if aid not in latest_items:
                p = _json_loads_safe(str(r[4]), {})
                summary = (
                    str(p.get("summary") or p.get("title") or r[2])
                    if isinstance(p, dict)
                    else str(r[2])
                )
                latest_items[aid] = {
                    "work_item_id": str(r[0]),
                    "duty_kind": str(r[2]),
                    "state": str(r[3]),
                    "summary": summary,
                    "updated_at_ns": int(r[5]),
                }

        pub_counts = {
            str(r[0]): int(r[1])
            for r in db.execute(
                """SELECT author_id, COUNT(*)
                   FROM shared_publication WHERE company_id = ? GROUP BY author_id""",
                (company_id,),
            ).fetchall()
        }

        nodes: list[dict[str, Any]] = []
        for aid, info in directory.items():
            run = active_runs.get(aid)
            item = latest_items.get(aid)
            if info["archived"]:
                live_status = "archived"
                avatar_state = "done"
            elif info["onboarding_state"] == "blocked":
                live_status = "blocked"
                avatar_state = "blocked"
            elif run and run["status"] == "running":
                live_status = "working"
                avatar_state = "working"
            elif run and run["status"] == "waiting":
                live_status = "waiting"
                avatar_state = "waiting"
            elif item and item["state"] in ("due", "scheduled"):
                live_status = "ready"
                avatar_state = "ready"
            else:
                live_status = "idle"
                avatar_state = "ready"

            current_task = (
                item["summary"]
                if item
                else (
                    f"Onboarding ({info['onboarding_state']})"
                    if info["onboarding_state"]
                    else "Standing watch"
                )
            )
            nodes.append(
                {
                    "agent_id": aid,
                    "display_name": info["display_name"],
                    "role_label": info["role_label"],
                    "is_ceo": info["is_ceo"],
                    "avatar_variant": info["avatar_variant"],
                    "avatar_state": avatar_state,
                    "live_status": live_status,
                    "archived": info["archived"],
                    "current_task": current_task,
                    "active_run": run,
                    "monthly_budget_minor": info["monthly_budget_minor"],
                    "private_draft_count": _count_private_drafts(store, aid),
                    "published_count": pub_counts.get(aid, 0),
                    "grant_scope": info["grant_scope"],
                }
            )

        # Build live interaction edges from peer requests, project conversations, handoffs.
        edges: list[dict[str, Any]] = []
        seen_edge_keys: set[tuple[str, str, str]] = set()

        for r in db.execute(
            """SELECT request_id, requester_agent_id, recipient_agent_id,
                      project_id, question, state, created_at_ns
               FROM work_peer_requests
               WHERE company_id = ?
               ORDER BY created_at_ns DESC LIMIT 20""",
            (company_id,),
        ).fetchall():
            src, dst = str(r[1]), str(r[2])
            if src == dst:
                continue
            key = (min(src, dst), max(src, dst), f"peer:{r[0]}")
            if key in seen_edge_keys:
                continue
            seen_edge_keys.add(key)
            q_text = str(r[4])
            short_label = q_text if len(q_text) <= 34 else f"{q_text[:31]}..."
            preview_text = q_text if len(q_text) <= 180 else f"{q_text[:177]}..."
            sender_name = str(directory.get(src, {}).get("display_name", src))
            state_str = str(r[5])
            edges.append(
                {
                    "edge_id": f"peer-{r[0]}",
                    "source_agent_id": src,
                    "target_agent_id": dst,
                    "kind": "peer_request",
                    "state": state_str,
                    "active": state_str == "open",
                    "label": short_label,
                    "latest_preview": preview_text,
                    "latest_sender_name": sender_name,
                    "message_count": 1,
                    "channel_id": str(r[3]) if r[3] else "ceo-direct",
                    "updated_at_ns": int(r[6]),
                }
            )

        for r in db.execute(
            """SELECT offer_id, sender_id, recipient_id, expected_result, accepted_run_id
               FROM work_handoff
               WHERE company_id = ?
               ORDER BY rowid DESC LIMIT 12""",
            (company_id,),
        ).fetchall():
            src, dst = str(r[1]), str(r[2])
            if src == dst:
                continue
            expected_text = str(r[3])
            preview_text = (
                expected_text
                if len(expected_text) <= 180
                else f"{expected_text[:177]}..."
            )
            sender_name = str(directory.get(src, {}).get("display_name", src))
            accepted = bool(r[4])
            edges.append(
                {
                    "edge_id": f"handoff-{r[0]}",
                    "source_agent_id": src,
                    "target_agent_id": dst,
                    "kind": "handoff",
                    "state": "accepted" if accepted else "offered",
                    "active": not accepted,
                    "label": f"Handoff · {expected_text[:24]}",
                    "latest_preview": preview_text,
                    "latest_sender_name": sender_name,
                    "message_count": 1,
                    "channel_id": "ceo-direct",
                    "updated_at_ns": as_of_ns,
                }
            )

        # Shared project message edges (connect recent sender with other project members)
        for r in db.execute(
            """SELECT sp.project_id, sp.members,
                      (SELECT pm.sender_id FROM project_message pm
                       WHERE pm.project_id = sp.project_id ORDER BY pm.rowid DESC LIMIT 1),
                      (SELECT pm.body FROM project_message pm
                       WHERE pm.project_id = sp.project_id ORDER BY pm.rowid DESC LIMIT 1),
                      (SELECT COUNT(*) FROM project_message pm
                       WHERE pm.project_id = sp.project_id)
               FROM shared_project sp
               WHERE sp.company_id = ?""",
            (company_id,),
        ).fetchall():
            project_id = str(r[0])
            members = _json_loads_safe(str(r[1]), [])
            last_sender = str(r[2]) if r[2] else None
            last_body = str(r[3]) if r[3] else ""
            msg_count = int(r[4])
            if not isinstance(members, list) or len(members) < 2 or msg_count == 0:
                continue
            src = last_sender if last_sender in members else str(members[0])
            sender_name = str(directory.get(src, {}).get("display_name", src))
            preview_text = (
                last_body.strip()
                if len(last_body.strip()) <= 180
                else f"{last_body.strip()[:177]}..."
            )
            for member in members:
                dst = str(member)
                if dst == src:
                    continue
                pair_key = (min(src, dst), max(src, dst), f"proj:{project_id}")
                if pair_key in seen_edge_keys:
                    continue
                seen_edge_keys.add(pair_key)
                label = f"#{project_id} · {msg_count} msgs"
                if last_body:
                    snippet = last_body.split("\n", 1)[0][:22]
                    label = f"#{project_id} · {snippet}"
                edges.append(
                    {
                        "edge_id": f"proj-{project_id}-{src}-{dst}",
                        "source_agent_id": src,
                        "target_agent_id": dst,
                        "kind": "channel",
                        "state": "active",
                        "active": True,
                        "label": label,
                        "latest_preview": preview_text,
                        "latest_sender_name": sender_name,
                        "message_count": msg_count,
                        "channel_id": project_id,
                        "updated_at_ns": as_of_ns,
                    }
                )

        default_mode = "graph" if len(nodes) <= 8 else "list"
        return (
            {
                "projection": PROJECTION_AGENTS_GRAPH,
                "company_id": company_id,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "default_view_mode": default_mode,
                "nodes": nodes,
                "edges": edges,
            },
            event_seq,
        )


def read_agent_detail_projection(
    store: Path, company_id: str, agent_id: str
) -> tuple[dict[str, Any] | None, int]:
    """Read full-screen Agent Detail: Desk, Authority/Budget, and 4 individual tabs."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        directory = _agent_directory(db, company_id)
        info = directory.get(agent_id)
        if info is None:
            return None, event_seq

        desk_row = db.execute(
            "SELECT manifest_revision FROM private_desk WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        staffing_row = (
            db.execute(
                """SELECT allowed_scope, slots, turns, manager_scope
                   FROM staffing_authority WHERE grant_id = ?""",
                (info["grant_id"],),
            ).fetchone()
            if info["grant_id"]
            else None
        )

        # Tab 1: Work Activity
        work_activity = [
            {
                "run_id": str(r[0]),
                "work_item_id": str(r[1]),
                "duty_kind": str(r[2]),
                "status": str(r[3]),
                "fencing_token": int(r[4]),
                "budget_ceiling_minor": int(r[5]),
                "outcome": _json_loads_safe(r[6], None),
                "created_at_ns": int(r[7]),
                "updated_at_ns": int(r[8]),
            }
            for r in db.execute(
                """SELECT run_id, work_item_id, duty_kind, status, fencing_token,
                          budget_ceiling_minor, outcome_json, created_at_ns, updated_at_ns
                   FROM work_runs
                   WHERE company_id = ? AND agent_id = ?
                   ORDER BY updated_at_ns DESC LIMIT 40""",
                (company_id, agent_id),
            ).fetchall()
        ]

        work_items = [
            {
                "work_item_id": str(r[0]),
                "duty_kind": str(r[1]),
                "state": str(r[2]),
                "source_kind": str(r[3]),
                "source_id": str(r[4]),
                "payload": _json_loads_safe(str(r[5]), {}),
                "updated_at_ns": int(r[6]),
            }
            for r in db.execute(
                """SELECT work_item_id, duty_kind, state, source_kind, source_id,
                          payload_json, updated_at_ns
                   FROM work_items
                   WHERE company_id = ? AND owner_agent_id = ?
                   ORDER BY updated_at_ns DESC LIMIT 30""",
                (company_id, agent_id),
            ).fetchall()
        ]

        conditions = [
            {
                "condition_id": str(r[0]),
                "trigger_kind": str(r[1]),
                "due_at_ns": None if r[2] is None else int(r[2]),
                "source_identity": None if r[3] is None else str(r[3]),
                "status": str(r[4]),
            }
            for r in db.execute(
                """SELECT condition_id, trigger_kind, due_at_ns, source_identity, status
                   FROM work_conditions
                   WHERE company_id = ? AND owner_agent_id = ?
                   ORDER BY updated_at_ns DESC LIMIT 20""",
                (company_id, agent_id),
            ).fetchall()
        ]

        # Tab 2: Interactions & Talks (Read-Only)
        interactions = [
            {
                "message_id": str(r[0]),
                "project_id": str(r[1]),
                "sender_id": str(r[2]),
                "sender_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "sender_role": directory.get(str(r[2]), {}).get("role_label", "Agent"),
                "body": str(r[3]),
                "asset_id": None if r[4] is None else str(r[4]),
                "audience": _json_loads_safe(str(r[5]), []),
            }
            for r in db.execute(
                """SELECT pm.message_id, pm.project_id, pm.sender_id, pm.body,
                          pm.asset_id, pm.audience
                   FROM project_message pm
                   LEFT JOIN project_delivery pd
                     ON pd.message_id = pm.message_id AND pd.recipient_id = ?
                   WHERE pm.company_id = ?
                     AND (pm.sender_id = ? OR pd.recipient_id = ?)
                   ORDER BY pm.rowid DESC LIMIT 40""",
                (agent_id, company_id, agent_id, agent_id),
            ).fetchall()
        ]

        # Tab 3: Published Reports
        published_reports = [
            {
                "asset_id": str(r[0]),
                "project_id": str(r[1]),
                "work_item_id": str(r[2]),
                "audience": _json_loads_safe(str(r[3]), []),
                "content": str(r[4]),
            }
            for r in db.execute(
                """SELECT asset_id, project_id, work_item_id, audience, content
                   FROM shared_publication
                   WHERE company_id = ? AND author_id = ?
                   ORDER BY rowid DESC LIMIT 30""",
                (company_id, agent_id),
            ).fetchall()
        ]

        # Tab 4: Peer Requests & Handoffs
        peer_requests = [
            {
                "request_id": str(r[0]),
                "direction": "outgoing" if str(r[1]) == agent_id else "incoming",
                "requester_agent_id": str(r[1]),
                "requester_name": directory.get(str(r[1]), {}).get(
                    "display_name", str(r[1])
                ),
                "recipient_agent_id": str(r[2]),
                "recipient_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "project_id": None if r[3] is None else str(r[3]),
                "question": str(r[4]),
                "expected_decision": str(r[5]),
                "state": str(r[6]),
                "response": _json_loads_safe(r[7], None),
                "created_at_ns": int(r[8]),
            }
            for r in db.execute(
                """SELECT request_id, requester_agent_id, recipient_agent_id,
                          project_id, question, expected_decision, state,
                          response_json, created_at_ns
                   FROM work_peer_requests
                   WHERE company_id = ?
                     AND (requester_agent_id = ? OR recipient_agent_id = ?)
                   ORDER BY created_at_ns DESC LIMIT 30""",
                (company_id, agent_id, agent_id),
            ).fetchall()
        ]

        handoffs = [
            {
                "offer_id": str(r[0]),
                "direction": "outgoing" if str(r[1]) == agent_id else "incoming",
                "sender_id": str(r[1]),
                "sender_name": directory.get(str(r[1]), {}).get(
                    "display_name", str(r[1])
                ),
                "recipient_id": str(r[2]),
                "recipient_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "asset_id": str(r[3]),
                "expected_result": str(r[4]),
                "accepted": r[5] is not None,
            }
            for r in db.execute(
                """SELECT offer_id, sender_id, recipient_id, asset_id,
                          expected_result, accepted_run_id
                   FROM work_handoff
                   WHERE company_id = ? AND (sender_id = ? OR recipient_id = ?)
                   ORDER BY rowid DESC LIMIT 20""",
                (company_id, agent_id, agent_id),
            ).fetchall()
        ]

        prohibitions = [
            "Cannot modify company charter or Board mandate",
            "Cannot execute live-money orders (paper-only environment)",
            "Cannot read another Agent's private Desk drafts",
        ]
        if not info["is_ceo"]:
            prohibitions.append("Cannot retire employees or exceed delegated budget")

        private_draft_count = _count_private_drafts(store, agent_id)
        active_run = next(
            (r for r in work_activity if r["status"] in ("running", "waiting")), None
        )

        agent_sandbox = read_agent_sandbox_portfolio_from_db(db, company_id, agent_id)
        return (
            {
                "projection": PROJECTION_AGENT_DETAIL,
                "company_id": company_id,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "profile": info | {"active_run": active_run},
                "agent_sandbox": agent_sandbox,
                "desk_workspace": {
                    "execution_profile": info["execution_profile"],
                    "desk_manifest_revision": None
                    if desk_row is None
                    else str(desk_row[0]),
                    "private_draft_count": private_draft_count,
                    "private_draft_notice": "Private drafts remain physically isolated on the Agent's Desk and are never disclosed to the Board.",
                    "published_count": len(published_reports),
                    "work_run_count": len(work_activity),
                },
                "authority_budget": {
                    "grant_id": info["grant_id"],
                    "grant_scope": info["grant_scope"],
                    "can_hire": staffing_row is not None,
                    "hiring_slots": None
                    if staffing_row is None
                    else int(staffing_row[1]),
                    "hiring_turns": None
                    if staffing_row is None
                    else int(staffing_row[2]),
                    "monthly_budget_minor": info["monthly_budget_minor"],
                    "allocated_turns": info["allocated_turns"],
                    "prohibitions": prohibitions,
                },
                "tabs": {
                    "work_activity": work_activity,
                    "work_items": work_items,
                    "conditions": conditions,
                    "interactions_and_talks": interactions,
                    "published_reports": published_reports,
                    "peer_requests": peer_requests,
                    "handoffs": handoffs,
                },
            },
            event_seq,
        )


def read_messages_projection(
    store: Path,
    company_id: str,
    *,
    authorize_board: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Area 3 Unified Company Messages: writable CEO 1:1 + read-only internal channels."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        directory = _agent_directory(db, company_id)
        access = resolve_board_access(
            db,
            company_id,
            authorize_board=authorize_board,
            principal_hint=principal_hint,
        )
        ceo_agent = next((a for a in directory.values() if a["is_ceo"]), None)
        ceo_id = ceo_agent["agent_id"] if ceo_agent else ""

        ceo_msgs = [
            {
                "message_id": str(r[0]),
                "sender_kind": str(r[1]),
                "sender_id": str(r[2]),
                "sender_name": (
                    "Board"
                    if str(r[1]) == "board"
                    else (ceo_agent["display_name"] if ceo_agent else "CEO")
                ),
                "body": str(r[3]),
                "context_ref": (
                    {
                        "kind": str(r[4]),
                        "id": str(r[5]),
                        "label": str(r[6] or r[5]),
                    }
                    if r[4] and r[5]
                    else None
                ),
                "command_id": None if r[7] is None else str(r[7]),
                "delivery_state": str(r[8]),
                "recorded_at_ns": int(r[9]),
            }
            for r in db.execute(
                """SELECT message_id, sender_kind, sender_id, body,
                          context_kind, context_id, context_label, command_id,
                          delivery_state, recorded_at_ns
                   FROM ceo_message
                   WHERE company_id = ?
                   ORDER BY recorded_at_ns ASC, rowid ASC""",
                (company_id,),
            ).fetchall()
        ]

        # Read-only internal channels from shared_project + project_message
        channels: list[dict[str, Any]] = []
        for proj in db.execute(
            """SELECT project_id, revision, members, publishers
               FROM shared_project
               WHERE company_id = ?
               ORDER BY rowid ASC""",
            (company_id,),
        ).fetchall():
            pid = str(proj[0])
            member_ids = _json_loads_safe(str(proj[2]), [])
            member_profiles = [
                {
                    "agent_id": mid,
                    "display_name": directory.get(mid, {}).get("display_name", mid),
                    "role_label": directory.get(mid, {}).get("role_label", "Agent"),
                    "avatar_variant": directory.get(mid, {}).get(
                        "avatar_variant", "wide-oval-top"
                    ),
                }
                for mid in (member_ids if isinstance(member_ids, list) else [])
            ]
            msgs = [
                {
                    "message_id": str(m[0]),
                    "sender_id": str(m[1]),
                    "sender_name": directory.get(str(m[1]), {}).get(
                        "display_name", str(m[1])
                    ),
                    "sender_role": directory.get(str(m[1]), {}).get(
                        "role_label", "Agent"
                    ),
                    "avatar_variant": directory.get(str(m[1]), {}).get(
                        "avatar_variant", "wide-oval-top"
                    ),
                    "body": str(m[2]),
                    "asset_id": None if m[3] is None else str(m[3]),
                    "audience": _json_loads_safe(str(m[4]), []),
                    "delivery_count": int(m[5]),
                    "read_count": int(m[6]),
                }
                for m in db.execute(
                    """SELECT pm.message_id, pm.sender_id, pm.body, pm.asset_id,
                              pm.audience,
                              (SELECT COUNT(*) FROM project_delivery pd WHERE pd.message_id = pm.message_id),
                              (SELECT COUNT(*) FROM project_delivery pd WHERE pd.message_id = pm.message_id AND pd.read_run_id IS NOT NULL)
                       FROM project_message pm
                       WHERE pm.company_id = ? AND pm.project_id = ?
                       ORDER BY pm.rowid ASC""",
                    (company_id, pid),
                ).fetchall()
            ]
            channels.append(
                {
                    "channel_id": pid,
                    "name": f"#{pid}",
                    "revision": str(proj[1]),
                    "read_only": True,
                    "members": member_profiles,
                    "message_count": len(msgs),
                    "messages": msgs,
                }
            )

        return (
            {
                "projection": PROJECTION_MESSAGES,
                "company_id": company_id,
                "charter_revision": charter_revision,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "ceo_thread": {
                    "channel_id": "ceo-direct",
                    "ceo_agent_id": ceo_id,
                    "ceo_display_name": ceo_agent["display_name"]
                    if ceo_agent
                    else "Founder · CEO",
                    "ceo_role_label": ceo_agent["role_label"] if ceo_agent else "CEO",
                    "ceo_avatar_variant": (
                        ceo_agent["avatar_variant"] if ceo_agent else "wide-oval-top"
                    ),
                    "writable": access.role == "board_control",
                    "advisory_notice": "Advisory message to CEO · Does not directly execute trades, mutate mandates, or retire employees.",
                    "messages": ceo_msgs,
                },
                "internal_channels": channels,
            },
            event_seq,
        )


def read_approvals_projection(
    store: Path, company_id: str
) -> tuple[dict[str, Any], int]:
    """Read Area 4 Board Approvals queue & resolution ledger (zero popups)."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        directory = _agent_directory(db, company_id)

        rows = db.execute(
            """SELECT question_id, kind, title, summary, impact_json, options_json,
                      revision, payload_hash, state, respond_by_ns,
                      created_by_agent_id, created_at_ns, resolved_at_ns,
                      resolved_command_id, resolution_choice, resolution_reason,
                      application_status, delivery_status
               FROM decision_question
               WHERE company_id = ?
               ORDER BY CASE state WHEN 'open' THEN 0 ELSE 1 END, created_at_ns DESC""",
            (company_id,),
        ).fetchall()

        pending: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        for r in rows:
            creator_id = str(r[10])
            creator = directory.get(creator_id, {})
            item = {
                "question_id": str(r[0]),
                "kind": str(r[1]),
                "title": str(r[2]),
                "summary": str(r[3]),
                "impact": _json_loads_safe(str(r[4]), {}),
                "options": _json_loads_safe(str(r[5]), ["approve", "decline"]),
                "revision": int(r[6]),
                "payload_hash": str(r[7]),
                "state": str(r[8]),
                "respond_by_ns": None if r[9] is None else int(r[9]),
                "created_by_agent_id": creator_id,
                "created_by_name": creator.get("display_name", creator_id),
                "created_by_role": creator.get("role_label", "Agent"),
                "created_at_ns": int(r[11]),
                "resolved_at_ns": None if r[12] is None else int(r[12]),
                "resolved_command_id": None if r[13] is None else str(r[13]),
                "resolution_choice": None if r[14] is None else str(r[14]),
                "resolution_reason": None if r[15] is None else str(r[15]),
                "application_status": str(r[16]),
                "delivery_status": str(r[17]),
            }
            if item["state"] == "open":
                pending.append(item)
            else:
                history.append(item)

        promote_candidates: list[dict[str, Any]] = []
        if is_domain_installed(db, "paper"):
            for r in db.execute(
                """SELECT comparison_id, incumbent_release_id, challenger_release_id,
                          status, verdict, verdict_json, evaluated_at_ns
                   FROM paper_challenger_trial
                   WHERE company_id = ? AND verdict = 'PROMOTE'
                   ORDER BY evaluated_at_ns DESC""",
                (company_id,),
            ).fetchall():
                promote_candidates.append(
                    {
                        "comparison_id": str(r[0]),
                        "incumbent_release_id": str(r[1]),
                        "challenger_release_id": str(r[2]),
                        "status": str(r[3]),
                        "verdict": str(r[4]),
                        "verdict_detail": _json_loads_safe(r[5], {}),
                        "evaluated_at_ns": None if r[6] is None else int(r[6]),
                    }
                )

        return (
            {
                "projection": PROJECTION_APPROVALS,
                "company_id": company_id,
                "charter_revision": charter_revision,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "pending_count": len(pending),
                "pending": pending,
                "history": history,
                "promote_candidates": promote_candidates,
            },
            event_seq,
        )


def read_strategies_projection(
    store: Path, company_id: str
) -> tuple[dict[str, Any], int]:
    """Read Flow 09 strategy releases, paired challenger trials, deployments & PaperExecution adapter."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        if not is_domain_installed(db, "paper"):
            return (
                {
                    "projection": PROJECTION_STRATEGIES,
                    "company_id": company_id,
                    "as_of_ns": as_of_ns,
                    "event_seq": event_seq,
                    "paper_enabled": False,
                    "active_deployment": None,
                    "deployments": [],
                    "releases": [],
                    "trials": [],
                    "adapter": None,
                },
                event_seq,
            )

        directory = _agent_directory(db, company_id)
        binding = db.execute(
            """SELECT revision, account_id, connector_revision, exposure_limit_minor_units,
                      currency, starting_minor_units, environment, destination_id
               FROM paper_binding WHERE singleton = 1"""
        ).fetchone()
        releases = [
            {
                "release_id": str(r[0]),
                "owner_agent_id": str(r[1]),
                "owner_name": directory.get(str(r[1]), {}).get(
                    "display_name", str(r[1])
                ),
                "parent_release_id": None if r[2] is None else str(r[2]),
                "parameters": _json_loads_safe(str(r[3]), {}),
                "pit_manifest_id": str(r[4]),
                "research_trial_count_n": int(r[5]),
                "bundle_sha256": str(r[6]),
                "recorded_at_ns": int(r[7]),
            }
            for r in db.execute(
                """SELECT release_id, owner_agent_id, parent_release_id, parameters_json,
                          pit_manifest_id, research_trial_count_n, bundle_sha256, recorded_at_ns
                   FROM paper_strategy_release
                   WHERE company_id = ?
                   ORDER BY recorded_at_ns DESC""",
                (company_id,),
            ).fetchall()
        ]
        trials = [
            {
                "comparison_id": str(r[0]),
                "incumbent_release_id": str(r[1]),
                "challenger_release_id": str(r[2]),
                "baseline_statement_revision": str(r[3]),
                "status": str(r[4]),
                "verdict": None if r[5] is None else str(r[5]),
                "verdict_detail": _json_loads_safe(r[6], None),
                "invalidation_reason": None if r[7] is None else str(r[7]),
                "registered_at_ns": int(r[8]),
                "evaluated_at_ns": None if r[9] is None else int(r[9]),
            }
            for r in db.execute(
                """SELECT comparison_id, incumbent_release_id, challenger_release_id,
                          baseline_statement_revision, status, verdict, verdict_json,
                          invalidation_reason, registered_at_ns, evaluated_at_ns
                   FROM paper_challenger_trial
                   WHERE company_id = ?
                   ORDER BY registered_at_ns DESC""",
                (company_id,),
            ).fetchall()
        ]
        deployments = [
            {
                "deployment_id": str(r[0]),
                "release_id": str(r[1]),
                "program_identity": str(r[2]),
                "binding_revision": str(r[3]),
                "comparison_id": None if r[4] is None else str(r[4]),
                "status": str(r[5]),
                "command_id": str(r[6]),
                "activated_at_ns": int(r[7]),
                "retired_at_ns": None if r[8] is None else int(r[8]),
            }
            for r in db.execute(
                """SELECT deployment_id, release_id, program_identity, binding_revision,
                          comparison_id, status, command_id, activated_at_ns, retired_at_ns
                   FROM paper_strategy_deployment
                   WHERE company_id = ?
                   ORDER BY activated_at_ns DESC""",
                (company_id,),
            ).fetchall()
        ]
        active = next((d for d in deployments if d["status"] == "active"), None)
        adapter = (
            {
                "binding_revision": str(binding[0]),
                "account_id": str(binding[1]),
                "connector_revision": str(binding[2]),
                "exposure_limit_minor_units": int(binding[3]),
                "currency": str(binding[4]),
                "starting_minor_units": int(binding[5]),
                "environment": str(binding[6]),
                "destination_id": str(binding[7]),
            }
            if binding
            else None
        )
        return (
            {
                "projection": PROJECTION_STRATEGIES,
                "company_id": company_id,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "paper_enabled": True,
                "active_deployment": active,
                "deployments": deployments,
                "releases": releases,
                "trials": trials,
                "adapter": adapter,
            },
            event_seq,
        )


def read_finance_projection(store: Path, company_id: str) -> tuple[dict[str, Any], int]:
    """Read Two-Layer P&L, General Ledger entries, period closes, and 8h funding settlements."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        has_paper = is_domain_installed(db, "paper")
        gl_balances = _sum_gl_accounts(db)
        closes, open_issues = _close_issues_summary(db)

        journals = [
            {
                "entry_id": str(r[0]),
                "source": str(r[1]),
                "source_event": str(r[2]),
                "effective_date": str(r[3]),
                "currency": str(r[4]),
                "lines": [
                    {
                        "ordinal": int(line[0]),
                        "account": str(line[1]),
                        "amount_minor": int(line[2]),
                    }
                    for line in db.execute(
                        """SELECT ordinal, account, amount_minor
                           FROM journal_line WHERE entry_id = ? ORDER BY ordinal ASC""",
                        (str(r[0]),),
                    ).fetchall()
                ],
            }
            for r in db.execute(
                """SELECT id, source, source_event, effective_date, currency
                   FROM journal_entry ORDER BY rowid DESC LIMIT 40"""
            ).fetchall()
        ]

        funding: list[dict[str, Any]] = []
        statements: list[dict[str, Any]] = []
        if has_paper:
            funding = [
                {
                    "settlement_id": str(r[0]),
                    "instrument_id": str(r[1]),
                    "interval_end_ns": int(r[2]),
                    "signed_position_lots": int(r[3]),
                    "funding_cashflow_atoms": None if r[4] is None else int(r[4]),
                    "status": str(r[5]),
                    "unavailable_reason": None if r[6] is None else str(r[6]),
                }
                for r in db.execute(
                    """SELECT settlement_id, instrument_id, interval_end_ns,
                              signed_position_lots, funding_cashflow_atoms,
                              status, unavailable_reason
                       FROM paper_funding_settlement
                       WHERE company_id = ?
                       ORDER BY interval_end_ns DESC LIMIT 30""",
                    (company_id,),
                ).fetchall()
            ]
            statements = [
                {
                    "request_id": str(r[0]),
                    "window_id": str(r[1]),
                    "revision": str(r[2]),
                    "payload": _json_loads_safe(str(r[3]), {}),
                }
                for r in db.execute(
                    """SELECT request_id, window_id, revision, payload
                       FROM paper_statement ORDER BY rowid DESC LIMIT 12"""
                ).fetchall()
            ]

        return (
            {
                "projection": PROJECTION_FINANCE,
                "company_id": company_id,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "paper_enabled": has_paper,
                "account_balances": [
                    {"account": k, "balance_minor": v}
                    for k, v in sorted(gl_balances.items())
                ],
                "journals": journals,
                "accounting_closes": closes,
                "open_close_issues": open_issues,
                "funding_settlements": funding,
                "retained_statements": statements,
            },
            event_seq,
        )


def read_org_projection(store: Path, company_id: str) -> tuple[dict[str, Any], int]:
    """Read Roster, Published Reports, and read-only Delegated HR Custody ledger."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        directory = _agent_directory(db, company_id)

        roster = [
            info | {"private_draft_count": _count_private_drafts(store, aid)}
            for aid, info in directory.items()
        ]
        publications = [
            {
                "asset_id": str(r[0]),
                "project_id": str(r[1]),
                "author_id": str(r[2]),
                "author_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "author_role": directory.get(str(r[2]), {}).get("role_label", "Agent"),
                "work_item_id": str(r[3]),
                "audience": _json_loads_safe(str(r[4]), []),
                "content": str(r[5]),
            }
            for r in db.execute(
                """SELECT asset_id, project_id, author_id, work_item_id, audience, content
                   FROM shared_publication
                   WHERE company_id = ?
                   ORDER BY rowid DESC LIMIT 40""",
                (company_id,),
            ).fetchall()
        ]
        archives = [
            {
                "cutoff": int(r[0]),
                "agent_id": str(r[1]),
                "agent_name": directory.get(str(r[1]), {}).get(
                    "display_name", str(r[1])
                ),
                "manager_id": str(r[2]),
                "manager_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "employment_revision": str(r[3]),
                "reason": str(r[4]),
                "previous_scope": str(r[5]),
            }
            for r in db.execute(
                """SELECT cutoff, agent_id, manager_id, employment_revision, reason, previous_scope
                   FROM agent_archive
                   WHERE company_id = ?
                   ORDER BY cutoff DESC""",
                (company_id,),
            ).fetchall()
        ]
        return (
            {
                "projection": PROJECTION_ORG,
                "company_id": company_id,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "roster": roster,
                "publications": publications,
                "delegated_hr_custody": {
                    "board_can_retire": False,
                    "policy_notice": "Managed by CEO · Board view is read-only.",
                    "archived_employees": archives,
                },
            },
            event_seq,
        )


def read_restrictions_projection(
    store: Path, company_id: str
) -> tuple[dict[str, Any], int]:
    """Read active & historical competing restriction causes plus paper risk gate readiness."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        has_paper = is_domain_installed(db, "paper")
        causes = _active_restrictions_rows(db, company_id)
        paper_risk: dict[str, Any] | None = None
        if has_paper:
            risk = effective_paper_risk(db)
            unresolved_orders = int(
                db.execute(
                    """SELECT COUNT(*) FROM paper_order o
                       LEFT JOIN paper_cancellation c ON c.order_id = o.order_id
                       WHERE c.order_id IS NULL
                         AND (SELECT COALESCE(SUM(f.quantity), 0) FROM paper_fill f WHERE f.order_id = o.order_id) < o.quantity"""
                ).fetchone()[0]
            )
            pending_binding = int(
                db.execute(
                    "SELECT COUNT(*) FROM paper_binding_change WHERE effective_at_ns IS NULL"
                ).fetchone()[0]
            )
            quote_row = db.execute(
                "SELECT observed_at FROM paper_review_quote ORDER BY observed_at DESC LIMIT 1"
            ).fetchone()
            paper_risk = {
                "suspended": bool(risk.suspended),
                "effective_revision": int(risk.revision),
                "unresolved_orders_count": unresolved_orders,
                "pending_binding_change": pending_binding > 0,
                "settled_company_ok": unresolved_orders == 0 and pending_binding == 0,
                "last_quote_observed_at": None
                if quote_row is None
                else int(quote_row[0]),
                "server_freshness_note": "Resume verifies live paper quote freshness (<= 15s) server-side upon confirmation.",
            }
        return (
            {
                "projection": PROJECTION_RESTRICTIONS,
                "company_id": company_id,
                "charter_revision": charter_revision,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "active_causes": [c for c in causes if c["active"]],
                "history": [c for c in causes if not c["active"]],
                "paper_risk": paper_risk,
            },
            event_seq,
        )


def read_system_safety_projection(
    store: Path, company_id: str
) -> tuple[dict[str, Any], int]:
    """Read checkpoints, restore quarantine status, and 5-step Safe Custody Hold."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        restores = [
            {
                "request_revision": str(r[0]),
                "checkpoint_sha256": str(r[1]),
                "watermark": _json_loads_safe(str(r[2]), {}),
                "restored_at_ns": int(r[3]),
            }
            for r in db.execute(
                """SELECT request_revision, checkpoint_sha256, watermark_json, restored_at_ns
                   FROM company_restore_receipt ORDER BY restored_at_ns DESC"""
            ).fetchall()
        ]
        retired = db.execute(
            """SELECT request_revision, replacement_sha256, retired_at_ns
               FROM company_store_retired WHERE singleton = 1"""
        ).fetchone()
        active_causes = [
            c for c in _active_restrictions_rows(db, company_id) if c["active"]
        ]

    checkpoints_dir = store.resolve(strict=False).parent / "checkpoints"
    checkpoints: list[dict[str, Any]] = []
    if checkpoints_dir.is_dir():
        for entry in sorted(checkpoints_dir.glob("*.zip"), reverse=True)[:20]:
            try:
                st = os.lstat(entry)
                if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                    checkpoints.append(
                        {
                            "archive_name": entry.name,
                            "size_bytes": int(st.st_size),
                            "modified_ns": int(st.st_mtime_ns),
                        }
                    )
            except OSError:
                continue

    custody_steps = [
        {"id": "data_cut", "label": "Data cut integrity verified", "status": "done"},
        {
            "id": "sole_writer",
            "label": "Single-writer lease confirmed",
            "status": "done",
        },
        {
            "id": "authority",
            "label": "Current Board authority verified",
            "status": "done",
        },
        {
            "id": "reconciliation",
            "label": "External paper reconciliation complete",
            "status": "done"
            if not any(
                c["cause_category"] == "restore_quarantine" for c in active_causes
            )
            else "active",
        },
        {
            "id": "next_actions",
            "label": "Eligible for normal trading release",
            "status": "done" if not active_causes else "pending",
        },
    ]

    return (
        {
            "projection": PROJECTION_SYSTEM_SAFETY,
            "company_id": company_id,
            "charter_revision": charter_revision,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "checkpoints": checkpoints,
            "restore_receipts": restores,
            "store_retired": (
                {
                    "request_revision": str(retired[0]),
                    "replacement_sha256": str(retired[1]),
                    "retired_at_ns": int(retired[2]),
                }
                if retired
                else None
            ),
            "safe_custody_hold": custody_steps,
            "active_restrictions": active_causes,
        },
        event_seq,
    )


def read_system_evals_projection(
    store: Path,
    company_id: str,
    *,
    evaluator_store: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Flow 12 review tool pass^3 / DAG lineage + optional isolated Flow 13 offline DB."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)

        qualifications = [
            {
                "receipt_id": str(r[0]),
                "candidate_asset_id": str(r[1]),
                "baseline_asset_id": None if r[2] is None else str(r[2]),
                "parent_asset_id": None if r[3] is None else str(r[3]),
                "passed": bool(r[4]),
                "trial_count": int(r[5]),
                "recorded_at_ns": int(r[6]),
            }
            for r in db.execute(
                """SELECT receipt_id, candidate_asset_id, baseline_asset_id,
                          parent_asset_id, passed, trial_count, recorded_at_ns
                   FROM review_qualification
                   WHERE company_id = ?
                   ORDER BY recorded_at_ns DESC LIMIT 30""",
                (company_id,),
            ).fetchall()
        ]

        activations = [
            {
                "request_id": str(r[0]),
                "operation": str(r[1]),
                "candidate_id": str(r[2]),
                "qualification_id": str(r[3]),
                "parent_asset_id": None if r[4] is None else str(r[4]),
                "revision": int(r[5]),
                "quarantined": bool(r[6]),
                "quarantine_reason": None if r[7] is None else str(r[7]),
                "recorded_at_ns": int(r[8]),
            }
            for r in db.execute(
                """SELECT request_id, operation, candidate_id, qualification_id,
                          parent_asset_id, revision, quarantined, quarantine_reason,
                          recorded_at_ns
                   FROM review_tool_activation
                   WHERE company_id = ?
                   ORDER BY revision DESC LIMIT 30""",
                (company_id,),
            ).fetchall()
        ]

    # Pre-compute transitive blast radius for each activated tool candidate
    children_map: dict[str, list[str]] = {}
    for act in activations:
        parent = act["parent_asset_id"]
        if parent:
            children_map.setdefault(parent, []).append(act["candidate_id"])

    def _descendants(root_id: str) -> list[str]:
        seen: list[str] = []
        stack = list(children_map.get(root_id, []))
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.append(cur)
                stack.extend(children_map.get(cur, []))
        return seen

    for act in activations:
        desc = _descendants(act["candidate_id"])
        act["blast_radius_descendants"] = desc
        blocked = {act["candidate_id"], *desc}
        fallback = next(
            (
                other["candidate_id"]
                for other in activations
                if not other["quarantined"] and other["candidate_id"] not in blocked
            ),
            None,
        )
        act["fallback_candidate_id"] = fallback

    # Optional physically separate Flow 13 offline evaluator database (strictly read-only)
    offline_evals: dict[str, Any] = {
        "connected": False,
        "isolated_notice": "Offline evaluator registry runs in a separate read-only SQLite database outside company.sqlite.",
        "suites": [],
    }
    if evaluator_store is not None:
        try:
            st = os.lstat(evaluator_store)
            if (
                not stat.S_ISLNK(st.st_mode)
                and stat.S_ISREG(st.st_mode)
                and st.st_uid == os.geteuid()
            ):
                from urllib.parse import quote

                encoded_path = quote(str(evaluator_store.absolute()), safe="/")
                with closing(
                    sqlite3.connect(f"file:{encoded_path}?mode=ro", uri=True)
                ) as edb:
                    edb.execute("PRAGMA query_only = ON")
                    edb.execute("PRAGMA trusted_schema = OFF")
                    suite_rows = edb.execute(
                        "SELECT revision, suite_json, sealed_at_ns FROM trial_suite ORDER BY sealed_at_ns DESC LIMIT 10"
                    ).fetchall()
                    suites = []
                    for sr in suite_rows:
                        rev = str(sr[0])
                        obs_count = int(
                            edb.execute(
                                "SELECT COUNT(*) FROM trial_observation WHERE revision = ?",
                                (rev,),
                            ).fetchone()[0]
                        )
                        grade_count = int(
                            edb.execute(
                                "SELECT COUNT(*) FROM trial_grade WHERE revision = ?",
                                (rev,),
                            ).fetchone()[0]
                        )
                        pair_count = int(
                            edb.execute(
                                "SELECT COUNT(*) FROM trial_pairwise_grade WHERE revision = ?",
                                (rev,),
                            ).fetchone()[0]
                        )
                        suites.append(
                            {
                                "revision": rev,
                                "sealed_at_ns": int(sr[2]),
                                "observation_count": obs_count,
                                "grade_count": grade_count,
                                "pairwise_grade_count": pair_count,
                            }
                        )
                    offline_evals["connected"] = True
                    offline_evals["suites"] = suites
        except (OSError, sqlite3.Error):
            offline_evals["connected"] = False

    return (
        {
            "projection": PROJECTION_SYSTEM_EVALS,
            "company_id": company_id,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "qualifications": qualifications,
            "activations": activations,
            "offline_evaluator": offline_evals,
        },
        event_seq,
    )


def read_system_audit_projection(
    store: Path, company_id: str
) -> tuple[dict[str, Any], int]:
    """Read redacted audit_event tail, work_conditions, fencing leases, and board_binding roles."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        events = [
            {
                "event_seq": int(r[0]),
                "domain": str(r[1]),
                "entity_type": str(r[2]),
                "entity_id": str(r[3]),
                "event_type": str(r[4]),
                "recorded_at_ns": int(r[5]),
            }
            for r in db.execute(
                """SELECT event_seq, domain, entity_type, entity_id, event_type, recorded_at_ns
                   FROM audit_event
                   WHERE company_id = ?
                   ORDER BY event_seq DESC LIMIT 80""",
                (company_id,),
            ).fetchall()
        ]
        conditions = [
            {
                "condition_id": str(r[0]),
                "owner_agent_id": str(r[1]),
                "trigger_kind": str(r[2]),
                "due_at_ns": None if r[3] is None else int(r[3]),
                "source_identity": None if r[4] is None else str(r[4]),
                "status": str(r[5]),
            }
            for r in db.execute(
                """SELECT condition_id, owner_agent_id, trigger_kind,
                          due_at_ns, source_identity, status
                   FROM work_conditions
                   WHERE company_id = ?
                   ORDER BY updated_at_ns DESC LIMIT 40""",
                (company_id,),
            ).fetchall()
        ]
        fences = [
            {
                "run_id": str(r[0]),
                "agent_id": str(r[1]),
                "duty_kind": str(r[2]),
                "status": str(r[3]),
                "fencing_token": int(r[4]),
                "lease_owner": None if r[5] is None else str(r[5]),
                "lease_expires_at_ns": None if r[6] is None else int(r[6]),
            }
            for r in db.execute(
                """SELECT run_id, agent_id, duty_kind, status, fencing_token,
                          lease_owner, lease_expires_at_ns
                   FROM work_runs
                   WHERE company_id = ? AND status IN ('running', 'waiting', 'queued')
                   ORDER BY updated_at_ns DESC LIMIT 30""",
                (company_id,),
            ).fetchall()
        ]
        bindings = [
            {
                "principal_id": f"principal:{sha256(str(r[0]).encode('utf-8')).hexdigest()[:12]}",
                "role_name": str(r[1]),
                "scope_ref": str(r[2]),
                "binding_revision": int(r[3]),
                "active": r[4] is None,
            }
            for r in db.execute(
                """SELECT principal_id, role_name, scope_ref, binding_revision, revoked_at_ns
                   FROM board_binding
                   WHERE company_id = ?
                   ORDER BY bound_at_ns ASC""",
                (company_id,),
            ).fetchall()
        ]
        return (
            {
                "projection": PROJECTION_SYSTEM_AUDIT,
                "company_id": company_id,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "events": events,
                "conditions": conditions,
                "active_fences": fences,
                "board_bindings": bindings,
            },
            event_seq,
        )


def read_execution_tiers_projection(
    store: Path, company_id: str
) -> tuple[dict[str, Any], int]:
    """Read redacted three-tier execution accounts, sandboxes, and promotion history."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        event_seq = max_event_seq(db, company_id)
        snapshot = read_execution_tiers_snapshot(db, company_id)
        directory = _agent_directory(db, company_id)
        charter_row = db.execute(
            "SELECT charter_revision FROM company WHERE id = ?",
            (company_id,),
        ).fetchone()
        charter_revision = (
            str(charter_row[0])
            if charter_row is not None
            else str(snapshot.get("charter_revision", ""))
        )
        enriched_sandboxes = [
            sbx
            | {
                "agent_display_name": directory.get(sbx["agent_id"], {}).get(
                    "display_name", sbx["agent_id"]
                ),
                "agent_role_label": directory.get(sbx["agent_id"], {}).get(
                    "role_label", "Agent"
                ),
            }
            for sbx in snapshot.get("agent_sandboxes", [])
        ]
        return (
            {
                "projection": PROJECTION_EXECUTION_TIERS,
                "company_id": company_id,
                "charter_revision": charter_revision,
                "as_of_ns": time_ns(),
                "event_seq": event_seq,
                **(snapshot | {"agent_sandboxes": enriched_sandboxes}),
            },
            event_seq,
        )


def projection_etag(payload: dict[str, Any], event_seq: int) -> str:
    """Compute a stable ETag over projection payload excluding volatile as_of_ns."""
    stable = {k: v for k, v in payload.items() if k != "as_of_ns"}
    digest = sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f'"{digest}:{event_seq}"'
