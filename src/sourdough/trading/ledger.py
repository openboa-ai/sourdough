"""Unified TradingBook ledgers across agent_sandbox, company_paper, and company_live."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from sqlite3 import Connection
from time import time_ns
from typing import Any
from uuid import uuid4

from ..company import _identity
from ..duty_run import employment_cutoff
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..storage.kernel import append_audit_event
from ..storage.schema import is_domain_installed
from ..writer_lease import WriterLease
from .domain import (
    _SELECT_BINDING_COLS,
    ENV_AGENT_SANDBOX,
    ENV_LIVE,
    ENV_PAPER,
    TIER_AGENT_SANDBOX,
    TIER_COMPANY_LIVE,
    TIER_COMPANY_PAPER,
    ExecutionBinding,
    ExecutionTierRefused,
    SandboxOrderReceipt,
    _require_positive_int,
    _require_safe_id,
    _require_text,
    _row_to_binding,
)
from .risk import require_trading_risk_clear


def _assert_active_agent(db: Connection, company_id: str, agent_id: str) -> None:
    row = db.execute(
        "SELECT 1 FROM agent WHERE id = ? AND company_id = ?",
        (agent_id, company_id),
    ).fetchone()
    if row is None:
        raise ExecutionTierRefused("Agent does not belong to this company")
    if employment_cutoff(db, agent_id) is not None:
        raise ExecutionTierRefused("Retired agent cannot operate an execution sandbox")
    grants = db.execute(
        "SELECT scope FROM standing_grant WHERE agent_id = ?",
        (agent_id,),
    ).fetchall()
    if grants and all(str(r[0]).strip().lower() == "revoked" for r in grants):
        raise ExecutionTierRefused("Revoked agent cannot operate an execution sandbox")


def _compute_unified_book_metrics_in_tx(
    db: Connection,
    binding: ExecutionBinding,
    *,
    mark_prices: dict[str, int] | None = None,
) -> dict[str, int]:
    """Compute cash, gross exposure, realized/unrealized P&L, fees, and exact NAV."""
    cash = binding.starting_minor_units
    for side, qty, price, fee in db.execute(
        """SELECT side, quantity, price_minor_units, fee_minor_units
           FROM trading_book_fill
           WHERE binding_id = ? AND environment = ?""",
        (binding.binding_id, binding.environment),
    ).fetchall():
        notional = int(qty) * int(price)
        if side == "buy":
            cash -= notional + int(fee)
        else:
            cash += notional - int(fee)

    gross_exposure = 0
    realized_pnl = 0
    unrealized_pnl = 0
    total_fees = 0
    for inst, qty, avg_entry, stored_mark, pos_realized, pos_fees in db.execute(
        """SELECT instrument_id, quantity, avg_entry_minor_units,
                  mark_price_minor_units, realized_pnl_minor_units,
                  total_fees_minor_units
           FROM trading_book_position
           WHERE binding_id = ? AND environment = ?""",
        (binding.binding_id, binding.environment),
    ).fetchall():
        q = int(qty)
        avg = int(avg_entry)
        mark = (
            int(mark_prices[str(inst)])
            if mark_prices and str(inst) in mark_prices
            else (int(stored_mark) if int(stored_mark) > 0 else avg)
        )
        realized_pnl += int(pos_realized)
        total_fees += int(pos_fees)
        if q != 0:
            gross_exposure += abs(q) * mark
            if q > 0:
                unrealized_pnl += (mark - avg) * q
            else:
                unrealized_pnl += (avg - mark) * abs(q)

    nav = binding.starting_minor_units + realized_pnl + unrealized_pnl
    return {
        "cash_minor_units": cash,
        "gross_exposure_minor_units": gross_exposure,
        "realized_pnl_minor_units": realized_pnl,
        "unrealized_pnl_minor_units": unrealized_pnl,
        "total_fees_minor_units": total_fees,
        "nav_minor_units": nav,
    }


def _insert_book_valuation_in_tx(
    db: Connection,
    binding: ExecutionBinding,
    *,
    label: str,
    mark_prices: dict[str, int] | None = None,
    source: str = "book_valuation",
    recorded_at_ns: int | None = None,
) -> dict[str, Any]:
    """Append a sealed NAV/P&L valuation cut into trading_book_valuation inside an open write_tx."""
    now = time_ns() if recorded_at_ns is None else recorded_at_ns
    if mark_prices:
        for inst, px in mark_prices.items():
            if type(px) is int and px > 0:
                db.execute(
                    """UPDATE trading_book_position
                       SET mark_price_minor_units = ?, updated_at_ns = ?
                       WHERE binding_id = ? AND environment = ? AND instrument_id = ?""",
                    (px, now, binding.binding_id, binding.environment, inst),
                )
    metrics = _compute_unified_book_metrics_in_tx(db, binding, mark_prices=mark_prices)
    valuation_id = f"bval-{uuid4().hex[:16]}"
    db.execute(
        """INSERT INTO trading_book_valuation (
               valuation_id, binding_id, environment, company_id, label,
               nav_minor_units, cash_minor_units, gross_exposure_minor_units,
               realized_pnl_minor_units, unrealized_pnl_minor_units,
               total_fees_minor_units, source, recorded_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            valuation_id,
            binding.binding_id,
            binding.environment,
            binding.company_id,
            label,
            metrics["nav_minor_units"],
            metrics["cash_minor_units"],
            metrics["gross_exposure_minor_units"],
            metrics["realized_pnl_minor_units"],
            metrics["unrealized_pnl_minor_units"],
            metrics["total_fees_minor_units"],
            source,
            now,
        ),
    )
    return {
        "valuation_id": valuation_id,
        "binding_id": binding.binding_id,
        "environment": binding.environment,
        "label": label,
        "source": source,
        "recorded_at_ns": now,
        **metrics,
    }


def record_trading_book_valuation(
    writer: WriterLease,
    *,
    binding_id: str,
    label: str,
    mark_prices: dict[str, int] | None = None,
    source: str = "book_mark",
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Seal a mark-to-market NAV valuation cut for any active company_paper or company_live book."""
    writer.verify()
    _require_safe_id(binding_id, "binding_id")
    clean_label = _require_text(label, "label", max_len=128)
    clean_source = _require_text(source, "source", max_len=64)
    if mark_prices is not None:
        if not isinstance(mark_prices, dict):
            raise ExecutionTierRefused("mark_prices must be a dict[str, int]")
        for inst, px in mark_prices.items():
            _require_safe_id(inst, "instrument_id")
            _require_positive_int(px, f"mark_price[{inst}]")

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "trading"):
            raise ExecutionTierRefused("Trading domain is not installed")
        row = db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ? AND binding_id = ?
                  AND tier IN (?, ?) AND status = 'active'""",
            (writer.company_id, binding_id, TIER_COMPANY_PAPER, TIER_COMPANY_LIVE),
        ).fetchone()
        if row is None:
            raise ExecutionTierRefused(
                f"Active company trading book '{binding_id}' not found"
            )
        binding = _row_to_binding(row)
        cut = _insert_book_valuation_in_tx(
            db,
            binding,
            label=clean_label,
            mark_prices=mark_prices,
            source=clean_source,
            recorded_at_ns=now_ns,
        )
        writer.verify()
        return cut


def has_company_paper_soak_evidence(db: Connection, company_id: str) -> bool:
    """Return True iff company_paper is active AND has observed fills, valuations, or statements."""
    if not is_domain_installed(db, "trading"):
        return False
    has_tier_paper = (
        db.execute(
            """SELECT 1 FROM execution_environment_binding
               WHERE company_id = ? AND tier = ? AND environment = ? AND status = 'active'
               LIMIT 1""",
            (company_id, TIER_COMPANY_PAPER, ENV_PAPER),
        ).fetchone()
        is not None
    )
    if not has_tier_paper:
        return False
    if (
        db.execute(
            "SELECT 1 FROM trading_book_fill WHERE company_id = ? AND environment = 'paper' LIMIT 1",
            (company_id,),
        ).fetchone()
        is not None
    ):
        return True
    if (
        db.execute(
            """SELECT 1 FROM trading_book_valuation
               WHERE company_id = ? AND environment = 'paper'
                 AND source NOT IN ('book_open', 'book_init')
               LIMIT 1""",
            (company_id,),
        ).fetchone()
        is not None
    ):
        return True
    if db.execute("SELECT 1 FROM trading_statement LIMIT 1").fetchone() is not None:
        return True
    return False


# ---------------------------------------------------------------------------
# Tier 1: Per-Agent Isolated Sandbox Orders & Portfolio
# ---------------------------------------------------------------------------


def submit_agent_sandbox_order(
    writer: WriterLease,
    *,
    agent_id: str,
    action_id: str,
    instrument_id: str,
    side: str,
    quantity: int,
    limit_minor_units: int,
    market_price_minor_units: int,
    quote_observed_at_ns: int | None = None,
    fill_mode: str = "immediate",
    run_id: str | None = None,
    decision_id: str | None = None,
    mandate_revision: str | None = None,
) -> SandboxOrderReceipt:
    """Submit a simulated order strictly inside an Agent's isolated Tier 1 sandbox."""
    writer.verify()
    _require_safe_id(agent_id, "agent_id")
    _require_safe_id(action_id, "action_id")
    _require_safe_id(instrument_id, "instrument_id")
    if side not in ("buy", "sell"):
        raise ExecutionTierRefused("side must be 'buy' or 'sell'")
    if fill_mode not in ("immediate", "resting"):
        raise ExecutionTierRefused("fill_mode must be 'immediate' or 'resting'")
    _require_positive_int(quantity, "quantity")
    _require_positive_int(limit_minor_units, "limit_minor_units")
    _require_positive_int(market_price_minor_units, "market_price_minor_units")
    observed_ns = time_ns() if quote_observed_at_ns is None else quote_observed_at_ns
    _require_positive_int(observed_ns, "quote_observed_at_ns")

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "trading"):
            raise ExecutionTierRefused("Agent sandbox is not opened")
        _assert_active_agent(db, writer.company_id, agent_id)

        binding_row = db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ? AND tier = ? AND environment = ?
                  AND owner_agent_id = ? AND status = 'active'""",
            (writer.company_id, TIER_AGENT_SANDBOX, ENV_AGENT_SANDBOX, agent_id),
        ).fetchone()
        if binding_row is None:
            raise ExecutionTierRefused("Agent has no active sandbox account")
        binding = _row_to_binding(binding_row)

        existing = db.execute(
            """SELECT order_id, instrument_id, side, quantity, limit_minor_units,
                      market_price_minor_units, status, filled_quantity,
                      fill_price_minor_units, fee_minor_units
               FROM trading_book_order
               WHERE binding_id = ? AND environment = 'agent_sandbox' AND action_id = ?""",
            (binding.binding_id, action_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing[1]),
                str(existing[2]),
                int(existing[3]),
                int(existing[4]),
            ) != (instrument_id, side, quantity, limit_minor_units):
                raise ExecutionTierRefused(
                    "Sandbox action_id conflicts with previously submitted order terms"
                )
            fill_row = db.execute(
                """SELECT fill_id, realized_pnl_minor_units
                   FROM trading_book_fill
                   WHERE order_id = ? AND environment = 'agent_sandbox'""",
                (str(existing[0]),),
            ).fetchone()
            return SandboxOrderReceipt(
                order_id=str(existing[0]),
                binding_id=binding.binding_id,
                agent_id=agent_id,
                action_id=action_id,
                instrument_id=instrument_id,
                side=side,
                quantity=quantity,
                limit_minor_units=limit_minor_units,
                market_price_minor_units=int(existing[5]),
                status=str(existing[6]),
                filled_quantity=int(existing[7]),
                fill_price_minor_units=None
                if existing[8] is None
                else int(existing[8]),
                fee_minor_units=int(existing[9]),
                fill_id=None if fill_row is None else str(fill_row[0]),
                realized_pnl_minor_units=0 if fill_row is None else int(fill_row[1]),
                replayed=True,
            )

        if side == "buy":
            slipped_price = (
                market_price_minor_units
                + (market_price_minor_units * binding.slippage_bps + 9_999) // 10_000
            )
            crosses_limit = slipped_price <= limit_minor_units
        else:
            slipped_price = max(
                1,
                market_price_minor_units
                - (market_price_minor_units * binding.slippage_bps) // 10_000,
            )
            crosses_limit = slipped_price >= limit_minor_units

        metrics_before = _compute_unified_book_metrics_in_tx(db, binding)
        cash_available = metrics_before["cash_minor_units"]
        gross_exposure = metrics_before["gross_exposure_minor_units"]

        pos_row = db.execute(
            """SELECT quantity, avg_entry_minor_units, mark_price_minor_units,
                      realized_pnl_minor_units, total_fees_minor_units
               FROM trading_book_position
               WHERE binding_id = ? AND environment = 'agent_sandbox' AND instrument_id = ?""",
            (binding.binding_id, instrument_id),
        ).fetchone()
        cur_qty = int(pos_row[0]) if pos_row else 0
        cur_avg = int(pos_row[1]) if pos_row else 0
        cur_mark = int(pos_row[2]) if pos_row and int(pos_row[2]) > 0 else cur_avg
        cur_realized = int(pos_row[3]) if pos_row else 0
        cur_fees = int(pos_row[4]) if pos_row else 0

        now = time_ns()
        order_id = f"sbxo-{uuid4().hex[:16]}"
        should_fill = fill_mode == "immediate" and crosses_limit

        if should_fill:
            notional = quantity * slipped_price
            fee = (notional * binding.fee_bps + 5_000) // 10_000
            delta = quantity if side == "buy" else -quantity
            new_qty = cur_qty + delta
            other_exposure = gross_exposure - abs(cur_qty) * cur_mark

            realized_delta = 0
            if (
                cur_qty == 0
                or (cur_qty > 0 and delta > 0)
                or (cur_qty < 0 and delta < 0)
            ):
                total_cost = abs(cur_qty) * cur_avg + quantity * slipped_price
                new_avg = total_cost // abs(new_qty)
            else:
                closing_qty = min(abs(cur_qty), quantity)
                if cur_qty > 0:
                    realized_delta = (slipped_price - cur_avg) * closing_qty
                else:
                    realized_delta = (cur_avg - slipped_price) * closing_qty
                if new_qty == 0:
                    new_avg = 0
                elif (cur_qty > 0 and new_qty > 0) or (cur_qty < 0 and new_qty < 0):
                    new_avg = cur_avg
                else:
                    new_avg = slipped_price

            projected_exposure = other_exposure + abs(new_qty) * slipped_price
            if projected_exposure > binding.exposure_limit_minor_units:
                raise ExecutionTierRefused(
                    "Sandbox order exceeds agent sandbox exposure limit"
                )
            if side == "buy" and notional + fee > cash_available:
                raise ExecutionTierRefused(
                    "Sandbox order exceeds available virtual cash"
                )
            realized_net = realized_delta - fee
            fill_id = f"sbxf-{uuid4().hex[:16]}"
            db.execute(
                """INSERT INTO trading_book_order (
                       order_id, binding_id, environment, company_id, actor_id,
                       action_id, external_order_id, run_id, decision_id,
                       mandate_revision, instrument_id, side, quantity,
                       limit_minor_units, market_price_minor_units,
                       quote_observed_at_ns, quote_source, fee_bps, slippage_bps,
                       status, filled_quantity, fill_price_minor_units,
                       fee_minor_units, created_at_ns, updated_at_ns
                   ) VALUES (?, ?, 'agent_sandbox', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    binding.binding_id,
                    writer.company_id,
                    agent_id,
                    action_id,
                    run_id,
                    decision_id,
                    mandate_revision,
                    instrument_id,
                    side,
                    quantity,
                    limit_minor_units,
                    market_price_minor_units,
                    observed_ns,
                    binding.quote_source,
                    binding.fee_bps,
                    binding.slippage_bps,
                    quantity,
                    slipped_price,
                    fee,
                    now,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO trading_book_fill (
                       fill_id, order_id, binding_id, environment, company_id,
                       actor_id, external_fill_id, instrument_id, side, quantity,
                       price_minor_units, fee_minor_units, realized_pnl_minor_units,
                       filled_at_ns
                   ) VALUES (?, ?, ?, 'agent_sandbox', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fill_id,
                    order_id,
                    binding.binding_id,
                    writer.company_id,
                    agent_id,
                    instrument_id,
                    side,
                    quantity,
                    slipped_price,
                    fee,
                    realized_net,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO trading_book_position (
                       binding_id, environment, company_id, instrument_id,
                       quantity, avg_entry_minor_units, mark_price_minor_units,
                       realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
                   ) VALUES (?, 'agent_sandbox', ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (binding_id, instrument_id) DO UPDATE SET
                       quantity = excluded.quantity,
                       avg_entry_minor_units = excluded.avg_entry_minor_units,
                       mark_price_minor_units = excluded.mark_price_minor_units,
                       realized_pnl_minor_units = excluded.realized_pnl_minor_units,
                       total_fees_minor_units = excluded.total_fees_minor_units,
                       updated_at_ns = excluded.updated_at_ns""",
                (
                    binding.binding_id,
                    writer.company_id,
                    instrument_id,
                    new_qty,
                    new_avg,
                    slipped_price,
                    cur_realized + realized_net,
                    cur_fees + fee,
                    now,
                ),
            )
            append_audit_event(
                db,
                company_id=writer.company_id,
                domain="trading",
                entity_type="trading_book_fill",
                entity_id=fill_id,
                event_type="agent_sandbox.order_filled",
                actor_id=agent_id,
                payload={
                    "order_id": order_id,
                    "fill_id": fill_id,
                    "binding_id": binding.binding_id,
                    "environment": ENV_AGENT_SANDBOX,
                    "instrument_id": instrument_id,
                    "side": side,
                    "quantity": quantity,
                    "price_minor_units": slipped_price,
                    "fee_minor_units": fee,
                },
                recorded_at_ns=now,
            )
            writer.verify()
            return SandboxOrderReceipt(
                order_id=order_id,
                binding_id=binding.binding_id,
                agent_id=agent_id,
                action_id=action_id,
                instrument_id=instrument_id,
                side=side,
                quantity=quantity,
                limit_minor_units=limit_minor_units,
                market_price_minor_units=market_price_minor_units,
                status="filled",
                filled_quantity=quantity,
                fill_price_minor_units=slipped_price,
                fee_minor_units=fee,
                fill_id=fill_id,
                realized_pnl_minor_units=realized_net,
                replayed=False,
            )

        if side == "buy" and quantity * limit_minor_units > cash_available:
            raise ExecutionTierRefused(
                "Sandbox limit order exceeds available virtual cash"
            )
        db.execute(
            """INSERT INTO trading_book_order (
                   order_id, binding_id, environment, company_id, actor_id,
                   action_id, external_order_id, run_id, decision_id,
                   mandate_revision, instrument_id, side, quantity,
                   limit_minor_units, market_price_minor_units,
                   quote_observed_at_ns, quote_source, fee_bps, slippage_bps,
                   status, filled_quantity, fill_price_minor_units,
                   fee_minor_units, created_at_ns, updated_at_ns
               ) VALUES (?, ?, 'agent_sandbox', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, NULL, 0, ?, ?)""",
            (
                order_id,
                binding.binding_id,
                writer.company_id,
                agent_id,
                action_id,
                run_id,
                decision_id,
                mandate_revision,
                instrument_id,
                side,
                quantity,
                limit_minor_units,
                market_price_minor_units,
                observed_ns,
                binding.quote_source,
                binding.fee_bps,
                binding.slippage_bps,
                now,
                now,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="trading_book_order",
            entity_id=order_id,
            event_type="agent_sandbox.order_opened",
            actor_id=agent_id,
            payload={
                "order_id": order_id,
                "binding_id": binding.binding_id,
                "environment": ENV_AGENT_SANDBOX,
                "instrument_id": instrument_id,
                "side": side,
                "quantity": quantity,
                "limit_minor_units": limit_minor_units,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return SandboxOrderReceipt(
            order_id=order_id,
            binding_id=binding.binding_id,
            agent_id=agent_id,
            action_id=action_id,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            limit_minor_units=limit_minor_units,
            market_price_minor_units=market_price_minor_units,
            status="open",
            filled_quantity=0,
            fill_price_minor_units=None,
            fee_minor_units=0,
            fill_id=None,
            realized_pnl_minor_units=0,
            replayed=False,
        )


def cancel_agent_sandbox_order(
    writer: WriterLease,
    *,
    agent_id: str,
    order_id: str,
) -> dict[str, Any]:
    """Cancel an open order strictly within the owning Agent's Tier 1 sandbox."""
    writer.verify()
    _require_safe_id(agent_id, "agent_id")
    _require_safe_id(order_id, "order_id")
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "trading"):
            raise ExecutionTierRefused("Agent sandbox order not found")
        _assert_active_agent(db, writer.company_id, agent_id)
        row = db.execute(
            """SELECT binding_id, actor_id, status
               FROM trading_book_order
               WHERE company_id = ? AND environment = 'agent_sandbox' AND order_id = ?""",
            (writer.company_id, order_id),
        ).fetchone()
        if row is None or str(row[1]) != agent_id:
            raise ExecutionTierRefused("Sandbox order does not belong to this agent")
        status = str(row[2])
        if status == "cancelled":
            return {"order_id": order_id, "status": "cancelled", "replayed": True}
        if status != "open":
            raise ExecutionTierRefused(
                f"Cannot cancel sandbox order in terminal state '{status}'"
            )
        now = time_ns()
        db.execute(
            """UPDATE trading_book_order
               SET status = 'cancelled', cancelled_at_ns = ?,
                   cancel_reason = 'agent_cancelled', updated_at_ns = ?
               WHERE order_id = ? AND environment = 'agent_sandbox'""",
            (now, now, order_id),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="trading_book_order",
            entity_id=order_id,
            event_type="agent_sandbox.order_cancelled",
            actor_id=agent_id,
            payload={
                "order_id": order_id,
                "binding_id": str(row[0]),
                "environment": ENV_AGENT_SANDBOX,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return {"order_id": order_id, "status": "cancelled", "replayed": False}


def read_agent_sandbox_portfolio_from_db(
    db: Connection, company_id: str, agent_id: str
) -> dict[str, Any] | None:
    """Read an Agent's active Tier 1 sandbox portfolio inside an existing read transaction."""
    if not is_domain_installed(db, "trading"):
        return None
    row = db.execute(
        f"""{_SELECT_BINDING_COLS}
            WHERE company_id = ? AND tier = ? AND environment = ?
              AND owner_agent_id = ? AND status = 'active'""",
        (company_id, TIER_AGENT_SANDBOX, ENV_AGENT_SANDBOX, agent_id),
    ).fetchone()
    if row is None:
        return None
    binding = _row_to_binding(row)
    metrics = _compute_unified_book_metrics_in_tx(db, binding)
    positions = [
        {
            "instrument_id": str(r[0]),
            "quantity": int(r[1]),
            "avg_entry_minor_units": int(r[2]),
            "realized_pnl_minor_units": int(r[3]),
            "total_fees_minor_units": int(r[4]),
            "updated_at_ns": int(r[5]),
        }
        for r in db.execute(
            """SELECT instrument_id, quantity, avg_entry_minor_units,
                      realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
               FROM trading_book_position
               WHERE binding_id = ? AND environment = 'agent_sandbox'
               ORDER BY instrument_id ASC""",
            (binding.binding_id,),
        ).fetchall()
    ]
    orders = [
        {
            "order_id": str(r[0]),
            "action_id": str(r[1]),
            "instrument_id": str(r[2]),
            "side": str(r[3]),
            "quantity": int(r[4]),
            "limit_minor_units": int(r[5]),
            "status": str(r[6]),
            "filled_quantity": int(r[7]),
            "fill_price_minor_units": None if r[8] is None else int(r[8]),
            "fee_minor_units": int(r[9]),
            "created_at_ns": int(r[10]),
        }
        for r in db.execute(
            """SELECT order_id, action_id, instrument_id, side, quantity,
                      limit_minor_units, status, filled_quantity,
                      fill_price_minor_units, fee_minor_units, created_at_ns
               FROM trading_book_order
               WHERE binding_id = ? AND environment = 'agent_sandbox'
               ORDER BY created_at_ns DESC LIMIT 50""",
            (binding.binding_id,),
        ).fetchall()
    ]
    fill_count = int(
        db.execute(
            "SELECT COUNT(*) FROM trading_book_fill WHERE binding_id = ? AND environment = 'agent_sandbox'",
            (binding.binding_id,),
        ).fetchone()[0]
    )
    return {
        "binding": asdict(binding),
        "cash_minor_units": metrics["cash_minor_units"],
        "gross_exposure_minor_units": metrics["gross_exposure_minor_units"],
        "exposure_limit_minor_units": binding.exposure_limit_minor_units,
        "realized_pnl_minor_units": metrics["realized_pnl_minor_units"],
        "total_fees_minor_units": metrics["total_fees_minor_units"],
        "fill_count": fill_count,
        "positions": positions,
        "orders": orders,
    }


def read_agent_sandbox_portfolio(
    store: Path, company_id: str, agent_id: str
) -> dict[str, Any] | None:
    """Read an Agent's active Tier 1 sandbox portfolio via open_reader."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        return read_agent_sandbox_portfolio_from_db(db, company_id, agent_id)


# ---------------------------------------------------------------------------
# Tier 2: Company Paper Book Orders & Portfolio (Unified trading_book_*)
# ---------------------------------------------------------------------------


def submit_company_paper_book_order(
    writer: WriterLease,
    *,
    binding_id: str,
    actor_id: str,
    action_id: str,
    instrument_id: str,
    side: str,
    quantity: int,
    limit_minor_units: int,
    market_price_minor_units: int,
    quote_observed_at_ns: int | None = None,
    fill_mode: str = "immediate",
    valuation_label: str | None = None,
    now_ns: int | None = None,
    run_id: str | None = None,
    decision_id: str | None = None,
    mandate_revision: str | None = None,
) -> dict[str, Any]:
    """Submit a realistic simulated order strictly inside one Company Paper Book."""
    writer.verify()
    _require_safe_id(binding_id, "binding_id")
    _require_safe_id(actor_id, "actor_id")
    _require_safe_id(action_id, "action_id")
    _require_safe_id(instrument_id, "instrument_id")
    if side not in ("buy", "sell"):
        raise ExecutionTierRefused("side must be 'buy' or 'sell'")
    _require_positive_int(quantity, "quantity")
    _require_positive_int(limit_minor_units, "limit_minor_units")
    _require_positive_int(market_price_minor_units, "market_price_minor_units")
    if fill_mode not in ("immediate", "resting"):
        raise ExecutionTierRefused("fill_mode must be 'immediate' or 'resting'")
    observed_ns = time_ns() if quote_observed_at_ns is None else quote_observed_at_ns
    _require_positive_int(observed_ns, "quote_observed_at_ns")

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "trading"):
            raise ExecutionTierRefused("Trading domain is not installed")
        row = db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ? AND binding_id = ? AND tier = ? AND status = 'active'""",
            (writer.company_id, binding_id, TIER_COMPANY_PAPER),
        ).fetchone()
        if row is None:
            raise ExecutionTierRefused(
                f"Active company_paper book binding '{binding_id}' not found"
            )
        binding = _row_to_binding(row)
        require_trading_risk_clear(db, writer.company_id, ENV_PAPER)

        existing = db.execute(
            """SELECT order_id, instrument_id, side, quantity, limit_minor_units,
                      market_price_minor_units, status, filled_quantity,
                      fill_price_minor_units, fee_minor_units
               FROM trading_book_order
               WHERE binding_id = ? AND environment = 'paper' AND action_id = ?""",
            (binding.binding_id, action_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing[1]) != instrument_id
                or str(existing[2]) != side
                or int(existing[3]) != quantity
                or int(existing[4]) != limit_minor_units
            ):
                raise ExecutionTierRefused(
                    "action_id already used for a different company paper order"
                )
            fill_row = db.execute(
                """SELECT fill_id, realized_pnl_minor_units
                   FROM trading_book_fill WHERE order_id = ?""",
                (str(existing[0]),),
            ).fetchone()
            return {
                "order_id": str(existing[0]),
                "binding_id": binding.binding_id,
                "action_id": action_id,
                "instrument_id": instrument_id,
                "side": side,
                "quantity": quantity,
                "status": str(existing[6]),
                "filled_quantity": int(existing[7]),
                "fill_price_minor_units": None
                if existing[8] is None
                else int(existing[8]),
                "fee_minor_units": int(existing[9]),
                "fill_id": None if fill_row is None else str(fill_row[0]),
                "realized_pnl_minor_units": 0 if fill_row is None else int(fill_row[1]),
                "replayed": True,
            }

        if side == "buy":
            slipped_price = (
                market_price_minor_units
                + (market_price_minor_units * binding.slippage_bps + 9_999) // 10_000
            )
            crosses_limit = slipped_price <= limit_minor_units
        else:
            slipped_price = max(
                1,
                market_price_minor_units
                - (market_price_minor_units * binding.slippage_bps) // 10_000,
            )
            crosses_limit = slipped_price >= limit_minor_units

        metrics_before = _compute_unified_book_metrics_in_tx(db, binding)
        cash_available = metrics_before["cash_minor_units"]
        gross_exposure = metrics_before["gross_exposure_minor_units"]

        pos_row = db.execute(
            """SELECT quantity, avg_entry_minor_units, mark_price_minor_units,
                      realized_pnl_minor_units, total_fees_minor_units
               FROM trading_book_position
               WHERE binding_id = ? AND environment = 'paper' AND instrument_id = ?""",
            (binding.binding_id, instrument_id),
        ).fetchone()
        cur_qty = int(pos_row[0]) if pos_row else 0
        cur_avg = int(pos_row[1]) if pos_row else 0
        cur_mark = int(pos_row[2]) if pos_row and int(pos_row[2]) > 0 else cur_avg
        cur_realized = int(pos_row[3]) if pos_row else 0
        cur_fees = int(pos_row[4]) if pos_row else 0

        now = time_ns() if now_ns is None else now_ns
        order_id = f"cpbo-{uuid4().hex[:16]}"
        should_fill = fill_mode == "immediate" and crosses_limit

        if should_fill:
            notional = quantity * slipped_price
            fee = (notional * binding.fee_bps + 5_000) // 10_000
            delta = quantity if side == "buy" else -quantity
            new_qty = cur_qty + delta
            other_exposure = gross_exposure - abs(cur_qty) * cur_mark

            realized_delta = 0
            if (
                cur_qty == 0
                or (cur_qty > 0 and delta > 0)
                or (cur_qty < 0 and delta < 0)
            ):
                total_cost = abs(cur_qty) * cur_avg + quantity * slipped_price
                new_avg = total_cost // abs(new_qty)
            else:
                closing_qty = min(abs(cur_qty), quantity)
                if cur_qty > 0:
                    realized_delta = (slipped_price - cur_avg) * closing_qty
                else:
                    realized_delta = (cur_avg - slipped_price) * closing_qty
                if new_qty == 0:
                    new_avg = 0
                elif (cur_qty > 0 and new_qty > 0) or (cur_qty < 0 and new_qty < 0):
                    new_avg = cur_avg
                else:
                    new_avg = slipped_price

            projected_exposure = other_exposure + abs(new_qty) * slipped_price
            if projected_exposure > binding.exposure_limit_minor_units:
                raise ExecutionTierRefused(
                    "Company paper book order exceeds exposure limit"
                )
            if side == "buy" and notional + fee > cash_available:
                raise ExecutionTierRefused(
                    "Company paper book order exceeds available virtual cash"
                )
            realized_net = realized_delta - fee
            fill_id = f"cpbf-{uuid4().hex[:16]}"
            db.execute(
                """INSERT INTO trading_book_order (
                       order_id, binding_id, environment, company_id, actor_id,
                       action_id, external_order_id, run_id, decision_id,
                       mandate_revision, instrument_id, side, quantity,
                       limit_minor_units, market_price_minor_units,
                       quote_observed_at_ns, quote_source, fee_bps, slippage_bps,
                       status, filled_quantity, fill_price_minor_units,
                       fee_minor_units, created_at_ns, updated_at_ns
                   ) VALUES (?, ?, 'paper', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    binding.binding_id,
                    writer.company_id,
                    actor_id,
                    action_id,
                    run_id,
                    decision_id,
                    mandate_revision or binding.binding_revision,
                    instrument_id,
                    side,
                    quantity,
                    limit_minor_units,
                    market_price_minor_units,
                    observed_ns,
                    binding.quote_source,
                    binding.fee_bps,
                    binding.slippage_bps,
                    quantity,
                    slipped_price,
                    fee,
                    now,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO trading_book_fill (
                       fill_id, order_id, binding_id, environment, company_id,
                       actor_id, external_fill_id, instrument_id, side, quantity,
                       price_minor_units, fee_minor_units, realized_pnl_minor_units, filled_at_ns
                   ) VALUES (?, ?, ?, 'paper', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fill_id,
                    order_id,
                    binding.binding_id,
                    writer.company_id,
                    actor_id,
                    instrument_id,
                    side,
                    quantity,
                    slipped_price,
                    fee,
                    realized_net,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO trading_book_position (
                       binding_id, environment, company_id, instrument_id,
                       quantity, avg_entry_minor_units, mark_price_minor_units,
                       realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
                   ) VALUES (?, 'paper', ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (binding_id, instrument_id) DO UPDATE SET
                       quantity = excluded.quantity,
                       avg_entry_minor_units = excluded.avg_entry_minor_units,
                       mark_price_minor_units = excluded.mark_price_minor_units,
                       realized_pnl_minor_units = excluded.realized_pnl_minor_units,
                       total_fees_minor_units = excluded.total_fees_minor_units,
                       updated_at_ns = excluded.updated_at_ns""",
                (
                    binding.binding_id,
                    writer.company_id,
                    instrument_id,
                    new_qty,
                    new_avg,
                    slipped_price,
                    cur_realized + realized_net,
                    cur_fees + fee,
                    now,
                ),
            )
            _insert_book_valuation_in_tx(
                db,
                binding,
                label=valuation_label or f"{side.upper()} {instrument_id}",
                mark_prices={instrument_id: slipped_price},
                source="paper_fill",
                recorded_at_ns=now,
            )
            append_audit_event(
                db,
                company_id=writer.company_id,
                domain="trading",
                entity_type="trading_book_fill",
                entity_id=fill_id,
                event_type="company_paper.order_filled",
                actor_id=actor_id,
                payload={
                    "order_id": order_id,
                    "fill_id": fill_id,
                    "binding_id": binding.binding_id,
                    "environment": ENV_PAPER,
                    "instrument_id": instrument_id,
                    "side": side,
                    "quantity": quantity,
                    "price_minor_units": slipped_price,
                    "fee_minor_units": fee,
                },
                recorded_at_ns=now,
            )
            writer.verify()
            return {
                "order_id": order_id,
                "binding_id": binding.binding_id,
                "action_id": action_id,
                "instrument_id": instrument_id,
                "side": side,
                "quantity": quantity,
                "status": "filled",
                "filled_quantity": quantity,
                "fill_price_minor_units": slipped_price,
                "fee_minor_units": fee,
                "fill_id": fill_id,
                "realized_pnl_minor_units": realized_net,
                "replayed": False,
            }

        if side == "buy" and quantity * limit_minor_units > cash_available:
            raise ExecutionTierRefused(
                "Company paper book limit order exceeds available virtual cash"
            )
        db.execute(
            """INSERT INTO trading_book_order (
                   order_id, binding_id, environment, company_id, actor_id,
                   action_id, external_order_id, run_id, decision_id,
                   mandate_revision, instrument_id, side, quantity,
                   limit_minor_units, market_price_minor_units,
                   quote_observed_at_ns, quote_source, fee_bps, slippage_bps,
                   status, filled_quantity, fill_price_minor_units,
                   fee_minor_units, created_at_ns, updated_at_ns
               ) VALUES (?, ?, 'paper', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, NULL, 0, ?, ?)""",
            (
                order_id,
                binding.binding_id,
                writer.company_id,
                actor_id,
                action_id,
                run_id,
                decision_id,
                mandate_revision or binding.binding_revision,
                instrument_id,
                side,
                quantity,
                limit_minor_units,
                market_price_minor_units,
                observed_ns,
                binding.quote_source,
                binding.fee_bps,
                binding.slippage_bps,
                now,
                now,
            ),
        )
        writer.verify()
        return {
            "order_id": order_id,
            "binding_id": binding.binding_id,
            "action_id": action_id,
            "instrument_id": instrument_id,
            "side": side,
            "quantity": quantity,
            "status": "open",
            "filled_quantity": 0,
            "fill_price_minor_units": None,
            "fee_minor_units": 0,
            "fill_id": None,
            "realized_pnl_minor_units": 0,
            "replayed": False,
        }


def cancel_company_paper_book_order(
    writer: WriterLease,
    *,
    binding_id: str,
    actor_id: str,
    order_id: str,
    reason: str = "agent_cancelled",
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Idempotently cancel an open resting order strictly within an active company_paper book."""
    writer.verify()
    _require_safe_id(binding_id, "binding_id")
    _require_safe_id(actor_id, "actor_id")
    _require_safe_id(order_id, "order_id")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
        raise ExecutionTierRefused("cancel reason must be a non-empty string")
    clean_reason = reason.strip()
    now = time_ns() if now_ns is None else now_ns

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "trading"):
            raise ExecutionTierRefused("Trading domain is not installed")
        _assert_active_agent(db, writer.company_id, actor_id)
        row = db.execute(
            """SELECT binding_id, status
               FROM trading_book_order
               WHERE company_id = ? AND binding_id = ? AND environment = 'paper' AND order_id = ?""",
            (writer.company_id, binding_id, order_id),
        ).fetchone()
        if row is None:
            raise ExecutionTierRefused(
                f"Company paper order '{order_id}' not found on book '{binding_id}'"
            )
        status = str(row[1])
        if status == "cancelled":
            return {
                "order_id": order_id,
                "binding_id": binding_id,
                "status": "cancelled",
                "replayed": True,
            }
        if status != "open":
            raise ExecutionTierRefused(
                f"Cannot cancel company paper order in terminal state '{status}'"
            )
        db.execute(
            """UPDATE trading_book_order
               SET status = 'cancelled', cancelled_at_ns = ?,
                   cancel_reason = ?, updated_at_ns = ?
               WHERE company_id = ? AND binding_id = ? AND environment = 'paper' AND order_id = ?""",
            (now, clean_reason, now, writer.company_id, binding_id, order_id),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="trading_book_order",
            entity_id=order_id,
            event_type="company_paper.order_cancelled",
            actor_id=actor_id,
            payload={
                "order_id": order_id,
                "binding_id": binding_id,
                "environment": ENV_PAPER,
                "reason": clean_reason,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return {
            "order_id": order_id,
            "binding_id": binding_id,
            "status": "cancelled",
            "replayed": False,
        }


def _binding_has_paper_soak_evidence(
    db: Connection, company_id: str, binding_id: str, *, fill_count: int
) -> bool:
    """Return True iff the given paper binding has per-binding fills or non-init valuations."""
    if fill_count > 0:
        return True
    return (
        db.execute(
            """SELECT 1 FROM trading_book_valuation
               WHERE company_id = ? AND binding_id = ? AND environment = 'paper'
                 AND source NOT IN ('book_open', 'book_init')
               LIMIT 1""",
            (company_id, binding_id),
        ).fetchone()
        is not None
    )


def read_company_paper_book_portfolio_from_db(
    db: Connection, company_id: str, binding_id: str
) -> dict[str, Any] | None:
    """Read one Company Paper Book's independent virtual cash, exposure, fills, and soak state."""
    if not is_domain_installed(db, "trading"):
        return None
    row = db.execute(
        f"""{_SELECT_BINDING_COLS}
            WHERE company_id = ? AND binding_id = ? AND tier = ?""",
        (company_id, binding_id, TIER_COMPANY_PAPER),
    ).fetchone()
    if row is None:
        return None
    binding = _row_to_binding(row)
    book_name = binding.book_name or binding.account_id
    strategy_release_id = binding.strategy_release_id
    role = (
        binding.book_role
        if binding.book_role is not None
        else ("retired" if binding.status == "retired" else "active")
    )
    shadow_of_live_binding_id = binding.shadow_of_live_binding_id
    nominated_question_id = binding.nominated_question_id
    if role == "nominated" and nominated_question_id is not None:
        q_row = db.execute(
            "SELECT state FROM decision_question WHERE company_id = ? AND question_id = ?",
            (company_id, nominated_question_id),
        ).fetchone()
        if q_row is not None and str(q_row[0]) != "open":
            role = "active"
            nominated_question_id = None

    metrics = _compute_unified_book_metrics_in_tx(db, binding)
    positions = [
        {
            "instrument_id": str(r[0]),
            "quantity": int(r[1]),
            "avg_entry_minor_units": int(r[2]),
            "realized_pnl_minor_units": int(r[3]),
            "total_fees_minor_units": int(r[4]),
            "updated_at_ns": int(r[5]),
        }
        for r in db.execute(
            """SELECT instrument_id, quantity, avg_entry_minor_units,
                      realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
               FROM trading_book_position
               WHERE binding_id = ? AND environment = 'paper'
               ORDER BY instrument_id ASC""",
            (binding.binding_id,),
        ).fetchall()
    ]
    orders = [
        {
            "order_id": str(r[0]),
            "action_id": str(r[1]),
            "instrument_id": str(r[2]),
            "side": str(r[3]),
            "quantity": int(r[4]),
            "limit_minor_units": int(r[5]),
            "status": str(r[6]),
            "filled_quantity": int(r[7]),
            "fill_price_minor_units": None if r[8] is None else int(r[8]),
            "fee_minor_units": int(r[9]),
            "created_at_ns": int(r[10]),
        }
        for r in db.execute(
            """SELECT order_id, action_id, instrument_id, side, quantity,
                      limit_minor_units, status, filled_quantity,
                      fill_price_minor_units, fee_minor_units, created_at_ns
               FROM trading_book_order
               WHERE binding_id = ? AND environment = 'paper'
               ORDER BY created_at_ns DESC LIMIT 30""",
            (binding.binding_id,),
        ).fetchall()
    ]
    fill_count = int(
        db.execute(
            "SELECT COUNT(*) FROM trading_book_fill WHERE binding_id = ? AND environment = 'paper'",
            (binding.binding_id,),
        ).fetchone()[0]
    )
    open_orders_count = int(
        db.execute(
            "SELECT COUNT(*) FROM trading_book_order WHERE binding_id = ? AND environment = 'paper' AND status = 'open'",
            (binding.binding_id,),
        ).fetchone()[0]
    )
    open_positions_count = sum(1 for p in positions if p["quantity"] != 0)

    soak_ready = _binding_has_paper_soak_evidence(
        db, company_id, binding.binding_id, fill_count=fill_count
    )

    return {
        "binding_id": binding.binding_id,
        "account_id": binding.account_id,
        "book_name": book_name,
        "strategy_release_id": strategy_release_id,
        "role": role,
        "status": binding.status,
        "shadow_of_live_binding_id": shadow_of_live_binding_id,
        "nominated_question_id": nominated_question_id,
        "currency": binding.currency,
        "starting_minor_units": binding.starting_minor_units,
        "exposure_limit_minor_units": binding.exposure_limit_minor_units,
        "fee_bps": binding.fee_bps,
        "slippage_bps": binding.slippage_bps,
        "quote_source": binding.quote_source,
        "cash_minor_units": metrics["cash_minor_units"],
        "gross_exposure_minor_units": metrics["gross_exposure_minor_units"],
        "realized_pnl_minor_units": metrics["realized_pnl_minor_units"],
        "total_fees_minor_units": metrics["total_fees_minor_units"],
        "fill_count": fill_count,
        "open_positions_count": open_positions_count,
        "open_orders_count": open_orders_count,
        "soak_ready": soak_ready,
        "positions": positions,
        "orders": orders,
    }


# ---------------------------------------------------------------------------
# Tier 3: Live TradingBook Fills & Unified Book State Reader
# ---------------------------------------------------------------------------


def record_live_book_order_fill(
    writer: WriterLease,
    *,
    binding_id: str,
    actor_id: str,
    action_id: str,
    external_order_id: str,
    external_fill_id: str,
    instrument_id: str,
    side: str,
    quantity: int,
    fill_price_minor_units: int,
    fee_minor_units: int,
    limit_minor_units: int | None = None,
    valuation_label: str | None = None,
    now_ns: int | None = None,
    run_id: str | None = None,
    decision_id: str | None = None,
    mandate_revision: str | None = None,
) -> dict[str, Any]:
    """Record a reconciled external exchange order & fill into an active company_live book."""
    writer.verify()
    _require_safe_id(binding_id, "binding_id")
    _require_safe_id(actor_id, "actor_id")
    _require_safe_id(action_id, "action_id")
    _require_safe_id(external_order_id, "external_order_id")
    _require_safe_id(external_fill_id, "external_fill_id")
    _require_safe_id(instrument_id, "instrument_id")
    if side not in ("buy", "sell"):
        raise ExecutionTierRefused("side must be 'buy' or 'sell'")
    _require_positive_int(quantity, "quantity")
    _require_positive_int(fill_price_minor_units, "fill_price_minor_units")
    if type(fee_minor_units) is not int or fee_minor_units < 0:
        raise ExecutionTierRefused("fee_minor_units must be a non-negative integer")
    eff_limit = (
        fill_price_minor_units if limit_minor_units is None else limit_minor_units
    )
    _require_positive_int(eff_limit, "limit_minor_units")
    now = time_ns() if now_ns is None else now_ns

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "trading"):
            raise ExecutionTierRefused("Trading domain is not installed")

        require_trading_risk_clear(db, writer.company_id, ENV_LIVE)

        row = db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ? AND binding_id = ?
                  AND tier = ? AND environment = ? AND status = 'active'""",
            (writer.company_id, binding_id, TIER_COMPANY_LIVE, ENV_LIVE),
        ).fetchone()
        if row is None:
            raise ExecutionTierRefused(
                f"Active company_live binding '{binding_id}' not found"
            )
        binding = _row_to_binding(row)

        existing = db.execute(
            """SELECT order_id, instrument_id, side, quantity, fill_price_minor_units,
                      fee_minor_units, status
               FROM trading_book_order
               WHERE binding_id = ? AND environment = 'live' AND action_id = ?""",
            (binding.binding_id, action_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing[1]) != instrument_id
                or str(existing[2]) != side
                or int(existing[3]) != quantity
            ):
                raise ExecutionTierRefused(
                    "action_id already used for a different live book order"
                )
            fill_row = db.execute(
                """SELECT fill_id, realized_pnl_minor_units
                   FROM trading_book_fill WHERE order_id = ?""",
                (str(existing[0]),),
            ).fetchone()
            return {
                "order_id": str(existing[0]),
                "binding_id": binding.binding_id,
                "environment": ENV_LIVE,
                "action_id": action_id,
                "external_order_id": external_order_id,
                "external_fill_id": external_fill_id,
                "instrument_id": instrument_id,
                "side": side,
                "quantity": quantity,
                "status": str(existing[6]),
                "fill_price_minor_units": None
                if existing[4] is None
                else int(existing[4]),
                "fee_minor_units": int(existing[5]),
                "fill_id": None if fill_row is None else str(fill_row[0]),
                "realized_pnl_minor_units": 0 if fill_row is None else int(fill_row[1]),
                "replayed": True,
            }

        metrics_before = _compute_unified_book_metrics_in_tx(db, binding)
        cash_available = metrics_before["cash_minor_units"]
        gross_exposure = metrics_before["gross_exposure_minor_units"]

        pos_row = db.execute(
            """SELECT quantity, avg_entry_minor_units, mark_price_minor_units,
                      realized_pnl_minor_units, total_fees_minor_units
               FROM trading_book_position
               WHERE binding_id = ? AND environment = 'live' AND instrument_id = ?""",
            (binding.binding_id, instrument_id),
        ).fetchone()
        cur_qty = int(pos_row[0]) if pos_row else 0
        cur_avg = int(pos_row[1]) if pos_row else 0
        cur_mark = int(pos_row[2]) if pos_row and int(pos_row[2]) > 0 else cur_avg
        cur_realized = int(pos_row[3]) if pos_row else 0
        cur_fees = int(pos_row[4]) if pos_row else 0

        notional = quantity * fill_price_minor_units
        delta = quantity if side == "buy" else -quantity
        new_qty = cur_qty + delta
        other_exposure = gross_exposure - abs(cur_qty) * cur_mark

        realized_delta = 0
        if cur_qty == 0 or (cur_qty > 0 and delta > 0) or (cur_qty < 0 and delta < 0):
            total_cost = abs(cur_qty) * cur_avg + quantity * fill_price_minor_units
            new_avg = total_cost // abs(new_qty)
        else:
            closing_qty = min(abs(cur_qty), quantity)
            if cur_qty > 0:
                realized_delta = (fill_price_minor_units - cur_avg) * closing_qty
            else:
                realized_delta = (cur_avg - fill_price_minor_units) * closing_qty
            if new_qty == 0:
                new_avg = 0
            elif (cur_qty > 0 and new_qty > 0) or (cur_qty < 0 and new_qty < 0):
                new_avg = cur_avg
            else:
                new_avg = fill_price_minor_units

        projected_exposure = other_exposure + abs(new_qty) * fill_price_minor_units
        if projected_exposure > binding.exposure_limit_minor_units:
            raise ExecutionTierRefused(
                "Live book order exceeds live binding exposure limit"
            )
        if side == "buy" and notional + fee_minor_units > cash_available:
            raise ExecutionTierRefused("Live book order exceeds available live cash")

        realized_net = realized_delta - fee_minor_units
        order_id = f"livo-{uuid4().hex[:16]}"
        fill_id = f"livf-{uuid4().hex[:16]}"
        db.execute(
            """INSERT INTO trading_book_order (
                   order_id, binding_id, environment, company_id, actor_id,
                   action_id, external_order_id, run_id, decision_id,
                   mandate_revision, instrument_id, side, quantity,
                   limit_minor_units, market_price_minor_units,
                   quote_observed_at_ns, quote_source, fee_bps, slippage_bps,
                   status, filled_quantity, fill_price_minor_units,
                   fee_minor_units, created_at_ns, updated_at_ns
               ) VALUES (?, ?, 'live', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?)""",
            (
                order_id,
                binding.binding_id,
                writer.company_id,
                actor_id,
                action_id,
                external_order_id,
                run_id,
                decision_id,
                mandate_revision or binding.binding_revision,
                instrument_id,
                side,
                quantity,
                eff_limit,
                fill_price_minor_units,
                now,
                binding.quote_source,
                binding.fee_bps,
                binding.slippage_bps,
                quantity,
                fill_price_minor_units,
                fee_minor_units,
                now,
                now,
            ),
        )
        db.execute(
            """INSERT INTO trading_book_fill (
                   fill_id, order_id, binding_id, environment, company_id,
                   actor_id, external_fill_id, instrument_id, side, quantity,
                   price_minor_units, fee_minor_units, realized_pnl_minor_units, filled_at_ns
               ) VALUES (?, ?, ?, 'live', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fill_id,
                order_id,
                binding.binding_id,
                writer.company_id,
                actor_id,
                external_fill_id,
                instrument_id,
                side,
                quantity,
                fill_price_minor_units,
                fee_minor_units,
                realized_net,
                now,
            ),
        )
        db.execute(
            """INSERT INTO trading_book_position (
                   binding_id, environment, company_id, instrument_id,
                   quantity, avg_entry_minor_units, mark_price_minor_units,
                   realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
               ) VALUES (?, 'live', ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (binding_id, instrument_id) DO UPDATE SET
                   quantity = excluded.quantity,
                   avg_entry_minor_units = excluded.avg_entry_minor_units,
                   mark_price_minor_units = excluded.mark_price_minor_units,
                   realized_pnl_minor_units = excluded.realized_pnl_minor_units,
                   total_fees_minor_units = excluded.total_fees_minor_units,
                   updated_at_ns = excluded.updated_at_ns""",
            (
                binding.binding_id,
                writer.company_id,
                instrument_id,
                new_qty,
                new_avg,
                fill_price_minor_units,
                cur_realized + realized_net,
                cur_fees + fee_minor_units,
                now,
            ),
        )
        _insert_book_valuation_in_tx(
            db,
            binding,
            label=valuation_label or f"Live {side.upper()} {instrument_id}",
            mark_prices={instrument_id: fill_price_minor_units},
            source="live_fill",
            recorded_at_ns=now,
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="trading_book_fill",
            entity_id=fill_id,
            event_type="company_live.order_filled",
            actor_id=actor_id,
            payload={
                "order_id": order_id,
                "fill_id": fill_id,
                "external_order_id": external_order_id,
                "external_fill_id": external_fill_id,
                "binding_id": binding.binding_id,
                "environment": ENV_LIVE,
                "instrument_id": instrument_id,
                "side": side,
                "quantity": quantity,
                "price_minor_units": fill_price_minor_units,
                "fee_minor_units": fee_minor_units,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return {
            "order_id": order_id,
            "binding_id": binding.binding_id,
            "environment": ENV_LIVE,
            "action_id": action_id,
            "external_order_id": external_order_id,
            "external_fill_id": external_fill_id,
            "instrument_id": instrument_id,
            "side": side,
            "quantity": quantity,
            "status": "filled",
            "fill_price_minor_units": fill_price_minor_units,
            "fee_minor_units": fee_minor_units,
            "fill_id": fill_id,
            "realized_pnl_minor_units": realized_net,
            "replayed": False,
        }


def read_trading_book_state_from_db(
    db: Connection, company_id: str, binding_id: str
) -> dict[str, Any] | None:
    """Read unified portfolio KPIs, positions, execution tape, and valuation series for one book."""
    if not is_domain_installed(db, "trading"):
        return None
    row = db.execute(
        f"""{_SELECT_BINDING_COLS}
            WHERE company_id = ? AND binding_id = ? AND tier IN (?, ?)""",
        (company_id, binding_id, TIER_COMPANY_PAPER, TIER_COMPANY_LIVE),
    ).fetchone()
    if row is None:
        return None
    binding = _row_to_binding(row)

    book_name = binding.book_name or binding.account_id
    strategy_release_id = binding.strategy_release_id
    role = binding.book_role or binding.status
    shadow_of_live_binding_id = binding.shadow_of_live_binding_id
    source_paper_binding_id: str | None = None
    nominated_question_id = binding.nominated_question_id

    if binding.environment == ENV_PAPER:
        if role == "nominated" and nominated_question_id is not None:
            q_row = db.execute(
                "SELECT state FROM decision_question WHERE company_id = ? AND question_id = ?",
                (company_id, nominated_question_id),
            ).fetchone()
            if q_row is not None and str(q_row[0]) != "open":
                role = "active"
                nominated_question_id = None
    else:
        mig_row = db.execute(
            """SELECT source_paper_binding_id FROM trading_live_migration_record
               WHERE company_id = ? AND target_live_binding_id = ?
               ORDER BY recorded_at_ns DESC LIMIT 1""",
            (company_id, binding.binding_id),
        ).fetchone()
        if mig_row is not None:
            source_paper_binding_id = str(mig_row[0])
            src_meta = db.execute(
                """SELECT book_name, strategy_release_id
                   FROM execution_environment_binding
                   WHERE binding_id = ?""",
                (source_paper_binding_id,),
            ).fetchone()
            if src_meta is not None:
                src_name = (
                    str(src_meta[0])
                    if src_meta[0] is not None
                    else source_paper_binding_id
                )
                book_name = f"{src_name} ({binding.account_id})"
                strategy_release_id = None if src_meta[1] is None else str(src_meta[1])

    metrics = _compute_unified_book_metrics_in_tx(db, binding)
    nav = metrics["nav_minor_units"]
    pnl_minor = nav - binding.starting_minor_units
    return_pct = (
        round((pnl_minor / binding.starting_minor_units) * 100.0, 4)
        if binding.starting_minor_units > 0
        else 0.0
    )

    positions = []
    for r in db.execute(
        """SELECT instrument_id, quantity, avg_entry_minor_units,
                  mark_price_minor_units, realized_pnl_minor_units,
                  total_fees_minor_units, updated_at_ns
           FROM trading_book_position
           WHERE binding_id = ? AND environment = ?
           ORDER BY instrument_id ASC""",
        (binding.binding_id, binding.environment),
    ).fetchall():
        q = int(r[1])
        avg = int(r[2])
        mark = int(r[3]) if int(r[3]) > 0 else avg
        unrl = (mark - avg) * q if q > 0 else (avg - mark) * abs(q) if q < 0 else 0
        positions.append(
            {
                "instrument_id": str(r[0]),
                "quantity": q,
                "avg_entry_minor_units": avg,
                "mark_price_minor_units": mark,
                "notional_minor_units": abs(q) * mark,
                "unrealized_pnl_minor_units": unrl,
                "realized_pnl_minor_units": int(r[4]),
                "total_fees_minor_units": int(r[5]),
                "updated_at_ns": int(r[6]),
            }
        )

    orders = [
        {
            "order_id": str(r[0]),
            "action_id": str(r[1]),
            "actor_id": str(r[2]),
            "external_order_id": None if r[3] is None else str(r[3]),
            "instrument_id": str(r[4]),
            "side": str(r[5]),
            "quantity": int(r[6]),
            "limit_minor_units": int(r[7]),
            "status": str(r[8]),
            "filled_quantity": int(r[9]),
            "fill_price_minor_units": None if r[10] is None else int(r[10]),
            "fee_minor_units": int(r[11]),
            "created_at_ns": int(r[12]),
        }
        for r in db.execute(
            """SELECT order_id, action_id, actor_id, external_order_id,
                      instrument_id, side, quantity, limit_minor_units,
                      status, filled_quantity, fill_price_minor_units,
                      fee_minor_units, created_at_ns
               FROM trading_book_order
               WHERE binding_id = ? AND environment = ?
               ORDER BY created_at_ns DESC LIMIT 50""",
            (binding.binding_id, binding.environment),
        ).fetchall()
    ]

    fill_count = int(
        db.execute(
            "SELECT COUNT(*) FROM trading_book_fill WHERE binding_id = ? AND environment = ?",
            (binding.binding_id, binding.environment),
        ).fetchone()[0]
    )
    open_orders_count = int(
        db.execute(
            "SELECT COUNT(*) FROM trading_book_order WHERE binding_id = ? AND environment = ? AND status = 'open'",
            (binding.binding_id, binding.environment),
        ).fetchone()[0]
    )

    val_rows = db.execute(
        """SELECT valuation_id, label, nav_minor_units, cash_minor_units,
                  gross_exposure_minor_units, realized_pnl_minor_units,
                  unrealized_pnl_minor_units, total_fees_minor_units,
                  source, recorded_at_ns
           FROM trading_book_valuation
           WHERE binding_id = ? AND environment = ?
           ORDER BY recorded_at_ns ASC, valuation_id ASC""",
        (binding.binding_id, binding.environment),
    ).fetchall()
    series = []
    for vr in val_rows:
        v_nav = int(vr[2])
        v_pnl = v_nav - binding.starting_minor_units
        v_ret = (
            round((v_pnl / binding.starting_minor_units) * 100.0, 4)
            if binding.starting_minor_units > 0
            else 0.0
        )
        series.append(
            {
                "id": str(vr[0]),
                "label": str(vr[1]),
                "nav_minor": v_nav,
                "return_pct": v_ret,
                "pnl_minor": v_pnl,
                "source": str(vr[8]),
                "observed_at_ns": int(vr[9]),
            }
        )

    return {
        "binding_id": binding.binding_id,
        "account_id": binding.account_id,
        "book_name": book_name,
        "environment": binding.environment,
        "tier": binding.tier,
        "destination_id": binding.destination_id,
        "connector_revision": binding.connector_revision,
        "strategy_release_id": strategy_release_id,
        "role": role,
        "status": binding.status,
        "source_paper_binding_id": source_paper_binding_id,
        "shadow_of_live_binding_id": shadow_of_live_binding_id,
        "nominated_question_id": nominated_question_id,
        "currency": binding.currency,
        "starting_minor_units": binding.starting_minor_units,
        "exposure_limit_minor_units": binding.exposure_limit_minor_units,
        "fee_bps": binding.fee_bps,
        "slippage_bps": binding.slippage_bps,
        "quote_source": binding.quote_source,
        "nav_minor_units": nav,
        "pnl_minor_units": pnl_minor,
        "return_pct": return_pct,
        "cash_minor_units": metrics["cash_minor_units"],
        "gross_exposure_minor_units": metrics["gross_exposure_minor_units"],
        "realized_pnl_minor_units": metrics["realized_pnl_minor_units"],
        "unrealized_pnl_minor_units": metrics["unrealized_pnl_minor_units"],
        "total_fees_minor_units": metrics["total_fees_minor_units"],
        "fill_count": fill_count,
        "open_positions_count": sum(1 for p in positions if p["quantity"] != 0),
        "open_orders_count": open_orders_count,
        "soak_ready": (
            _binding_has_paper_soak_evidence(
                db, company_id, binding.binding_id, fill_count=fill_count
            )
            if binding.environment == ENV_PAPER
            else fill_count > 0
        ),
        "positions": positions,
        "orders": orders,
        "cumulative_return_series": series,
    }
