"""Crash-safe two-phase live order execution, external reconciliation, and mark-to-market."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import closing
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from time import time_ns
from typing import Any

from ..board_control import record_system_restriction
from ..company import _identity
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..storage.kernel import (
    ExternalActionConflict,
    prepare_external_action,
    settle_external_action,
)
from ..storage.schema import is_domain_installed
from ..writer_lease import WriterLease
from .domain import (
    ENV_EXTERNAL_TEST,
    ENV_LIVE,
    ExecutionTierRefused,
    TIER_COMPANY_LIVE,
    TIER_COMPANY_PAPER,
    _require_positive_int,
    _require_safe_id,
)
from .external_venue import (
    ExternalFillRecord,
    ExternalOrderReplyUnknown,
    ExternalOrderRequest,
    ExternalVenueAdapter,
    ExternalVenueRefused,
)
from .ledger import (
    read_trading_book_state_from_db,
    record_live_book_order_fill,
    record_trading_book_valuation,
)
from .quotes import (
    MarketQuoteProvider,
    QuoteRefused,
    decimal_price_to_minor_units,
    minor_units_to_decimal,
    normalize_instrument_id,
    to_venue_symbol,
)
from .risk import (
    QUOTE_FRESHNESS_MAX_NS,
    SCOPE_EXECUTION_LIVE,
    TRADING_RISK_CAUSE_CATEGORY,
    require_trading_risk_clear,
)

_STABLE_QUOTE_ASSETS = frozenset({"USD", "USDT", "USDC", "BUSD", "FDUSD"})
_LIVE_ORDER_OPERATION = "submit_order"
_SAFE_SUFFIX_RE = re.compile(r"[^A-Za-z0-9._:-]+")


def _safe_fill_action_id(base_action_id: str, fill_id: str) -> str:
    clean_fill = _SAFE_SUFFIX_RE.sub("-", str(fill_id).strip()).strip("-") or "fill"
    combined = f"{base_action_id}:{clean_fill}"
    if len(combined) <= 120:
        return combined
    digest = sha256(combined.encode("utf-8")).hexdigest()[:12]
    return f"{base_action_id[:100]}:{digest}"


def _fee_minor_from_fill(fill: ExternalFillRecord) -> int:
    if fill.commission <= 0:
        return 0
    asset = fill.commission_asset.strip().upper()
    if asset in _STABLE_QUOTE_ASSETS:
        usd_fee = fill.commission
    else:
        usd_fee = fill.commission * fill.price
    minor = int(
        (usd_fee * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    return max(0, minor)


def _book_external_fills(
    writer: WriterLease,
    *,
    binding_id: str,
    actor_id: str,
    base_action_id: str,
    instrument_id: str,
    side: str,
    limit_minor_units: int,
    fills: tuple[ExternalFillRecord, ...],
    run_id: str | None,
    now_ns: int,
) -> list[dict[str, Any]]:
    booked: list[dict[str, Any]] = []
    for fill in fills:
        qty_int = int(fill.quantity.to_integral_value(rounding=ROUND_HALF_UP))
        if qty_int <= 0:
            continue
        price_minor = decimal_price_to_minor_units(fill.price)
        fee_minor = _fee_minor_from_fill(fill)
        fill_action = _safe_fill_action_id(base_action_id, fill.fill_id)
        fill_ts_ns = (
            int(fill.transact_time_ms) * 1_000_000
            if fill.transact_time_ms > 0
            else now_ns
        )
        rec = record_live_book_order_fill(
            writer,
            binding_id=binding_id,
            actor_id=actor_id,
            action_id=fill_action,
            external_order_id=str(fill.venue_order_id),
            external_fill_id=str(fill.fill_id),
            instrument_id=instrument_id,
            side=side.lower(),
            quantity=qty_int,
            fill_price_minor_units=price_minor,
            fee_minor_units=fee_minor,
            limit_minor_units=limit_minor_units,
            valuation_label=f"Live {side.upper()} {instrument_id}",
            now_ns=fill_ts_ns,
            run_id=run_id,
        )
        booked.append(rec)
    return booked


def submit_company_live_book_order(
    writer: WriterLease,
    *,
    binding_id: str,
    actor_id: str,
    action_id: str,
    instrument_id: str,
    side: str,
    quantity: int,
    limit_minor_units: int,
    adapter: ExternalVenueAdapter,
    quote_provider: MarketQuoteProvider | None = None,
    time_in_force: str = "IOC",
    run_id: str | None = None,
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Execute a live order via two-phase durable `external_actions` outbox.

    1. Validates active `company_live` binding, risk gate, fresh quote, and cash/exposure cap.
    2. Persists `external_actions` (`prepared` -> `in_flight`) BEFORE network dispatch.
    3. On timeout (`ExternalOrderReplyUnknown`), settles outbox to `unknown` fail-closed
       without fabricating fills or permitting duplicate retries prior to reconciliation.
    4. Books verified venue fills idempotently into `trading_book_*` (`environment='live'`).
    """
    writer.verify()
    _require_safe_id(binding_id, "binding_id")
    _require_safe_id(actor_id, "actor_id")
    _require_safe_id(action_id, "action_id")
    norm_inst = normalize_instrument_id(instrument_id)
    symbol = to_venue_symbol(norm_inst)
    norm_side = side.strip().lower() if isinstance(side, str) else ""
    if norm_side not in ("buy", "sell"):
        raise ExecutionTierRefused("side must be 'buy' or 'sell'")
    _require_positive_int(quantity, "quantity")
    _require_positive_int(limit_minor_units, "limit_minor_units")
    tif = time_in_force.strip().upper() if isinstance(time_in_force, str) else ""
    if tif not in ("IOC", "GTC"):
        raise ExecutionTierRefused("time_in_force must be 'IOC' or 'GTC'")
    now = time_ns() if now_ns is None else now_ns

    caps = adapter.capabilities
    if caps.environment not in (ENV_LIVE, ENV_EXTERNAL_TEST):
        raise ExecutionTierRefused(
            f"External venue adapter environment '{caps.environment}' is invalid for live execution"
        )

    # Enforce fresh quote when quote_provider is configured.
    if quote_provider is not None:
        quote = quote_provider.latest_quote(norm_inst)
        if quote is None or not quote.is_fresh(
            now_ns=now, max_age_ns=QUOTE_FRESHNESS_MAX_NS
        ):
            raise ExecutionTierRefused(
                f"Fresh market quote (<= 15s) required before live order for {norm_inst}"
            )

    client_order_id = adapter.client_order_id_for(action_id)
    idem_key = f"live-order:{binding_id}:{action_id}"
    req_descriptor = {
        "binding_id": binding_id,
        "action_id": action_id,
        "client_order_id": client_order_id,
        "instrument_id": norm_inst,
        "symbol": symbol,
        "side": norm_side,
        "quantity": quantity,
        "limit_minor_units": limit_minor_units,
        "time_in_force": tif,
        "actor_id": actor_id,
        "run_id": run_id,
    }
    req_sha = sha256(
        json.dumps(req_descriptor, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()

    # Phase 1: Pre-flight validation + durable outbox intent BEFORE touching network.
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "trading"):
            raise ExecutionTierRefused("Trading domain is not installed")
        require_trading_risk_clear(db, writer.company_id, ENV_LIVE)

        book = read_trading_book_state_from_db(db, writer.company_id, binding_id)
        if (
            book is None
            or book["tier"] != TIER_COMPANY_LIVE
            or book["environment"] != ENV_LIVE
            or book["status"] != "active"
        ):
            raise ExecutionTierRefused(
                f"Active company_live binding '{binding_id}' not found"
            )

        # Pre-check cash and gross exposure cap conservatively at limit price.
        est_notional = quantity * limit_minor_units
        if norm_side == "buy" and est_notional > int(book["cash_minor_units"]):
            raise ExecutionTierRefused(
                "Live order notional exceeds available live book cash"
            )
        cur_pos = next(
            (p for p in book["positions"] if p["instrument_id"] == norm_inst), None
        )
        cur_qty = int(cur_pos["quantity"]) if cur_pos else 0
        cur_mark = (
            int(cur_pos["mark_price_minor_units"]) if cur_pos else limit_minor_units
        )
        delta_qty = quantity if norm_side == "buy" else -quantity
        other_exp = int(book["gross_exposure_minor_units"]) - abs(cur_qty) * cur_mark
        projected_exp = other_exp + abs(cur_qty + delta_qty) * limit_minor_units
        if projected_exp > int(book["exposure_limit_minor_units"]):
            raise ExecutionTierRefused(
                "Live order would exceed active live book exposure ceiling"
            )

        try:
            action_rec, replayed = prepare_external_action(
                db,
                idempotency_key=idem_key,
                company_id=writer.company_id,
                connector=caps.adapter_revision,
                operation=_LIVE_ORDER_OPERATION,
                request_sha256=req_sha,
                run_id=run_id,
                now_ns=now,
            )
        except ExternalActionConflict as exc:
            raise ExecutionTierRefused(str(exc)) from exc

        if replayed:
            if action_rec.status == "confirmed":
                prior_receipt = (
                    json.loads(action_rec.receipt_json)
                    if action_rec.receipt_json
                    else {}
                )
                return prior_receipt | {"replayed": True}
            if action_rec.status in ("in_flight", "unknown"):
                raise ExecutionTierRefused(
                    f"Live order action '{action_id}' is '{action_rec.status}'; reconcile before retrying"
                )
            if action_rec.status == "rejected":
                raise ExecutionTierRefused(
                    f"Live order action '{action_id}' was previously rejected by the venue"
                )

        blocking_ext = db.execute(
            """SELECT idempotency_key, status FROM external_actions
               WHERE company_id = ? AND operation = ?
                 AND status IN ('prepared', 'in_flight', 'unknown')
                 AND idempotency_key != ?
               LIMIT 1""",
            (writer.company_id, _LIVE_ORDER_OPERATION, idem_key),
        ).fetchone()
        if blocking_ext is not None:
            raise ExecutionTierRefused(
                f"Live execution blocked while prior external order '{blocking_ext[0]}' "
                f"is unresolved ('{blocking_ext[1]}'); reconcile before submitting new orders"
            )

        settle_external_action(
            db,
            idem_key,
            status="in_flight",
            receipt=req_descriptor | {"venue_status": "in_flight"},
            now_ns=now,
        )
        writer.verify()

    # Phase 2: External venue dispatch (outside SQLite write transaction).
    ext_req = ExternalOrderRequest(
        action_id=action_id,
        symbol=symbol,
        side=norm_side.upper(),
        quantity=Decimal(quantity),
        limit_price=minor_units_to_decimal(limit_minor_units),
        time_in_force=tif,
    )
    try:
        status_obj = adapter.submit_order(ext_req)
    except ExternalOrderReplyUnknown as exc:
        unknown_receipt = req_descriptor | {
            "venue_status": "unknown",
            "error": str(exc),
        }
        with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
            writer.verify()
            settle_external_action(
                db,
                idem_key,
                status="unknown",
                receipt=unknown_receipt,
                now_ns=time_ns(),
            )
        return {
            "binding_id": binding_id,
            "action_id": action_id,
            "client_order_id": client_order_id,
            "instrument_id": norm_inst,
            "side": norm_side,
            "quantity": quantity,
            "status": "unknown",
            "fills_booked": 0,
            "replayed": False,
            "detail": str(exc),
        }
    except ExternalVenueRefused as exc:
        rejected_receipt = req_descriptor | {
            "venue_status": "rejected",
            "error": str(exc),
        }
        with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
            writer.verify()
            settle_external_action(
                db,
                idem_key,
                status="rejected",
                receipt=rejected_receipt,
                now_ns=time_ns(),
            )
        raise ExecutionTierRefused(
            f"External venue rejected live order: {exc}"
        ) from exc

    # Query fills whenever filled_quantity > 0 so exact venue fill IDs & fees are booked.
    fills: tuple[ExternalFillRecord, ...] = ()
    if status_obj.filled_quantity > 0:
        try:
            fills = adapter.query_fills(symbol, client_order_id=client_order_id)
        except (ExternalVenueRefused, ExternalOrderReplyUnknown):
            fills = ()

    booked_fills = _book_external_fills(
        writer,
        binding_id=binding_id,
        actor_id=actor_id,
        base_action_id=action_id,
        instrument_id=norm_inst,
        side=norm_side,
        limit_minor_units=limit_minor_units,
        fills=fills,
        run_id=run_id,
        now_ns=now,
    )

    is_terminal_confirmed = status_obj.status in (
        "filled",
        "cancelled",
        "expired",
    ) and (status_obj.filled_quantity == 0 or len(booked_fills) > 0)
    outbox_status = (
        "rejected"
        if status_obj.status == "rejected"
        else ("confirmed" if is_terminal_confirmed else "in_flight")
    )
    final_receipt = req_descriptor | {
        "venue_order_id": status_obj.venue_order_id,
        "venue_status": status_obj.status,
        "filled_quantity": str(status_obj.filled_quantity),
        "remaining_quantity": str(status_obj.remaining_quantity),
        "fills_booked": len(booked_fills),
        "order_ids": [b["order_id"] for b in booked_fills],
    }
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        settle_external_action(
            db,
            idem_key,
            status=outbox_status,
            receipt=final_receipt,
            now_ns=time_ns(),
        )
    return {
        "binding_id": binding_id,
        "action_id": action_id,
        "client_order_id": client_order_id,
        "venue_order_id": status_obj.venue_order_id,
        "instrument_id": norm_inst,
        "side": norm_side,
        "quantity": quantity,
        "status": status_obj.status,
        "outbox_status": outbox_status,
        "fills_booked": len(booked_fills),
        "fills": booked_fills,
        "replayed": False,
    }


def reconcile_trading_books(
    writer: WriterLease,
    *,
    quote_provider: MarketQuoteProvider | None = None,
    live_venue_resolver: Callable[[str], ExternalVenueAdapter | None] | None = None,
    max_quote_age_ns: int = 60 * 10**9,
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Run live venue outbox recovery, external drift detection, and mark-to-market valuations."""
    writer.verify()
    now = time_ns() if now_ns is None else now_ns

    with closing(open_reader(writer.store)) as db, read_tx(db):
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "trading"):
            return {
                "trading_domain_installed": False,
                "books_marked": [],
                "live_actions_reconciled": 0,
                "live_fills_booked": 0,
                "drift_detected": False,
                "restrictions_applied": [],
                "reconciled_at_ns": now,
            }

        active_bindings = [
            (str(r[0]), str(r[1]), str(r[2]))
            for r in db.execute(
                """SELECT binding_id, tier, environment
                   FROM execution_environment_binding
                   WHERE company_id = ? AND status = 'active'
                     AND tier IN (?, ?)
                   ORDER BY created_at_ns ASC, binding_id ASC""",
                (writer.company_id, TIER_COMPANY_PAPER, TIER_COMPANY_LIVE),
            ).fetchall()
        ]
        unresolved_actions = [
            (str(r[0]), str(r[1]), str(r[2]), r[3])
            for r in db.execute(
                """SELECT idempotency_key, status, operation, receipt_json
                   FROM external_actions
                   WHERE company_id = ? AND operation = ?
                     AND status IN ('prepared', 'in_flight', 'unknown')
                   ORDER BY created_at_ns ASC""",
                (writer.company_id, _LIVE_ORDER_OPERATION),
            ).fetchall()
        ]
        all_live_actions_receipts = [
            r[0]
            for r in db.execute(
                """SELECT receipt_json FROM external_actions
                   WHERE company_id = ? AND operation = ? AND receipt_json IS NOT NULL""",
                (writer.company_id, _LIVE_ORDER_OPERATION),
            ).fetchall()
        ]

    live_actions_reconciled = 0
    live_fills_booked = 0
    drift_detected = False
    restrictions_applied: list[str] = []

    # 1. Reconcile unresolved external live order actions.
    if live_venue_resolver is not None:
        for idem_key, _status, _op, raw_receipt in unresolved_actions:
            if not raw_receipt:
                continue
            try:
                ctx_data = json.loads(str(raw_receipt))
            except json.JSONDecodeError:
                continue
            if not isinstance(ctx_data, dict):
                continue
            bid = str(ctx_data.get("binding_id") or "")
            symbol = str(ctx_data.get("symbol") or "")
            coid = str(ctx_data.get("client_order_id") or "")
            action_id = str(ctx_data.get("action_id") or "")
            inst_id = str(ctx_data.get("instrument_id") or symbol)
            side = str(ctx_data.get("side") or "buy")
            limit_minor = int(ctx_data.get("limit_minor_units") or 1)
            actor_id = str(ctx_data.get("actor_id") or "trading_reconciler")
            run_id = ctx_data.get("run_id")
            if not (bid and symbol and coid and action_id):
                continue
            adapter = live_venue_resolver(bid)
            if adapter is None:
                continue
            try:
                order_st = adapter.query_order(symbol, coid)
            except (ExternalVenueRefused, ExternalOrderReplyUnknown):
                continue
            if order_st.status == "absent_unsealed":
                # Fail closed: external venue does not seal absent IDs.
                continue

            fills: tuple[ExternalFillRecord, ...] = ()
            if order_st.filled_quantity > 0:
                try:
                    fills = adapter.query_fills(symbol, client_order_id=coid)
                except (ExternalVenueRefused, ExternalOrderReplyUnknown):
                    continue

            booked = _book_external_fills(
                writer,
                binding_id=bid,
                actor_id=actor_id,
                base_action_id=action_id,
                instrument_id=inst_id,
                side=side,
                limit_minor_units=limit_minor,
                fills=fills,
                run_id=None if run_id is None else str(run_id),
                now_ns=now,
            )
            live_fills_booked += sum(1 for b in booked if not b.get("replayed"))

            is_terminal = order_st.status in ("filled", "cancelled", "expired") and (
                order_st.filled_quantity == 0 or len(booked) > 0
            )
            next_status = (
                "rejected"
                if order_st.status == "rejected"
                else ("confirmed" if is_terminal else "in_flight")
            )
            if next_status in ("confirmed", "rejected"):
                updated_receipt = ctx_data | {
                    "venue_order_id": order_st.venue_order_id,
                    "venue_status": order_st.status,
                    "filled_quantity": str(order_st.filled_quantity),
                    "remaining_quantity": str(order_st.remaining_quantity),
                    "fills_booked": len(booked),
                    "order_ids": [b["order_id"] for b in booked],
                    "reconciled_at_ns": now,
                }
                with (
                    closing(open_writer(writer.store, lease=writer)) as db,
                    write_tx(db),
                ):
                    writer.verify()
                    settle_external_action(
                        db,
                        idem_key,
                        status=next_status,
                        receipt=updated_receipt,
                        now_ns=now,
                    )
                live_actions_reconciled += 1

        # 2. Check active live bindings for unattributed external fills or positions!
        known_by_binding: dict[str, set[str]] = {}
        symbols_by_binding: dict[str, set[str]] = {}
        for raw_r in all_live_actions_receipts:
            try:
                parsed = json.loads(str(raw_r))
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            b_id = parsed.get("binding_id")
            c_id = parsed.get("client_order_id")
            sym = parsed.get("symbol")
            if isinstance(b_id, str) and isinstance(c_id, str):
                known_by_binding.setdefault(b_id, set()).add(c_id)
            if isinstance(b_id, str) and isinstance(sym, str):
                symbols_by_binding.setdefault(b_id, set()).add(sym)

        for bid, tier, env in active_bindings:
            if tier != TIER_COMPANY_LIVE or env != ENV_LIVE:
                continue
            adapter = live_venue_resolver(bid)
            if adapter is None:
                continue
            with closing(open_reader(writer.store)) as db, read_tx(db):
                book = read_trading_book_state_from_db(db, writer.company_id, bid)
            syms = set(symbols_by_binding.get(bid, ()))
            if book is not None:
                for pos in book["positions"]:
                    try:
                        syms.add(to_venue_symbol(pos["instrument_id"]))
                    except QuoteRefused:
                        pass
            # Also observe account snapshot to discover unexpected positions in other symbols!
            try:
                snap = adapter.observe_account()
                for ext_pos in snap.positions:
                    if ext_pos.quantity != 0:
                        syms.add(ext_pos.symbol)
            except (ExternalVenueRefused, ExternalOrderReplyUnknown):
                pass

            known_ids = frozenset(known_by_binding.get(bid, ()))
            for sym in sorted(syms):
                try:
                    report = adapter.reconcile_external(sym, known_ids)
                except (ExternalVenueRefused, ExternalOrderReplyUnknown):
                    continue
                if not report.clean:
                    drift_detected = True
                    rid = f"drift-live-{bid[:16]}-{sym.lower()}"
                    restr = record_system_restriction(
                        writer,
                        restriction_id=rid,
                        cause_category=TRADING_RISK_CAUSE_CATEGORY,
                        scope_ref=SCOPE_EXECUTION_LIVE,
                        reason=(
                            f"External live venue drift detected on {sym}: "
                            f"{len(report.unattributed_fills)} unattributed fill(s), "
                            f"{len(report.unattributed_positions)} unattributed position(s)"
                        ),
                        operator_releasable=True,
                        actor_id="trading_reconciler",
                        now_ns=now,
                    )
                    restrictions_applied.append(restr["restriction_id"])

    # 3. Mark-to-Market active Paper (including Shadow Benchmarks) and Live books!
    books_marked: list[dict[str, Any]] = []
    if quote_provider is not None:
        for bid, _tier, _env in active_bindings:
            with closing(open_reader(writer.store)) as db, read_tx(db):
                book = read_trading_book_state_from_db(db, writer.company_id, bid)
            if book is None:
                continue
            open_instruments = [
                str(p["instrument_id"])
                for p in book["positions"]
                if int(p["quantity"]) != 0
            ]
            if not open_instruments:
                continue
            quotes = quote_provider.latest_quotes(open_instruments)
            fresh_marks: dict[str, int] = {}
            for inst in open_instruments:
                q = quotes.get(inst)
                if q is not None and q.is_fresh(
                    now_ns=now, max_age_ns=max_quote_age_ns
                ):
                    fresh_marks[inst] = q.price_minor_units
            if not fresh_marks:
                continue
            val = record_trading_book_valuation(
                writer,
                binding_id=bid,
                label="Mark-to-Market",
                mark_prices=fresh_marks,
                source="market_mark",
                now_ns=now,
            )
            books_marked.append(
                {
                    "binding_id": bid,
                    "environment": book["environment"],
                    "book_name": book["book_name"],
                    "nav_minor_units": val["nav_minor_units"],
                    "instruments_marked": sorted(fresh_marks),
                }
            )

    return {
        "trading_domain_installed": True,
        "books_marked": books_marked,
        "live_actions_reconciled": live_actions_reconciled,
        "live_fills_booked": live_fills_booked,
        "drift_detected": drift_detected,
        "restrictions_applied": restrictions_applied,
        "reconciled_at_ns": now,
    }
