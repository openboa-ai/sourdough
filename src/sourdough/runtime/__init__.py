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
    PeerRequest,
    RunFenced,
    WorkCondition,
    WorkItem,
    WorkKernelRefused,
    WorkResponsibility,
    WorkRun,
    assert_run_fencing,
    cancel_condition,
    claim_work_run,
    complete_work_run,
    create_work_item,
    evaluate_conditions,
    fail_work_run,
    has_active_grant,
    list_conditions,
    open_peer_request,
    reclaim_expired_runs,
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
        ProfileBindingLookup,
        build_production_duty_registry,
        sync_board_triggers,
    )
    from .resident import ResidentCompanyService, ResidentHealth

_LAZY_HANDLERS = frozenset(
    {
        "DUTY_BOARD_APPLY_DECISION",
        "DUTY_CEO_RESPOND_MESSAGE",
        "ProfileBindingLookup",
        "build_production_duty_registry",
        "sync_board_triggers",
    }
)
_LAZY_RESIDENT = frozenset({"ResidentCompanyService", "ResidentHealth"})


def __getattr__(name: str) -> Any:
    if name in _LAZY_HANDLERS:
        from . import handlers

        return getattr(handlers, name)
    if name in _LAZY_RESIDENT:
        from . import resident

        return getattr(resident, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseDomainPlugin",
    "CommandDescriptor",
    "CommandDispatchContext",
    "CommittedResultPredicate",
    "CompanyRuntime",
    "DUTY_BOARD_APPLY_DECISION",
    "DUTY_CEO_RESPOND_MESSAGE",
    "DecisionActionContext",
    "DecisionActionHandler",
    "DecisionActionRegistry",
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
    "RunFenced",
    "RuntimeRefused",
    "RuntimeStepSummary",
    "ToolContribution",
    "ToolContributor",
    "ToolQueryContext",
    "WorkCondition",
    "WorkDomainPolicy",
    "WorkItem",
    "WorkKernelRefused",
    "WorkResponsibility",
    "WorkRun",
    "WorkTimelineContribution",
    "assert_run_fencing",
    "build_default_registry",
    "build_production_duty_registry",
    "cancel_condition",
    "claim_work_run",
    "complete_work_run",
    "create_work_item",
    "evaluate_conditions",
    "fail_work_run",
    "has_active_grant",
    "list_conditions",
    "load_domain_plugins",
    "merge_tool_contributions",
    "merge_work_policies",
    "open_peer_request",
    "reclaim_expired_runs",
    "register_responsibility",
    "replace_condition",
    "resolve_peer_request",
    "revoke_responsibility",
    "set_condition",
    "sync_board_triggers",
    "wake_waiting_runs",
    "yield_run_to_wait",
]
