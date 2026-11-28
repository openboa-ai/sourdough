"""Pure read-only SQLite projections for the Sourdough Board Client.

Every projection executes inside a single SQLite read_tx snapshot and returns
(payload, event_seq) so ETag and X-Sourdough-Event-Seq never tear. Private Desk
drafts are never opened or disclosed (opaque count only), missing accounting or
valuation telemetry is never coerced to zero, and Core projections remain
domain-neutral unless an installed DomainPlugin enriches them.
"""

from __future__ import annotations

from ...storage.db import open_reader, read_tx
from ..redaction import (
    AUDIT_ONE_AGENT_ENTITY_TYPES,
    _OPAQUE_TIMER_SOURCE_RE,
    _load_one_agent_method_sets,
    _redact_active_method_for_board,
    _redact_condition_source_for_board,
    _redact_evidence_refs_for_board,
    _redact_one_agent_ref_for_board,
    _redact_run_outcome_for_board,
    _redact_work_item_payload_for_board,
    _sanitize_action_kind_for_board,
    load_one_agent_method_sets,
    redact_audit_entity_id_for_board,
)
from ._shared import (
    ALL_CLIENT_PROJECTIONS,
    CORE_CLIENT_PROJECTIONS,
    PROJECTION_AGENT_DETAIL,
    PROJECTION_AGENTS_GRAPH,
    PROJECTION_APPROVALS,
    PROJECTION_FINANCE,
    PROJECTION_MESSAGES,
    PROJECTION_ORDER_TRACE,
    PROJECTION_ORG,
    PROJECTION_RESTRICTIONS,
    PROJECTION_STRATEGIES,
    PROJECTION_SYSTEM_AUDIT,
    PROJECTION_SYSTEM_EVALS,
    PROJECTION_SYSTEM_SAFETY,
    PROJECTION_TRADING_DESK,
    _DEFAULT_AVATAR_VARIANTS,
    _active_restrictions_rows,
    _agent_directory,
    _apply_core_enrichers,
    _close_issues_summary,
    _count_private_drafts,
    _json_loads_safe,
    _sum_gl_accounts,
    projection_etag,
)
from .agents import read_agent_detail_projection, read_agents_graph_projection
from .comms import read_messages_projection
from .governance import (
    read_approvals_projection,
    read_finance_projection,
    read_org_projection,
    read_restrictions_projection,
)
from .system import (
    read_system_audit_projection,
    read_system_evals_projection,
    read_system_safety_projection,
)

__all__ = [
    "ALL_CLIENT_PROJECTIONS",
    "AUDIT_ONE_AGENT_ENTITY_TYPES",
    "CORE_CLIENT_PROJECTIONS",
    "PROJECTION_AGENT_DETAIL",
    "PROJECTION_AGENTS_GRAPH",
    "PROJECTION_APPROVALS",
    "PROJECTION_FINANCE",
    "PROJECTION_MESSAGES",
    "PROJECTION_ORDER_TRACE",
    "PROJECTION_ORG",
    "PROJECTION_RESTRICTIONS",
    "PROJECTION_STRATEGIES",
    "PROJECTION_SYSTEM_AUDIT",
    "PROJECTION_SYSTEM_EVALS",
    "PROJECTION_SYSTEM_SAFETY",
    "PROJECTION_TRADING_DESK",
    "_DEFAULT_AVATAR_VARIANTS",
    "_OPAQUE_TIMER_SOURCE_RE",
    "_active_restrictions_rows",
    "_agent_directory",
    "_apply_core_enrichers",
    "_close_issues_summary",
    "_count_private_drafts",
    "_json_loads_safe",
    "_load_one_agent_method_sets",
    "_redact_active_method_for_board",
    "_redact_condition_source_for_board",
    "_redact_evidence_refs_for_board",
    "_redact_one_agent_ref_for_board",
    "_redact_run_outcome_for_board",
    "_redact_work_item_payload_for_board",
    "_sanitize_action_kind_for_board",
    "_sum_gl_accounts",
    "load_one_agent_method_sets",
    "open_reader",
    "projection_etag",
    "read_agent_detail_projection",
    "read_agents_graph_projection",
    "read_approvals_projection",
    "read_finance_projection",
    "read_messages_projection",
    "read_org_projection",
    "read_restrictions_projection",
    "read_system_audit_projection",
    "read_system_evals_projection",
    "read_system_safety_projection",
    "read_tx",
    "redact_audit_entity_id_for_board",
]
