"""TradingDomainPlugin wiring Trading capabilities into the 8 Core Company OS seams."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from ..agent_desk import desk_directory
from ..agent_runtime.cost_accounting import settle_shift_cost_to_ledger
from ..agent_runtime.protocol import (
    OuterToolBroker,
    ShiftRequest,
    ToolSpec,
    hydrate_shift_context,
)
from ..execution_tiers import (
    AgentSandboxTools,
    ExecutionTierRefused,
    execute_paper_to_live_migration,
    open_agent_sandbox,
    read_execution_tiers_snapshot,
)
from ..paper_risk import PaperRiskRefused, effective_paper_risk
from ..runtime.domains import (
    BaseDomainPlugin,
    CommandDescriptor,
    CommandDispatchContext,
    CommittedResultPredicate,
    DecisionActionContext,
    DecisionActionHandler,
    DomainActionRefused,
    DutyRuntimeServices,
    ToolContribution,
    ToolQueryContext,
    WorkDomainPolicy,
    WorkTimelineContribution,
)
from ..runtime.registry import DutyContext, DutyDescriptor, DutyOutcome
from ..runtime.work_kernel import WorkItem
from ..storage.db import open_reader, read_tx
from ..storage.schema import is_domain_installed
from .commands import (
    ACTIVATE_STRATEGY_RELEASE,
    ROLLBACK_STRATEGY_DEPLOYMENT,
    STRATEGY_COMMAND,
    execute_strategy_command,
)
from .duty import TRADING_WORK_POLICY, has_trading_committed_result

DUTY_QUANT_AGENT_SANDBOX = "quant.agent_sandbox"
DUTY_QUANT_PAPER_DECISION = "quant.paper_decision"
DUTY_QUANT_RECONCILE = "quant.reconcile"

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
    has_risk_receipt = (
        connection.execute(
            """SELECT 1 FROM command_receipt
               WHERE company_id = ?
                 AND command_type IN ('suspend_new_risk', 'resume_new_risk')
                 AND (? = 1 OR state = 'applied')
               LIMIT 1""",
            (company_id, int(has_trading)),
        ).fetchone()
        is not None
    )
    if not has_trading and not has_risk_receipt:
        return None
    has_paper_binding = (
        has_trading
        and connection.execute("SELECT 1 FROM paper_binding LIMIT 1").fetchone()
        is not None
    )
    has_risk_state = (
        has_trading
        and connection.execute(
            "SELECT 1 FROM paper_risk_state WHERE company_id = ? LIMIT 1",
            (company_id,),
        ).fetchone()
        is not None
    )
    if not (has_paper_binding or has_risk_state or has_risk_receipt):
        return None
    try:
        current = effective_paper_risk(connection)
    except PaperRiskRefused:
        return {
            "state": "unavailable",
            "effective_revision": None,
            "last_applied": None,
        }
    applied = current.applied
    return {
        "state": "suspended" if current.suspended else "not_suspended",
        "effective_revision": current.revision,
        "last_applied": (
            {
                "request_id": applied.request_id,
                "operation": applied.operation,
                "recorded_at_ns": applied.recorded_at_ns,
            }
            if applied is not None
            else None
        ),
    }


def _trading_decisions(
    connection: sqlite3.Connection, has_trading: bool
) -> dict[str, dict[str, Any]]:
    if not has_trading:
        return {}
    decisions: dict[str, dict[str, Any]] = {}
    rows = _rows(
        connection,
        """
        SELECT d.duty_id,d.decision_id,d.finding_id,d.decided_at,
               f.revision AS finding_revision, d.content, s.inputs
        FROM paper_research_decision d
        JOIN paper_research_finding f ON f.finding_id=d.finding_id
        JOIN paper_research_snapshot s ON s.run_id=d.run_id
    """,
    )
    for row in rows:
        row["known_at"] = json.loads(row.pop("inputs"))["cut_at"]
        proposal = json.loads(row.pop("content"))
        row.update(action=proposal["action"], rationale=proposal["rationale"])
        decisions[row["duty_id"]] = row
    for row in _rows(
        connection,
        """
        SELECT duty_id,decision_id,result_revision,action,rationale,decided_at
        FROM paper_decision
    """,
    ):
        decisions[row["duty_id"]] = row
    return decisions


def _paper_timeline_projection(
    connection: sqlite3.Connection, has_trading: bool
) -> dict[str, Any] | None:
    if not has_trading:
        return None
    trades = _rows(
        connection,
        """
        SELECT o.order_id,o.run_id,o.action_id,o.account_id,o.instrument_rules_digest,
               o.quantity AS ordered_quantity,o.reserved_minor_units,o.limit_minor_units,
               (SELECT latest.fill_id FROM paper_fill latest WHERE order_id=o.order_id
                ORDER BY latest.rowid DESC LIMIT 1) AS fill_id,
               SUM(p.quantity) AS quantity,
               CASE WHEN COUNT(p.fill_id)=1 THEN MAX(p.price_minor_units) END AS price_minor_units,
               SUM(f.fee_minor_units) AS fee_minor_units,
               CASE WHEN c.released=1 THEN c.outcome END AS terminal
        FROM paper_order o
        LEFT JOIN paper_fill p ON o.order_id=p.order_id
        LEFT JOIN paper_fill_fee f ON f.fill_id=p.fill_id
        LEFT JOIN paper_cancellation c ON c.order_id=o.order_id
        GROUP BY o.order_id ORDER BY o.rowid
    """,
    )
    if not trades:
        return None
    for order in trades:
        order["remaining_quantity"] = order["ordered_quantity"] - (
            order["quantity"] or 0
        )
        limit = order.pop("limit_minor_units")
        order["status"] = order.pop("terminal") or (
            "filled" if order["remaining_quantity"] == 0 else "unresolved"
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
    return value


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
    """M2 bridge plugging Trading duties, tools, decisions, timeline, commands, and CLI into Core OS."""

    domain_name: str = "trading"

    def work_policy(self) -> WorkDomainPolicy:
        return TRADING_WORK_POLICY

    def contribute_tools(self, ctx: ToolQueryContext) -> ToolContribution:
        if (
            "trading" not in ctx.installed_domains
            and "paper" not in ctx.installed_domains
        ):
            return ToolContribution()
        return ToolContribution(
            specs=(_READ_EXECUTION_TIERS_SPEC,),
            handlers={
                "read_execution_tiers": lambda _args: _read_tiers(
                    ctx.store, ctx.company_id
                )
            },
        )

    def contribute_duties(
        self, services: DutyRuntimeServices
    ) -> tuple[DutyDescriptor, ...]:
        def _run_quant_agent_sandbox(ctx: DutyContext) -> DutyOutcome:
            payload = json.loads(ctx.work_item.payload_json)
            instruction = str(
                payload.get("instruction")
                or "Run your isolated Tier 1 sandbox strategy experiment and summarize results."
            )
            agent_id = ctx.work_item.owner_agent_id
            persisted_profile, grant_scope = services.read_agent_state(
                ctx.writer.store, ctx.company_id, agent_id
            )
            profile = services.resolve_profile(services.profile_bindings, agent_id)
            sandbox_tools = AgentSandboxTools(writer=ctx.writer, agent_id=agent_id)
            broker = OuterToolBroker(
                company_id=ctx.company_id,
                agent_id=agent_id,
                run_id=ctx.run.run_id,
                fencing_token=ctx.run.fencing_token,
                handlers={
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
            )
            desk = desk_directory(ctx.writer.store, agent_id)
            desk.mkdir(parents=True, exist_ok=True)
            context_pack = hydrate_shift_context(
                company_id=ctx.company_id,
                agent_id=agent_id,
                run_id=ctx.run.run_id,
                fencing_token=ctx.run.fencing_token,
                grant_scope=grant_scope,
                mandate_summary="Autonomous isolated Tier 1 per-Agent sandbox experiment.",
            )
            budget_ceiling = (
                ctx.run.budget_ceiling_minor
                if ctx.run.budget_ceiling_minor > 0
                else services.default_budget_ceiling_minor
            )
            req = ShiftRequest(
                company_id=ctx.company_id,
                agent_id=agent_id,
                run_id=ctx.run.run_id,
                turn_id="turn-1",
                fencing_token=ctx.run.fencing_token,
                instruction=instruction,
                context=context_pack,
                tools=_SANDBOX_TOOL_SPECS,
                budget_ceiling_minor_units=budget_ceiling,
                desk_path=desk,
                store_path=ctx.writer.store,
            )
            result = services.adapter_registry.execute_shift(
                persisted_profile_id=persisted_profile,
                profile=profile,
                request=req,
                broker=broker,
            )
            settlement = settle_shift_cost_to_ledger(
                ctx.writer,
                profile=profile,
                run_id=ctx.run.run_id,
                turn_id="turn-1",
                usage=result.usage,
                budget_ceiling_minor_units=budget_ceiling,
            )
            return DutyOutcome(
                disposition="completed",
                result={
                    "agent_id": agent_id,
                    "provider": result.provider,
                    "model": result.model,
                    "tool_calls_executed": result.tool_calls_executed,
                    "output_text": result.output_text,
                    "cost_minor_units": settlement.cost_minor_units,
                    "journal_entry_id": settlement.journal_entry_id,
                },
            )

        def _run_quant_paper_decision(ctx: DutyContext) -> DutyOutcome:
            payload = json.loads(ctx.work_item.payload_json)
            instruction = str(
                payload.get("instruction")
                or "Evaluate company paper execution tier posture and record decision summary."
            )
            agent_id = ctx.work_item.owner_agent_id
            persisted_profile, grant_scope = services.read_agent_state(
                ctx.writer.store, ctx.company_id, agent_id
            )
            profile = services.resolve_profile(services.profile_bindings, agent_id)
            broker = OuterToolBroker(
                company_id=ctx.company_id,
                agent_id=agent_id,
                run_id=ctx.run.run_id,
                fencing_token=ctx.run.fencing_token,
                handlers={
                    "read_company_status": lambda _args: services.read_company_status(
                        ctx.writer.store, ctx.company_id
                    ),
                    "read_execution_tiers": lambda _args: _read_tiers(
                        ctx.writer.store, ctx.company_id
                    ),
                },
            )
            desk = desk_directory(ctx.writer.store, agent_id)
            desk.mkdir(parents=True, exist_ok=True)
            context_pack = hydrate_shift_context(
                company_id=ctx.company_id,
                agent_id=agent_id,
                run_id=ctx.run.run_id,
                fencing_token=ctx.run.fencing_token,
                grant_scope=grant_scope,
                mandate_summary="Company Tier 2 paper decision shift.",
            )
            budget_ceiling = (
                ctx.run.budget_ceiling_minor
                if ctx.run.budget_ceiling_minor > 0
                else services.default_budget_ceiling_minor
            )
            decision_tools = (
                ToolSpec(
                    name="read_company_status",
                    description="Read current company agent count, active restrictions, and open work summary.",
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
                _READ_EXECUTION_TIERS_SPEC,
            )
            req = ShiftRequest(
                company_id=ctx.company_id,
                agent_id=agent_id,
                run_id=ctx.run.run_id,
                turn_id="turn-1",
                fencing_token=ctx.run.fencing_token,
                instruction=instruction,
                context=context_pack,
                tools=decision_tools,
                budget_ceiling_minor_units=budget_ceiling,
                desk_path=desk,
                store_path=ctx.writer.store,
            )
            result = services.adapter_registry.execute_shift(
                persisted_profile_id=persisted_profile,
                profile=profile,
                request=req,
                broker=broker,
            )
            settlement = settle_shift_cost_to_ledger(
                ctx.writer,
                profile=profile,
                run_id=ctx.run.run_id,
                turn_id="turn-1",
                usage=result.usage,
                budget_ceiling_minor_units=budget_ceiling,
            )
            return DutyOutcome(
                disposition="completed",
                result={
                    "agent_id": agent_id,
                    "provider": result.provider,
                    "output_text": result.output_text,
                    "cost_minor_units": settlement.cost_minor_units,
                    "journal_entry_id": settlement.journal_entry_id,
                },
            )

        def _run_quant_reconcile(ctx: DutyContext) -> DutyOutcome:
            snapshot = _read_tiers(ctx.writer.store, ctx.company_id)
            return DutyOutcome(
                disposition="completed",
                result={
                    "paper_domain_installed": snapshot["paper_domain_installed"],
                    "company_paper_active": snapshot["company_paper"] is not None,
                    "company_live_active": snapshot["company_live"] is not None,
                    "active_agent_sandboxes": len(snapshot["agent_sandboxes"]),
                    "reconciled_at_ns": ctx.now_ns,
                },
            )

        return (
            DutyDescriptor(
                duty_kind=DUTY_QUANT_AGENT_SANDBOX,
                execute=_run_quant_agent_sandbox,
            ),
            DutyDescriptor(
                duty_kind=DUTY_QUANT_PAPER_DECISION,
                execute=_run_quant_paper_decision,
                precondition=_no_trading_risk_restriction,
            ),
            DutyDescriptor(
                duty_kind=DUTY_QUANT_RECONCILE,
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

    def contribute_work_timeline(
        self, connection: sqlite3.Connection, company_id: str
    ) -> WorkTimelineContribution | None:
        has_trading = is_domain_installed(connection, "trading")
        risk_control = _trading_risk_control(connection, company_id, has_trading)
        paper = _paper_timeline_projection(connection, has_trading)
        decisions = _trading_decisions(connection, has_trading)
        if not has_trading and risk_control is None and paper is None and not decisions:
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

        return (
            CommandDescriptor(
                command_type=ACTIVATE_STRATEGY_RELEASE,
                contract_version=STRATEGY_COMMAND,
                execute=_exec_strategy,
            ),
            CommandDescriptor(
                command_type=ROLLBACK_STRATEGY_DEPLOYMENT,
                contract_version=STRATEGY_COMMAND,
                execute=_exec_strategy,
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
