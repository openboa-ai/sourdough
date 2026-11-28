"""Declarative tool flow-class policy, refusal classification, and audit trace redaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from ...agent_runtime.protocol import ToolFlowClass, ToolSpec

_PRIVATE_DESK_READ_TOOLS = frozenset(
    {
        "desk_list_drafts",
        "desk_read_draft",
        "desk_read_offloaded_log",
        "desk_run_command",
        "memory_search_episodes",
        "memory_read_episode",
        "skill_list_active",
        "skill_read",
    }
)
_PRIVATE_DESK_WRITE_TOOLS = frozenset(
    {
        "desk_save_draft",
        "desk_delete_draft",
        "desk_publish_selection",
        "skill_draft_candidate",
        "skill_record_invocation",
    }
)
_PRIVATE_DESK_ALL_TOOLS = frozenset(
    {
        *_PRIVATE_DESK_READ_TOOLS,
        *_PRIVATE_DESK_WRITE_TOOLS,
    }
)
_PEER_INPUT_TOOLS = frozenset(
    {
        "read_messages",
        "read_inbox",
        "discover_handoffs",
        "read_shared_asset",
    }
)
_PEER_OUTBOUND_SINKS = frozenset(
    {
        "append_message",
        "request_peer_response",
        "respond_peer_request",
        "offer_handoff",
        "decline_handoff",
    }
)
_TRADING_WRITE_SINKS = frozenset(
    {
        "submit_company_paper_order",
        "cancel_company_paper_order",
        "submit_company_live_order",
        "propose_live_migration",
        "open_agent_sandbox",
        "submit_agent_sandbox_order",
        "cancel_agent_sandbox_order",
    }
)
_PRIVATE_DESK_RECEIPT_TOOLS = frozenset(
    {
        "desk_list_drafts",
        "desk_read_draft",
        "desk_save_draft",
        "desk_delete_draft",
        "desk_read_offloaded_log",
        "desk_run_command",
        "memory_search_episodes",
        "memory_read_episode",
    }
)
_PRIVATE_RECEIPT_ERROR_TOOLS = frozenset(
    {
        *_PRIVATE_DESK_RECEIPT_TOOLS,
        "desk_publish_selection",
        "skill_read",
        "skill_list_active",
        "skill_draft_candidate",
        "skill_record_invocation",
    }
)
_PUBLIC_RESULT_PRIVATE_TOOLS = frozenset(
    {
        "desk_publish_selection",
        "skill_read",
        "skill_list_active",
        "skill_draft_candidate",
        "skill_record_invocation",
    }
)
_SAFE_DESK_RECEIPT_FIELDS = (
    "ok",
    "revision",
    "count",
    "deleted",
    "returncode",
    "timed_out",
    "quota_exceeded",
)
_NON_AUTHORITY_INPUT_MARKERS = (
    "cursor is unavailable",
    "arguments must be",
    "do not match schema",
    "must be a ",
    "must be an ",
    "invalid cursor",
    "duplicate message",
    "does not exist",
    "revision changed",
    "state version mismatch",
    "trajectory loop detected",
)
_AUTHORITY_REFUSAL_MARKERS = (
    "project discussion is unavailable",
    "shared project is unavailable",
    "shared asset is unavailable",
    "handoff offer is unavailable",
    "decision route is unavailable",
    "tool is unavailable",
    "unauthorized",
    "not authorized",
    "lacks '",
    "lacks ",
    "authority",
    "grant",
    "forbidden",
    "permission",
    "not permitted",
    "not admitted",
    "only the named recipient",
    "only the owner",
    "cannot read private desk",
    "cannot access private desk",
    "cannot write free-form peer",
    "cannot read peer-authored",
    "cannot execute external",
    "cannot execute trading",
    "cannot access",
    "cannot publish",
    "cannot offer",
    "outside the recipient",
    "outside ",
    "belongs to another",
    "not a member",
    "redaction",
    "private desk",
    "symlink",
    "sandbox",
    "spoofed",
    "cross-agent",
    "cross-company",
    "unreadable",
    "not readable",
    "not writable",
)


def classify_tool_flow(name: str, spec: ToolSpec | None = None) -> ToolFlowClass:
    """Resolve the effective information-flow class of a tool, preferring declarative ToolSpec.flow_class."""
    if spec is not None and spec.flow_class != "neutral":
        return spec.flow_class
    if name in _PRIVATE_DESK_READ_TOOLS:
        return "private_desk_read"
    if name in _PRIVATE_DESK_WRITE_TOOLS:
        return "private_desk_write"
    if name in _PEER_INPUT_TOOLS:
        return "peer_input"
    if name in _PEER_OUTBOUND_SINKS:
        return "peer_outbound"
    if name in _TRADING_WRITE_SINKS:
        return "external_write_sink"
    return "neutral"


def is_private_desk_read(name: str, spec: ToolSpec | None = None) -> bool:
    return classify_tool_flow(name, spec) == "private_desk_read"


def is_private_desk_tool(name: str, spec: ToolSpec | None = None) -> bool:
    return classify_tool_flow(name, spec) in ("private_desk_read", "private_desk_write")


def is_peer_input(name: str, spec: ToolSpec | None = None) -> bool:
    return classify_tool_flow(name, spec) == "peer_input"


def is_peer_outbound(name: str, spec: ToolSpec | None = None) -> bool:
    return classify_tool_flow(name, spec) == "peer_outbound"


def is_external_write_sink(name: str, spec: ToolSpec | None = None) -> bool:
    return classify_tool_flow(name, spec) == "external_write_sink"


def is_private_receipt_tool(name: str, spec: ToolSpec | None = None) -> bool:
    if name in _PRIVATE_DESK_RECEIPT_TOOLS:
        return True
    if name in _PUBLIC_RESULT_PRIVATE_TOOLS:
        return False
    return is_private_desk_tool(name, spec)


def is_private_error_tool(name: str, spec: ToolSpec | None = None) -> bool:
    return (
        name in _PRIVATE_RECEIPT_ERROR_TOOLS
        or name.startswith("desk_")
        or is_private_desk_tool(name, spec)
    )


def is_authority_refusal_message(
    error_type: str | None, error_message: str | None
) -> bool:
    """Classify whether a tool refusal represents an authority/isolation breach vs input error."""
    if error_type in (
        "ExecutionTierRefused",
        "AgentRuntimeRefused",
        "RunFenced",
        "LeaseLost",
    ):
        return True
    if not error_message:
        return False
    msg = str(error_message)
    if msg.startswith("redacted_private_authority_error:"):
        return True
    low = msg.lower()
    if any(marker in low for marker in _NON_AUTHORITY_INPUT_MARKERS):
        return False
    return any(marker in low for marker in _AUTHORITY_REFUSAL_MARKERS)


def redact_causal_trace_for_audit(
    db: sqlite3.Connection,
    company_id: str,
    causal_trace: Mapping[str, Any],
) -> dict[str, Any]:
    """Opaque-map one_agent method identifiers before persisting causal_method_trace audit events."""
    from ...method_learning import (
        opaque_one_agent_delta_id,
        opaque_one_agent_method_id,
    )

    one_agent_mids = {
        str(r[0])
        for r in db.execute(
            """SELECT DISTINCT method_id FROM method_delta
               WHERE company_id = ? AND audience_scope = 'one_agent'""",
            (company_id,),
        ).fetchall()
    }
    audit_trace = dict(causal_trace)
    for key in ("available", "retrieved", "invoked"):
        vals = audit_trace.get(key)
        if not isinstance(vals, list):
            continue
        redacted_vals: list[Any] = []
        for v in vals:
            if not isinstance(v, str):
                redacted_vals.append(v)
            elif ":" in v:
                mid, rev = v.rsplit(":", 1)
                redacted_vals.append(
                    opaque_one_agent_delta_id(mid, rev) if mid in one_agent_mids else v
                )
            elif v in one_agent_mids:
                redacted_vals.append(opaque_one_agent_method_id(v))
            else:
                redacted_vals.append(v)
        audit_trace[key] = redacted_vals
    return audit_trace
