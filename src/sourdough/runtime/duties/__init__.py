"""Core production duty handlers organized by domain: board, collaboration, discovery, and dreaming."""

from .board import BoardDuties, sync_board_triggers
from .collaboration import CollaborationDuties
from .deps import (
    DUTY_BOARD_APPLY_DECISION,
    DUTY_CEO_RESPOND_MESSAGE,
    DUTY_WORK_ASSESSMENT_REVIEW,
    DUTY_WORK_DISCOVERY_REVIEW,
    DUTY_WORK_DREAMING,
    DUTY_WORK_HANDOFF_FOLLOWUP,
    DUTY_WORK_PEER_RESPONSE,
    CoreDutyDeps,
    build_proactive_tool_handlers,
    build_store_profile_lookup,
    hydrate_resolved_child_peer_outcome,
    read_agent_state,
    read_company_status,
    resolve_profile,
)
from .discovery import DiscoveryDuties
from .dreaming import DreamingDuties, dreaming_precondition

__all__ = [
    "DUTY_BOARD_APPLY_DECISION",
    "DUTY_CEO_RESPOND_MESSAGE",
    "DUTY_WORK_ASSESSMENT_REVIEW",
    "DUTY_WORK_DISCOVERY_REVIEW",
    "DUTY_WORK_DREAMING",
    "DUTY_WORK_HANDOFF_FOLLOWUP",
    "DUTY_WORK_PEER_RESPONSE",
    "BoardDuties",
    "CollaborationDuties",
    "CoreDutyDeps",
    "DiscoveryDuties",
    "DreamingDuties",
    "build_proactive_tool_handlers",
    "build_store_profile_lookup",
    "dreaming_precondition",
    "hydrate_resolved_child_peer_outcome",
    "read_agent_state",
    "read_company_status",
    "resolve_profile",
    "sync_board_triggers",
]
