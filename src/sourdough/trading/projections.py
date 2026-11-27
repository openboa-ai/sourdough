"""Read-only SQLite projections and Core projection enrichers for the Trading domain."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from time import time_ns
from typing import Any

from ..company import FoundingRefused, _identity
from ..ipc.projections import (
    PROJECTION_AGENT_DETAIL,
    PROJECTION_AGENTS_GRAPH,
    PROJECTION_APPROVALS,
    PROJECTION_FINANCE,
    PROJECTION_RESTRICTIONS,
    PROJECTION_SYSTEM_SAFETY,
    _active_restrictions_rows,
    _agent_directory,
    _close_issues_summary,
    _json_loads_safe,
    _sum_gl_accounts,
)
from ..storage.db import open_reader, read_tx
from ..storage.kernel import max_event_seq
from ..storage.schema import installed_domains, is_domain_installed
from .domain import ENV_LIVE, ENV_PAPER, TIER_COMPANY_LIVE, TIER_COMPANY_PAPER
from .ledger import (
    read_agent_sandbox_portfolio_from_db,
    read_trading_book_state_from_db,
)
from .migration import read_execution_tiers_snapshot
from .risk import active_trading_risk_restrictions

PROJECTION_TRADING_DESK = "trading-desk-v1"
PROJECTION_ORDER_TRACE = "order-trace-v1"
PROJECTION_STRATEGIES = "strategies-v1"


def _is_sandbox_authorized(info: dict[str, Any]) -> bool:
    """Return True iff the agent is active (unarchived) and holds a trading standing grant."""
    if info.get("archived"):
        return False
    scope = str(info.get("grant_scope") or "").strip().lower()
    return scope in ("paper", "trading", "live")


def read_trading_desk_projection(
    store: Path,
    company_id: str,
    *,
    environment: str,
    book_id: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Unified symmetric trading desk projection serving both Live Trading and Paper Trading.

    Strictly scoped to `environment` ('live' or 'paper') so paper and live capital,
    positions, fills, and valuation series are never commingled.
    """
    if environment not in (ENV_LIVE, ENV_PAPER):
        raise ValueError("environment must be 'live' or 'paper'")

    target_tier = TIER_COMPANY_LIVE if environment == ENV_LIVE else TIER_COMPANY_PAPER

    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        domains = sorted(installed_domains(db))
        has_trading = is_domain_installed(db, "trading")
        charter_row = db.execute(
            "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
        ).fetchone()
        charter_revision = str(charter_row[0]) if charter_row else ""
        directory = _agent_directory(db, company_id)
        gl_balances = _sum_gl_accounts(db)
        closes, open_close_issues = _close_issues_summary(db)

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

        tiers = read_execution_tiers_snapshot(db, company_id)
        env_restrictions = (
            active_trading_risk_restrictions(db, company_id, environment)
            if has_trading
            else []
        )
        risk_revision = max(
            (int(r["restriction_revision"]) for r in env_restrictions),
            default=0,
        )

        books: list[dict[str, Any]] = []
        if has_trading:
            binding_rows = db.execute(
                """SELECT binding_id FROM execution_environment_binding
                   WHERE company_id = ? AND tier = ? AND environment = ?
                   ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'registered' THEN 1 WHEN 'suspended' THEN 2 ELSE 3 END,
                            created_at_ns ASC, binding_id ASC""",
                (company_id, target_tier, environment),
            ).fetchall()
            for (bid,) in binding_rows:
                state = read_trading_book_state_from_db(db, company_id, str(bid))
                if state is not None:
                    books.append(state)

        selected_book: dict[str, Any] | None = None
        if book_id:
            selected_book = next((b for b in books if b["binding_id"] == book_id), None)
        elif environment == ENV_LIVE:
            selected_book = next((b for b in books if b["status"] == "active"), None)
        else:
            selected_book = (
                next((b for b in books if b["role"] == "nominated"), None)
                or next((b for b in books if b["role"] == "shadow"), None)
                or next((b for b in books if b["status"] == "active"), None)
                or (books[0] if books else None)
            )

        currency = selected_book["currency"] if selected_book else "USD"
        close_has_issues = len(open_close_issues) > 0
        opex_available = has_opex_data and not close_has_issues

        cumulative_return_series: list[dict[str, Any]] = []
        open_positions: list[dict[str, Any]] = []
        execution_tape: list[dict[str, Any]] = []
        pnl_bridge: list[dict[str, Any]] = []
        exposure_used_minor = 0
        exposure_limit_minor: int | None = None

        if selected_book is not None:
            nav = int(selected_book["nav_minor_units"])
            trading_pnl = int(selected_book["pnl_minor_units"])
            ret_pct = float(selected_book["return_pct"])
            total_fees = int(selected_book["total_fees_minor_units"])
            realized_pnl = int(selected_book["realized_pnl_minor_units"])
            unrealized_pnl = int(selected_book["unrealized_pnl_minor_units"])
            gross_trading_pnl = trading_pnl + total_fees
            exposure_used_minor = int(selected_book["gross_exposure_minor_units"])
            exposure_limit_minor = int(selected_book["exposure_limit_minor_units"])
            cumulative_return_series = list(selected_book["cumulative_return_series"])

            for p in selected_book["positions"]:
                if p["quantity"] != 0:
                    open_positions.append(p)

            for o in selected_book["orders"]:
                actor_info = directory.get(o["actor_id"], {})
                execution_tape.append(
                    o
                    | {
                        "actor_name": actor_info.get("display_name", o["actor_id"]),
                        "actor_role": actor_info.get("role_label", "Agent"),
                        "book_name": selected_book["book_name"],
                        "binding_id": selected_book["binding_id"],
                    }
                )

            if opex_available:
                company_net: int | None = trading_pnl - llm_opex_minor
                net_status = "ok"
                net_source = (
                    "Live BOR minus Opex"
                    if environment == ENV_LIVE
                    else "Paper BOR minus Opex"
                )
            else:
                company_net = trading_pnl
                net_status = "ok"
                net_source = (
                    "Live BOR (no opex posted)"
                    if environment == ENV_LIVE
                    else "Paper BOR (no opex posted)"
                )

            kpis = {
                "portfolio_value": {
                    "value_minor": nav,
                    "return_pct": ret_pct,
                    "currency": currency,
                    "status": "ok",
                    "source": (
                        f"Live book · {selected_book['account_id']}"
                        if environment == ENV_LIVE
                        else f"Paper book · {selected_book['book_name']}"
                    ),
                },
                "trading_pnl": {
                    "value_minor": trading_pnl,
                    "realized_minor": realized_pnl,
                    "unrealized_minor": unrealized_pnl,
                    "fees_minor": total_fees,
                    "currency": currency,
                    "status": "ok",
                    "source": (
                        "Reconciled live fills & marks"
                        if environment == ENV_LIVE
                        else f"{selected_book['fee_bps']} bps fee / {selected_book['slippage_bps']} bps slip"
                    ),
                },
                "agent_llm_opex": {
                    "value_minor": -llm_opex_minor if has_opex_data else 0,
                    "currency": currency,
                    "status": "ok" if has_opex_data else "unavailable",
                    "source": "General ledger",
                },
                "company_net_pnl": {
                    "value_minor": company_net,
                    "currency": currency,
                    "status": net_status,
                    "source": net_source,
                },
            }
            pnl_bridge = [
                {
                    "step": "gross_trading_pnl",
                    "label": "Gross Trading P&L",
                    "amount_minor": gross_trading_pnl,
                    "status": "ok",
                },
                {
                    "step": "trading_fees",
                    "label": "Venue Fees & Slippage",
                    "amount_minor": -total_fees,
                    "status": "ok",
                },
                {
                    "step": "realized_pnl",
                    "label": "Net Realized P&L",
                    "amount_minor": realized_pnl,
                    "status": "ok",
                },
                {
                    "step": "unrealized_pnl",
                    "label": "Open Unrealized P&L",
                    "amount_minor": unrealized_pnl,
                    "status": "ok",
                },
                {
                    "step": "agent_llm_opex",
                    "label": "Agent LLM & Compute Opex",
                    "amount_minor": -llm_opex_minor if has_opex_data else 0,
                    "status": "ok" if has_opex_data else "unavailable",
                },
                {
                    "step": "company_net_pnl",
                    "label": (
                        "Live Net P&L" if environment == ENV_LIVE else "Paper Net P&L"
                    ),
                    "amount_minor": company_net,
                    "status": net_status,
                },
            ]
        else:
            kpis = {
                "portfolio_value": {
                    "value_minor": None,
                    "return_pct": None,
                    "currency": currency,
                    "status": "standby",
                    "source": (
                        "Standby (0% live capital deployed)"
                        if environment == ENV_LIVE
                        else "No paper book active"
                    ),
                },
                "trading_pnl": {
                    "value_minor": None,
                    "realized_minor": None,
                    "unrealized_minor": None,
                    "fees_minor": None,
                    "currency": currency,
                    "status": "standby",
                    "source": "Awaiting Board activation",
                },
                "agent_llm_opex": {
                    "value_minor": -llm_opex_minor if has_opex_data else 0,
                    "currency": currency,
                    "status": "ok" if has_opex_data else "unavailable",
                    "source": "General ledger",
                },
                "company_net_pnl": {
                    "value_minor": None,
                    "currency": currency,
                    "status": "standby",
                    "source": "Awaiting activation",
                },
            }

        shadow_benchmark: dict[str, Any] | None = None
        if environment == ENV_LIVE and selected_book is not None:
            shadow_row = db.execute(
                """SELECT binding_id FROM execution_environment_binding
                   WHERE company_id = ? AND environment = 'paper'
                     AND shadow_of_live_binding_id = ?
                   ORDER BY updated_at_ns DESC LIMIT 1""",
                (company_id, selected_book["binding_id"]),
            ).fetchone()
            shadow_bid = (
                str(shadow_row[0])
                if shadow_row is not None
                else selected_book.get("source_paper_binding_id")
            )
            if shadow_bid:
                shadow_state = read_trading_book_state_from_db(
                    db, company_id, shadow_bid
                )
                if shadow_state is not None:
                    raw_shadow = list(shadow_state["cumulative_return_series"])
                    live_start_ns = (
                        int(cumulative_return_series[0]["observed_at_ns"])
                        if cumulative_return_series
                        and cumulative_return_series[0].get("observed_at_ns")
                        else 0
                    )
                    rebased_series: list[dict[str, Any]] = []
                    if raw_shadow:
                        pre_or_at = [
                            pt
                            for pt in raw_shadow
                            if int(pt.get("observed_at_ns") or 0) <= live_start_ns
                        ]
                        anchor = pre_or_at[-1] if pre_or_at else raw_shadow[0]
                        anchor_nav = (
                            int(anchor["nav_minor"])
                            if anchor.get("nav_minor")
                            else int(shadow_state["starting_minor_units"])
                        )
                        rebased_series.append(
                            {
                                "id": f"{anchor['id']}-anchor",
                                "label": (
                                    str(cumulative_return_series[0]["label"])
                                    if cumulative_return_series
                                    else "Live Launch"
                                ),
                                "return_pct": 0.0,
                                "pnl_minor": 0,
                                "nav_minor": anchor_nav,
                                "source": "shadow_anchor",
                                "observed_at_ns": live_start_ns
                                or int(anchor.get("observed_at_ns") or 0),
                            }
                        )
                        live_start_cap = int(selected_book["starting_minor_units"])
                        for pt in raw_shadow:
                            pt_ns = int(pt.get("observed_at_ns") or 0)
                            if live_start_ns > 0 and pt_ns <= live_start_ns:
                                continue
                            if pt is anchor:
                                continue
                            pt_nav = int(pt.get("nav_minor") or anchor_nav)
                            delta_nav = pt_nav - anchor_nav
                            rebased_ret = (
                                round((delta_nav / anchor_nav) * 100.0, 4)
                                if anchor_nav > 0
                                else 0.0
                            )
                            equiv_pnl = (
                                round(live_start_cap * (delta_nav / anchor_nav))
                                if anchor_nav > 0 and live_start_cap > 0
                                else delta_nav
                            )
                            rebased_series.append(
                                {
                                    "id": str(pt["id"]),
                                    "label": str(pt["label"]),
                                    "return_pct": rebased_ret,
                                    "pnl_minor": equiv_pnl,
                                    "nav_minor": pt_nav,
                                    "source": str(pt.get("source") or "shadow_mark"),
                                    "observed_at_ns": pt_ns,
                                }
                            )
                    post_launch_ret = (
                        float(rebased_series[-1]["return_pct"])
                        if rebased_series
                        else 0.0
                    )
                    drift_pp = round(
                        float(selected_book["return_pct"]) - post_launch_ret, 4
                    )
                    shadow_benchmark = {
                        "binding_id": shadow_state["binding_id"],
                        "book_name": shadow_state["book_name"],
                        "strategy_release_id": shadow_state["strategy_release_id"],
                        "role": shadow_state["role"],
                        "return_pct": shadow_state["return_pct"],
                        "post_launch_return_pct": post_launch_ret,
                        "execution_drift_pp": drift_pp,
                        "nav_minor_units": shadow_state["nav_minor_units"],
                        "pnl_minor_units": shadow_state["pnl_minor_units"],
                        "series": rebased_series,
                    }

        paper_books = tiers.get("company_paper_books", [])
        soak_ready_books = sum(
            1 for b in paper_books if b["soak_ready"] and b["status"] == "active"
        )
        book_summaries = [
            {
                k: v
                for k, v in b.items()
                if k not in ("positions", "orders", "cumulative_return_series")
            }
            for b in books
        ]

        return (
            {
                "projection": PROJECTION_TRADING_DESK,
                "company_id": company_id,
                "charter_revision": charter_revision,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "active_domains": domains,
                "trading_enabled": has_trading,
                "environment": environment,
                "selected_book_id": (
                    selected_book["binding_id"] if selected_book else None
                ),
                "selected_book": (
                    {
                        k: v
                        for k, v in selected_book.items()
                        if k not in ("positions", "orders", "cumulative_return_series")
                    }
                    if selected_book
                    else None
                ),
                "books": book_summaries,
                "kpis": kpis,
                "cumulative_return_series": cumulative_return_series,
                "shadow_benchmark": shadow_benchmark,
                "open_positions": open_positions,
                "execution_tape": execution_tape,
                "pnl_reconciliation_bridge": pnl_bridge,
                "exposure_cap": {
                    "used_minor_units": exposure_used_minor,
                    "limit_minor_units": exposure_limit_minor,
                    "currency": currency,
                },
                "close_count": len(closes),
                "open_close_issues": open_close_issues,
                "risk_gate": {
                    "suspended": len(env_restrictions) > 0,
                    "effective_revision": risk_revision,
                    "active_restriction_count": len(env_restrictions),
                    "active_restrictions": env_restrictions,
                },
                "governance": {
                    "company_live": tiers.get("company_live"),
                    "live_candidates": tiers.get("live_candidates", []),
                    "qualification_receipts": tiers.get("qualification_receipts", []),
                    "promotion_history": tiers.get("promotion_history", []),
                    "migration_history": tiers.get("migration_history", []),
                    "company_paper_soak_ready": bool(
                        tiers.get("company_paper_soak_ready", False)
                    ),
                    "paper_soak_ready": bool(
                        tiers.get("company_paper_soak_ready", False)
                    ),
                    "paper_books_count": len(paper_books),
                    "soak_ready_books_count": soak_ready_books,
                    "current_core_digest": tiers.get("current_core_digest", ""),
                },
            },
            event_seq,
        )


def read_order_trace_projection(
    store: Path, company_id: str, order_id: str
) -> tuple[dict[str, Any] | None, int]:
    """Read causal lineage for any canonical trading_book_order (paper, live, or agent_sandbox)."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        event_seq = max_event_seq(db, company_id)
        if not is_domain_installed(db, "trading"):
            return None, event_seq

        row = db.execute(
            """SELECT o.order_id, o.binding_id, o.environment, o.actor_id, o.action_id,
                      o.external_order_id, o.run_id, o.decision_id, o.mandate_revision,
                      o.cancelled_at_ns, o.cancel_reason, o.instrument_id, o.side,
                      o.quantity, o.limit_minor_units, o.market_price_minor_units,
                      o.quote_observed_at_ns, o.quote_source, o.fee_bps, o.slippage_bps,
                      o.status, o.filled_quantity, o.fill_price_minor_units,
                      o.fee_minor_units, o.created_at_ns, o.updated_at_ns,
                      b.tier, b.account_id, b.destination_id, b.currency, b.book_name,
                      b.owner_agent_id
               FROM trading_book_order o
               JOIN execution_environment_binding b ON b.binding_id = o.binding_id
               WHERE o.company_id = ? AND o.order_id = ?""",
            (company_id, order_id),
        ).fetchone()
        if row is None:
            return None, event_seq

        environment = str(row[2])
        actor_id = str(row[3])
        action_id = str(row[4])
        external_order_id = None if row[5] is None else str(row[5])
        run_id = None if row[6] is None else str(row[6])
        decision_id = None if row[7] is None else str(row[7])
        mandate_revision = None if row[8] is None else str(row[8])
        cancelled_at_ns = None if row[9] is None else int(row[9])
        cancel_reason = None if row[10] is None else str(row[10])
        instrument_id = str(row[11])
        side = str(row[12])
        ordered_qty = int(row[13])
        limit_minor = int(row[14])
        market_price_minor = int(row[15])
        status = str(row[20])
        filled_qty = int(row[21])
        avg_fill_minor = None if row[22] is None else int(row[22])
        order_fee_minor = int(row[23])
        binding_id = str(row[1])
        tier = str(row[26])
        account_id = str(row[27])
        destination_id = str(row[28])
        currency = str(row[29])
        book_name = str(row[30] or account_id)

        directory = _agent_directory(db, company_id)
        actor_info = directory.get(actor_id, {})
        actor_name = actor_info.get("display_name", actor_id or "System")
        actor_role = actor_info.get("role_label", "Trading")

        run_row = (
            db.execute(
                """SELECT wr.work_item_id, wr.duty_kind, wr.status, dr.duty_id
                   FROM work_runs wr
                   LEFT JOIN duty_run dr ON dr.run_id = wr.run_id
                   WHERE wr.company_id = ? AND wr.run_id = ?
                   LIMIT 1""",
                (company_id, run_id),
            ).fetchone()
            if run_id
            else None
        )
        duty_id = str(run_row[3]) if run_row and run_row[3] is not None else None
        duty_kind = str(run_row[1]) if run_row and run_row[1] is not None else None

        ext_row = db.execute(
            """SELECT idempotency_key, status, connector, operation, receipt_json
               FROM external_actions
               WHERE company_id = ? AND (idempotency_key = ? OR command_id = ?)
               LIMIT 1""",
            (company_id, action_id, action_id),
        ).fetchone()
        ext_action = (
            {
                "idempotency_key": str(ext_row[0]),
                "status": str(ext_row[1]),
                "connector": str(ext_row[2]),
                "operation": str(ext_row[3]),
                "receipt": _json_loads_safe(ext_row[4], None),
            }
            if ext_row is not None
            else None
        )

        fills = [
            {
                "fill_id": str(f[0]),
                "quantity": int(f[1]),
                "price_minor_units": int(f[2]),
                "fee_minor_units": int(f[3]),
                "notional_minor_units": int(f[1]) * int(f[2]),
                "realized_pnl_minor_units": int(f[4]),
                "filled_at_ns": int(f[5]),
                "delivery_status": (
                    "acknowledged"
                    if f[6] == 1
                    else ("pending" if f[6] is not None else None)
                ),
            }
            for f in db.execute(
                """SELECT f.fill_id, f.quantity, f.price_minor_units,
                          f.fee_minor_units, f.realized_pnl_minor_units,
                          f.filled_at_ns, fd.acknowledged
                   FROM trading_book_fill f
                   LEFT JOIN trading_fee_delivery fd ON fd.fill_id = f.fill_id
                   WHERE f.order_id = ?
                   ORDER BY f.filled_at_ns ASC, f.fill_id ASC""",
                (order_id,),
            ).fetchall()
        ]
        total_fee_minor = (
            sum(f["fee_minor_units"] for f in fills) if fills else order_fee_minor
        )
        if avg_fill_minor is None and filled_qty > 0 and fills:
            avg_fill_minor = int(
                sum(f["price_minor_units"] * f["quantity"] for f in fills) / filled_qty
            )

        pos_row = db.execute(
            """SELECT quantity FROM trading_book_position
               WHERE binding_id = ? AND instrument_id = ?""",
            (binding_id, instrument_id),
        ).fetchone()
        residual_pos = int(pos_row[0]) if pos_row is not None else 0

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
                (company_id, actor_id),
            ).fetchall()
        ]

        back_link = (
            "/trading/live"
            if environment == ENV_LIVE
            else (
                "/trading/paper" if environment == ENV_PAPER else f"/agents/{actor_id}"
            )
        )
        steps: list[dict[str, Any]] = [
            {
                "stage": "Work Run",
                "id": duty_id or run_id or "direct",
                "detail": (
                    f"Actor: {actor_name} ({actor_role})"
                    + (f" · Duty: {duty_kind}" if duty_kind else "")
                ),
                "status": "complete" if run_id else "unavailable",
            },
            {
                "stage": "Execution Binding",
                "id": decision_id or binding_id,
                "detail": (
                    f"Environment: {environment} · Book: {book_name} ({account_id})"
                    + (f" · Mandate: {mandate_revision}" if mandate_revision else "")
                ),
                "status": "complete",
            },
            {
                "stage": "Order",
                "id": order_id,
                "detail": (
                    f"{side.upper()} {ordered_qty} @ limit {limit_minor} "
                    f"({instrument_id}) · {environment}"
                ),
                "status": "complete",
            },
            {
                "stage": "Fills & Fees",
                "id": f"{len(fills)} fills",
                "detail": f"Filled {filled_qty} / {ordered_qty} · Fees {total_fee_minor}",
                "status": (
                    "complete"
                    if fills
                    else ("unavailable" if status == "rejected" else "pending")
                ),
            },
        ]
        if status == "cancelled" or cancelled_at_ns is not None:
            steps.append(
                {
                    "stage": "Cancellation",
                    "id": order_id,
                    "detail": (
                        f"{cancel_reason or 'Cancelled'} · "
                        f"Observed book position: {residual_pos}"
                    ),
                    "status": "warning" if residual_pos != 0 else "complete",
                }
            )
        if ext_action is not None or external_order_id is not None:
            steps.append(
                {
                    "stage": "Venue Action",
                    "id": external_order_id or action_id,
                    "detail": (
                        f"Destination: {destination_id} · Status: "
                        f"{ext_action['status'] if ext_action else status}"
                    ),
                    "status": (
                        "complete"
                        if (
                            ext_action is None
                            or ext_action["status"] in ("confirmed", "rejected")
                        )
                        else "pending"
                    ),
                }
            )
        steps.append(
            {
                "stage": "Position",
                "id": instrument_id,
                "detail": f"Observed quantity: {residual_pos}",
                "status": "complete",
            }
        )

        match = {
            "order_id": order_id,
            "binding_id": binding_id,
            "book_name": book_name,
            "tier": tier,
            "environment": environment,
            "account_id": account_id,
            "destination_id": destination_id,
            "currency": currency,
            "instrument": instrument_id,
            "instrument_id": instrument_id,
            "side": side,
            "action": side,
            "actor_id": actor_id,
            "agent_id": actor_id,
            "agent_name": actor_name,
            "agent_role": actor_role,
            "run_id": run_id,
            "duty_id": duty_id,
            "duty_kind": duty_kind,
            "decision_id": decision_id,
            "mandate_revision": mandate_revision,
            "action_id": action_id,
            "external_order_id": external_order_id,
            "ordered_qty": ordered_qty,
            "filled_qty": filled_qty,
            "remaining_qty": max(ordered_qty - filled_qty, 0),
            "limit_minor_units": limit_minor,
            "market_price_minor_units": market_price_minor,
            "avg_fill_minor_units": avg_fill_minor,
            "total_fee_minor_units": total_fee_minor,
            "status": status,
            "cancelled_at_ns": cancelled_at_ns,
            "cancel_reason": cancel_reason,
            "residual_position_qty": residual_pos,
            "cancel_not_flat": bool(status == "cancelled" and residual_pos != 0),
            "fills": fills,
            "external_action": ext_action,
            "created_at_ns": int(row[24]),
            "updated_at_ns": int(row[25]),
            "back_link": back_link,
        }
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


def read_strategies_projection(
    store: Path, company_id: str
) -> tuple[dict[str, Any], int]:
    """Read strategy releases, paired challenger trials, deployments & execution binding."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        if not is_domain_installed(db, "trading"):
            return (
                {
                    "projection": PROJECTION_STRATEGIES,
                    "company_id": company_id,
                    "as_of_ns": as_of_ns,
                    "event_seq": event_seq,
                    "trading_enabled": False,
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
            """SELECT binding_revision, account_id, connector_revision,
                      exposure_limit_minor_units, currency, starting_minor_units,
                      environment, destination_id, binding_id
               FROM execution_environment_binding
               WHERE company_id = ? AND tier = 'company_paper'
               ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'registered' THEN 1 ELSE 2 END,
                        updated_at_ns DESC
               LIMIT 1""",
            (company_id,),
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
                   FROM trading_strategy_release
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
                   FROM trading_challenger_trial
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
                   FROM trading_strategy_deployment
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
                "binding_id": str(binding[8]),
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
                "trading_enabled": True,
                "active_deployment": active,
                "deployments": deployments,
                "releases": releases,
                "trials": trials,
                "adapter": adapter,
            },
            event_seq,
        )


def enrich_trading_core_projection(
    projection_name: str,
    db: sqlite3.Connection,
    company_id: str,
    payload: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> None:
    """Enrich Core projections with Trading state ONLY when trading domain is installed."""
    if not is_domain_installed(db, "trading"):
        return

    if projection_name == PROJECTION_AGENTS_GRAPH:
        directory = _agent_directory(db, company_id)
        for node in payload.get("nodes", []):
            aid = str(node.get("agent_id", ""))
            info = directory.get(aid, {})
            if not info.get("is_ceo") and node.get("role_label") == "Operations & QA":
                scope = str(info.get("grant_scope") or "").lower()
                if "research" in scope or "paper" in scope or "trading" in scope:
                    node["role_label"] = "Quant Research"
            sandbox_auth = _is_sandbox_authorized(info)
            node["sandbox_authorized"] = sandbox_auth
            sandbox_activity: dict[str, Any] | None = None
            if sandbox_auth:
                portfolio = read_agent_sandbox_portfolio_from_db(db, company_id, aid)
                if portfolio is not None:
                    open_pos = sum(
                        1 for p in portfolio["positions"] if p["quantity"] != 0
                    )
                    run = node.get("active_run")
                    is_running_sandbox = (
                        isinstance(run, dict)
                        and run.get("status") in ("running", "waiting")
                        and run.get("duty_kind")
                        in ("quant.agent_sandbox", "trading.agent_sandbox")
                    )
                    if (
                        portfolio["fill_count"] > 0
                        or len(portfolio["orders"]) > 0
                        or open_pos > 0
                        or is_running_sandbox
                    ):
                        binding = portfolio["binding"]
                        sandbox_activity = {
                            "account_id": binding["account_id"],
                            "currency": binding["currency"],
                            "fill_count": portfolio["fill_count"],
                            "open_positions_count": open_pos,
                            "cash_minor_units": portfolio["cash_minor_units"],
                            "gross_exposure_minor_units": portfolio[
                                "gross_exposure_minor_units"
                            ],
                        }
            node["sandbox_activity"] = sandbox_activity
        return

    if projection_name == PROJECTION_AGENT_DETAIL:
        profile = payload.get("profile")
        if not isinstance(profile, dict):
            return
        aid = str(profile.get("agent_id", ""))
        if not profile.get("is_ceo") and profile.get("role_label") == "Operations & QA":
            scope = str(profile.get("grant_scope") or "").lower()
            if "research" in scope or "paper" in scope or "trading" in scope:
                profile["role_label"] = "Quant Research"
        sandbox_auth = _is_sandbox_authorized(profile)
        payload["sandbox_authorized"] = sandbox_auth
        payload["agent_sandbox"] = (
            read_agent_sandbox_portfolio_from_db(db, company_id, aid)
            if sandbox_auth
            else None
        )
        auth_budget = payload.get("authority_budget")
        if isinstance(auth_budget, dict):
            prohibs = list(auth_budget.get("prohibitions") or [])
            prohibs.append("Cannot execute live-money orders without live promotion")
            auth_budget["prohibitions"] = prohibs
        return

    if projection_name == PROJECTION_APPROVALS:
        promote_candidates: list[dict[str, Any]] = []
        for r in db.execute(
            """SELECT comparison_id, incumbent_release_id, challenger_release_id,
                      status, verdict, verdict_json, evaluated_at_ns
               FROM trading_challenger_trial
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
        payload["promote_candidates"] = promote_candidates
        return

    if projection_name == PROJECTION_FINANCE:
        payload["trading_enabled"] = True
        payload["funding_settlements"] = [
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
                   FROM trading_funding_settlement
                   WHERE company_id = ?
                   ORDER BY interval_end_ns DESC LIMIT 30""",
                (company_id,),
            ).fetchall()
        ]
        payload["retained_statements"] = [
            {
                "request_id": str(r[0]),
                "window_id": str(r[1]),
                "revision": str(r[2]),
                "payload": _json_loads_safe(str(r[3]), {}),
            }
            for r in db.execute(
                """SELECT request_id, window_id, revision, payload
                   FROM trading_statement ORDER BY rowid DESC LIMIT 12"""
            ).fetchall()
        ]
        return

    if projection_name == PROJECTION_RESTRICTIONS:
        paper_risk = active_trading_risk_restrictions(db, company_id, ENV_PAPER)
        live_risk = active_trading_risk_restrictions(db, company_id, ENV_LIVE)
        all_exec_risk = active_trading_risk_restrictions(db, company_id, ENV_PAPER) + [
            r for r in live_risk if r["scope_ref"] == "execution:live"
        ]
        open_orders = int(
            db.execute(
                """SELECT COUNT(*) FROM trading_book_order
                   WHERE company_id = ? AND status = 'open'
                     AND environment IN ('paper', 'live')""",
                (company_id,),
            ).fetchone()[0]
        )
        pending_ext = int(
            db.execute(
                """SELECT COUNT(*) FROM external_actions
                   WHERE company_id = ?
                     AND status IN ('prepared', 'in_flight', 'unknown')""",
                (company_id,),
            ).fetchone()[0]
        )
        max_rev = max(
            (int(r["restriction_revision"]) for r in all_exec_risk),
            default=0,
        )
        payload["trading_risk"] = {
            "paper_suspended": len(paper_risk) > 0,
            "live_suspended": len(live_risk) > 0,
            "effective_revision": max_rev,
            "unresolved_orders_count": open_orders,
            "unresolved_external_actions_count": pending_ext,
            "settled_company_ok": open_orders == 0 and pending_ext == 0,
            "server_freshness_note": "Resume verifies live quote freshness (<= 15s) server-side upon confirmation.",
        }
        return

    if projection_name == PROJECTION_SYSTEM_SAFETY:
        active_causes = [
            c for c in _active_restrictions_rows(db, company_id) if c["active"]
        ]
        has_quarantine = any(
            c["cause_category"] == "restore_quarantine" for c in active_causes
        )
        has_trading_pause = any(
            c["scope_ref"].startswith("execution:")
            or c["cause_category"] == "trading_risk_pause"
            for c in active_causes
        )
        for step in payload.get("safe_custody_hold", []):
            if step.get("id") == "reconciliation":
                step["label"] = "External trading reconciliation complete"
                step["status"] = "active" if has_quarantine else "done"
            elif step.get("id") == "next_actions":
                step["label"] = "Eligible for normal trading release"
                step["status"] = (
                    "pending" if (active_causes or has_trading_pause) else "done"
                )
        return
