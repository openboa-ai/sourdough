"""Core Work kernel types, constants, exceptions, and foundational helpers."""

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from hashlib import sha256
from time import time_ns as _stdlib_time_ns
from typing import Any

from ...duty_run import employment_cutoff


def _current_time_ns() -> int:
    facade = sys.modules.get("sourdough.runtime.work_kernel")
    fn = getattr(facade, "time_ns", None) if facade is not None else None
    return int(fn()) if callable(fn) else _stdlib_time_ns()


_OPAQUE_ACTION_ID_RE = re.compile(r"^action:[0-9a-f]{16}$")

MAX_PEER_CHAIN_DEPTH = 4
PAIRWISE_RATE_LIMIT_COUNT = 6
PAIR_WINDOW_NS = 3600 * 1_000_000_000
DEFAULT_DISCOVERY_WAKE_WINDOW_NS = 3_600_000_000_000
DEFAULT_DISCOVERY_WAKE_BUDGET = 16
MAX_DISCOVERY_COOLDOWN_NS = 3_600_000_000_000
MAX_PEER_EVIDENCE_REFS = 32

WORK_DECISION_ACTION_KINDS: frozenset[str] = frozenset(
    {
        "noop",
        "hold",
        "defer",
        "investigate",
        "escalate",
        "proceed",
        "approve",
        "approve_with_sla_cap",
        "reject",
        "publish",
        "delegate",
        "reschedule",
        "trial_decision",
        "apply_baseline",
        "apply_baseline_heuristic",
        "apply_transfer_method",
        "counterexample_verified",
        "boundary_verified",
        "preserve_boundary",
        "counterexample_check",
        "constraint_ignored",
        "policy_bypassed",
        "discard_valid_boundary",
        "deadline_filter_contrast",
        "witness_contrast",
        "witness_ok",
        "execute_with_adopted_gen2",
        "execute_after_rollback",
    }
)

DUTY_TO_WORK_ITEM_STATE: dict[str, str] = {
    "due": "due",
    "scheduled": "scheduled",
    "running": "running",
    "waiting": "waiting",
    "settled": "settled",
    "incomplete": "blocked",
    "archived": "cancelled",
    "handed_off": "cancelled",
}

_VALID_WORK_ITEM_STATES = frozenset(
    {"due", "scheduled", "running", "waiting", "settled", "cancelled", "blocked"}
)

_NEXT_CONDITION_ALLOWED_KEYS = frozenset(
    {
        "responsibility_id",
        "work_item_id",
        "trigger_kind",
        "due_at_ns",
        "source_identity",
        "expected_revision",
        "expires_at_ns",
        "cooldown_ns",
        "max_fires",
        "condition_revision",
    }
)

CORE_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {
        "private_draft",
        "shared_publication",
        "peer_request",
        "work_run",
        "work_item",
        "tool_call_receipt",
    }
)

_PEER_REQUEST_COLUMNS = """request_id, company_id, requester_work_item_id, requester_run_id,
    requester_agent_id, recipient_agent_id, response_work_item_id,
    question, expected_decision, evidence_refs_json, deadline_ns,
    state, response_json, resolved_at_ns, created_at_ns,
    request_key, project_id, read_run_id, answered_run_id, resumed_run_id,
    hop_count, chain_depth, root_work_item_id, delegation_path_json"""


class WorkKernelRefused(Exception):
    """The Work kernel transition violated authority, ownership, or state invariants."""


class RunFenced(WorkKernelRefused):
    """A stale or expired execution run attempted to commit after being fenced."""


@dataclass(frozen=True)
class WorkItem:
    work_item_id: str
    company_id: str
    owner_agent_id: str
    duty_kind: str
    state: str
    source_kind: str
    source_id: str
    parent_work_item_id: str | None
    dedupe_key: str | None
    payload_json: str
    created_at_ns: int
    updated_at_ns: int
    attempt_count: int = 0
    next_retry_at_ns: int | None = None


@dataclass(frozen=True)
class WorkRun:
    run_id: str
    company_id: str
    work_item_id: str
    agent_id: str
    duty_kind: str
    status: str
    fencing_token: int
    lease_owner: str | None
    lease_expires_at_ns: int | None
    wait_until_ns: int | None
    wait_event_key: str | None
    budget_ceiling_minor: int
    dedupe_key: str | None
    outcome_json: str | None
    created_at_ns: int
    updated_at_ns: int


@dataclass(frozen=True)
class WorkResponsibility:
    responsibility_id: str
    company_id: str
    owner_agent_id: str
    duty_kind: str
    remit: str
    readable_scope_json: str
    status: str
    created_at_ns: int
    updated_at_ns: int


@dataclass(frozen=True)
class WorkCondition:
    condition_id: str
    company_id: str
    owner_agent_id: str
    responsibility_id: str | None
    work_item_id: str | None
    condition_revision: int
    trigger_kind: str
    due_at_ns: int | None
    source_identity: str | None
    expected_revision: str | None
    expires_at_ns: int | None
    status: str
    created_at_ns: int
    updated_at_ns: int
    cooldown_ns: int = 0
    max_fires: int | None = None
    fire_count: int = 0


@dataclass(frozen=True)
class WorkDiscoveryState:
    company_id: str
    responsibility_id: str
    consecutive_noops: int
    cooldown_until_ns: int
    window_started_ns: int
    window_wakes: int
    updated_at_ns: int


@dataclass(frozen=True)
class WorkEventBatch:
    last_event_seq: int
    observed_assets: dict[str, str] = field(default_factory=dict)
    asset_authors: dict[str, str] = field(default_factory=dict)
    peer_events: dict[str, str] = field(default_factory=dict)
    events_processed: int = 0
    has_more: bool = False


@dataclass(frozen=True)
class DecisionEvidence:
    kind: str
    resource_id: str
    revision: str
    observed_at_ns: int
    available_at_ns: int


@dataclass(frozen=True)
class WorkDecision:
    decision_id: str
    company_id: str
    work_item_id: str
    run_id: str
    agent_id: str
    action_id: str
    action_kind: str
    evidence_refs_json: str
    evidence_digest: str
    rationale: str
    next_condition_id: str | None
    active_snapshot_id: str | None
    decision_at_ns: int


@dataclass(frozen=True)
class PeerRequest:
    request_id: str
    company_id: str
    requester_work_item_id: str
    requester_run_id: str
    requester_agent_id: str
    recipient_agent_id: str
    response_work_item_id: str
    question: str
    expected_decision: str
    evidence_refs_json: str
    deadline_ns: int
    state: str
    response_json: str | None
    resolved_at_ns: int | None
    created_at_ns: int
    request_key: str | None = None
    project_id: str | None = None
    read_run_id: str | None = None
    answered_run_id: str | None = None
    resumed_run_id: str | None = None
    hop_count: int = 0
    chain_depth: int = 0
    root_work_item_id: str | None = None
    delegation_path_json: str = "[]"


def opaque_decision_action_id(raw_action_id: str) -> str:
    """Return a deterministic opaque digest identifier for a decision action_id."""
    if _OPAQUE_ACTION_ID_RE.fullmatch(raw_action_id):
        return raw_action_id
    return f"action:{sha256(raw_action_id.encode('utf-8')).hexdigest()[:16]}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _require_nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkKernelRefused(f"{name} must be a nonempty string")
    return value


def _require_agent(
    connection: sqlite3.Connection, company_id: str, agent_id: str
) -> None:
    row = connection.execute(
        "SELECT 1 FROM agent WHERE id = ? AND company_id = ?",
        (agent_id, company_id),
    ).fetchone()
    if row is None:
        raise WorkKernelRefused("Agent does not belong to this company")


def has_active_grant(
    connection: sqlite3.Connection,
    company_id: str,
    agent_id: str,
    *,
    required_scope: str | None = None,
) -> bool:
    """Return True iff the Agent is unarchived and holds a current non-revoked standing grant."""
    if employment_cutoff(connection, agent_id) is not None:
        return False
    if required_scope is not None:
        row = connection.execute(
            """SELECT 1
               FROM standing_grant g
               JOIN agent a ON a.id = g.agent_id
               WHERE a.company_id = ? AND a.id = ?
                 AND g.scope = ? AND lower(trim(g.scope)) != 'revoked'""",
            (company_id, agent_id, required_scope),
        ).fetchone()
    else:
        row = connection.execute(
            """SELECT 1
               FROM standing_grant g
               JOIN agent a ON a.id = g.agent_id
               WHERE a.company_id = ? AND a.id = ?
                 AND trim(g.scope) != '' AND lower(trim(g.scope)) != 'revoked'""",
            (company_id, agent_id),
        ).fetchone()
    return row is not None
