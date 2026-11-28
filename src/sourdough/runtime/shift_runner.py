"""Unified ShiftRunner facade orchestrating Core & DomainPlugin agent shifts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agent_runtime._http import compute_stable_prefix_sha256
from ..agent_runtime.cost_accounting import TurnCostSettlement
from ..agent_runtime.protocol import (
    PROVIDER_CODEX_CLI,
    AgentProfileBinding,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
    ToolHandler,
    ToolSpec,
)
from ..harness import create_agent_session
from ..memory import AgentEpisode
from ..method_learning import default_active_methods_provider
from ..storage.db import scoped_reader
from .domains import ProfileBindingLookup, ToolContribution, ToolQueryContext
from .registry import DutyContext
from .shift import (
    _PEER_INPUT_TOOLS,
    _PEER_OUTBOUND_SINKS,
    _PRIVATE_DESK_ALL_TOOLS,
    _PRIVATE_DESK_READ_TOOLS,
    _PRIVATE_DESK_RECEIPT_TOOLS,
    _PRIVATE_RECEIPT_ERROR_TOOLS,
    _TRADING_WRITE_SINKS,
    ProvenanceIndex,
    ShiftFlowGuard,
    ShiftReceiptStore,
    _sanitize_receipt_error_message,
    _sanitize_receipt_result_json,
    append_trace_line,
    finalize_shift_run,
    hydrate_shift_session,
    is_authority_refusal_message,
    reconcile_shift_episode_outcome,
)
from .shift.hydration import ActiveMethodsProvider
from .work_kernel import assert_run_fencing

if TYPE_CHECKING:
    from ..agent_runtime.registry import AgentAdapterRegistry


@dataclass(frozen=True)
class ShiftRunResult:
    shift_result: ShiftResult
    settlement: TurnCostSettlement
    episode: AgentEpisode | None
    desk_path: Path
    run_status: str = "completed"
    wait_until_ns: int | None = None
    wait_event_key: str | None = None
    active_snapshot_id: str | None = None
    causal_method_trace: dict[str, Any] | None = None
    stable_prefix_sha256: str = ""


class ShiftRunner:
    """Shared shift orchestrator for Core and DomainPlugin duties."""

    def __init__(
        self,
        *,
        adapter_registry: AgentAdapterRegistry,
        profile_bindings: ProfileBindingLookup,
        default_budget_ceiling_minor: int,
        read_agent_state: Callable[[Path, str, str], tuple[str, str]],
        resolve_profile: Callable[[ProfileBindingLookup, str], AgentProfileBinding],
        contribute_tools: Callable[[ToolQueryContext], ToolContribution],
        active_methods_provider: ActiveMethodsProvider | None = None,
    ) -> None:
        self._adapter_registry = adapter_registry
        self._profile_bindings = profile_bindings
        self._default_budget_ceiling_minor = int(default_budget_ceiling_minor)
        self._read_agent_state = read_agent_state
        self._resolve_profile = resolve_profile
        self._contribute_tools = contribute_tools
        self._active_methods_provider = (
            active_methods_provider
            if active_methods_provider is not None
            else default_active_methods_provider
        )

    def run(
        self,
        ctx: DutyContext,
        *,
        mandate_summary: str,
        instruction: str,
        turn_id: str = "turn-1",
        purpose: str | None = None,
        base_tools: Sequence[ToolSpec] = (),
        base_handlers: Mapping[str, ToolHandler] = (),
        history_notes: Sequence[str] = (),
        open_receipts: Sequence[dict[str, Any]] = (),
        include_core_desk_tools: bool = True,
        include_core_skill_tools: bool | None = None,
        include_plugin_tools: bool = True,
        extra_recoverable_exceptions: tuple[type[BaseException], ...] = (),
        max_tool_calls: int = 16,
        timeout_seconds: float | None = None,
        pre_resolved_active_methods: tuple[Sequence[Any], str | None] | None = None,
        allowed_tool_names: frozenset[str] | None = None,
    ) -> ShiftRunResult:
        agent_id = ctx.work_item.owner_agent_id
        persisted_profile, grant_scope = self._read_agent_state(
            ctx.writer.store, ctx.company_id, agent_id
        )
        profile = self._resolve_profile(self._profile_bindings, agent_id)
        profile.verify_matches_agent(persisted_profile)

        hydrated = hydrate_shift_session(
            ctx,
            persisted_profile=persisted_profile,
            grant_scope=grant_scope,
            profile=profile,
            mandate_summary=mandate_summary,
            purpose=purpose,
            base_tools=base_tools,
            base_handlers=base_handlers,
            history_notes=history_notes,
            open_receipts=open_receipts,
            include_core_desk_tools=include_core_desk_tools,
            include_core_skill_tools=include_core_skill_tools,
            include_plugin_tools=include_plugin_tools,
            extra_recoverable_exceptions=extra_recoverable_exceptions,
            pre_resolved_active_methods=pre_resolved_active_methods,
            allowed_tool_names=allowed_tool_names,
            contribute_tools=self._contribute_tools,
            active_methods_provider=self._active_methods_provider,
        )

        specs_by_name = {spec.name: spec for spec in hydrated.tool_specs}
        provenance = ProvenanceIndex(
            ctx.writer.store,
            ctx.company_id,
            agent_id,
            ctx.work_item.work_item_id,
        )
        flow_guard = ShiftFlowGuard(
            agent_id=agent_id,
            provenance=provenance,
            specs_by_name=specs_by_name,
            public_offloaded_paths=hydrated.public_offloaded_paths,
            read_private_desk=hydrated.has_hydrated_private_context,
            read_peer_input=hydrated.initial_had_peer_input,
        )
        flow_guard.seed_from_shift_context(
            work_item_source_kind=ctx.work_item.source_kind,
            work_item_payload_json=ctx.work_item.payload_json,
            open_receipts=open_receipts,
            prior_had_peer_input=hydrated.prior_had_peer_input,
            prior_peer_rows=hydrated.prior_peer_rows,
        )

        locked_handlers: dict[str, ToolHandler] = {
            name: flow_guard.wrap_handler(
                name,
                fn,
                lock_factory=(
                    ctx.write_lock_ctx if name != "desk_run_command" else None
                ),
            )
            for name, fn in hydrated.merged_handlers.items()
        }

        session = create_agent_session(
            ctx.writer, hydrated.claim, hydrated.claim.purpose
        )

        def _fencing_check() -> None:
            with scoped_reader(ctx.writer.store) as db:
                assert_run_fencing(
                    db, ctx.run.run_id, ctx.run.fencing_token, now_ns=ctx.now_ns
                )

        budget_ceiling = (
            ctx.run.budget_ceiling_minor
            if ctx.run.budget_ceiling_minor > 0
            else self._default_budget_ceiling_minor
        )
        eff_timeout_seconds = (
            (120.0 if profile.provider == PROVIDER_CODEX_CLI else 30.0)
            if timeout_seconds is None
            else timeout_seconds
        )
        shift_req = ShiftRequest(
            company_id=ctx.company_id,
            agent_id=agent_id,
            run_id=ctx.run.run_id,
            turn_id=turn_id,
            fencing_token=ctx.run.fencing_token,
            instruction=instruction,
            context=hydrated.context_pack,
            tools=hydrated.tool_specs,
            budget_ceiling_minor_units=budget_ceiling,
            max_tool_calls=max_tool_calls,
            timeout_seconds=eff_timeout_seconds,
            desk_path=(
                hydrated.desk
                if (
                    hydrated.effective_include_desk_tools
                    and not hydrated.has_external_sinks
                )
                else None
            ),
            store_path=ctx.writer.store,
        )
        stable_prefix_sha256 = compute_stable_prefix_sha256(shift_req)

        receipt_store = ShiftReceiptStore(
            writer=ctx.writer,
            session=session,
            specs_by_name=specs_by_name,
            public_offloaded_paths=hydrated.public_offloaded_paths,
            stable_prefix_sha256=stable_prefix_sha256,
            tx_lock=ctx.tx_lock,
        )

        broker = OuterToolBroker(
            company_id=ctx.company_id,
            agent_id=agent_id,
            run_id=ctx.run.run_id,
            fencing_token=ctx.run.fencing_token,
            handlers=locked_handlers,
            max_tool_calls=max_tool_calls,
            fencing_check=_fencing_check,
            receipt_recorder=receipt_store.record_receipt,
            trace_recorder=receipt_store.record_trace,
            offload_handler=receipt_store.offload,
            recoverable_exceptions=hydrated.merged_recoverable,
            clock=lambda: max(1, int(ctx.now_ns)),
        )

        append_trace_line(
            ctx.writer.store,
            session.trace_path,
            {
                "seq": 0,
                "kind": "shift_prefix",
                "recorded_at_ns": max(1, int(ctx.now_ns)),
                "company_id": ctx.company_id,
                "agent_id": agent_id,
                "run_id": ctx.run.run_id,
                "turn_id": turn_id,
                "fencing_token": ctx.run.fencing_token,
                "active_snapshot_id": hydrated.active_snapshot_id,
                "stable_prefix_sha256": stable_prefix_sha256,
                "tool_names": [spec.name for spec in shift_req.tools],
            },
        )

        shift_result: ShiftResult | None = None
        shift_error: BaseException | None = None
        finalized = None
        try:
            shift_result = self._adapter_registry.execute_shift(
                persisted_profile_id=persisted_profile,
                profile=profile,
                request=shift_req,
                broker=broker,
            )
        except BaseException as exc:
            shift_error = exc
            raise
        finally:
            finalized = finalize_shift_run(
                ctx,
                agent_id=agent_id,
                profile=profile,
                turn_id=turn_id,
                budget_ceiling=budget_ceiling,
                session=session,
                broker=broker,
                context_pack=hydrated.context_pack,
                active_snapshot_id=hydrated.active_snapshot_id,
                available_method_ids=hydrated.available_method_ids,
                retrieved_method_ids=hydrated.retrieved_method_ids,
                invoked_method_ids=hydrated.invoked_method_ids,
                stable_prefix_sha256=stable_prefix_sha256,
                shift_result=shift_result,
                shift_error=shift_error,
            )

        assert shift_result is not None
        assert finalized is not None and finalized.settlement is not None
        return ShiftRunResult(
            shift_result=shift_result,
            settlement=finalized.settlement,
            episode=finalized.episode,
            desk_path=hydrated.desk,
            run_status=finalized.final_run_status,
            wait_until_ns=finalized.final_wait_until_ns,
            wait_event_key=finalized.final_wait_event_key,
            active_snapshot_id=hydrated.active_snapshot_id,
            causal_method_trace=finalized.causal_trace_dict,
            stable_prefix_sha256=stable_prefix_sha256,
        )


__all__ = [
    "_PEER_INPUT_TOOLS",
    "_PEER_OUTBOUND_SINKS",
    "_PRIVATE_DESK_ALL_TOOLS",
    "_PRIVATE_DESK_READ_TOOLS",
    "_PRIVATE_DESK_RECEIPT_TOOLS",
    "_PRIVATE_RECEIPT_ERROR_TOOLS",
    "_TRADING_WRITE_SINKS",
    "ActiveMethodsProvider",
    "ShiftRunResult",
    "ShiftRunner",
    "_sanitize_receipt_error_message",
    "_sanitize_receipt_result_json",
    "is_authority_refusal_message",
    "reconcile_shift_episode_outcome",
]
