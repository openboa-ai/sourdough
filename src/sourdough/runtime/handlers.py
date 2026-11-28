"""Production DutyRegistry builder wiring Core duties, multi-provider Agents, and domain plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from ..method_learning import default_active_methods_provider
from .domains import (
    DecisionActionRegistry,
    DomainPlugin,
    DutyRuntimeServices,
    ProfileBindingLookup,
    ToolContribution,
    ToolQueryContext,
    merge_tool_contributions,
)
from .duties import (
    DUTY_BOARD_APPLY_DECISION,
    DUTY_CEO_RESPOND_MESSAGE,
    DUTY_WORK_ASSESSMENT_REVIEW,
    DUTY_WORK_DISCOVERY_REVIEW,
    DUTY_WORK_DREAMING,
    DUTY_WORK_HANDOFF_FOLLOWUP,
    DUTY_WORK_PEER_RESPONSE,
    BoardDuties,
    CollaborationDuties,
    CoreDutyDeps,
    DiscoveryDuties,
    DreamingDuties,
    build_proactive_tool_handlers,
    build_store_profile_lookup,
    dreaming_precondition,
    read_agent_state as _read_agent_state,
    read_company_status as _read_company_status,
    resolve_profile as _resolve_profile,
    sync_board_triggers,
)
from .registry import DutyContext, DutyDescriptor, DutyOutcome, DutyRegistry
from .shift_runner import ActiveMethodsProvider, ShiftRunner

if TYPE_CHECKING:
    from ..agent_runtime.registry import AgentAdapterRegistry


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
    plugins_tuple = tuple(domain_plugins)
    decision_actions = DecisionActionRegistry()
    for plugin in plugins_tuple:
        for action_name, handler in plugin.contribute_decision_actions().items():
            decision_actions.register(action_name, handler)

    def _contribute_tools(tool_ctx: ToolQueryContext) -> ToolContribution:
        contributions = tuple(
            plugin.contribute_tools(tool_ctx) for plugin in plugins_tuple
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
    deps = CoreDutyDeps(
        shift_runner=shift_runner,
        decision_actions=decision_actions,
        domain_plugins=plugins_tuple,
        active_methods_provider=eff_active_methods_provider,
    )
    board = BoardDuties(deps)
    collab = CollaborationDuties(deps)
    discovery = DiscoveryDuties(deps)
    dreaming = DreamingDuties(deps)

    extra_map = dict(extra_handlers or {})
    core_descriptors = (
        DutyDescriptor(
            duty_kind=DUTY_CEO_RESPOND_MESSAGE,
            execute=board.run_ceo_respond_message,
        ),
        DutyDescriptor(
            duty_kind=DUTY_BOARD_APPLY_DECISION,
            execute=board.run_board_apply_decision,
        ),
        DutyDescriptor(
            duty_kind=DUTY_WORK_PEER_RESPONSE,
            execute=collab.run_work_peer_response,
        ),
        DutyDescriptor(
            duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
            execute=collab.run_work_assessment_review,
        ),
        DutyDescriptor(
            duty_kind=DUTY_WORK_HANDOFF_FOLLOWUP,
            execute=collab.run_work_handoff_followup,
        ),
        DutyDescriptor(
            duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
            execute=discovery.run_work_discovery_review,
        ),
        DutyDescriptor(
            duty_kind=DUTY_WORK_DREAMING,
            execute=dreaming.run_work_dreaming,
            precondition=dreaming_precondition,
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
    for plugin in plugins_tuple:
        for descriptor in plugin.contribute_duties(services):
            registry.register(descriptor)
    for duty_kind, fn in extra_map.items():
        registry.register(DutyDescriptor(duty_kind=duty_kind, execute=fn))
    return registry


__all__ = [
    "DUTY_BOARD_APPLY_DECISION",
    "DUTY_CEO_RESPOND_MESSAGE",
    "DUTY_WORK_ASSESSMENT_REVIEW",
    "DUTY_WORK_DISCOVERY_REVIEW",
    "DUTY_WORK_DREAMING",
    "DUTY_WORK_HANDOFF_FOLLOWUP",
    "DUTY_WORK_PEER_RESPONSE",
    "_read_agent_state",
    "_read_company_status",
    "_resolve_profile",
    "build_proactive_tool_handlers",
    "build_production_duty_registry",
    "build_store_profile_lookup",
    "sync_board_triggers",
]
