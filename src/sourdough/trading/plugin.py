"""TradingDomainPlugin wiring Trading capabilities into the 8 Core Company OS seams."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..agent_runtime.protocol import ToolSpec
from ..ipc.protocol import IpcRefused, valid_path_segment as _valid_path_segment
from ..runtime.domains import (
    BaseDomainPlugin,
    CommandDescriptor,
    CommandDispatchContext,
    CommittedResultPredicate,
    DecisionActionContext,
    DecisionActionHandler,
    DomainActionRefused,
    DutyRuntimeServices,
    ProjectionDescriptor,
    RestrictionGuard,
    ToolContribution,
    ToolQueryContext,
    WorkDomainPolicy,
    WorkTimelineContribution,
)
from ..runtime.registry import DutyContext, DutyDescriptor, DutyOutcome, RuntimeRefused
from ..runtime.work_kernel import WorkItem
from ..storage.db import open_reader, read_tx
from ..storage.schema import is_domain_installed
from .accounts import open_agent_sandbox
from .commands import (
    ACTIVATE_STRATEGY_RELEASE,
    DEMOTE_LIVE_ENVIRONMENT,
    DEMOTE_PRECONDITION_KEYS,
    EXECUTION_TIER_COMMAND,
    OPEN_COMPANY_PAPER_BOOK,
    OPEN_PAPER_BOOK_PRECONDITION_KEYS,
    PROMOTE_EXECUTION_ENVIRONMENT,
    PROMOTE_PRECONDITION_KEYS,
    RECONCILE_TRADING_BOOKS,
    RECONCILE_TRADING_PRECONDITION_KEYS,
    ROLLBACK_PRECONDITION_KEYS,
    ROLLBACK_STRATEGY_DEPLOYMENT,
    STRATEGY_COMMAND,
    execute_execution_tier_command,
    execute_strategy_command,
)
from .company_tools import (
    COMPANY_TRADING_TOOL_SPECS,
    CompanyTradingTools,
    READ_MARKET_QUOTE_SPEC,
    READ_TRADING_BOOKS_SPEC,
    read_trading_books_summary,
)
from .domain import ENV_EXTERNAL_TEST, ENV_LIVE, ENV_PAPER, ExecutionTierRefused
from .duty import TRADING_WORK_POLICY, has_trading_committed_result
from .external_venue import ExternalVenueAdapter
from .migration import execute_paper_to_live_migration, read_execution_tiers_snapshot
from .projections import (
    PROJECTION_ORDER_TRACE,
    PROJECTION_STRATEGIES,
    PROJECTION_TRADING_DESK,
    enrich_trading_core_projection,
    read_order_trace_projection,
    read_strategies_projection,
    read_trading_desk_projection,
)
from .quotes import (
    MarketQuoteProvider,
    QuoteRefused,
    build_quote_provider_from_spec,
    normalize_instrument_id,
)
from .reconcile import reconcile_trading_books
from .risk import (
    TRADING_RISK_CAUSE_CATEGORY,
    TradingRestrictionGuard,
    active_trading_risk_restrictions,
)
from .sandbox_tools import AgentSandboxTools
from .venue_resolver import build_default_live_venue_resolver

DUTY_QUANT_AGENT_SANDBOX = "quant.agent_sandbox"
DUTY_TRADING_AGENT_SANDBOX = "trading.agent_sandbox"
DUTY_QUANT_PAPER_DECISION = "quant.paper_decision"
DUTY_TRADING_DECISION = "trading.decision"
DUTY_QUANT_RECONCILE = "quant.reconcile"
DUTY_TRADING_RECONCILE = "trading.reconcile"

_READ_EXECUTION_TIERS_SPEC = ToolSpec(
    name="read_execution_tiers",
    description="Read Tier 1 agent sandboxes, Tier 2 company paper, and Tier 3 company live state.",
    parameters={"type": "object", "properties": {}, "required": []},
)

_SANDBOX_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="open_agent_sandbox",
        description="Open this Agent's isolated Tier 1 paper sandbox account.",
        parameters={
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "currency": {"type": "string"},
                "starting_minor_units": {"type": "integer"},
                "exposure_limit_minor_units": {"type": "integer"},
                "fee_bps": {"type": "integer"},
                "slippage_bps": {"type": "integer"},
                "quote_source": {"type": "string"},
            },
            "required": ["account_id"],
        },
    ),
    ToolSpec(
        name="submit_agent_sandbox_order",
        description="Submit a simulated order strictly inside this Agent's isolated Tier 1 sandbox.",
        parameters={
            "type": "object",
            "properties": {
                "action_id": {"type": "string"},
                "instrument_id": {"type": "string"},
                "side": {"type": "string"},
                "quantity": {"type": "integer"},
                "limit_minor_units": {"type": "integer"},
                "market_price_minor_units": {"type": "integer"},
                "fill_mode": {"type": "string"},
            },
            "required": [
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
                "market_price_minor_units",
            ],
        },
    ),
    ToolSpec(
        name="cancel_agent_sandbox_order",
        description="Cancel a resting order inside this Agent's isolated Tier 1 sandbox.",
        parameters={
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    ),
    ToolSpec(
        name="read_agent_sandbox_portfolio",
        description="Inspect cash, positions, fees, and realized PnL of this Agent's Tier 1 sandbox.",
        parameters={"type": "object", "properties": {}, "required": []},
    ),
)


def _read_tiers(store: Path, company_id: str) -> dict[str, Any]:
    with closing(open_reader(store)) as db, read_tx(db):
        return read_execution_tiers_snapshot(db, company_id)


def _recurring_timer_next_condition(
    store: Path,
    company_id: str,
    owner_agent_id: str,
    payload: Mapping[str, Any],
    now_ns: int,
) -> dict[str, Any] | None:
    """Return a successor timer condition when a scheduled standing trading timer completes."""
    cond_id = payload.get("condition_id")
    if not isinstance(cond_id, str) or not cond_id.strip():
        return None
    with closing(open_reader(store)) as db, read_tx(db):
        row = db.execute(
            """SELECT c.cooldown_ns, c.responsibility_id
               FROM work_conditions c
               JOIN work_responsibilities r
                 ON r.company_id = c.company_id
                AND r.responsibility_id = c.responsibility_id
               WHERE c.company_id = ?
                 AND c.condition_id = ?
                 AND c.trigger_kind = 'timer'
                 AND c.cooldown_ns > 0
                 AND r.owner_agent_id = ?
                 AND r.status = 'active'
               LIMIT 1""",
            (company_id, cond_id.strip(), owner_agent_id),
        ).fetchone()
    if row is None:
        return None
    interval_ns = int(row[0])
    resp_id = str(row[1])
    if interval_ns <= 0:
        return None
    return {
        "trigger_kind": "timer",
        "responsibility_id": resp_id,
        "due_at_ns": now_ns + interval_ns,
        "cooldown_ns": interval_ns,
    }


def _no_trading_risk_restriction(
    conn: sqlite3.Connection, item: WorkItem
) -> tuple[bool, str | None]:
    row = conn.execute(
        """SELECT restriction_id FROM restriction_cause
           WHERE company_id = ? AND active = 1
             AND scope_ref IN ('company:all', 'execution:all', 'execution:paper')
           LIMIT 1""",
        (item.company_id,),
    ).fetchone()
    if row is not None:
        return False, f"blocked_by_restriction:{row[0]}"
    return True, None


def _rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    cursor = connection.execute(sql)
    labels = [column[0] for column in cursor.description]
    return [dict(zip(labels, row, strict=True)) for row in cursor]


def _trading_risk_control(
    connection: sqlite3.Connection, company_id: str, has_trading: bool
) -> dict[str, Any] | None:
    if not has_trading:
        return None
    last_receipt = connection.execute(
        """SELECT command_id, command_type, COALESCE(finalized_at_ns, created_at_ns)
           FROM command_receipt
           WHERE company_id = ?
             AND command_type IN ('pause_company', 'resume_company', 'suspend_new_risk', 'resume_new_risk')
             AND state = 'applied'
           ORDER BY COALESCE(finalized_at_ns, created_at_ns) DESC, rowid DESC
           LIMIT 1""",
        (company_id,),
    ).fetchone()

    has_binding = (
        has_trading
        and connection.execute(
            "SELECT 1 FROM execution_environment_binding WHERE company_id = ? LIMIT 1",
            (company_id,),
        ).fetchone()
        is not None
    )
    risk_rows = connection.execute(
        """SELECT restriction_id, scope_ref, restriction_revision, active, recorded_at_ns, last_command_id
           FROM restriction_cause
           WHERE company_id = ?
             AND (scope_ref LIKE 'execution:%' OR cause_category = ? OR scope_ref = 'company:all')
           ORDER BY restriction_revision DESC, recorded_at_ns DESC""",
        (company_id, TRADING_RISK_CAUSE_CATEGORY),
    ).fetchall()
    if not (has_binding or risk_rows or last_receipt is not None):
        return None

    active_paper = active_trading_risk_restrictions(connection, company_id, ENV_PAPER)
    active_live = active_trading_risk_restrictions(connection, company_id, ENV_LIVE)
    active_exec = active_paper + [
        r for r in active_live if r["scope_ref"] == "execution:live"
    ]
    max_rev = max(
        (int(r[2]) for r in risk_rows),
        default=0,
    )
    return {
        "state": "suspended" if active_exec else "not_suspended",
        "effective_revision": max_rev,
        "last_applied": (
            {
                "request_id": str(last_receipt[0]),
                "operation": str(last_receipt[1]),
                "recorded_at_ns": int(last_receipt[2]),
            }
            if last_receipt is not None
            else None
        ),
    }


def _trading_decisions(
    connection: sqlite3.Connection, has_trading: bool
) -> dict[str, dict[str, Any]]:
    if not has_trading:
        return {}
    decisions: dict[str, dict[str, Any]] = {}
    for row in _rows(
        connection,
        """
        SELECT dr.duty_id, o.decision_id, o.mandate_revision AS result_revision,
               o.side AS action, o.instrument_id || ' ' || o.status AS rationale,
               o.created_at_ns AS decided_at
        FROM trading_book_order o
        JOIN duty_run dr ON dr.run_id = o.run_id
        WHERE o.decision_id IS NOT NULL
        ORDER BY o.created_at_ns ASC
    """,
    ):
        decisions[str(row["duty_id"])] = row
    return decisions


def _paper_timeline_projection(
    connection: sqlite3.Connection, has_trading: bool
) -> dict[str, Any] | None:
    if not has_trading:
        return None
    trades = _rows(
        connection,
        """
        SELECT o.order_id, o.run_id, o.action_id, b.account_id,
               b.instrument_rules_digest, o.quantity AS ordered_quantity,
               o.quantity * o.limit_minor_units AS reserved_minor_units,
               o.limit_minor_units,
               (SELECT latest.fill_id FROM trading_book_fill latest
                WHERE latest.order_id = o.order_id
                ORDER BY latest.filled_at_ns DESC, latest.fill_id DESC LIMIT 1) AS fill_id,
               o.filled_quantity AS quantity,
               o.fill_price_minor_units AS price_minor_units,
               o.fee_minor_units,
               o.status AS order_status
        FROM trading_book_order o
        JOIN execution_environment_binding b ON b.binding_id = o.binding_id
        WHERE o.environment IN ('paper', 'live')
        ORDER BY o.created_at_ns ASC, o.order_id ASC
    """,
    )
    if not trades:
        return None
    for order in trades:
        order["remaining_quantity"] = order["ordered_quantity"] - (
            order["quantity"] or 0
        )
        limit = order.pop("limit_minor_units")
        raw_status = order.pop("order_status")
        order["status"] = (
            "unresolved"
            if raw_status == "open"
            else ("filled" if raw_status == "filled" else str(raw_status))
        )
        order["open_reservation_minor_units"] = (
            order["remaining_quantity"] * limit
            if order["status"] == "unresolved"
            else 0
        )
    return {
        "orders": trades,
        "unresolved_orders": sum(record["status"] == "unresolved" for record in trades),
        "booked_position": sum(
            record["quantity"] for record in trades if record["quantity"] is not None
        ),
    }


def _require_impact_str(impact: Mapping[str, Any], key: str) -> str:
    if key not in impact:
        raise DomainActionRefused(f"Missing required field {key}")
    value = impact[key]
    if not isinstance(value, str) or not value.strip():
        raise DomainActionRefused(f"Invalid {key}")
    return value.strip()


def _optional_impact_str(impact: Mapping[str, Any], key: str, default: str) -> str:
    if key not in impact:
        return default
    value = impact[key]
    if not isinstance(value, str) or not value.strip():
        raise DomainActionRefused(f"Invalid {key}")
    return value.strip()


def _optional_impact_int(impact: Mapping[str, Any], key: str, default: int) -> int:
    if key not in impact:
        return default
    value = impact[key]
    if type(value) is not int:
        raise DomainActionRefused(f"Invalid {key}")
    return value


def _handle_open_agent_sandbox(ctx: DecisionActionContext) -> str:
    try:
        target_agent = _require_impact_str(ctx.impact, "agent_id")
        account_id = _require_impact_str(ctx.impact, "account_id")
        currency = _optional_impact_str(ctx.impact, "currency", "USD")
        starting_minor_units = _optional_impact_int(
            ctx.impact, "starting_minor_units", 10_000_000
        )
        fee_bps = _optional_impact_int(ctx.impact, "fee_bps", 4)
        slippage_bps = _optional_impact_int(ctx.impact, "slippage_bps", 2)
        binding = open_agent_sandbox(
            ctx.writer,
            agent_id=target_agent,
            account_id=account_id,
            currency=currency,
            starting_minor_units=starting_minor_units,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
    except (ExecutionTierRefused, KeyError, TypeError, ValueError) as exc:
        raise DomainActionRefused(str(exc)) from exc
    return f"opened sandbox {binding.binding_id} for {target_agent}"


def _handle_migrate_paper_to_live(ctx: DecisionActionContext) -> str:
    try:
        source_paper_binding_id = _require_impact_str(
            ctx.impact, "source_paper_binding_id"
        )
        target_live_binding_id = _require_impact_str(
            ctx.impact, "target_live_binding_id"
        )
        qualification_receipt_id = _require_impact_str(
            ctx.impact, "qualification_receipt_id"
        )
        migration_mode = _optional_impact_str(
            ctx.impact, "migration_mode", "clean_start"
        )
        source_paper_disposition = _optional_impact_str(
            ctx.impact, "source_paper_disposition", "keep_shadow"
        )
        reason = _optional_impact_str(
            ctx.impact,
            "reason",
            f"Board approved live migration decision {ctx.question_id}",
        )
        mig = execute_paper_to_live_migration(
            ctx.writer,
            source_paper_binding_id=source_paper_binding_id,
            target_live_binding_id=target_live_binding_id,
            qualification_receipt_id=qualification_receipt_id,
            migration_mode=migration_mode,
            source_paper_disposition=source_paper_disposition,
            reason=reason,
            question_id=ctx.question_id,
            actor_id="board_apply_decision",
            now_ns=ctx.now_ns,
        )
    except (ExecutionTierRefused, KeyError, TypeError, ValueError) as exc:
        raise DomainActionRefused(str(exc)) from exc
    return (
        f"migrated paper book {mig['source_paper_binding_id']} -> "
        f"live {mig['target_live_binding_id']} "
        f"(mode={mig['migration_mode']}, source={mig['source_book_final_role']}, "
        f"positions_copied=0)"
    )


class TradingDomainPlugin(BaseDomainPlugin):
    """Bridge plugging Trading duties, tools, decisions, timeline, commands, and CLI into Core OS.

    ``restore_guard()`` intentionally fails closed (returns ``None``) unless an
    explicit external-venue ``RestoreDomainGuard`` is supplied at composition time,
    preventing un-reconciled checkpoint restores on Trading-enabled stores.
    """

    domain_name: str = "trading"

    def __init__(
        self,
        *,
        quote_provider: MarketQuoteProvider | None = None,
        live_venue_resolver: Callable[[str], ExternalVenueAdapter | None] | None = None,
        auto_resolve_live_venues: bool = False,
        live_venue_environment: str | None = None,
        max_quote_age_ns: int = 60 * 10**9,
    ) -> None:
        if quote_provider is None:
            env_spec = os.environ.get("SOURDOUGH_TRADING_QUOTE_SOURCE", "").strip()
            if env_spec:
                quote_provider = build_quote_provider_from_spec(env_spec)
        env_live = (
            live_venue_environment
            if live_venue_environment is not None
            else os.environ.get("SOURDOUGH_TRADING_LIVE_ENV", "").strip().lower()
        )
        if env_live and env_live not in (ENV_LIVE, ENV_EXTERNAL_TEST):
            raise ExecutionTierRefused(
                f"SOURDOUGH_TRADING_LIVE_ENV must be '{ENV_LIVE}' or '{ENV_EXTERNAL_TEST}'"
            )
        self._quote_provider = quote_provider
        self._live_venue_resolver = live_venue_resolver
        self._auto_resolve_live_venues = auto_resolve_live_venues or bool(env_live)
        self._default_live_env = env_live or ENV_LIVE
        self._max_quote_age_ns = max_quote_age_ns

    def _effective_live_venue_resolver(
        self, store: Path, company_id: str, *, for_recovery: bool = False
    ) -> Callable[[str], ExternalVenueAdapter | None] | None:
        if self._live_venue_resolver is not None:
            return self._live_venue_resolver
        if self._auto_resolve_live_venues:
            return build_default_live_venue_resolver(
                store,
                company_id,
                environment=self._default_live_env,
                allow_recovery_statuses=for_recovery,
            )
        return None

    def work_policy(self) -> WorkDomainPolicy:
        return TRADING_WORK_POLICY

    def contribute_tools(self, ctx: ToolQueryContext) -> ToolContribution:
        if (
            "trading" not in ctx.installed_domains
            and "paper" not in ctx.installed_domains
        ):
            return ToolContribution()
        specs: list[ToolSpec] = [_READ_EXECUTION_TIERS_SPEC, READ_TRADING_BOOKS_SPEC]
        handlers: dict[str, Any] = {
            "read_execution_tiers": lambda _args: _read_tiers(
                ctx.store, ctx.company_id
            ),
            "read_trading_books": lambda args: read_trading_books_summary(
                ctx.store,
                ctx.company_id,
                environment=str(
                    (args if isinstance(args, dict) else {}).get(
                        "environment", ENV_PAPER
                    )
                )
                .strip()
                .lower(),
            ),
        }
        if self._quote_provider is not None:
            provider = self._quote_provider

            def _read_quote(args: dict[str, Any]) -> dict[str, Any]:
                if not isinstance(args, dict) or set(args) != {"instrument_id"}:
                    raise ExecutionTierRefused("Invalid read_market_quote arguments")
                try:
                    norm = normalize_instrument_id(args["instrument_id"])
                except QuoteRefused as exc:
                    raise ExecutionTierRefused(str(exc)) from exc
                q = provider.latest_quote(norm)
                if q is None:
                    raise ExecutionTierRefused(
                        f"No market quote available for '{norm}'"
                    )
                return {
                    "instrument_id": q.instrument_id,
                    "price_minor_units": q.price_minor_units,
                    "observed_at_ns": q.observed_at_ns,
                    "source": q.source,
                }

            specs.append(READ_MARKET_QUOTE_SPEC)
            handlers["read_market_quote"] = _read_quote
        return ToolContribution(
            specs=tuple(specs),
            handlers=handlers,
            recoverable_exceptions=(ExecutionTierRefused, QuoteRefused),
        )

    def contribute_duties(
        self, services: DutyRuntimeServices
    ) -> tuple[DutyDescriptor, ...]:
        def _run_quant_agent_sandbox(ctx: DutyContext) -> DutyOutcome:
            if services.shift_runner is None:
                raise RuntimeRefused("ShiftRunner service is unavailable")
            payload = json.loads(ctx.work_item.payload_json)
            instruction = str(
                payload.get("instruction")
                or "Run your isolated Tier 1 sandbox strategy experiment and summarize results."
            )
            agent_id = ctx.work_item.owner_agent_id
            sandbox_tools = AgentSandboxTools(writer=ctx.writer, agent_id=agent_id)
            run_res = services.shift_runner.run(
                ctx,
                mandate_summary="Autonomous isolated Tier 1 per-Agent sandbox experiment.",
                instruction=instruction,
                base_tools=_SANDBOX_TOOL_SPECS,
                base_handlers={
                    "open_agent_sandbox": lambda a: sandbox_tools.invoke(
                        "open_agent_sandbox", a
                    ),
                    "submit_agent_sandbox_order": lambda a: sandbox_tools.invoke(
                        "submit_agent_sandbox_order", a
                    ),
                    "cancel_agent_sandbox_order": lambda a: sandbox_tools.invoke(
                        "cancel_agent_sandbox_order", a
                    ),
                    "read_agent_sandbox_portfolio": lambda a: sandbox_tools.invoke(
                        "read_agent_sandbox_portfolio", a
                    ),
                },
                include_core_desk_tools=True,
                include_plugin_tools=False,
                extra_recoverable_exceptions=(ExecutionTierRefused, QuoteRefused),
            )
            result = run_res.shift_result
            settlement = run_res.settlement
            sbx_out_bytes = result.output_text.strip().encode("utf-8")
            return DutyOutcome(
                disposition="completed",
                result={
                    "agent_id": agent_id,
                    "provider": result.provider,
                    "model": result.model,
                    "tool_calls_executed": result.tool_calls_executed,
                    "output_digest": sha256(sbx_out_bytes).hexdigest(),
                    "output_bytes": len(sbx_out_bytes),
                    "cost_minor_units": settlement.cost_minor_units,
                    "journal_entry_id": settlement.journal_entry_id,
                },
            )

        def _run_quant_paper_decision(ctx: DutyContext) -> DutyOutcome:
            if services.shift_runner is None:
                raise RuntimeRefused("ShiftRunner service is unavailable")
            payload = json.loads(ctx.work_item.payload_json)
            instruction = str(
                payload.get("instruction")
                or "Evaluate market quotes and active Company Trading books, execute authorized trades or recommendations, and schedule your next check."
            )
            agent_id = ctx.work_item.owner_agent_id
            trading_tools = CompanyTradingTools(
                writer=ctx.writer,
                agent_id=agent_id,
                run_id=ctx.run.run_id,
                quote_provider=self._quote_provider,
                live_venue_resolver=self._effective_live_venue_resolver(
                    ctx.writer.store, ctx.company_id
                ),
                max_quote_age_ns=self._max_quote_age_ns,
            )
            broker_handlers: dict[str, Any] = {
                "read_company_status": lambda _args: services.read_company_status(
                    ctx.writer.store, ctx.company_id
                ),
                "read_execution_tiers": lambda _args: _read_tiers(
                    ctx.writer.store, ctx.company_id
                ),
            }
            for spec in COMPANY_TRADING_TOOL_SPECS:
                tool_name = spec.name
                broker_handlers[tool_name] = (
                    lambda a, _name=tool_name: trading_tools.invoke(_name, a)
                )
            decision_tools = (
                ToolSpec(
                    name="read_company_status",
                    description="Read current company agent count, active restrictions, and open work summary.",
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
                _READ_EXECUTION_TIERS_SPEC,
                *COMPANY_TRADING_TOOL_SPECS,
            )
            run_res = services.shift_runner.run(
                ctx,
                mandate_summary="Autonomous Company Trading decision shift (Paper & Board-approved Live).",
                instruction=instruction,
                base_tools=decision_tools,
                base_handlers=broker_handlers,
                include_core_desk_tools=True,
                include_plugin_tools=False,
                extra_recoverable_exceptions=(ExecutionTierRefused, QuoteRefused),
            )
            result = run_res.shift_result
            settlement = run_res.settlement
            sbx_out_bytes = result.output_text.strip().encode("utf-8")
            next_cond = _recurring_timer_next_condition(
                ctx.writer.store,
                ctx.company_id,
                agent_id,
                payload if isinstance(payload, Mapping) else {},
                ctx.now_ns,
            )
            return DutyOutcome(
                disposition="completed",
                result={
                    "agent_id": agent_id,
                    "provider": result.provider,
                    "model": result.model,
                    "tool_calls_executed": result.tool_calls_executed,
                    "output_digest": sha256(sbx_out_bytes).hexdigest(),
                    "output_bytes": len(sbx_out_bytes),
                    "cost_minor_units": settlement.cost_minor_units,
                    "journal_entry_id": settlement.journal_entry_id,
                },
                next_condition=next_cond,
            )

        def _run_quant_reconcile(ctx: DutyContext) -> DutyOutcome:
            try:
                raw_payload = json.loads(ctx.work_item.payload_json or "{}")
            except json.JSONDecodeError:
                raw_payload = {}
            reconcile_summary = reconcile_trading_books(
                ctx.writer,
                quote_provider=self._quote_provider,
                live_venue_resolver=self._effective_live_venue_resolver(
                    ctx.writer.store, ctx.company_id, for_recovery=True
                ),
                max_quote_age_ns=self._max_quote_age_ns,
                now_ns=ctx.now_ns,
            )
            snapshot = _read_tiers(ctx.writer.store, ctx.company_id)
            next_cond = _recurring_timer_next_condition(
                ctx.writer.store,
                ctx.company_id,
                ctx.work_item.owner_agent_id,
                raw_payload if isinstance(raw_payload, Mapping) else {},
                ctx.now_ns,
            )
            return DutyOutcome(
                disposition="completed",
                result={
                    "trading_domain_installed": snapshot["trading_domain_installed"],
                    "company_paper_active": snapshot["company_paper"] is not None,
                    "company_live_active": snapshot["company_live"] is not None,
                    "active_agent_sandboxes": len(snapshot["agent_sandboxes"]),
                    "reconciled_at_ns": ctx.now_ns,
                    "books_marked": reconcile_summary["books_marked"],
                    "live_actions_reconciled": reconcile_summary[
                        "live_actions_reconciled"
                    ],
                    "live_fills_booked": reconcile_summary["live_fills_booked"],
                    "drift_detected": reconcile_summary["drift_detected"],
                    "restrictions_applied": reconcile_summary["restrictions_applied"],
                },
                next_condition=next_cond,
            )

        return (
            DutyDescriptor(
                duty_kind=DUTY_QUANT_AGENT_SANDBOX,
                execute=_run_quant_agent_sandbox,
            ),
            DutyDescriptor(
                duty_kind=DUTY_TRADING_AGENT_SANDBOX,
                execute=_run_quant_agent_sandbox,
            ),
            DutyDescriptor(
                duty_kind=DUTY_QUANT_PAPER_DECISION,
                execute=_run_quant_paper_decision,
                precondition=_no_trading_risk_restriction,
            ),
            DutyDescriptor(
                duty_kind=DUTY_TRADING_DECISION,
                execute=_run_quant_paper_decision,
                precondition=_no_trading_risk_restriction,
            ),
            DutyDescriptor(
                duty_kind=DUTY_QUANT_RECONCILE,
                execute=_run_quant_reconcile,
            ),
            DutyDescriptor(
                duty_kind=DUTY_TRADING_RECONCILE,
                execute=_run_quant_reconcile,
            ),
        )

    def contribute_decision_actions(self) -> Mapping[str, DecisionActionHandler]:
        return {
            "open_agent_sandbox": _handle_open_agent_sandbox,
            "migrate_paper_to_live": _handle_migrate_paper_to_live,
        }

    def committed_result_predicate(self) -> CommittedResultPredicate:
        return has_trading_committed_result

    def restriction_guard(self) -> RestrictionGuard:
        return TradingRestrictionGuard()

    def enrich_core_projection(
        self,
        projection_name: str,
        db: sqlite3.Connection,
        company_id: str,
        payload: dict[str, Any],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        enrich_trading_core_projection(
            projection_name,
            db,
            company_id,
            payload,
            context=dict(context) if context is not None else None,
        )

    def contribute_work_timeline(
        self, connection: sqlite3.Connection, company_id: str
    ) -> WorkTimelineContribution | None:
        has_trading = is_domain_installed(connection, "trading")
        if not has_trading:
            return None
        risk_control = _trading_risk_control(connection, company_id, has_trading)
        paper = _paper_timeline_projection(connection, has_trading)
        decisions = _trading_decisions(connection, has_trading)
        if risk_control is None and paper is None and not decisions:
            return None
        return WorkTimelineContribution(
            decisions_by_duty_id=decisions,
            domain_payload={"paper": paper, "risk_control": risk_control},
        )

    def contribute_commands(self) -> tuple[CommandDescriptor, ...]:
        def _exec_strategy(ctx: CommandDispatchContext):
            return execute_strategy_command(
                ctx.writer,
                command_id=ctx.command_id,
                command_type=ctx.command_type,
                preconditions=dict(ctx.preconditions),
                payload=dict(ctx.payload),
                authorize=ctx.authorize_board,
                principal_hint=ctx.principal_hint,
                peer_pid=ctx.peer_pid,
            )

        def _exec_tier(ctx: CommandDispatchContext):
            return execute_execution_tier_command(
                ctx.writer,
                command_id=ctx.command_id,
                command_type=ctx.command_type,
                preconditions=dict(ctx.preconditions),
                payload=dict(ctx.payload),
                authorize=ctx.authorize_board,
                principal_hint=ctx.principal_hint,
                peer_pid=ctx.peer_pid,
                quote_provider=self._quote_provider,
                live_venue_resolver=self._effective_live_venue_resolver(
                    ctx.writer.store,
                    ctx.writer.company_id,
                    for_recovery=(ctx.command_type == RECONCILE_TRADING_BOOKS),
                ),
            )

        return (
            CommandDescriptor(
                command_type=ACTIVATE_STRATEGY_RELEASE,
                contract_version=STRATEGY_COMMAND,
                execute=_exec_strategy,
                requires_domain="trading",
            ),
            CommandDescriptor(
                command_type=ROLLBACK_STRATEGY_DEPLOYMENT,
                contract_version=STRATEGY_COMMAND,
                execute=_exec_strategy,
                precondition_keys=ROLLBACK_PRECONDITION_KEYS,
                requires_domain="trading",
            ),
            CommandDescriptor(
                command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                contract_version=EXECUTION_TIER_COMMAND,
                execute=_exec_tier,
                precondition_keys=PROMOTE_PRECONDITION_KEYS,
                requires_domain="trading",
            ),
            CommandDescriptor(
                command_type=DEMOTE_LIVE_ENVIRONMENT,
                contract_version=EXECUTION_TIER_COMMAND,
                execute=_exec_tier,
                precondition_keys=DEMOTE_PRECONDITION_KEYS,
                requires_domain="trading",
            ),
            CommandDescriptor(
                command_type=OPEN_COMPANY_PAPER_BOOK,
                contract_version=EXECUTION_TIER_COMMAND,
                execute=_exec_tier,
                precondition_keys=OPEN_PAPER_BOOK_PRECONDITION_KEYS,
                requires_domain="trading",
            ),
            CommandDescriptor(
                command_type=RECONCILE_TRADING_BOOKS,
                contract_version=EXECUTION_TIER_COMMAND,
                execute=_exec_tier,
                precondition_keys=RECONCILE_TRADING_PRECONDITION_KEYS,
                requires_domain="trading",
            ),
        )

    def contribute_projections(self) -> tuple[ProjectionDescriptor, ...]:
        def _valid_query_segment(raw: str) -> str | None:
            seg = raw.strip()
            if (
                not seg
                or "/" in seg
                or "\\" in seg
                or "\x00" in seg
                or seg in (".", "..")
                or len(seg) > 128
                or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in seg)
            ):
                return None
            return seg

        def _read_desk(
            store: Path,
            company_id: str,
            _suffix: str,
            query: Mapping[str, list[str]],
            _authorize: Any,
            _principal: str | None,
        ) -> tuple[dict[str, Any] | None, int]:
            env_raw = (query.get("environment", ["live"])[0] or "live").strip().lower()
            if env_raw not in ("live", "paper"):
                raise IpcRefused(
                    "Invalid environment parameter (expected 'live' or 'paper')"
                )
            book_raw = query.get("book_id", [None])[0]
            book_id: str | None = None
            if book_raw is not None:
                book_id = _valid_query_segment(book_raw)
                if book_id is None:
                    raise IpcRefused("Invalid book_id parameter")
            return read_trading_desk_projection(
                store,
                company_id,
                environment=env_raw,
                book_id=book_id,
            )

        def _read_order(
            store: Path,
            company_id: str,
            suffix: str,
            _query: Mapping[str, list[str]],
            _authorize: Any,
            _principal: str | None,
        ) -> tuple[dict[str, Any] | None, int]:
            order_id = _valid_path_segment(suffix[len("orders/") :])
            if order_id is None:
                raise IpcRefused("Invalid order identifier")
            return read_order_trace_projection(store, company_id, order_id)

        def _read_strategies(
            store: Path,
            company_id: str,
            _suffix: str,
            _query: Mapping[str, list[str]],
            _authorize: Any,
            _principal: str | None,
        ) -> tuple[dict[str, Any] | None, int]:
            return read_strategies_projection(store, company_id)

        return (
            ProjectionDescriptor(
                name=PROJECTION_TRADING_DESK,
                route_prefix="trading-desk",
                read=_read_desk,
                requires_domain="trading",
            ),
            ProjectionDescriptor(
                name=PROJECTION_ORDER_TRACE,
                route_prefix="orders/",
                read=_read_order,
                requires_domain="trading",
            ),
            ProjectionDescriptor(
                name=PROJECTION_STRATEGIES,
                route_prefix="strategies",
                read=_read_strategies,
                requires_domain="trading",
            ),
        )

    def register_cli_subcommands(
        self, subparsers: argparse._SubParsersAction[argparse.ArgumentParser]
    ) -> None:
        from .cli import register_trading_subcommands

        register_trading_subcommands(subparsers)

    def handle_cli_command(self, args: argparse.Namespace) -> int | None:
        from .cli import handle_trading_cli_command

        return handle_trading_cli_command(args)


create_domain_plugin = TradingDomainPlugin
