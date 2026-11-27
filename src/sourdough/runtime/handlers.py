"""Production DutyRegistry handlers wiring multi-provider Agents, Board triggers, and domain plugins."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from hashlib import sha256
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
from ..company_assets import project_membership, shared_asset_for
from ..duty_run import DutyRefused, WorkRunClaim, work_run_claim_from_context
from ..method_learning import (
    audit_episodes_for_dreaming,
    compute_active_snapshot_id,
    default_active_methods_provider,
)
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..writer_lease import WriterLease
from .collab_tools import build_core_collab_tool_contribution
from .proactive_tools import build_core_proactive_tool_contribution
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
from .shift_runner import (
    ActiveMethodsProvider,
    ShiftRunner,
    reconcile_shift_episode_outcome,
)
from .work_kernel import (
    _PEER_REQUEST_COLUMNS,
    PeerRequest,
    WorkItem,
    assert_run_fencing,
    create_work_item,
    mark_peer_request_resumed,
    read_peer_request_outcome,
    record_peer_request_read,
    resolve_peer_request,
)

DUTY_CEO_RESPOND_MESSAGE = "ceo.respond_message"
DUTY_BOARD_APPLY_DECISION = "board.apply_decision"
DUTY_WORK_PEER_RESPONSE = "work.peer_response"
DUTY_WORK_ASSESSMENT_REVIEW = "work.assessment_review"
DUTY_WORK_HANDOFF_FOLLOWUP = "work.handoff_followup"
DUTY_WORK_DISCOVERY_REVIEW = "work.discovery_review"
DUTY_WORK_DREAMING = "work.dreaming"

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


def build_store_profile_lookup(
    store: Path,
    company_id: str,
    *,
    overrides: Mapping[str, AgentProfileBinding] | None = None,
) -> Callable[[str], AgentProfileBinding]:
    """Resolve AgentProfileBinding dynamically from overrides or persisted agent_runtime_binding."""
    override_map = dict(overrides or {})

    def _lookup(agent_id: str) -> AgentProfileBinding:
        if agent_id in override_map:
            return override_map[agent_id]
        with closing(open_reader(store)) as db, read_tx(db):
            row = db.execute(
                """SELECT provider, model, revision, sandbox_backend,
                          credential_handle_ref, profile_id
                   FROM agent_runtime_binding
                   WHERE company_id = ? AND agent_id = ?""",
                (company_id, agent_id),
            ).fetchone()
        if row is None:
            raise RuntimeRefused(
                f"No AgentProfileBinding configured for agent '{agent_id}'"
            )
        binding = AgentProfileBinding(
            provider=str(row[0]),
            model=str(row[1]),
            revision=str(row[2]),
            sandbox_backend=str(row[3]),
            credential_handle_ref=None if row[4] is None else str(row[4]),
        )
        binding.verify_matches_agent(str(row[5]))
        return binding

    return _lookup


def build_proactive_tool_handlers(
    writer: WriterLease,
    claim: WorkRunClaim,
    *,
    tx_lock: threading.RLock | None = None,
    domain_plugins: Sequence[DomainPlugin] = (),
) -> dict[str, ToolHandler]:
    """Return fenced proactive condition-scheduling and work_decision tool handlers."""
    return dict(
        build_core_proactive_tool_contribution(
            writer=writer,
            claim=claim,
            tx_lock=tx_lock,
            domain_plugins=domain_plugins,
        ).handlers
    )


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

    eff_active_methods_provider = (
        active_methods_provider
        if active_methods_provider is not None
        else default_active_methods_provider
    )
    shift_runner = ShiftRunner(
        adapter_registry=adapter_registry,
        profile_bindings=profile_bindings,
        default_budget_ceiling_minor=default_budget_ceiling_minor,
        read_agent_state=_read_agent_state,
        resolve_profile=_resolve_profile,
        contribute_tools=_contribute_tools,
        active_methods_provider=eff_active_methods_provider,
    )

    def _run_ceo_respond_message(ctx: DutyContext) -> DutyOutcome:
        payload = json.loads(ctx.work_item.payload_json)
        message_id = str(payload.get("message_id") or ctx.work_item.source_id)
        board_body = str(payload.get("body") or "")
        ctx_kind = payload.get("context_kind")
        ctx_id = payload.get("context_id")
        ctx_label = payload.get("context_label")
        reply_command_id = f"ceo-reply:{ctx.work_item.work_item_id}:{message_id}"

        with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
            prior_reply = db.execute(
                """SELECT message_id FROM ceo_message
                   WHERE company_id = ? AND command_id = ? AND sender_kind = 'ceo'
                   LIMIT 1""",
                (ctx.company_id, reply_command_id),
            ).fetchone()
            history_rows = db.execute(
                """SELECT sender_kind, body FROM ceo_message
                   WHERE company_id = ?
                   ORDER BY recorded_at_ns DESC LIMIT 6""",
                (ctx.company_id,),
            ).fetchall()
        if prior_reply is not None:
            with ctx.write_lock_ctx():
                mark_ceo_message_delivered(
                    ctx.writer, message_id=message_id, now_ns=ctx.now_ns
                )
            return DutyOutcome(
                disposition="completed",
                result={
                    "inbound_message_id": message_id,
                    "reply_message_id": str(prior_reply[0]),
                    "recovered_reply": True,
                    "cost_minor_units": 0,
                },
            )
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
                command_id=reply_command_id,
                inbound_message_id=message_id,
                now_ns=ctx.now_ns,
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
                "active_snapshot_id": run_res.active_snapshot_id,
                "causal_method_trace": run_res.causal_method_trace,
                "stable_prefix_sha256": run_res.stable_prefix_sha256,
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

    def _hydrate_resolved_child_peer_outcome(
        db: Any,
        *,
        company_id: str,
        work_item_id: str,
        run_id: str,
        agent_id: str,
        receipts: list[dict[str, Any]],
        notes: list[str],
    ) -> None:
        unresolved_resume = db.execute(
            """SELECT request_id
               FROM work_peer_requests
               WHERE company_id = ? AND requester_work_item_id = ?
                 AND state != 'open' AND resumed_run_id IS NULL
               ORDER BY rowid DESC LIMIT 1""",
            (company_id, work_item_id),
        ).fetchone()
        if unresolved_resume is not None:
            mark_peer_request_resumed(
                db,
                company_id=company_id,
                request_id=str(unresolved_resume[0]),
                resumed_run_id=run_id,
            )
        peer_outcome = read_peer_request_outcome(
            db,
            company_id=company_id,
            requester_work_item_id=work_item_id,
            actor_id=agent_id,
        )
        if peer_outcome is not None:
            receipts.append({"kind": "peer_outcome", **peer_outcome})
            notes.append(
                f"Peer outcome ({peer_outcome['request_key']}): "
                f"{peer_outcome['outcome']} -> {peer_outcome['answer']}"
            )

    def _run_work_peer_response(ctx: DutyContext) -> DutyOutcome:
        agent_id = ctx.work_item.owner_agent_id
        accessible_evidence: list[dict[str, str]] = []
        evidence_notes: list[str] = []
        child_receipts: list[dict[str, Any]] = []
        with (
            ctx.write_lock_ctx(),
            closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
            write_tx(db),
        ):
            row = db.execute(
                f"""SELECT {_PEER_REQUEST_COLUMNS}
                    FROM work_peer_requests
                    WHERE company_id = ? AND response_work_item_id = ?""",
                (ctx.company_id, ctx.work_item.work_item_id),
            ).fetchone()
            if row is None:
                raise RuntimeRefused(
                    f"Peer response item '{ctx.work_item.work_item_id}' has no linked peer request"
                )
            peer = PeerRequest(*row)
            if peer.state != "open":
                return DutyOutcome(
                    disposition="completed",
                    result={"request_id": peer.request_id, "outcome": peer.state},
                )
            if ctx.now_ns >= peer.deadline_ns:
                resolve_peer_request(
                    db,
                    company_id=ctx.company_id,
                    request_id=peer.request_id,
                    actor_id="work_scheduler",
                    resolution_state="expired",
                    response_payload={"reason": "deadline_elapsed"},
                    caller_response_run_id=ctx.run.run_id,
                    now_ns=ctx.now_ns,
                )
                return DutyOutcome(
                    disposition="completed",
                    result={"request_id": peer.request_id, "outcome": "expired"},
                )
            record_peer_request_read(
                db,
                company_id=ctx.company_id,
                request_id=peer.request_id,
                actor_id=agent_id,
                response_run_id=ctx.run.run_id,
                response_fencing_token=ctx.run.fencing_token,
                now_ns=ctx.now_ns,
            )
            _hydrate_resolved_child_peer_outcome(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
                agent_id=agent_id,
                receipts=child_receipts,
                notes=evidence_notes,
            )
            raw_refs = json.loads(peer.evidence_refs_json)
            if isinstance(raw_refs, list):
                for ref in raw_refs:
                    if not isinstance(ref, str):
                        continue
                    if ref.startswith("shared_publication:"):
                        asset_id = ref.removeprefix("shared_publication:").split(
                            "@", 1
                        )[0]
                        try:
                            asset = shared_asset_for(db, agent_id, asset_id)
                        except DutyRefused:
                            continue
                        accessible_evidence.append(
                            {
                                "asset_id": asset.asset_id,
                                "project_id": asset.project_id,
                                "author_id": asset.author_id,
                                "content": asset.content,
                            }
                        )
                        evidence_notes.append(
                            f"Shared evidence {asset.asset_id} (project {asset.project_id}): {asset.content[:500]}"
                        )
            claim = work_run_claim_from_context(db, ctx, purpose="peer_response")

        peer_receipt: dict[str, Any] = {
            "kind": "peer_request",
            "request_id": peer.request_id,
            "request_key": peer.request_key,
            "project_id": peer.project_id,
            "requester_agent_id": peer.requester_agent_id,
            "question": peer.question,
            "expected_decision": peer.expected_decision,
            "deadline_ns": peer.deadline_ns,
            "evidence": accessible_evidence,
        }
        collab = build_core_collab_tool_contribution(
            writer=ctx.writer,
            claim=claim,
            purpose=claim.purpose,
            tx_lock=ctx.tx_lock,
            include_messaging=peer.project_id is not None,
            include_peer_request=True,
            include_peer_respond=True,
            include_handoffs=False,
            include_roster=True,
            scoped_project_id=peer.project_id,
        )
        run_res = shift_runner.run(
            ctx,
            mandate_summary=(
                "Respond to a scoped peer decision request within your standing grant. "
                "Do not take over the requester's work."
            ),
            instruction=(
                f"Review peer request ({peer.request_id}) from {peer.requester_agent_id}: "
                f"{peer.question} (expected decision: {peer.expected_decision}). "
                "Use respond_peer_request to answer or decline."
            ),
            purpose="peer_response",
            base_tools=collab.specs,
            base_handlers=collab.handlers,
            history_notes=tuple(evidence_notes),
            open_receipts=(peer_receipt, *child_receipts),
            include_core_desk_tools=True,
            include_plugin_tools=False,
            extra_recoverable_exceptions=collab.recoverable_exceptions,
        )
        if run_res.run_status == "waiting":
            return DutyOutcome(
                disposition="waiting",
                wait_until_ns=run_res.wait_until_ns,
                wait_event_key=run_res.wait_event_key,
                wait_reason="waiting_for_peer_response",
                result={"request_id": peer.request_id, "waiting": True},
            )
        blocked_no_peer = False
        with (
            ctx.write_lock_ctx(),
            closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
            write_tx(db),
        ):
            post_row = db.execute(
                """SELECT state, deadline_ns
                   FROM work_peer_requests
                   WHERE company_id = ? AND request_id = ?""",
                (ctx.company_id, peer.request_id),
            ).fetchone()
            post_state = str(post_row[0]) if post_row is not None else "open"
            post_deadline = (
                int(post_row[1]) if post_row is not None else peer.deadline_ns
            )
            if post_state == "open":
                if ctx.now_ns >= post_deadline:
                    resolve_peer_request(
                        db,
                        company_id=ctx.company_id,
                        request_id=peer.request_id,
                        actor_id="work_scheduler",
                        resolution_state="expired",
                        response_payload={"reason": "deadline_elapsed"},
                        caller_response_run_id=ctx.run.run_id,
                        now_ns=ctx.now_ns,
                    )
                    return DutyOutcome(
                        disposition="completed",
                        result={"request_id": peer.request_id, "outcome": "expired"},
                    )
                blocked_no_peer = True
        if blocked_no_peer:
            reconciled_trace = reconcile_shift_episode_outcome(
                ctx,
                outcome_status="blocked",
                reason="no_peer_disposition",
                causal_method_trace=run_res.causal_method_trace,
            )
            return DutyOutcome(
                disposition="blocked",
                result={
                    "request_id": peer.request_id,
                    "reason": "no_peer_disposition",
                    "causal_method_trace": reconciled_trace,
                },
            )
        return DutyOutcome(
            disposition="completed",
            result={
                "request_id": peer.request_id,
                "outcome": post_state,
                "output_text": run_res.shift_result.output_text.strip(),
                "cost_minor_units": run_res.settlement.cost_minor_units,
                "journal_entry_id": run_res.settlement.journal_entry_id,
                "active_snapshot_id": run_res.active_snapshot_id,
                "causal_method_trace": run_res.causal_method_trace,
                "stable_prefix_sha256": run_res.stable_prefix_sha256,
            },
        )

    def _run_collaborative_review(
        ctx: DutyContext,
        *,
        purpose: str,
        mandate_summary: str,
        default_instruction: str,
    ) -> DutyOutcome:
        agent_id = ctx.work_item.owner_agent_id
        receipts: list[dict[str, Any]] = []
        notes: list[str] = []
        with (
            ctx.write_lock_ctx(),
            closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
            write_tx(db),
        ):
            _hydrate_resolved_child_peer_outcome(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
                agent_id=agent_id,
                receipts=receipts,
                notes=notes,
            )
            handoff_row = db.execute(
                """SELECT offer_id, sender_id, asset_id, expected_result
                   FROM work_handoff
                   WHERE company_id = ? AND successor_work_item_id = ?
                     AND recipient_id = ? AND state = 'accepted'""",
                (ctx.company_id, ctx.work_item.work_item_id, agent_id),
            ).fetchone()
            if handoff_row is not None:
                offer_id, sender_id, asset_id, expected_result = handoff_row
                handoff_receipt: dict[str, Any] = {
                    "kind": "accepted_handoff",
                    "offer_id": str(offer_id),
                    "sender_id": str(sender_id),
                    "asset_id": str(asset_id),
                    "expected_result": str(expected_result),
                }
                try:
                    asset = shared_asset_for(db, agent_id, str(asset_id))
                    handoff_receipt["shared_content"] = asset.content
                    handoff_receipt["project_id"] = asset.project_id
                    notes.append(
                        f"Accepted handoff {offer_id} from {sender_id} "
                        f"(asset {asset_id}, expected: {expected_result}): {asset.content[:500]}"
                    )
                except DutyRefused:
                    notes.append(
                        f"Accepted handoff {offer_id} from {sender_id} "
                        f"(expected: {expected_result})"
                    )
                receipts.append(handoff_receipt)
            brief_row = db.execute(
                """SELECT project_id, deliverable
                   FROM project_review_brief
                   WHERE company_id = ? AND work_item_id = ? AND agent_id = ?""",
                (ctx.company_id, ctx.work_item.work_item_id, agent_id),
            ).fetchone()
            if brief_row is not None:
                proj_id, deliverable = str(brief_row[0]), str(brief_row[1])
                try:
                    _, members, _ = project_membership(db, proj_id)
                except DutyRefused:
                    members = ()
                if agent_id in members:
                    receipts.append(
                        {
                            "kind": "project_review_brief",
                            "project_id": proj_id,
                            "deliverable": deliverable,
                        }
                    )
                    notes.append(
                        f"Assigned company deliverable for project {proj_id}: {deliverable}"
                    )
            claim = work_run_claim_from_context(db, ctx, purpose=purpose)

        collab = build_core_collab_tool_contribution(
            writer=ctx.writer,
            claim=claim,
            purpose=claim.purpose,
            tx_lock=ctx.tx_lock,
            include_messaging=True,
            include_peer_request=True,
            include_peer_respond=False,
            include_handoffs=True,
            include_roster=True,
        )
        run_res = shift_runner.run(
            ctx,
            mandate_summary=mandate_summary,
            instruction=default_instruction,
            purpose=purpose,
            base_tools=collab.specs,
            base_handlers=collab.handlers,
            history_notes=tuple(notes),
            open_receipts=tuple(receipts),
            include_core_desk_tools=True,
            include_plugin_tools=True,
            extra_recoverable_exceptions=collab.recoverable_exceptions,
        )
        if run_res.run_status == "waiting":
            return DutyOutcome(
                disposition="waiting",
                wait_until_ns=run_res.wait_until_ns,
                wait_event_key=run_res.wait_event_key,
                wait_reason="waiting_for_peer_response",
                result={"waiting": True},
            )
        return DutyOutcome(
            disposition="completed",
            result={
                "output_text": run_res.shift_result.output_text.strip(),
                "provider": run_res.shift_result.provider,
                "model": run_res.shift_result.model,
                "cost_minor_units": run_res.settlement.cost_minor_units,
                "journal_entry_id": run_res.settlement.journal_entry_id,
                "active_snapshot_id": run_res.active_snapshot_id,
                "causal_method_trace": run_res.causal_method_trace,
                "stable_prefix_sha256": run_res.stable_prefix_sha256,
            },
        )

    def _run_work_assessment_review(ctx: DutyContext) -> DutyOutcome:
        return _run_collaborative_review(
            ctx,
            purpose="assessment_review",
            mandate_summary="Execute company assessment review within your standing grant.",
            default_instruction=(
                f"Execute assessment review work item {ctx.work_item.work_item_id} "
                "using your Desk, shared project assets, and collaboration tools."
            ),
        )

    def _run_work_handoff_followup(ctx: DutyContext) -> DutyOutcome:
        return _run_collaborative_review(
            ctx,
            purpose="handoff_followup",
            mandate_summary="Execute accepted handoff follow-up within your standing grant.",
            default_instruction=(
                f"Execute handoff follow-up work item {ctx.work_item.work_item_id} "
                "using the accepted handoff receipt, shared evidence, Desk, and collaboration tools."
            ),
        )

    def _find_committed_work_decision(
        db: Any,
        *,
        company_id: str,
        work_item_id: str,
        run_id: str,
    ) -> tuple[str, str, str | None, str] | None:
        row = db.execute(
            """SELECT d.decision_id, d.action_kind, d.next_condition_id, d.run_id
               FROM work_decision d
               JOIN work_runs r ON r.run_id = d.run_id
               WHERE d.company_id = ? AND d.work_item_id = ?
                 AND (d.run_id = ? OR r.status IN ('fenced', 'failed'))
               ORDER BY CASE WHEN d.run_id = ? THEN 0 ELSE 1 END,
                        d.decision_at_ns DESC, d.decision_id DESC
               LIMIT 1""",
            (company_id, work_item_id, run_id, run_id),
        ).fetchone()
        if row is None:
            return None
        return (
            str(row[0]),
            str(row[1]),
            None if row[2] is None else str(row[2]),
            str(row[3]),
        )

    def _run_work_discovery_review(ctx: DutyContext) -> DutyOutcome:
        agent_id = ctx.work_item.owner_agent_id
        receipts: list[dict[str, Any]] = []
        notes: list[str] = []
        try:
            payload = json.loads(ctx.work_item.payload_json)
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        with (
            ctx.write_lock_ctx(),
            closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
            write_tx(db),
        ):
            assert_run_fencing(
                db, ctx.run.run_id, ctx.run.fencing_token, now_ns=ctx.now_ns
            )
            committed = _find_committed_work_decision(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
            )
            if committed is not None:
                dec_id, act_kind, next_cid, dec_run_id = committed
                return DutyOutcome(
                    disposition="completed",
                    result={
                        "decision_id": dec_id,
                        "action_kind": act_kind,
                        "next_condition_id": next_cid,
                        "recovered_committed_decision": True,
                        "recovered_from_run_id": dec_run_id,
                        "cost_minor_units": 0,
                    },
                )
            _hydrate_resolved_child_peer_outcome(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
                agent_id=agent_id,
                receipts=receipts,
                notes=notes,
            )
            if payload:
                receipts.append({"kind": "discovery_trigger", **payload})
                notes.append(
                    f"Watched condition trigger ({payload.get('trigger_kind', 'manual')}): "
                    f"source={payload.get('source_identity')} rev={payload.get('observed_revision')}"
                )
            resp_id = payload.get("responsibility_id")
            if isinstance(resp_id, str) and resp_id.strip():
                resp_row = db.execute(
                    """SELECT remit, readable_scope_json
                       FROM work_responsibilities
                       WHERE company_id = ? AND responsibility_id = ?""",
                    (ctx.company_id, resp_id),
                ).fetchone()
                if resp_row is not None:
                    receipts.append(
                        {
                            "kind": "responsibility",
                            "responsibility_id": resp_id,
                            "remit": str(resp_row[0]),
                            "readable_scope_json": str(resp_row[1]),
                        }
                    )
                    notes.append(f"Standing responsibility remit: {resp_row[0]}")
            source_id = payload.get("source_identity")
            if isinstance(source_id, str) and source_id.strip():
                try:
                    asset = shared_asset_for(db, agent_id, source_id)
                except DutyRefused:
                    asset = None
                if asset is not None:
                    asset_rev = sha256(asset.content.encode("utf-8")).hexdigest()
                    receipts.append(
                        {
                            "kind": "shared_asset",
                            "asset_id": asset.asset_id,
                            "project_id": asset.project_id,
                            "author_id": asset.author_id,
                            "revision": asset_rev,
                            "content": asset.content,
                        }
                    )
                    notes.append(
                        f"Shared publication {asset.asset_id} (rev {asset_rev}): {asset.content[:500]}"
                    )
            claim = work_run_claim_from_context(db, ctx, purpose="discovery_review")

        resolved_methods, resolved_snapshot_id = eff_active_methods_provider(
            ctx.writer.store,
            ctx.company_id,
            agent_id,
            ctx.work_item.duty_kind,
        )
        if resolved_snapshot_id is None and resolved_methods:
            resolved_snapshot_id = compute_active_snapshot_id(resolved_methods)

        proactive = build_core_proactive_tool_contribution(
            writer=ctx.writer,
            claim=claim,
            tx_lock=ctx.tx_lock,
            domain_plugins=domain_plugins,
            active_snapshot_id=resolved_snapshot_id,
        )
        collab = build_core_collab_tool_contribution(
            writer=ctx.writer,
            claim=claim,
            purpose=claim.purpose,
            tx_lock=ctx.tx_lock,
            include_messaging=True,
            include_peer_request=True,
            include_peer_respond=False,
            include_handoffs=False,
            include_roster=True,
        )
        merged_specs, merged_handlers, merged_recoverable = merge_tool_contributions(
            (), {}, (proactive, collab)
        )
        run_res = shift_runner.run(
            ctx,
            mandate_summary=(
                "Review your standing responsibility conditions within your standing grant. "
                "Ground every work decision in verified DecisionEvidence via record_work_decision."
            ),
            instruction=(
                f"Conduct discovery review for work item {ctx.work_item.work_item_id} "
                f"(run_id={ctx.run.run_id}). "
                "If any improvement_procedure skill is listed in active_methods, read the latest-version "
                "improvement_procedure via skill_read first and follow it; otherwise inspect open_receipts "
                "and ALWAYS finish by calling record_work_decision (if no shared_publication applies, cite "
                "evidence=[{'kind':'work_run','resource_id':run_id,'revision':'1','available_at_ns':1,'observed_at_ns':2}])."
            ),
            purpose="discovery_review",
            base_tools=merged_specs,
            base_handlers=merged_handlers,
            history_notes=tuple(notes),
            open_receipts=tuple(receipts),
            include_core_desk_tools=True,
            include_plugin_tools=True,
            extra_recoverable_exceptions=merged_recoverable,
            pre_resolved_active_methods=(resolved_methods, resolved_snapshot_id),
        )
        if run_res.run_status == "waiting":
            return DutyOutcome(
                disposition="waiting",
                wait_until_ns=run_res.wait_until_ns,
                wait_event_key=run_res.wait_event_key,
                wait_reason="waiting_for_peer_response",
                result={"waiting": True},
            )
        with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
            dec = _find_committed_work_decision(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
            )
        if dec is None:
            reconciled_trace = reconcile_shift_episode_outcome(
                ctx,
                outcome_status="blocked",
                reason="no_work_decision",
                causal_method_trace=run_res.causal_method_trace,
            )
            return DutyOutcome(
                disposition="blocked",
                result={
                    "reason": "no_work_decision",
                    "output_text": run_res.shift_result.output_text.strip(),
                    "cost_minor_units": run_res.settlement.cost_minor_units,
                    "journal_entry_id": run_res.settlement.journal_entry_id,
                    "active_snapshot_id": run_res.active_snapshot_id,
                    "causal_method_trace": reconciled_trace,
                    "stable_prefix_sha256": run_res.stable_prefix_sha256,
                },
            )
        dec_id, act_kind, next_cid, dec_run_id = dec
        outcome_result: dict[str, Any] = {
            "decision_id": dec_id,
            "action_kind": act_kind,
            "next_condition_id": next_cid,
            "output_text": run_res.shift_result.output_text.strip(),
            "provider": run_res.shift_result.provider,
            "model": run_res.shift_result.model,
            "cost_minor_units": run_res.settlement.cost_minor_units,
            "journal_entry_id": run_res.settlement.journal_entry_id,
            "active_snapshot_id": run_res.active_snapshot_id,
            "causal_method_trace": run_res.causal_method_trace,
            "stable_prefix_sha256": run_res.stable_prefix_sha256,
        }
        if dec_run_id != ctx.run.run_id:
            outcome_result["recovered_committed_decision"] = True
            outcome_result["recovered_from_run_id"] = dec_run_id
        return DutyOutcome(disposition="completed", result=outcome_result)

    def _dreaming_precondition(conn: Any, item: WorkItem) -> tuple[bool, str]:
        blocking = conn.execute(
            """SELECT 1 FROM work_items
               WHERE company_id = ? AND work_item_id != ? AND state = 'due'
                 AND (
                     duty_kind NOT IN ('work.dreaming', 'work.discovery_review')
                     OR (owner_agent_id = ? AND duty_kind != 'work.dreaming')
                 )
               LIMIT 1""",
            (item.company_id, item.work_item_id, item.owner_agent_id),
        ).fetchone()
        if blocking is not None:
            return False, "mandatory_work_pending"
        return True, "idle_window"

    def _run_work_dreaming(ctx: DutyContext) -> DutyOutcome:
        agent_id = ctx.work_item.owner_agent_id
        try:
            payload = json.loads(ctx.work_item.payload_json)
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        def _int_opt(key: str, default: int | None, minimum: int = 0) -> int | None:
            val = payload.get(key)
            if val is None:
                return default
            if type(val) is int and val >= minimum:
                return val
            return default

        episode_limit = _int_opt("episode_limit", 20, minimum=1) or 20
        min_pattern_count = _int_opt("min_pattern_count", 2, minimum=2) or 2
        max_proposals = _int_opt("max_proposals", 3, minimum=1) or 3
        raw_cooldown_ns = _int_opt("cooldown_ns", None, minimum=0)
        max_runs_per_window = _int_opt("max_runs_per_window", None, minimum=1)
        raw_window_ns = _int_opt("window_ns", None, minimum=1)
        next_timer_delay_ns = _int_opt("next_timer_delay_ns", None, minimum=1)
        resp_id = payload.get("responsibility_id")
        eff_resp_id = (
            resp_id.strip() if isinstance(resp_id, str) and resp_id.strip() else None
        )

        next_cond: dict[str, Any] | None = None
        with (
            ctx.write_lock_ctx(),
            closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
            write_tx(db),
        ):
            assert_run_fencing(
                db, ctx.run.run_id, ctx.run.fencing_token, now_ns=ctx.now_ns
            )
            if eff_resp_id is not None:
                resp_row = db.execute(
                    """SELECT status, readable_scope_json
                       FROM work_responsibilities
                       WHERE company_id = ? AND responsibility_id = ?""",
                    (ctx.company_id, eff_resp_id),
                ).fetchone()
                if resp_row is not None and str(resp_row[0]) == "active":
                    try:
                        scope_cfg = json.loads(str(resp_row[1]))
                    except json.JSONDecodeError:
                        scope_cfg = None
                    if isinstance(scope_cfg, dict):
                        if (
                            max_runs_per_window is None
                            and type(scope_cfg.get("max_wakes_per_window")) is int
                            and scope_cfg["max_wakes_per_window"] >= 1
                        ):
                            max_runs_per_window = scope_cfg["max_wakes_per_window"]
                        if (
                            raw_window_ns is None
                            and type(scope_cfg.get("wake_window_ns")) is int
                            and scope_cfg["wake_window_ns"] >= 1
                        ):
                            raw_window_ns = scope_cfg["wake_window_ns"]
                        if (
                            raw_cooldown_ns is None
                            and type(scope_cfg.get("cooldown_ns")) is int
                            and scope_cfg["cooldown_ns"] >= 0
                        ):
                            raw_cooldown_ns = scope_cfg["cooldown_ns"]
            window_ns = (
                raw_window_ns if raw_window_ns is not None else 3_600_000_000_000
            )
            cooldown_ns = raw_cooldown_ns if raw_cooldown_ns is not None else 0
            if (
                eff_resp_id is not None
                and resp_row is not None
                and str(resp_row[0]) == "active"
            ):
                if next_timer_delay_ns is not None:
                    next_cond = {
                        "responsibility_id": eff_resp_id,
                        "trigger_kind": "timer",
                        "due_at_ns": ctx.now_ns + next_timer_delay_ns,
                        "cooldown_ns": cooldown_ns,
                    }

            if cooldown_ns > 0:
                last_row = db.execute(
                    """SELECT MAX(updated_at_ns)
                       FROM work_runs
                       WHERE company_id = ? AND agent_id = ?
                         AND duty_kind = ? AND status = 'completed'
                         AND run_id != ?
                         AND outcome_json IS NOT NULL
                         AND json_valid(outcome_json)
                         AND json_extract(outcome_json, '$.audited') = 1""",
                    (ctx.company_id, agent_id, DUTY_WORK_DREAMING, ctx.run.run_id),
                ).fetchone()
                if (
                    last_row is not None
                    and last_row[0] is not None
                    and ctx.now_ns < int(last_row[0]) + cooldown_ns
                ):
                    return DutyOutcome(
                        disposition="completed",
                        next_condition=next_cond,
                        result={
                            "audited": False,
                            "skipped_reason": "cooldown_active",
                            "episodes_audited": 0,
                            "patterns_detected": 0,
                            "proposals": [],
                        },
                    )

            if max_runs_per_window is not None:
                window_start = max(0, ctx.now_ns - window_ns)
                run_count = int(
                    db.execute(
                        """SELECT COUNT(*)
                           FROM work_runs
                           WHERE company_id = ? AND agent_id = ?
                             AND duty_kind = ? AND status = 'completed'
                             AND run_id != ? AND updated_at_ns >= ?
                             AND outcome_json IS NOT NULL
                             AND json_valid(outcome_json)
                             AND json_extract(outcome_json, '$.audited') = 1""",
                        (
                            ctx.company_id,
                            agent_id,
                            DUTY_WORK_DREAMING,
                            ctx.run.run_id,
                            window_start,
                        ),
                    ).fetchone()[0]
                )
                if run_count >= max_runs_per_window:
                    return DutyOutcome(
                        disposition="completed",
                        next_condition=next_cond,
                        result={
                            "audited": False,
                            "skipped_reason": "budget_cap_reached",
                            "episodes_audited": 0,
                            "patterns_detected": 0,
                            "proposals": [],
                        },
                    )

            claim = work_run_claim_from_context(db, ctx, purpose="dreaming")
            audit_res = audit_episodes_for_dreaming(
                db,
                claim,
                episode_limit=episode_limit,
                min_pattern_count=min_pattern_count,
                max_proposals=max_proposals,
                now_ns=ctx.now_ns,
            )

        return DutyOutcome(
            disposition="completed",
            next_condition=next_cond,
            result={
                "audited": True,
                "skipped_reason": audit_res.skipped_reason,
                "episodes_audited": audit_res.episodes_audited,
                "patterns_detected": len(audit_res.patterns_detected),
                "patterns": list(audit_res.patterns_detected),
                "proposals": [
                    {
                        "delta_id": d.delta_id,
                        "method_id": d.method_id,
                        "revision_sha256": d.revision_sha256,
                        "delta_kind": d.delta_kind,
                        "status": d.status,
                    }
                    for d in audit_res.drafted_deltas
                ],
            },
        )

    extra_map = dict(extra_handlers or {})
    core_descriptors = (
        DutyDescriptor(
            duty_kind=DUTY_CEO_RESPOND_MESSAGE,
            execute=_run_ceo_respond_message,
        ),
        DutyDescriptor(
            duty_kind=DUTY_BOARD_APPLY_DECISION,
            execute=_run_board_apply_decision,
        ),
        DutyDescriptor(
            duty_kind=DUTY_WORK_PEER_RESPONSE,
            execute=_run_work_peer_response,
        ),
        DutyDescriptor(
            duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
            execute=_run_work_assessment_review,
        ),
        DutyDescriptor(
            duty_kind=DUTY_WORK_HANDOFF_FOLLOWUP,
            execute=_run_work_handoff_followup,
        ),
        DutyDescriptor(
            duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
            execute=_run_work_discovery_review,
        ),
        DutyDescriptor(
            duty_kind=DUTY_WORK_DREAMING,
            execute=_run_work_dreaming,
            precondition=_dreaming_precondition,
        ),
    )
    for descriptor in core_descriptors:
        if descriptor.duty_kind not in extra_map:
            registry.register(descriptor)
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
    for duty_kind, fn in extra_map.items():
        registry.register(DutyDescriptor(duty_kind=duty_kind, execute=fn))
    return registry
