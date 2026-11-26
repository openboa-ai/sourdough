"""Production DutyRegistry handlers wiring multi-provider Agents, Board triggers, and execution tiers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from time import time_ns
from typing import TYPE_CHECKING, Any

from ..agent_desk import desk_directory
from ..agent_runtime.cost_accounting import settle_shift_cost_to_ledger
from ..agent_runtime.protocol import (
    AgentProfileBinding,
    OuterToolBroker,
    ShiftRequest,
    ToolSpec,
    hydrate_shift_context,
)

if TYPE_CHECKING:
    from ..agent_runtime.registry import AgentAdapterRegistry
from ..board_control import (
    mark_ceo_message_delivered,
    record_ceo_reply,
    record_decision_application_result,
    upsert_agent_profile,
)
from ..execution_tiers import (
    AgentSandboxTools,
    ExecutionTierRefused,
    execute_paper_to_live_migration,
    open_agent_sandbox,
    read_execution_tiers_snapshot,
)
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..writer_lease import WriterLease
from .registry import (
    DutyContext,
    DutyDescriptor,
    DutyOutcome,
    DutyRegistry,
    RuntimeRefused,
)
from .work_kernel import WorkItem, create_work_item

DUTY_CEO_RESPOND_MESSAGE = "ceo.respond_message"
DUTY_BOARD_APPLY_DECISION = "board.apply_decision"
DUTY_QUANT_AGENT_SANDBOX = "quant.agent_sandbox"
DUTY_QUANT_PAPER_DECISION = "quant.paper_decision"
DUTY_QUANT_RECONCILE = "quant.reconcile"

ProfileBindingLookup = (
    Mapping[str, AgentProfileBinding] | Callable[[str], AgentProfileBinding]
)

_CEO_READONLY_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read_company_status",
        description="Read current company agent count, active restrictions, and open work summary.",
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        name="read_execution_tiers",
        description="Read Tier 1 agent sandboxes, Tier 2 company paper, and Tier 3 company live state.",
        parameters={"type": "object", "properties": {}, "required": []},
    ),
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


def _resolve_profile(
    lookup: ProfileBindingLookup, agent_id: str
) -> AgentProfileBinding:
    if callable(lookup):
        return lookup(agent_id)
    binding = lookup.get(agent_id)
    if binding is None:
        raise RuntimeRefused(
            f"No AgentProfileBinding configured for agent '{agent_id}'"
        )
    return binding


def _read_agent_state(store: Path, company_id: str, agent_id: str) -> tuple[str, str]:
    with closing(open_reader(store)) as db, read_tx(db):
        row = db.execute(
            "SELECT execution_profile FROM agent WHERE company_id = ? AND id = ?",
            (company_id, agent_id),
        ).fetchone()
        if row is None:
            raise RuntimeRefused(f"Agent '{agent_id}' not found in company")
        grant_row = db.execute(
            """SELECT scope FROM standing_grant
               WHERE agent_id = ? AND lower(trim(scope)) != 'revoked'
               ORDER BY id LIMIT 1""",
            (agent_id,),
        ).fetchone()
        grant_scope = str(grant_row[0]) if grant_row is not None else "paper"
        return str(row[0]), grant_scope


def _read_tiers(store: Path, company_id: str) -> dict[str, Any]:
    with closing(open_reader(store)) as db, read_tx(db):
        return read_execution_tiers_snapshot(db, company_id)


def _read_company_status(store: Path, company_id: str) -> dict[str, Any]:
    with closing(open_reader(store)) as db, read_tx(db):
        agent_count = int(
            db.execute(
                "SELECT COUNT(*) FROM agent WHERE company_id = ?", (company_id,)
            ).fetchone()[0]
        )
        restrictions = [
            {"restriction_id": str(r[0]), "scope_ref": str(r[1]), "reason": str(r[2])}
            for r in db.execute(
                """SELECT restriction_id, scope_ref, reason
                   FROM restriction_cause
                   WHERE company_id = ? AND active = 1
                   ORDER BY restriction_id""",
                (company_id,),
            ).fetchall()
        ]
        due_work = int(
            db.execute(
                """SELECT COUNT(*) FROM work_items
                   WHERE company_id = ? AND state IN ('due', 'running', 'waiting')""",
                (company_id,),
            ).fetchone()[0]
        )
    return {
        "company_id": company_id,
        "agent_count": agent_count,
        "active_restrictions": restrictions,
        "open_work_items": due_work,
    }


def _no_company_risk_restriction(
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


def sync_board_triggers(
    writer: WriterLease,
    company_id: str,
    *,
    now_ns: int | None = None,
) -> int:
    """Idempotently enqueue durable work items for inbound Board messages and approved decisions."""
    writer.verify_for(writer.store, company_id)
    timestamp = time_ns() if now_ns is None else now_ns
    created_count = 0
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        founder_row = db.execute(
            "SELECT founder_id FROM founding_receipt WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        if founder_row is None:
            return 0
        founder_id = str(founder_row[0])

        # 1. Unacknowledged Board -> CEO messages trigger ceo.respond_message.
        msg_rows = db.execute(
            """SELECT message_id, sender_id, body, context_kind, context_id, context_label
               FROM ceo_message
               WHERE company_id = ? AND sender_kind = 'board' AND delivery_state = 'recorded'
               ORDER BY recorded_at_ns ASC, message_id ASC""",
            (company_id,),
        ).fetchall()
        for mid, sender_id, body, ctx_kind, ctx_id, ctx_label in msg_rows:
            _, reused = create_work_item(
                db,
                company_id=company_id,
                owner_agent_id=founder_id,
                duty_kind=DUTY_CEO_RESPOND_MESSAGE,
                source_kind="ceo_message",
                source_id=str(mid),
                dedupe_key=f"ceo-reply:{mid}",
                payload={
                    "message_id": str(mid),
                    "sender_id": str(sender_id),
                    "body": str(body),
                    "context_kind": None if ctx_kind is None else str(ctx_kind),
                    "context_id": None if ctx_id is None else str(ctx_id),
                    "context_label": None if ctx_label is None else str(ctx_label),
                },
                actor_id="board_trigger_sync",
                now_ns=timestamp,
            )
            if not reused:
                created_count += 1

        # 2. Approved actionable Board decisions trigger board.apply_decision.
        dec_rows = db.execute(
            """SELECT question_id, kind, revision, impact_json
               FROM decision_question
               WHERE company_id = ? AND state = 'applied' AND resolution_choice = 'approve'
               ORDER BY resolved_at_ns ASC, question_id ASC""",
            (company_id,),
        ).fetchall()
        for qid, kind, revision, impact_json in dec_rows:
            try:
                impact = json.loads(str(impact_json))
            except json.JSONDecodeError:
                continue
            if not isinstance(impact, dict) or not isinstance(
                impact.get("action"), str
            ):
                continue
            if not impact["action"].strip():
                continue
            _, reused = create_work_item(
                db,
                company_id=company_id,
                owner_agent_id=founder_id,
                duty_kind=DUTY_BOARD_APPLY_DECISION,
                source_kind="decision_question",
                source_id=str(qid),
                dedupe_key=f"decision-apply:{qid}:rev:{int(revision)}",
                payload={
                    "question_id": str(qid),
                    "kind": str(kind),
                    "revision": int(revision),
                    "impact": impact,
                },
                actor_id="board_trigger_sync",
                now_ns=timestamp,
            )
            if not reused:
                created_count += 1

    return created_count


def build_production_duty_registry(
    *,
    adapter_registry: AgentAdapterRegistry,
    profile_bindings: ProfileBindingLookup,
    default_budget_ceiling_minor: int = 10_000,
    extra_handlers: Mapping[str, Callable[[DutyContext], DutyOutcome]] | None = None,
) -> DutyRegistry:
    """Build the production DutyRegistry connecting live LLM adapters and domain owners."""
    registry = DutyRegistry()

    def _run_ceo_respond_message(ctx: DutyContext) -> DutyOutcome:
        payload = json.loads(ctx.work_item.payload_json)
        message_id = str(payload.get("message_id") or ctx.work_item.source_id)
        board_body = str(payload.get("body") or "")
        ctx_kind = payload.get("context_kind")
        ctx_id = payload.get("context_id")
        ctx_label = payload.get("context_label")

        agent_id = ctx.work_item.owner_agent_id
        persisted_profile, grant_scope = _read_agent_state(
            ctx.writer.store, ctx.company_id, agent_id
        )
        profile = _resolve_profile(profile_bindings, agent_id)

        with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
            history_rows = db.execute(
                """SELECT sender_kind, body FROM ceo_message
                   WHERE company_id = ?
                   ORDER BY recorded_at_ns DESC LIMIT 6""",
                (ctx.company_id,),
            ).fetchall()
        history_notes = tuple(
            f"{str(kind)}: {str(text)[:200]}" for kind, text in reversed(history_rows)
        )

        desk = desk_directory(ctx.writer.store, agent_id)
        desk.mkdir(parents=True, exist_ok=True)
        context_pack = hydrate_shift_context(
            company_id=ctx.company_id,
            agent_id=agent_id,
            run_id=ctx.run.run_id,
            fencing_token=ctx.run.fencing_token,
            grant_scope=grant_scope,
            mandate_summary="Founder-CEO direct advisory response to the Board.",
            history_notes=history_notes,
        )
        broker = OuterToolBroker(
            company_id=ctx.company_id,
            agent_id=agent_id,
            run_id=ctx.run.run_id,
            fencing_token=ctx.run.fencing_token,
            handlers={
                "read_company_status": lambda _args: _read_company_status(
                    ctx.writer.store, ctx.company_id
                ),
                "read_execution_tiers": lambda _args: _read_tiers(
                    ctx.writer.store, ctx.company_id
                ),
            },
        )
        budget_ceiling = (
            ctx.run.budget_ceiling_minor
            if ctx.run.budget_ceiling_minor > 0
            else default_budget_ceiling_minor
        )
        shift_req = ShiftRequest(
            company_id=ctx.company_id,
            agent_id=agent_id,
            run_id=ctx.run.run_id,
            turn_id="turn-1",
            fencing_token=ctx.run.fencing_token,
            instruction=f"Respond concisely as Founder-CEO to Board message ({message_id}): {board_body}",
            context=context_pack,
            tools=_CEO_READONLY_TOOLS,
            budget_ceiling_minor_units=budget_ceiling,
            desk_path=desk,
            store_path=ctx.writer.store,
        )
        result = adapter_registry.execute_shift(
            persisted_profile_id=persisted_profile,
            profile=profile,
            request=shift_req,
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
        reply_text = (
            result.output_text.strip()
            or "Acknowledged by Founder-CEO; reviewing company state."
        )
        reply_id = record_ceo_reply(
            ctx.writer,
            body=reply_text,
            context_kind=ctx_kind if isinstance(ctx_kind, str) else None,
            context_id=ctx_id if isinstance(ctx_id, str) else None,
            context_label=ctx_label if isinstance(ctx_label, str) else None,
            now_ns=ctx.now_ns,
        )
        mark_ceo_message_delivered(ctx.writer, message_id=message_id, now_ns=ctx.now_ns)
        return DutyOutcome(
            disposition="completed",
            result={
                "inbound_message_id": message_id,
                "reply_message_id": reply_id,
                "provider": result.provider,
                "model": result.model,
                "cost_minor_units": settlement.cost_minor_units,
                "journal_entry_id": settlement.journal_entry_id,
            },
        )

    def _run_board_apply_decision(ctx: DutyContext) -> DutyOutcome:
        payload = json.loads(ctx.work_item.payload_json)
        question_id = str(payload.get("question_id") or ctx.work_item.source_id)
        impact = payload.get("impact")
        if not isinstance(impact, dict):
            impact = {}
        action = str(impact.get("action") or "")
        summary = "no_op"

        if action == "update_agent_budget":
            target_agent = str(impact["agent_id"])
            new_budget = int(impact["monthly_budget_minor"])
            with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
                row = db.execute(
                    """SELECT display_name, role_label, avatar_variant
                       FROM agent_profile
                       WHERE company_id = ? AND agent_id = ?""",
                    (ctx.company_id, target_agent),
                ).fetchone()
            display_name = (
                str(row[0])
                if row is not None
                else str(impact.get("display_name") or target_agent)
            )
            role_label = (
                str(row[1])
                if row is not None
                else str(impact.get("role_label") or "Quant")
            )
            avatar_variant = str(row[2]) if row is not None else "wide-oval-top"
            upsert_agent_profile(
                ctx.writer,
                agent_id=target_agent,
                display_name=display_name,
                role_label=role_label,
                avatar_variant=avatar_variant,
                monthly_budget_minor=new_budget,
                now_ns=ctx.now_ns,
            )
            summary = f"updated monthly_budget_minor={new_budget} for {target_agent}"
        elif action == "open_agent_sandbox":
            target_agent = str(impact["agent_id"])
            binding = open_agent_sandbox(
                ctx.writer,
                agent_id=target_agent,
                account_id=str(impact["account_id"]),
                currency=str(impact.get("currency", "USD")),
                starting_minor_units=int(
                    impact.get("starting_minor_units", 10_000_000)
                ),
                fee_bps=int(impact.get("fee_bps", 4)),
                slippage_bps=int(impact.get("slippage_bps", 2)),
            )
            summary = f"opened sandbox {binding.binding_id} for {target_agent}"
        elif action == "migrate_paper_to_live":
            try:
                mig = execute_paper_to_live_migration(
                    ctx.writer,
                    source_paper_binding_id=str(impact["source_paper_binding_id"]),
                    target_live_binding_id=str(impact["target_live_binding_id"]),
                    qualification_receipt_id=str(impact["qualification_receipt_id"]),
                    migration_mode=str(impact.get("migration_mode") or "clean_start"),
                    source_paper_disposition=str(
                        impact.get("source_paper_disposition") or "keep_shadow"
                    ),
                    reason=str(
                        impact.get("reason")
                        or f"Board approved live migration decision {question_id}"
                    ),
                    question_id=question_id,
                    actor_id="board_apply_decision",
                    now_ns=ctx.now_ns,
                )
                summary = (
                    f"migrated paper book {mig['source_paper_binding_id']} -> "
                    f"live {mig['target_live_binding_id']} "
                    f"(mode={mig['migration_mode']}, source={mig['source_book_final_role']}, "
                    f"positions_copied=0)"
                )
            except ExecutionTierRefused as exc:
                failure_summary = f"migration_refused: {exc}"
                record_decision_application_result(
                    ctx.writer,
                    question_id=question_id,
                    application_status="failed",
                    delivery_status="delivered",
                    effect_summary=failure_summary,
                    now_ns=ctx.now_ns,
                )
                return DutyOutcome(
                    disposition="completed",
                    result={
                        "question_id": question_id,
                        "action": action,
                        "application_status": "failed",
                        "effect_summary": failure_summary,
                    },
                )
        else:
            summary = f"recorded approved advisory decision ({action or 'advisory'})"

        record_decision_application_result(
            ctx.writer,
            question_id=question_id,
            application_status="applied",
            delivery_status="delivered",
            effect_summary=summary,
            now_ns=ctx.now_ns,
        )
        return DutyOutcome(
            disposition="completed",
            result={
                "question_id": question_id,
                "action": action,
                "effect_summary": summary,
            },
        )

    def _run_quant_agent_sandbox(ctx: DutyContext) -> DutyOutcome:
        payload = json.loads(ctx.work_item.payload_json)
        instruction = str(
            payload.get("instruction")
            or "Run your isolated Tier 1 sandbox strategy experiment and summarize results."
        )
        agent_id = ctx.work_item.owner_agent_id
        persisted_profile, grant_scope = _read_agent_state(
            ctx.writer.store, ctx.company_id, agent_id
        )
        profile = _resolve_profile(profile_bindings, agent_id)
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
            else default_budget_ceiling_minor
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
        result = adapter_registry.execute_shift(
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
        persisted_profile, grant_scope = _read_agent_state(
            ctx.writer.store, ctx.company_id, agent_id
        )
        profile = _resolve_profile(profile_bindings, agent_id)
        broker = OuterToolBroker(
            company_id=ctx.company_id,
            agent_id=agent_id,
            run_id=ctx.run.run_id,
            fencing_token=ctx.run.fencing_token,
            handlers={
                "read_company_status": lambda _args: _read_company_status(
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
            else default_budget_ceiling_minor
        )
        req = ShiftRequest(
            company_id=ctx.company_id,
            agent_id=agent_id,
            run_id=ctx.run.run_id,
            turn_id="turn-1",
            fencing_token=ctx.run.fencing_token,
            instruction=instruction,
            context=context_pack,
            tools=_CEO_READONLY_TOOLS,
            budget_ceiling_minor_units=budget_ceiling,
            desk_path=desk,
            store_path=ctx.writer.store,
        )
        result = adapter_registry.execute_shift(
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

    registry.register(
        DutyDescriptor(
            duty_kind=DUTY_CEO_RESPOND_MESSAGE,
            execute=_run_ceo_respond_message,
        )
    )
    registry.register(
        DutyDescriptor(
            duty_kind=DUTY_BOARD_APPLY_DECISION,
            execute=_run_board_apply_decision,
        )
    )
    registry.register(
        DutyDescriptor(
            duty_kind=DUTY_QUANT_AGENT_SANDBOX,
            execute=_run_quant_agent_sandbox,
        )
    )
    registry.register(
        DutyDescriptor(
            duty_kind=DUTY_QUANT_PAPER_DECISION,
            execute=_run_quant_paper_decision,
            precondition=_no_company_risk_restriction,
        )
    )
    registry.register(
        DutyDescriptor(
            duty_kind=DUTY_QUANT_RECONCILE,
            execute=_run_quant_reconcile,
        )
    )
    for duty_kind, fn in (extra_handlers or {}).items():
        registry.register(DutyDescriptor(duty_kind=duty_kind, execute=fn))
    return registry
