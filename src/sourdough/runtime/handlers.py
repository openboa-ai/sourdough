"""Production DutyRegistry handlers wiring multi-provider Agents, Board triggers, and domain plugins."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from time import time_ns
from typing import TYPE_CHECKING, Any

from ..agent_runtime.protocol import (
    AgentProfileBinding,
    ToolHandler,
    ToolSpec,
)

if TYPE_CHECKING:
    from ..agent_runtime.registry import AgentAdapterRegistry
from ..board_control import (
    ALLOWED_AVATAR_VARIANTS,
    BoardControlRefused,
    mark_ceo_message_delivered,
    record_ceo_reply,
    record_decision_application_result,
    upsert_agent_profile,
)
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..writer_lease import WriterLease
from .domains import (
    DecisionActionContext,
    DecisionActionRegistry,
    DomainActionRefused,
    DomainPlugin,
    DutyRuntimeServices,
    ProfileBindingLookup,
    ToolContribution,
    ToolQueryContext,
    merge_tool_contributions,
)
from .registry import (
    DutyContext,
    DutyDescriptor,
    DutyOutcome,
    DutyRegistry,
    RuntimeRefused,
)
from .shift_runner import ActiveMethodsProvider, ShiftRunner
from .work_kernel import create_work_item

DUTY_CEO_RESPOND_MESSAGE = "ceo.respond_message"
DUTY_BOARD_APPLY_DECISION = "board.apply_decision"

_CEO_READONLY_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read_company_status",
        description="Read current company agent count, active restrictions, and open work summary.",
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
        if grant_row is None or not str(grant_row[0]).strip():
            raise RuntimeRefused(f"Agent '{agent_id}' has no active standing grant")
        return str(row[0]), str(grant_row[0])


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
            if not isinstance(impact, dict):
                continue
            raw_action = impact.get("action")
            if raw_action is None:
                continue
            if isinstance(raw_action, str) and not raw_action.strip():
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
    domain_plugins: Sequence[DomainPlugin] = (),
    extra_handlers: Mapping[str, Callable[[DutyContext], DutyOutcome]] | None = None,
    active_methods_provider: ActiveMethodsProvider | None = None,
) -> DutyRegistry:
    """Build the production DutyRegistry connecting live LLM adapters and domain plugins."""
    registry = DutyRegistry()
    decision_actions = DecisionActionRegistry()
    for plugin in domain_plugins:
        for action_name, handler in plugin.contribute_decision_actions().items():
            decision_actions.register(action_name, handler)

    def _contribute_tools(tool_ctx: ToolQueryContext) -> ToolContribution:
        contributions = tuple(
            plugin.contribute_tools(tool_ctx) for plugin in domain_plugins
        )
        specs, handlers, recoverable = merge_tool_contributions((), {}, contributions)
        return ToolContribution(
            specs=specs,
            handlers=handlers,
            recoverable_exceptions=recoverable,
        )

    shift_runner = ShiftRunner(
        adapter_registry=adapter_registry,
        profile_bindings=profile_bindings,
        default_budget_ceiling_minor=default_budget_ceiling_minor,
        read_agent_state=_read_agent_state,
        resolve_profile=_resolve_profile,
        contribute_tools=_contribute_tools,
        active_methods_provider=active_methods_provider,
    )

    def _run_ceo_respond_message(ctx: DutyContext) -> DutyOutcome:
        payload = json.loads(ctx.work_item.payload_json)
        message_id = str(payload.get("message_id") or ctx.work_item.source_id)
        board_body = str(payload.get("body") or "")
        ctx_kind = payload.get("context_kind")
        ctx_id = payload.get("context_id")
        ctx_label = payload.get("context_label")

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

        base_handlers: dict[str, ToolHandler] = {
            "read_company_status": lambda _args: _read_company_status(
                ctx.writer.store, ctx.company_id
            ),
        }
        run_res = shift_runner.run(
            ctx,
            mandate_summary="Founder-CEO direct advisory response to the Board.",
            instruction=f"Respond concisely as Founder-CEO to Board message ({message_id}): {board_body}",
            base_tools=_CEO_READONLY_TOOLS,
            base_handlers=base_handlers,
            history_notes=history_notes,
            include_core_desk_tools=True,
            include_plugin_tools=True,
        )
        result = run_res.shift_result
        settlement = run_res.settlement
        reply_text = (
            result.output_text.strip()
            or "Acknowledged by Founder-CEO; reviewing company state."
        )
        with ctx.write_lock_ctx():
            reply_id = record_ceo_reply(
                ctx.writer,
                body=reply_text,
                context_kind=ctx_kind if isinstance(ctx_kind, str) else None,
                context_id=ctx_id if isinstance(ctx_id, str) else None,
                context_label=ctx_label if isinstance(ctx_label, str) else None,
                now_ns=ctx.now_ns,
            )
            mark_ceo_message_delivered(
                ctx.writer, message_id=message_id, now_ns=ctx.now_ns
            )
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
        kind = str(payload.get("kind") or "")
        revision = int(payload.get("revision") or 1)
        impact = payload.get("impact")
        if not isinstance(impact, dict):
            impact = {}
        raw_action = impact.get("action")
        if raw_action is None:
            action = ""
        elif isinstance(raw_action, str):
            action = raw_action.strip()
        else:
            action = repr(raw_action)
        summary = "no_op"

        def _record_failure(failure_summary: str) -> DutyOutcome:
            with ctx.write_lock_ctx():
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

        if action == "update_agent_budget":
            try:
                raw_agent = impact["agent_id"]
                if not isinstance(raw_agent, str) or not raw_agent.strip():
                    raise BoardControlRefused("Invalid or missing agent_id")
                target_agent = raw_agent.strip()
                raw_budget = impact["monthly_budget_minor"]
                if type(raw_budget) is not int:
                    raise BoardControlRefused("Invalid monthly_budget_minor")
                new_budget = raw_budget
                raw_display = impact.get("display_name")
                if raw_display is not None and not isinstance(raw_display, str):
                    raise BoardControlRefused("Invalid display_name")
                raw_role = impact.get("role_label")
                if raw_role is not None and not isinstance(raw_role, str):
                    raise BoardControlRefused("Invalid role_label")
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
                    else (
                        raw_display.strip()
                        if isinstance(raw_display, str) and raw_display.strip()
                        else target_agent
                    )
                )
                role_label = (
                    str(row[1])
                    if row is not None
                    else (
                        raw_role.strip()
                        if isinstance(raw_role, str) and raw_role.strip()
                        else "Agent"
                    )
                )
                raw_variant = (
                    str(row[2]).strip()
                    if row is not None and row[2] is not None
                    else ""
                )
                avatar_variant = (
                    raw_variant
                    if raw_variant in ALLOWED_AVATAR_VARIANTS
                    else "wide-oval-top"
                )
                with ctx.write_lock_ctx():
                    upsert_agent_profile(
                        ctx.writer,
                        agent_id=target_agent,
                        display_name=display_name,
                        role_label=role_label,
                        avatar_variant=avatar_variant,
                        monthly_budget_minor=new_budget,
                        now_ns=ctx.now_ns,
                    )
                summary = (
                    f"updated monthly_budget_minor={new_budget} for {target_agent}"
                )
            except (
                BoardControlRefused,
                DomainActionRefused,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                return _record_failure(f"{action}_refused: {exc}")
        else:
            domain_handler = decision_actions.get(action)
            if domain_handler is not None:
                try:
                    with ctx.write_lock_ctx():
                        summary = domain_handler(
                            DecisionActionContext(
                                writer=ctx.writer,
                                company_id=ctx.company_id,
                                question_id=question_id,
                                kind=kind,
                                revision=revision,
                                impact=impact,
                                now_ns=ctx.now_ns,
                            )
                        )
                except (DomainActionRefused, BoardControlRefused) as exc:
                    return _record_failure(f"{action}_refused: {exc}")
            elif action in ("", "advisory"):
                summary = (
                    f"recorded approved advisory decision ({action or 'advisory'})"
                )
            else:
                return _record_failure(f"unsupported_decision_action: {action}")

        with ctx.write_lock_ctx():
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
    services = DutyRuntimeServices(
        adapter_registry=adapter_registry,
        profile_bindings=profile_bindings,
        default_budget_ceiling_minor=default_budget_ceiling_minor,
        read_agent_state=_read_agent_state,
        resolve_profile=_resolve_profile,
        read_company_status=_read_company_status,
        contribute_tools=_contribute_tools,
        shift_runner=shift_runner,
    )
    for plugin in domain_plugins:
        for descriptor in plugin.contribute_duties(services):
            registry.register(descriptor)
    for duty_kind, fn in (extra_handlers or {}).items():
        registry.register(DutyDescriptor(duty_kind=duty_kind, execute=fn))
    return registry
