"""Unified CompanyRuntime, ResidentCompanyService, DutyRegistry, Work kernel, and venue boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .engine import CompanyRuntime, RuntimeStepSummary, build_default_registry
from .registry import (
    DutyContext,
    DutyDescriptor,
    DutyOutcome,
    DutyRegistry,
    RuntimeRefused,
)
from .venue_coordinator import (
    DRAIN_MARGIN_SECONDS,
    MAX_SEGMENT_SECONDS,
    MIN_SEGMENT_SECONDS,
    BoundedVenueCoordinator,
    VenueSegment,
    VenueSegmentPolicy,
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
        DUTY_QUANT_AGENT_SANDBOX,
        DUTY_QUANT_PAPER_DECISION,
        DUTY_QUANT_RECONCILE,
        ProfileBindingLookup,
        build_production_duty_registry,
        sync_board_triggers,
    )
    from .resident import ResidentCompanyService, ResidentHealth

_LAZY_HANDLERS = frozenset(
    {
        "DUTY_BOARD_APPLY_DECISION",
        "DUTY_CEO_RESPOND_MESSAGE",
        "DUTY_QUANT_AGENT_SANDBOX",
        "DUTY_QUANT_PAPER_DECISION",
        "DUTY_QUANT_RECONCILE",
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
    "BoundedVenueCoordinator",
    "CompanyRuntime",
    "DRAIN_MARGIN_SECONDS",
    "DUTY_BOARD_APPLY_DECISION",
    "DUTY_CEO_RESPOND_MESSAGE",
    "DUTY_QUANT_AGENT_SANDBOX",
    "DUTY_QUANT_PAPER_DECISION",
    "DUTY_QUANT_RECONCILE",
    "DutyContext",
    "DutyDescriptor",
    "DutyOutcome",
    "DutyRegistry",
    "MAX_SEGMENT_SECONDS",
    "MIN_SEGMENT_SECONDS",
    "PeerRequest",
    "ProfileBindingLookup",
    "ResidentCompanyService",
    "ResidentHealth",
    "RunFenced",
    "RuntimeRefused",
    "RuntimeStepSummary",
    "VenueSegment",
    "VenueSegmentPolicy",
    "WorkCondition",
    "WorkItem",
    "WorkKernelRefused",
    "WorkResponsibility",
    "WorkRun",
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
