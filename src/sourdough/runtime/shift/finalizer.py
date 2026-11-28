"""Shift finalization: scratch cleanup, Desk projection sync, GL settlement, and episode recording."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from ...agent_desk import cleanup_run_scratch
from ...agent_runtime.cost_accounting import (
    DEFAULT_MODEL_PRICING,
    TurnCostSettlement,
    compute_usage_cost_minor_units,
    settle_shift_cost_to_ledger,
)
from ...agent_runtime.protocol import (
    AgentBudgetExceeded,
    AgentProfileBinding,
    ContextPack,
    OuterToolBroker,
    ShiftResult,
)
from ...duty_run import DutyRefused
from ...episode_index import upsert_episode_entry
from ...harness import AgentSession
from ...memory import AgentEpisode, record_episode, update_episode_outcome
from ...method_learning import build_causal_method_trace, sync_desk_harness_projections
from ...storage.db import scoped_reader, write_uow
from ...storage.kernel import append_audit_event
from ..registry import DutyContext
from ..work_kernel import RunFenced
from .policy import redact_causal_trace_for_audit
from .receipts import append_trace_line

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FinalizedShift:
    settlement: TurnCostSettlement | None
    episode: AgentEpisode | None
    causal_trace_dict: dict[str, Any] | None
    final_run_status: str
    final_wait_until_ns: int | None
    final_wait_event_key: str | None


def reconcile_shift_episode_outcome(
    ctx: DutyContext,
    *,
    outcome_status: str,
    reason: str | None = None,
    causal_method_trace: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Reconcile recorded episode + episode index + causal trace after duty validation."""
    reconciled: dict[str, Any] | None = None
    if causal_method_trace is not None:
        reconciled = dict(causal_method_trace)
        od = reconciled.get("outcome_delta")
        patched_od = dict(od) if isinstance(od, dict) else {}
        patched_od["status"] = outcome_status
        if reason is not None:
            patched_od["reason"] = reason
        reconciled["outcome_delta"] = patched_od
    with write_uow(ctx.writer, tx_lock=ctx.tx_lock) as db:
        updated_ep = update_episode_outcome(
            db,
            ctx.company_id,
            ctx.run.run_id,
            outcome_status=outcome_status,
            reason=reason,
        )
        if reconciled is not None and (
            reconciled.get("available")
            or reconciled.get("retrieved")
            or reconciled.get("invoked")
        ):
            audit_reconciled = redact_causal_trace_for_audit(
                db, ctx.company_id, reconciled
            )
            append_audit_event(
                db,
                company_id=ctx.company_id,
                domain="learning",
                entity_type="agent_episode",
                entity_id=ctx.run.run_id,
                event_type="learning.causal_method_trace_reconciled",
                actor_id=ctx.work_item.owner_agent_id,
                run_id=ctx.run.run_id,
                payload=audit_reconciled,
                recorded_at_ns=max(1, int(ctx.now_ns)),
            )
    if updated_ep is not None:
        upsert_episode_entry(ctx.writer.store, updated_ep)
    return reconciled


def finalize_shift_run(
    ctx: DutyContext,
    *,
    agent_id: str,
    profile: AgentProfileBinding,
    turn_id: str,
    budget_ceiling: int,
    session: AgentSession,
    broker: OuterToolBroker,
    context_pack: ContextPack,
    active_snapshot_id: str | None,
    available_method_ids: list[str],
    retrieved_method_ids: list[str],
    invoked_method_ids: list[str],
    stable_prefix_sha256: str,
    shift_result: ShiftResult | None,
    shift_error: BaseException | None,
) -> FinalizedShift:
    """Cleanup scratch, sync Desk projections, settle GL costs, and record episode/causal trace."""
    projection_error: BaseException | None = None
    settlement: TurnCostSettlement | None = None
    settlement_error: BaseException | None = None
    episode: AgentEpisode | None = None
    final_run_status = "completed"
    final_wait_until_ns: int | None = None
    final_wait_event_key: str | None = None

    try:
        cleanup_run_scratch(ctx.writer.store, agent_id, ctx.run.run_id)
    except Exception as exc:
        _LOGGER.warning(
            "Scratch cleanup failed for agent=%s run=%s: %s",
            agent_id,
            ctx.run.run_id,
            exc,
        )

    try:
        sync_desk_harness_projections(
            ctx.writer.store,
            ctx.company_id,
            agent_id,
        )
    except (DutyRefused, OSError) as exc:
        projection_error = exc
        _LOGGER.warning(
            "Desk harness projection sync failed for agent=%s run=%s: %s",
            agent_id,
            ctx.run.run_id,
            exc,
        )
        try:
            append_trace_line(
                ctx.writer.store,
                session.trace_path,
                {
                    "kind": "desk.harness_projection_error",
                    "recorded_at_ns": max(1, int(ctx.now_ns)),
                    "company_id": ctx.company_id,
                    "agent_id": agent_id,
                    "run_id": ctx.run.run_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
        except Exception as trace_exc:
            _LOGGER.warning(
                "Failed to record desk.harness_projection_error trace for agent=%s run=%s: %s",
                agent_id,
                ctx.run.run_id,
                trace_exc,
            )
    except Exception as exc:
        _LOGGER.warning(
            "Unexpected Desk harness projection error for agent=%s run=%s: %s",
            agent_id,
            ctx.run.run_id,
            exc,
        )

    incurred_usage = (
        shift_result.usage if shift_result is not None else broker.accrued_usage
    )
    try:
        settlement = settle_shift_cost_to_ledger(
            ctx.writer,
            profile=profile,
            run_id=ctx.run.run_id,
            turn_id=turn_id,
            usage=incurred_usage,
            budget_ceiling_minor_units=budget_ceiling,
            tx_lock=ctx.tx_lock,
        )
    except BaseException as exc:
        settlement_error = exc

    cost_minor = (
        settlement.cost_minor_units
        if settlement is not None
        else compute_usage_cost_minor_units(
            incurred_usage, DEFAULT_MODEL_PRICING[profile.provider]
        )
    )

    decision_ids: list[str] = []
    run_state_row: Any = None
    try:
        with scoped_reader(ctx.writer.store) as db:
            if shift_result is not None and settlement_error is None:
                run_state_row = db.execute(
                    """SELECT status, wait_until_ns, wait_event_key
                       FROM work_runs
                       WHERE run_id = ? AND fencing_token = ?""",
                    (ctx.run.run_id, ctx.run.fencing_token),
                ).fetchone()
            decision_ids = [
                str(r[0])
                for r in db.execute(
                    "SELECT decision_id FROM work_decision WHERE company_id = ? AND run_id = ? ORDER BY decision_at_ns, decision_id",
                    (ctx.company_id, ctx.run.run_id),
                ).fetchall()
            ]
    except Exception as exc:
        _LOGGER.warning(
            "Failed to read finalized run/decision metadata for agent=%s run=%s: %s",
            agent_id,
            ctx.run.run_id,
            exc,
        )

    if shift_result is not None and settlement_error is None:
        if run_state_row is not None and str(run_state_row[0]) == "waiting":
            final_run_status = "waiting"
            final_wait_until_ns = (
                None if run_state_row[1] is None else int(run_state_row[1])
            )
            final_wait_event_key = (
                None if run_state_row[2] is None else str(run_state_row[2])
            )
        ep_status = final_run_status
        raw_out = shift_result.output_text.strip()
        out_bytes = raw_out.encode("utf-8")
        out_digest = sha256(out_bytes).hexdigest()
        if final_run_status == "waiting":
            ep_summary = (
                f"status={final_run_status} duty={ctx.work_item.duty_kind} "
                f"wait_key={final_wait_event_key or 'none'} "
                f"tools={broker.call_count} output_bytes={len(out_bytes)} "
                f"output_digest={out_digest}"
            )
        else:
            ep_summary = (
                f"status={final_run_status} duty={ctx.work_item.duty_kind} "
                f"tools={broker.call_count} output_bytes={len(out_bytes)} "
                f"output_digest={out_digest}"
            )
        append_trace_line(
            ctx.writer.store,
            session.trace_path,
            {
                "seq": broker.call_count + 1,
                "kind": "shift_terminal",
                "recorded_at_ns": max(1, int(ctx.now_ns)),
                "run_id": ctx.run.run_id,
                "status": final_run_status,
                "output_bytes": len(out_bytes),
                "output_digest": out_digest,
                "output_text": raw_out,
            },
        )
    elif isinstance(shift_error, AgentBudgetExceeded) or (
        isinstance(settlement_error, AgentBudgetExceeded)
        and not isinstance(shift_error, RunFenced)
    ):
        ep_status = "blocked"
        ep_summary = str(
            settlement_error
            if isinstance(settlement_error, AgentBudgetExceeded)
            else shift_error
        )
    else:
        active_err = shift_error or settlement_error
        ep_status = "failed"
        ep_summary = (
            f"{type(active_err).__name__}: {active_err}"
            if active_err is not None
            else "Shift failed"
        )

    causal_trace = build_causal_method_trace(
        company_id=ctx.company_id,
        agent_id=agent_id,
        run_id=ctx.run.run_id,
        duty_kind=ctx.work_item.duty_kind,
        active_snapshot_id=active_snapshot_id,
        available_methods=available_method_ids,
        retrieved_methods=retrieved_method_ids,
        invoked_methods=invoked_method_ids,
        outcome_delta={
            "status": ep_status,
            "tool_calls_executed": (
                shift_result.tool_calls_executed
                if shift_result is not None
                else broker.call_count
            ),
            "input_tokens": incurred_usage.input_tokens,
            "cached_input_tokens": incurred_usage.cached_input_tokens,
            "output_tokens": incurred_usage.output_tokens,
            "cost_minor_units": cost_minor,
            "work_decisions": decision_ids,
            "stable_prefix_sha256": stable_prefix_sha256,
        },
        stable_prefix_sha256=stable_prefix_sha256,
    )
    causal_trace_dict = causal_trace.to_dict()

    receipt_refs = tuple(
        str(ev.payload["call_id"])
        for ev in broker.events
        if ev.kind == "tool_result" and ev.payload.get("call_id")
    )
    try:
        ep_ts = max(1, int(ctx.now_ns))
        with write_uow(ctx.writer, tx_lock=ctx.tx_lock) as db:
            episode = record_episode(
                db,
                company_id=ctx.company_id,
                agent_id=agent_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
                duty_kind=ctx.work_item.duty_kind,
                context_digest=context_pack.digest,
                outcome_status=ep_status,
                summary_text=ep_summary,
                active_snapshot_id=active_snapshot_id,
                decisions=[causal_trace_dict],
                receipts=receipt_refs,
                tool_calls_count=broker.call_count,
                input_tokens=incurred_usage.input_tokens,
                output_tokens=incurred_usage.output_tokens,
                cost_minor_units=cost_minor,
                recorded_at_ns=ep_ts,
            )
            if available_method_ids or retrieved_method_ids or invoked_method_ids:
                audit_trace = redact_causal_trace_for_audit(
                    db, ctx.company_id, causal_trace_dict
                )
                append_audit_event(
                    db,
                    company_id=ctx.company_id,
                    domain="learning",
                    entity_type="agent_episode",
                    entity_id=ctx.run.run_id,
                    event_type="learning.causal_method_trace",
                    actor_id=agent_id,
                    run_id=ctx.run.run_id,
                    payload=audit_trace,
                    recorded_at_ns=ep_ts,
                )
        if episode is not None:
            upsert_episode_entry(ctx.writer.store, episode)
    except Exception as exc:
        _LOGGER.warning(
            "Episode persistence failed for agent=%s run=%s: %s",
            agent_id,
            ctx.run.run_id,
            exc,
        )

    if isinstance(settlement_error, AgentBudgetExceeded) and not isinstance(
        shift_error, RunFenced
    ):
        raise settlement_error from shift_error
    if shift_error is None and settlement_error is not None:
        raise settlement_error
    if shift_error is None and projection_error is not None:
        raise projection_error

    return FinalizedShift(
        settlement=settlement,
        episode=episode,
        causal_trace_dict=causal_trace_dict,
        final_run_status=final_run_status,
        final_wait_until_ns=final_wait_until_ns,
        final_wait_event_key=final_wait_event_key,
    )
