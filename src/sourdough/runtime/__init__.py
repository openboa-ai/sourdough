"""Unified CompanyRuntime, ResidentCompanyService, DutyRegistry, Work kernel, and domain contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .domains import (
    BaseDomainPlugin,
    CommandDescriptor,
    CommandDispatchContext,
    CommittedResultPredicate,
    DecisionActionContext,
    DecisionActionHandler,
    DecisionActionRegistry,
    DomainActionRefused,
    DomainCliRefused,
    DomainPlugin,
    DomainPluginRegistry,
    DutyContributor,
    DutyRuntimeServices,
    IndependentProcessGuard,
    MergedWorkPolicy,
    ProjectionDescriptor,
    RestoreDomainGuard,
    RestrictionClearContext,
    RestrictionClearHook,
    RestrictionGuard,
    ToolContribution,
    ToolContributor,
    ToolQueryContext,
    WorkDomainPolicy,
    WorkTimelineContribution,
    load_domain_plugins,
    merge_tool_contributions,
    merge_work_policies,
)
from .engine import CompanyRuntime, RuntimeStepSummary, build_default_registry
from .registry import (
    DutyContext,
    DutyDescriptor,
    DutyOutcome,
    DutyRegistry,
    RuntimeRefused,
)
from .work_kernel import (
    DecisionEvidence,
    PeerRequest,
    RunFenced,
    WorkCondition,
    WorkDecision,
    WorkDiscoveryState,
    WorkEventBatch,
    WorkItem,
    WorkKernelRefused,
    WorkResponsibility,
    WorkRun,
    arm_result_continuation,
    assert_run_fencing,
    cancel_condition,
    claim_work_run,
    complete_work_run,
    create_work_item,
    drain_work_event_cursor,
    evaluate_conditions,
    fail_work_run,
    has_active_grant,
    list_conditions,
    open_peer_request,
    reclaim_expired_runs,
    record_discovery_hysteresis,
    record_work_decision,
    register_responsibility,
    replace_condition,
    resolve_peer_request,
    revoke_responsibility,
    set_condition,
    wake_waiting_runs,
    yield_run_to_wait,
)

if TYPE_CHECKING:
    from .handlers import (
        DUTY_BOARD_APPLY_DECISION,
        DUTY_CEO_RESPOND_MESSAGE,
        DUTY_WORK_ASSESSMENT_REVIEW,
        DUTY_WORK_DISCOVERY_REVIEW,
        DUTY_WORK_DREAMING,
        DUTY_WORK_HANDOFF_FOLLOWUP,
        DUTY_WORK_PEER_RESPONSE,
        ProfileBindingLookup,
        build_proactive_tool_handlers,
        build_production_duty_registry,
        build_store_profile_lookup,
        sync_board_triggers,
    )
    from .method_tools import build_core_skill_tool_contribution
    from .resident import ResidentCompanyService, ResidentHealth

_LAZY_HANDLERS = frozenset(
    {
        "DUTY_BOARD_APPLY_DECISION",
        "DUTY_CEO_RESPOND_MESSAGE",
        "DUTY_WORK_ASSESSMENT_REVIEW",
        "DUTY_WORK_DISCOVERY_REVIEW",
        "DUTY_WORK_DREAMING",
        "DUTY_WORK_HANDOFF_FOLLOWUP",
        "DUTY_WORK_PEER_RESPONSE",
        "ProfileBindingLookup",
        "build_proactive_tool_handlers",
        "build_production_duty_registry",
        "build_store_profile_lookup",
        "sync_board_triggers",
    }
)
_LAZY_RESIDENT = frozenset({"ResidentCompanyService", "ResidentHealth"})
_LAZY_METHOD_TOOLS = frozenset({"build_core_skill_tool_contribution"})


def __getattr__(name: str) -> Any:
    if name in _LAZY_HANDLERS:
        from . import handlers

        return getattr(handlers, name)
    if name in _LAZY_RESIDENT:
        from . import resident

        return getattr(resident, name)
    if name in _LAZY_METHOD_TOOLS:
        from . import method_tools

        return getattr(method_tools, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseDomainPlugin",
    "CommandDescriptor",
    "CommandDispatchContext",
    "CommittedResultPredicate",
    "CompanyRuntime",
    "DUTY_BOARD_APPLY_DECISION",
    "DUTY_CEO_RESPOND_MESSAGE",
    "DUTY_WORK_ASSESSMENT_REVIEW",
    "DUTY_WORK_DISCOVERY_REVIEW",
    "DUTY_WORK_DREAMING",
    "DUTY_WORK_HANDOFF_FOLLOWUP",
    "DUTY_WORK_PEER_RESPONSE",
    "DecisionActionContext",
    "DecisionActionHandler",
    "DecisionActionRegistry",
    "DecisionEvidence",
    "DomainActionRefused",
    "DomainCliRefused",
    "DomainPlugin",
    "DomainPluginRegistry",
    "DutyContext",
    "DutyContributor",
    "DutyDescriptor",
    "DutyOutcome",
    "DutyRegistry",
    "DutyRuntimeServices",
    "IndependentProcessGuard",
    "MergedWorkPolicy",
    "PeerRequest",
    "ProfileBindingLookup",
    "ProjectionDescriptor",
    "ResidentCompanyService",
    "ResidentHealth",
    "RestoreDomainGuard",
    "RestrictionClearContext",
    "RestrictionClearHook",
    "RestrictionGuard",
    "RunFenced",
    "RuntimeRefused",
    "RuntimeStepSummary",
    "ToolContribution",
    "ToolContributor",
    "ToolQueryContext",
    "WorkCondition",
    "WorkDecision",
    "WorkDiscoveryState",
    "WorkDomainPolicy",
    "WorkEventBatch",
    "WorkItem",
    "WorkKernelRefused",
    "WorkResponsibility",
    "WorkRun",
    "WorkTimelineContribution",
    "arm_result_continuation",
    "assert_run_fencing",
    "build_core_skill_tool_contribution",
    "build_default_registry",
    "build_proactive_tool_handlers",
    "build_production_duty_registry",
    "build_store_profile_lookup",
    "cancel_condition",
    "claim_work_run",
    "complete_work_run",
    "create_work_item",
    "drain_work_event_cursor",
    "evaluate_conditions",
    "fail_work_run",
    "has_active_grant",
    "list_conditions",
    "load_domain_plugins",
    "merge_tool_contributions",
    "merge_work_policies",
    "open_peer_request",
    "reclaim_expired_runs",
    "record_discovery_hysteresis",
    "record_work_decision",
    "register_responsibility",
    "replace_condition",
    "resolve_peer_request",
    "revoke_responsibility",
    "set_condition",
    "sync_board_triggers",
    "wake_waiting_runs",
    "yield_run_to_wait",
]
