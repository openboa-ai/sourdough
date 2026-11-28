"""Three-entity Work kernel: items, fenced runs, responsibilities, conditions, and peer requests."""

import json
import re
import sqlite3
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any
from uuid import uuid4

from ..duty_run import employment_cutoff, has_turn_capacity
from ..storage.kernel import append_audit_event

_OPAQUE_ACTION_ID_RE = re.compile(r"^action:[0-9a-f]{16}$")


def opaque_decision_action_id(raw_action_id: str) -> str:
    """Return a deterministic opaque digest identifier for a decision action_id."""
    if _OPAQUE_ACTION_ID_RE.fullmatch(raw_action_id):
        return raw_action_id
    return f"action:{sha256(raw_action_id.encode('utf-8')).hexdigest()[:16]}"


MAX_PEER_CHAIN_DEPTH = 4
PAIRWISE_RATE_LIMIT_COUNT = 6
PAIR_WINDOW_NS = 3600 * 1_000_000_000
DEFAULT_DISCOVERY_WAKE_WINDOW_NS = 3_600_000_000_000
DEFAULT_DISCOVERY_WAKE_BUDGET = 16
MAX_DISCOVERY_COOLDOWN_NS = 3_600_000_000_000


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


_PEER_REQUEST_COLUMNS = """request_id, company_id, requester_work_item_id, requester_run_id,
    requester_agent_id, recipient_agent_id, response_work_item_id,
    question, expected_decision, evidence_refs_json, deadline_ns,
    state, response_json, resolved_at_ns, created_at_ns,
    request_key, project_id, read_run_id, answered_run_id, resumed_run_id,
    hop_count, chain_depth, root_work_item_id, delegation_path_json"""


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


def sync_work_item(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    work_item_id: str,
    owner_agent_id: str,
    duty_kind: str,
    state: str,
    source_kind: str = "duty",
    source_id: str | None = None,
    now_ns: int | None = None,
) -> None:
    """Ensure a canonical work_items row exists for a company duty."""
    timestamp = time_ns() if now_ns is None else now_ns
    if state in DUTY_TO_WORK_ITEM_STATE:
        mapped_state = DUTY_TO_WORK_ITEM_STATE[state]
    elif state in _VALID_WORK_ITEM_STATES:
        mapped_state = state
    else:
        raise WorkKernelRefused(f"Unsupported duty or work item state: {state!r}")
    connection.execute(
        """INSERT INTO work_items (
               work_item_id, company_id, owner_agent_id, duty_kind, state,
               source_kind, source_id, parent_work_item_id, dedupe_key,
               payload_json, created_at_ns, updated_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, '{}', ?, ?)
           ON CONFLICT(work_item_id) DO UPDATE SET
               state = excluded.state,
               updated_at_ns = excluded.updated_at_ns""",
        (
            work_item_id,
            company_id,
            owner_agent_id,
            duty_kind,
            mapped_state,
            source_kind,
            source_id or work_item_id,
            timestamp,
            timestamp,
        ),
    )


def sync_work_run(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    work_item_id: str,
    run_id: str,
    agent_id: str,
    duty_kind: str,
    status: str,
    now_ns: int | None = None,
) -> int:
    """Ensure a canonical work_runs row exists for an execution run and return its fencing_token."""
    timestamp = time_ns() if now_ns is None else now_ns
    existing = connection.execute(
        "SELECT fencing_token FROM work_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if existing is not None:
        connection.execute(
            "UPDATE work_runs SET status = ?, updated_at_ns = ? WHERE run_id = ?",
            (status, timestamp, run_id),
        )
        return int(existing[0])
    max_token = int(
        connection.execute(
            "SELECT COALESCE(MAX(fencing_token), 0) FROM work_runs WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()[0]
    )
    token = max_token + 1
    connection.execute(
        """INSERT INTO work_runs (
               run_id, company_id, work_item_id, agent_id, duty_kind,
               status, fencing_token, lease_owner, lease_expires_at_ns,
               wait_until_ns, wait_event_key, budget_ceiling_minor,
               dedupe_key, outcome_json, created_at_ns, updated_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, 0, NULL, NULL, ?, ?)""",
        (
            run_id,
            company_id,
            work_item_id,
            agent_id,
            duty_kind,
            status,
            token,
            timestamp,
            timestamp,
        ),
    )
    return token


def reconcile_legacy_fenced_runs(connection: sqlite3.Connection, agent_id: str) -> None:
    """Fence any work_runs row whose legacy duty_run was fenced directly."""
    connection.execute(
        """UPDATE work_runs
           SET status = 'fenced', lease_owner = NULL, lease_expires_at_ns = NULL
           WHERE agent_id = ? AND status = 'running'
             AND EXISTS (
                 SELECT 1 FROM duty_run dr
                 WHERE dr.run_id = work_runs.run_id AND dr.is_current = 0
             )""",
        (agent_id,),
    )


def sync_duty_terminal(
    connection: sqlite3.Connection,
    work_item_id: str,
    *,
    item_state: str = "settled",
    run_status: str = "completed",
    now_ns: int | None = None,
) -> None:
    """Release active work_runs and update work_items state when a duty transitions."""
    timestamp = time_ns() if now_ns is None else now_ns
    if item_state == "cancelled":
        pending: list[str] = [work_item_id]
        visited: set[str] = set()
        while pending:
            target_id = pending.pop(0)
            if target_id in visited:
                continue
            visited.add(target_id)
            connection.execute(
                """UPDATE work_runs
                   SET status = ?, lease_owner = NULL, lease_expires_at_ns = NULL,
                       wait_until_ns = NULL, wait_event_key = NULL,
                       updated_at_ns = ?
                   WHERE work_item_id = ? AND status IN ('running', 'waiting', 'queued')""",
                (run_status, timestamp, target_id),
            )
            connection.execute(
                """UPDATE work_items
                   SET state = 'cancelled', updated_at_ns = ?
                   WHERE work_item_id = ?""",
                (timestamp, target_id),
            )
            connection.execute(
                """UPDATE work_conditions
                   SET status = 'cancelled', updated_at_ns = ?
                   WHERE work_item_id = ? AND status = 'armed'""",
                (timestamp, target_id),
            )
            outbound = connection.execute(
                """SELECT request_id, response_work_item_id
                   FROM work_peer_requests
                   WHERE requester_work_item_id = ? AND state = 'open'""",
                (target_id,),
            ).fetchall()
            for req_id, resp_item_id in outbound:
                connection.execute(
                    """UPDATE work_peer_requests
                       SET state = 'expired',
                           response_json = COALESCE(response_json, '{"reason":"requester_cancelled"}'),
                           resolved_at_ns = ?
                       WHERE request_id = ? AND state = 'open'""",
                    (timestamp, str(req_id)),
                )
                pending.append(str(resp_item_id))
        return
    connection.execute(
        """UPDATE work_runs
           SET status = ?, lease_owner = NULL, lease_expires_at_ns = NULL,
               updated_at_ns = ?
           WHERE work_item_id = ? AND status = 'running'""",
        (run_status, timestamp, work_item_id),
    )
    connection.execute(
        """UPDATE work_items
           SET state = ?, updated_at_ns = ?
           WHERE work_item_id = ?""",
        (item_state, timestamp, work_item_id),
    )


def _project_peer_responsibility_matches(
    responsibility_id: str,
    scope_json: str,
    *,
    company_id: str,
    project_id: str,
) -> bool:
    """Match project-managed peer responsibilities by exact project_id in readable_scope_json."""
    if not str(responsibility_id).startswith(f"project-peer:{company_id}:"):
        return False
    try:
        parsed = json.loads(str(scope_json))
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and parsed.get("project_id") == project_id


def upsert_project_peer_responsibility(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    owner_agent_id: str,
    project_id: str,
    allowed_requesters: list[str],
    now_ns: int | None = None,
) -> str:
    """Maintain a standing work.peer_response responsibility scoped to a shared project."""
    timestamp = time_ns() if now_ns is None else now_ns
    scope_json = _canonical_json(
        {
            "project_id": project_id,
            "allowed_requesters": sorted(set(allowed_requesters)),
            "evidence_prefixes": ["shared_publication:"],
        }
    )
    status = "active" if allowed_requesters else "revoked"
    existing_id: str | None = None
    for cand_id, cand_scope in connection.execute(
        """SELECT responsibility_id, readable_scope_json
           FROM work_responsibilities
           WHERE company_id = ? AND owner_agent_id = ?
             AND duty_kind = 'work.peer_response'""",
        (company_id, owner_agent_id),
    ).fetchall():
        if _project_peer_responsibility_matches(
            str(cand_id),
            str(cand_scope),
            company_id=company_id,
            project_id=project_id,
        ):
            existing_id = str(cand_id)
            break
    if existing_id is None:
        key_digest = sha256(
            _canonical_json([project_id, owner_agent_id]).encode("utf-8")
        ).hexdigest()[:24]
        responsibility_id = f"project-peer:{company_id}:{key_digest}"
        connection.execute(
            """INSERT INTO work_responsibilities (
                   responsibility_id, company_id, owner_agent_id, duty_kind, remit,
                   readable_scope_json, status, created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, 'work.peer_response', ?, ?, ?, ?, ?)""",
            (
                responsibility_id,
                company_id,
                owner_agent_id,
                f"Respond to project {project_id} peer requests",
                scope_json,
                status,
                timestamp,
                timestamp,
            ),
        )
    else:
        responsibility_id = existing_id
        connection.execute(
            """UPDATE work_responsibilities
               SET readable_scope_json = ?, status = ?, updated_at_ns = ?
               WHERE responsibility_id = ?""",
            (scope_json, status, timestamp, responsibility_id),
        )
    return responsibility_id


def list_project_peer_responders(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    project_id: str,
) -> tuple[str, ...]:
    """Return active work.peer_response owner_agent_ids configured for a project."""
    rows = connection.execute(
        """SELECT owner_agent_id, responsibility_id, readable_scope_json
           FROM work_responsibilities
           WHERE company_id = ? AND duty_kind = 'work.peer_response' AND status = 'active'
           ORDER BY owner_agent_id""",
        (company_id,),
    ).fetchall()
    return tuple(
        sorted(
            {
                str(owner_id)
                for owner_id, resp_id, scope_json in rows
                if _project_peer_responsibility_matches(
                    str(resp_id),
                    str(scope_json),
                    company_id=company_id,
                    project_id=project_id,
                )
            }
        )
    )


def has_project_peer_policy(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    project_id: str,
) -> bool:
    """Return True if any project-peer responsibility policy rows exist for project_id."""
    rows = connection.execute(
        """SELECT responsibility_id, readable_scope_json
           FROM work_responsibilities
           WHERE company_id = ? AND duty_kind = 'work.peer_response'""",
        (company_id,),
    ).fetchall()
    return any(
        _project_peer_responsibility_matches(
            str(resp_id),
            str(scope_json),
            company_id=company_id,
            project_id=project_id,
        )
        for resp_id, scope_json in rows
    )


def sync_project_peer_responsibilities(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    project_id: str,
    members: Sequence[str],
    active_responders: Sequence[str],
    empty_policy_owner_id: str | None = None,
    now_ns: int | None = None,
) -> None:
    """Activate authorized project peer_responders and revoke removed responders."""
    timestamp = time_ns() if now_ns is None else now_ns
    member_list = list(members)
    active_set = set(active_responders)
    for responder_id in sorted(active_set):
        upsert_project_peer_responsibility(
            connection,
            company_id=company_id,
            owner_agent_id=responder_id,
            project_id=project_id,
            allowed_requesters=[m for m in member_list if m != responder_id],
            now_ns=timestamp,
        )
    existing_rows = connection.execute(
        """SELECT owner_agent_id, responsibility_id, readable_scope_json
           FROM work_responsibilities
           WHERE company_id = ? AND duty_kind = 'work.peer_response' AND status != 'revoked'""",
        (company_id,),
    ).fetchall()
    for owner_id, resp_id, scope_json in existing_rows:
        if (
            _project_peer_responsibility_matches(
                str(resp_id),
                str(scope_json),
                company_id=company_id,
                project_id=project_id,
            )
            and str(owner_id) not in active_set
        ):
            upsert_project_peer_responsibility(
                connection,
                company_id=company_id,
                owner_agent_id=str(owner_id),
                project_id=project_id,
                allowed_requesters=[],
                now_ns=timestamp,
            )
    if (
        not active_set
        and empty_policy_owner_id is not None
        and not has_project_peer_policy(
            connection, company_id=company_id, project_id=project_id
        )
    ):
        upsert_project_peer_responsibility(
            connection,
            company_id=company_id,
            owner_agent_id=empty_policy_owner_id,
            project_id=project_id,
            allowed_requesters=[],
            now_ns=timestamp,
        )


def retire_agent_work(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    actor_id: str,
    now_ns: int | None = None,
) -> None:
    """Atomically fence all Work kernel state for a retired Agent inside the retirement write_tx."""
    timestamp = time_ns() if now_ns is None else now_ns
    connection.execute(
        """UPDATE work_runs
           SET status = 'fenced', lease_owner = NULL, lease_expires_at_ns = NULL,
               updated_at_ns = ?
           WHERE company_id = ? AND agent_id = ? AND status = 'running'""",
        (timestamp, company_id, agent_id),
    )
    connection.execute(
        """UPDATE work_runs
           SET status = 'cancelled', wait_until_ns = NULL, wait_event_key = NULL,
               updated_at_ns = ?
           WHERE company_id = ? AND agent_id = ? AND status = 'waiting'""",
        (timestamp, company_id, agent_id),
    )
    connection.execute(
        """UPDATE work_items
           SET state = 'cancelled', updated_at_ns = ?
           WHERE company_id = ? AND owner_agent_id = ?
             AND state IN ('due', 'scheduled', 'running', 'waiting')""",
        (timestamp, company_id, agent_id),
    )
    connection.execute(
        """UPDATE work_responsibilities
           SET status = 'revoked', updated_at_ns = ?
           WHERE company_id = ? AND owner_agent_id = ? AND status != 'revoked'""",
        (timestamp, company_id, agent_id),
    )
    connection.execute(
        """UPDATE work_conditions
           SET status = 'cancelled', updated_at_ns = ?
           WHERE company_id = ? AND owner_agent_id = ? AND status = 'armed'""",
        (timestamp, company_id, agent_id),
    )
    # Expire open peer requests authored by the retired requester and cancel open response items.
    authored = connection.execute(
        """SELECT request_id, response_work_item_id
           FROM work_peer_requests
           WHERE company_id = ? AND requester_agent_id = ? AND state = 'open'""",
        (company_id, agent_id),
    ).fetchall()
    for req_id, resp_item_id in authored:
        connection.execute(
            """UPDATE work_peer_requests
               SET state = 'expired',
                   response_json = '{"reason":"requester_retired"}',
                   resolved_at_ns = ?
               WHERE company_id = ? AND request_id = ? AND state = 'open'""",
            (timestamp, company_id, str(req_id)),
        )
        connection.execute(
            """UPDATE work_items
               SET state = 'cancelled', updated_at_ns = ?
               WHERE company_id = ? AND work_item_id = ?
                 AND state IN ('due', 'scheduled')""",
            (timestamp, company_id, str(resp_item_id)),
        )
    # Decline open peer requests addressed to the retired recipient and wake non-retired requesters.
    assigned = connection.execute(
        """SELECT request_id, requester_agent_id, response_work_item_id
           FROM work_peer_requests
           WHERE company_id = ? AND recipient_agent_id = ? AND state = 'open'""",
        (company_id, agent_id),
    ).fetchall()
    for req_id, req_agent_id, resp_item_id in assigned:
        connection.execute(
            """UPDATE work_peer_requests
               SET state = 'declined',
                   response_json = '{"reason":"recipient_retired"}',
                   resolved_at_ns = ?
               WHERE company_id = ? AND request_id = ? AND state = 'open'""",
            (timestamp, company_id, str(req_id)),
        )
        connection.execute(
            """UPDATE work_items
               SET state = 'cancelled', updated_at_ns = ?
               WHERE company_id = ? AND work_item_id = ?
                 AND state IN ('due', 'scheduled', 'running')""",
            (timestamp, company_id, str(resp_item_id)),
        )
        if (
            str(req_agent_id) != agent_id
            and employment_cutoff(connection, str(req_agent_id)) is None
        ):
            wake_waiting_runs(
                connection,
                company_id,
                now_ns=timestamp,
                event_key=f"peer_response:{req_id}",
                triggering_actor_id=actor_id,
            )
    append_audit_event(
        connection,
        company_id=company_id,
        domain="work",
        entity_type="agent",
        entity_id=agent_id,
        event_type="work.agent_retired",
        actor_id=actor_id,
        payload={"agent_id": agent_id},
        recorded_at_ns=timestamp,
    )


def create_work_item(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    owner_agent_id: str,
    duty_kind: str,
    source_kind: str,
    source_id: str,
    payload: dict[str, Any] | None = None,
    state: str = "due",
    parent_work_item_id: str | None = None,
    dedupe_key: str | None = None,
    work_item_id: str | None = None,
    actor_id: str = "work_scheduler",
    now_ns: int | None = None,
) -> tuple[WorkItem, bool]:
    """Create or idempotently return a durable work item."""
    for value, label in (
        (company_id, "company_id"),
        (owner_agent_id, "owner_agent_id"),
        (duty_kind, "duty_kind"),
        (source_kind, "source_kind"),
        (source_id, "source_id"),
    ):
        _require_nonempty(value, label)
    if state not in ("due", "scheduled"):
        raise WorkKernelRefused(f"Initial work item state cannot be {state}")
    _require_agent(connection, company_id, owner_agent_id)
    if parent_work_item_id is not None:
        parent_row = connection.execute(
            "SELECT 1 FROM work_items WHERE work_item_id = ? AND company_id = ?",
            (parent_work_item_id, company_id),
        ).fetchone()
        if parent_row is None:
            raise WorkKernelRefused("Parent work item not found for company")
    payload_json = _canonical_json(payload or {})

    if dedupe_key is not None:
        existing = connection.execute(
            """SELECT work_item_id, company_id, owner_agent_id, duty_kind, state,
                      source_kind, source_id, parent_work_item_id, dedupe_key,
                      payload_json, created_at_ns, updated_at_ns,
                      attempt_count, next_retry_at_ns
               FROM work_items WHERE dedupe_key = ?""",
            (dedupe_key,),
        ).fetchone()
        if existing is not None:
            prior = WorkItem(*existing)
            if (
                prior.company_id != company_id
                or prior.owner_agent_id != owner_agent_id
                or prior.duty_kind != duty_kind
                or prior.source_kind != source_kind
                or prior.source_id != source_id
                or prior.parent_work_item_id != parent_work_item_id
                or prior.payload_json != payload_json
            ):
                raise WorkKernelRefused(
                    "Work item dedupe_key reused with conflicting terms"
                )
            return prior, True

    item_id = work_item_id or uuid4().hex
    timestamp = time_ns() if now_ns is None else now_ns
    connection.execute(
        """INSERT INTO work_items (
               work_item_id, company_id, owner_agent_id, duty_kind, state,
               source_kind, source_id, parent_work_item_id, dedupe_key,
               payload_json, created_at_ns, updated_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            company_id,
            owner_agent_id,
            duty_kind,
            state,
            source_kind,
            source_id,
            parent_work_item_id,
            dedupe_key,
            payload_json,
            timestamp,
            timestamp,
        ),
    )
    append_audit_event(
        connection,
        company_id=company_id,
        domain="work",
        entity_type="work_item",
        entity_id=item_id,
        event_type="work.item_created",
        actor_id=actor_id,
        payload={
            "owner_agent_id": owner_agent_id,
            "duty_kind": duty_kind,
            "source_kind": source_kind,
            "source_id": source_id,
            "state": state,
        },
        recorded_at_ns=timestamp,
    )
    return (
        WorkItem(
            item_id,
            company_id,
            owner_agent_id,
            duty_kind,
            state,
            source_kind,
            source_id,
            parent_work_item_id,
            dedupe_key,
            payload_json,
            timestamp,
            timestamp,
        ),
        False,
    )


MAX_PEER_EVIDENCE_REFS = 32


def _verify_project_peer_scope(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    project_id: str,
    requester_agent_id: str,
    recipient_agent_id: str,
    evidence_refs: list[str],
) -> None:
    row = connection.execute(
        "SELECT members FROM shared_project WHERE company_id = ? AND project_id = ?",
        (company_id, project_id),
    ).fetchone()
    if row is None:
        raise WorkKernelRefused("Peer project scope is unavailable")
    members = set(json.loads(str(row[0])))
    if requester_agent_id not in members or recipient_agent_id not in members:
        raise WorkKernelRefused(
            "Requester and recipient must both be active project members"
        )
    for ref in evidence_refs:
        if ref.startswith("shared_publication:"):
            spec = ref.removeprefix("shared_publication:")
            if "@" not in spec:
                raise WorkKernelRefused(
                    "Peer evidence reference requires asset SHA-256"
                )
            asset_id, expected_digest = spec.split("@", 1)
            pub = connection.execute(
                """SELECT project_id, audience, content
                   FROM shared_publication
                   WHERE company_id = ? AND asset_id = ?""",
                (company_id, asset_id),
            ).fetchone()
            if pub is None or str(pub[0]) != project_id:
                raise WorkKernelRefused("Shared asset is unavailable")
            audience = set(json.loads(str(pub[1])))
            if requester_agent_id not in audience or recipient_agent_id not in audience:
                raise WorkKernelRefused("Shared asset is unavailable")
            actual_digest = sha256(str(pub[2]).encode("utf-8")).hexdigest()
            if actual_digest != expected_digest:
                raise WorkKernelRefused(
                    "Peer evidence is outside the admitted revision"
                )


def _authorize_peer_route(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    requester_agent_id: str,
    recipient_agent_id: str,
    evidence_refs: list[str],
    project_id: str | None = None,
    check_open_quota: bool = False,
    check_turn_capacity: bool = True,
) -> str | None:
    """Atomically verify peer-request route, recipient grant/responsibility, evidence scope, and budget."""
    if not has_active_grant(connection, company_id, requester_agent_id):
        raise WorkKernelRefused("Requester Agent has no active standing grant")
    if not has_active_grant(connection, company_id, recipient_agent_id):
        raise WorkKernelRefused("Recipient Agent has no active standing grant")
    if check_turn_capacity:
        if not has_turn_capacity(connection, requester_agent_id):
            raise WorkKernelRefused("Requester Agent has no remaining turn allocation")
        if not has_turn_capacity(connection, recipient_agent_id):
            raise WorkKernelRefused("Recipient Agent has no remaining turn allocation")
    if (
        not isinstance(evidence_refs, list)
        or len(evidence_refs) > MAX_PEER_EVIDENCE_REFS
    ):
        raise WorkKernelRefused("Peer request evidence references exceed allowed limit")
    for ref in evidence_refs:
        if (
            not isinstance(ref, str)
            or not ref.strip()
            or ".." in ref
            or any(ord(ch) < 32 for ch in ref)
        ):
            raise WorkKernelRefused("Invalid peer request evidence reference")
    if project_id is not None:
        _verify_project_peer_scope(
            connection,
            company_id=company_id,
            project_id=project_id,
            requester_agent_id=requester_agent_id,
            recipient_agent_id=recipient_agent_id,
            evidence_refs=evidence_refs,
        )

    rows = connection.execute(
        """SELECT readable_scope_json FROM work_responsibilities
           WHERE company_id = ? AND owner_agent_id = ?
             AND duty_kind = 'work.peer_response' AND status = 'active'
           ORDER BY created_at_ns, responsibility_id""",
        (company_id, recipient_agent_id),
    ).fetchall()
    if not rows:
        raise WorkKernelRefused(
            "Recipient Agent has no active peer_response responsibility"
        )

    open_count: int | None = None
    for (scope_raw,) in rows:
        try:
            scope = json.loads(str(scope_raw))
        except json.JSONDecodeError:
            continue
        if not isinstance(scope, dict):
            continue
        scoped_project = scope.get("project_id")
        if project_id is not None and scoped_project != project_id:
            continue
        if project_id is None and scoped_project is not None:
            try:
                _verify_project_peer_scope(
                    connection,
                    company_id=company_id,
                    project_id=str(scoped_project),
                    requester_agent_id=requester_agent_id,
                    recipient_agent_id=recipient_agent_id,
                    evidence_refs=evidence_refs,
                )
            except WorkKernelRefused:
                continue
        requesters = scope.get("allowed_requesters")
        if (
            not isinstance(requesters, list)
            or not all(isinstance(a, str) and a.strip() for a in requesters)
            or requester_agent_id not in requesters
        ):
            continue
        required_scope = scope.get("required_scope")
        if required_scope is not None:
            if not isinstance(required_scope, str) or not has_active_grant(
                connection,
                company_id,
                recipient_agent_id,
                required_scope=required_scope,
            ):
                continue
        if evidence_refs:
            prefixes = scope.get("evidence_prefixes")
            if not isinstance(prefixes, list) or not all(
                isinstance(p, str) and p.strip() for p in prefixes
            ):
                continue
            if not all(
                any(ref.startswith(prefix) for prefix in prefixes)
                for ref in evidence_refs
            ):
                continue
        max_open = scope.get("max_open_requests")
        if check_open_quota and max_open is not None:
            if type(max_open) is not int or max_open <= 0:
                continue
            if open_count is None:
                open_count = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM work_peer_requests
                           WHERE company_id = ? AND recipient_agent_id = ? AND state = 'open'""",
                        (company_id, recipient_agent_id),
                    ).fetchone()[0]
                )
            if open_count >= max_open:
                raise WorkKernelRefused(
                    "Recipient Agent has reached its open peer-request quota"
                )
        if project_id is not None:
            return project_id
        return str(scoped_project) if scoped_project is not None else None

    raise WorkKernelRefused(
        "Peer request route or evidence scope is not authorized by recipient responsibility"
    )


def has_authorized_peer_response_item(
    connection: sqlite3.Connection, company_id: str, work_item_id: str
) -> bool:
    """Verify a peer_response work item's linked request is open and currently authorized."""
    row = connection.execute(
        """SELECT requester_agent_id, recipient_agent_id, evidence_refs_json,
                  project_id, state
           FROM work_peer_requests
           WHERE company_id = ? AND response_work_item_id = ?""",
        (company_id, work_item_id),
    ).fetchone()
    if row is None:
        return True
    requester_id, recipient_id, evidence_json, project_id, state = row
    if str(state) != "open":
        return False
    try:
        refs = json.loads(str(evidence_json))
    except json.JSONDecodeError:
        return False
    try:
        _authorize_peer_route(
            connection,
            company_id=company_id,
            requester_agent_id=str(requester_id),
            recipient_agent_id=str(recipient_id),
            evidence_refs=refs if isinstance(refs, list) else [],
            project_id=None if project_id is None else str(project_id),
            check_open_quota=False,
            check_turn_capacity=False,
        )
    except WorkKernelRefused:
        return False
    return True


def cancel_closed_peer_response_item(
    connection: sqlite3.Connection,
    company_id: str,
    work_item_id: str,
    *,
    now_ns: int,
) -> bool:
    """Cancel a pending peer_response work item if its linked peer request is closed or past deadline."""
    req_row = connection.execute(
        """SELECT request_id, state, deadline_ns FROM work_peer_requests
           WHERE company_id = ? AND response_work_item_id = ?""",
        (company_id, work_item_id),
    ).fetchone()
    if req_row is None:
        return False
    req_id, state, deadline_ns = req_row
    if str(state) == "open":
        if now_ns < int(deadline_ns):
            return False
        connection.execute(
            """UPDATE work_peer_requests
               SET state = 'expired',
                   response_json = COALESCE(response_json, '{"reason":"deadline_elapsed"}'),
                   resolved_at_ns = ?
               WHERE company_id = ? AND request_id = ? AND state = 'open'""",
            (now_ns, company_id, str(req_id)),
        )
    connection.execute(
        """UPDATE work_items
           SET state = 'cancelled', updated_at_ns = ?
           WHERE company_id = ? AND work_item_id = ?
             AND state IN ('due', 'scheduled')""",
        (now_ns, company_id, work_item_id),
    )
    return True


def claim_work_run(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    work_item_id: str,
    lease_owner: str,
    lease_ttl_ns: int,
    budget_ceiling_minor: int = 0,
    dedupe_key: str | None = None,
    required_scope: str | None = None,
    now_ns: int | None = None,
) -> WorkRun:
    """Atomically claim a due work item with a monotonic fencing_token.

    Enforces single active running execution per agent_id and per work_item_id,
    current non-revoked standing grant authority, and hired-Agent turn capacity.
    """
    _require_nonempty(lease_owner, "lease_owner")
    if lease_ttl_ns <= 0 or budget_ceiling_minor < 0:
        raise WorkKernelRefused("Invalid lease TTL or budget ceiling")
    item_row = connection.execute(
        """SELECT work_item_id, company_id, owner_agent_id, duty_kind, state
           FROM work_items
           WHERE work_item_id = ? AND company_id = ?""",
        (work_item_id, company_id),
    ).fetchone()
    if item_row is None:
        raise WorkKernelRefused("Work item not found for company")
    _, _, agent_id, duty_kind, state = item_row
    if state != "due":
        raise WorkKernelRefused(f"Work item is not claimable in state {state}")
    if (
        connection.execute(
            "SELECT 1 FROM duty WHERE agent_id = ? AND state = 'running' LIMIT 1",
            (agent_id,),
        ).fetchone()
        is not None
    ):
        raise WorkKernelRefused("Agent already has an active running execution")
    timestamp = time_ns() if now_ns is None else now_ns
    if duty_kind == "work.peer_response":
        req_row = connection.execute(
            """SELECT requester_agent_id, recipient_agent_id, evidence_refs_json,
                      project_id, state, deadline_ns
               FROM work_peer_requests
               WHERE company_id = ? AND response_work_item_id = ?""",
            (company_id, work_item_id),
        ).fetchone()
        if req_row is not None:
            (
                requester_id,
                recipient_id,
                evidence_json,
                project_id,
                req_state,
                req_deadline,
            ) = req_row
            if str(req_state) != "open":
                raise WorkKernelRefused("Linked peer request is no longer open")
            if timestamp >= int(req_deadline):
                raise WorkKernelRefused("Linked peer request deadline has elapsed")
            refs = json.loads(str(evidence_json))
            _authorize_peer_route(
                connection,
                company_id=company_id,
                requester_agent_id=str(requester_id),
                recipient_agent_id=str(recipient_id),
                evidence_refs=refs if isinstance(refs, list) else [],
                project_id=None if project_id is None else str(project_id),
                check_open_quota=False,
                check_turn_capacity=False,
            )
    if not has_active_grant(
        connection, company_id, agent_id, required_scope=required_scope
    ):
        raise WorkKernelRefused("Agent has no active standing grant")
    if not has_turn_capacity(connection, agent_id):
        raise WorkKernelRefused("Agent has no remaining turn allocation")
    if dedupe_key is not None:
        dup_run = connection.execute(
            "SELECT 1 FROM work_runs WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        if dup_run is not None:
            raise WorkKernelRefused("Work run dedupe_key already exists")

    max_token = int(
        connection.execute(
            "SELECT COALESCE(MAX(fencing_token), 0) FROM work_runs WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()[0]
    )
    fencing_token = max_token + 1
    run_id = uuid4().hex
    expires_at = timestamp + lease_ttl_ns

    try:
        connection.execute(
            """INSERT INTO work_runs (
                   run_id, company_id, work_item_id, agent_id, duty_kind,
                   status, fencing_token, lease_owner, lease_expires_at_ns,
                   wait_until_ns, wait_event_key, budget_ceiling_minor,
                   dedupe_key, outcome_json, created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL, NULL, ?, ?, NULL, ?, ?)""",
            (
                run_id,
                company_id,
                work_item_id,
                agent_id,
                duty_kind,
                fencing_token,
                lease_owner,
                expires_at,
                budget_ceiling_minor,
                dedupe_key,
                timestamp,
                timestamp,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise WorkKernelRefused(
            "Agent or work item already has an active running execution"
        ) from error

    changed = connection.execute(
        """UPDATE work_items
           SET state = 'running', next_retry_at_ns = NULL, updated_at_ns = ?
           WHERE work_item_id = ? AND state IN ('due', 'scheduled')""",
        (timestamp, work_item_id),
    )
    if changed.rowcount != 1:
        raise WorkKernelRefused("Work item state changed concurrently")

    append_audit_event(
        connection,
        company_id=company_id,
        domain="work",
        entity_type="work_run",
        entity_id=run_id,
        event_type="work.run_claimed",
        actor_id=lease_owner,
        run_id=run_id,
        payload={
            "work_item_id": work_item_id,
            "agent_id": agent_id,
            "fencing_token": fencing_token,
            "lease_expires_at_ns": expires_at,
        },
        recorded_at_ns=timestamp,
    )
    return WorkRun(
        run_id,
        company_id,
        work_item_id,
        agent_id,
        duty_kind,
        "running",
        fencing_token,
        lease_owner,
        expires_at,
        None,
        None,
        budget_ceiling_minor,
        dedupe_key,
        None,
        timestamp,
        timestamp,
    )


def assert_run_fencing(
    connection: sqlite3.Connection,
    run_id: str,
    fencing_token: int,
    *,
    now_ns: int | None = None,
) -> WorkRun:
    """Verify the run is still active at the exact fencing_token and unexpired lease."""
    row = connection.execute(
        """SELECT run_id, company_id, work_item_id, agent_id, duty_kind, status,
                  fencing_token, lease_owner, lease_expires_at_ns, wait_until_ns,
                  wait_event_key, budget_ceiling_minor, dedupe_key, outcome_json,
                  created_at_ns, updated_at_ns
           FROM work_runs
           WHERE run_id = ?""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise RunFenced("Work run does not exist")
    run = WorkRun(*row)
    if run.status != "running" or run.fencing_token != fencing_token:
        raise RunFenced(
            f"Run {run_id} is fenced (status={run.status}, token={run.fencing_token} != {fencing_token})"
        )
    effective_now = time_ns() if now_ns is None else now_ns
    if run.lease_expires_at_ns is not None and effective_now >= run.lease_expires_at_ns:
        raise RunFenced(f"Run {run_id} lease has expired")
    if employment_cutoff(connection, run.agent_id) is not None:
        raise RunFenced(f"Agent {run.agent_id} employment has been retired")
    return run


def yield_run_to_wait(
    connection: sqlite3.Connection,
    run_id: str,
    fencing_token: int,
    *,
    wait_until_ns: int | None = None,
    wait_event_key: str | None = None,
    reason: str = "waiting",
    now_ns: int | None = None,
) -> WorkRun:
    """Transition an active run into a zero-compute durable wait, releasing its active slot."""
    if wait_until_ns is None and (not wait_event_key or not wait_event_key.strip()):
        raise WorkKernelRefused("Durable wait requires wait_until_ns or wait_event_key")
    timestamp = time_ns() if now_ns is None else now_ns
    run = assert_run_fencing(connection, run_id, fencing_token, now_ns=timestamp)
    if not has_active_grant(connection, run.company_id, run.agent_id):
        raise RunFenced(f"Agent {run.agent_id} no longer holds an active grant")
    outcome_json = _canonical_json({"disposition": "waiting", "reason": reason})
    changed = connection.execute(
        """UPDATE work_runs
           SET status = 'waiting', lease_owner = NULL, lease_expires_at_ns = NULL,
               wait_until_ns = ?, wait_event_key = ?, outcome_json = ?, updated_at_ns = ?
           WHERE run_id = ? AND fencing_token = ? AND status = 'running'""",
        (wait_until_ns, wait_event_key, outcome_json, timestamp, run_id, fencing_token),
    )
    if changed.rowcount != 1:
        raise RunFenced("Run was fenced before yielding to durable wait")
    connection.execute(
        "UPDATE work_items SET state = 'waiting', next_retry_at_ns = NULL, updated_at_ns = ? WHERE work_item_id = ?",
        (timestamp, run.work_item_id),
    )
    append_audit_event(
        connection,
        company_id=run.company_id,
        domain="work",
        entity_type="work_run",
        entity_id=run_id,
        event_type="work.run_yielded",
        actor_id=run.agent_id,
        run_id=run_id,
        payload={
            "work_item_id": run.work_item_id,
            "wait_until_ns": wait_until_ns,
            "wait_event_key": wait_event_key,
            "reason": reason,
        },
        recorded_at_ns=timestamp,
    )
    return WorkRun(
        run.run_id,
        run.company_id,
        run.work_item_id,
        run.agent_id,
        run.duty_kind,
        "waiting",
        run.fencing_token,
        None,
        None,
        wait_until_ns,
        wait_event_key,
        run.budget_ceiling_minor,
        run.dedupe_key,
        outcome_json,
        run.created_at_ns,
        timestamp,
    )


def wake_waiting_runs(
    connection: sqlite3.Connection,
    company_id: str,
    *,
    now_ns: int,
    event_key: str | None = None,
    triggering_actor_id: str | None = None,
    event_actors: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Wake eligible waiting runs while enforcing the anti-self-wake invariant."""
    rows = connection.execute(
        """SELECT run_id, work_item_id, agent_id, wait_until_ns, wait_event_key
           FROM work_runs
           WHERE company_id = ? AND status = 'waiting'
           ORDER BY created_at_ns, run_id""",
        (company_id,),
    ).fetchall()
    merged_actors: dict[str, str] = {}
    if event_actors:
        for key, actor in event_actors.items():
            if (
                isinstance(key, str)
                and key.strip()
                and isinstance(actor, str)
                and actor.strip()
            ):
                merged_actors[key] = actor.strip()
    if (
        isinstance(event_key, str)
        and event_key.strip()
        and isinstance(triggering_actor_id, str)
        and triggering_actor_id.strip()
    ):
        merged_actors[event_key] = triggering_actor_id.strip()

    woken_items: list[str] = []
    for run_id, work_item_id, agent_id, wait_until_ns, wait_event_key in rows:
        timer_due = wait_until_ns is not None and now_ns >= int(wait_until_ns)
        event_matched = False
        matched_actor = (
            merged_actors.get(wait_event_key)
            if isinstance(wait_event_key, str)
            else None
        )
        if matched_actor is not None:
            if matched_actor == agent_id:
                event_entity = f"{run_id}:{wait_event_key}"
                recorded = connection.execute(
                    """SELECT 1 FROM audit_event
                       WHERE company_id = ? AND domain = 'work'
                         AND entity_type = 'wait_event' AND entity_id = ?
                         AND actor_id = ?
                         AND event_type = 'work.self_authored_event_suppressed'
                       LIMIT 1""",
                    (company_id, event_entity, agent_id),
                ).fetchone()
                if recorded is None:
                    append_audit_event(
                        connection,
                        company_id=company_id,
                        domain="work",
                        entity_type="wait_event",
                        entity_id=event_entity,
                        event_type="work.self_authored_event_suppressed",
                        actor_id=agent_id,
                        run_id=run_id,
                        payload={"event_key": wait_event_key},
                        recorded_at_ns=now_ns,
                    )
            else:
                if isinstance(wait_event_key, str) and wait_event_key.startswith(
                    "peer_response:"
                ):
                    linked_req_id = wait_event_key.removeprefix("peer_response:")
                    peer_row = connection.execute(
                        """SELECT state FROM work_peer_requests
                           WHERE company_id = ? AND request_id = ?""",
                        (company_id, linked_req_id),
                    ).fetchone()
                    if peer_row is not None and str(peer_row[0]) != "open":
                        event_matched = True
                else:
                    event_matched = True
        if not timer_due and not event_matched:
            continue
        reason = "event" if event_matched else "timer"
        changed = connection.execute(
            """UPDATE work_runs
               SET status = 'completed', updated_at_ns = ?
               WHERE run_id = ? AND status = 'waiting'""",
            (now_ns, run_id),
        )
        if changed.rowcount != 1:
            continue
        connection.execute(
            """UPDATE work_items
               SET state = 'due', next_retry_at_ns = NULL, updated_at_ns = ?
               WHERE work_item_id = ? AND state = 'waiting'""",
            (now_ns, work_item_id),
        )
        if (
            reason == "timer"
            and isinstance(wait_event_key, str)
            and wait_event_key.startswith("peer_response:")
        ):
            expired_req_id = wait_event_key.removeprefix("peer_response:")
            open_peer = connection.execute(
                """SELECT response_work_item_id FROM work_peer_requests
                   WHERE company_id = ? AND request_id = ? AND state = 'open'""",
                (company_id, expired_req_id),
            ).fetchone()
            if open_peer is not None:
                connection.execute(
                    """UPDATE work_peer_requests
                       SET state = 'expired',
                           response_json = COALESCE(response_json, '{"reason":"deadline_elapsed"}'),
                           resolved_at_ns = ?
                       WHERE company_id = ? AND request_id = ? AND state = 'open'""",
                    (now_ns, company_id, expired_req_id),
                )
                resp_item_row = connection.execute(
                    """SELECT state FROM work_items
                       WHERE company_id = ? AND work_item_id = ?""",
                    (company_id, str(open_peer[0])),
                ).fetchone()
                if resp_item_row is not None and str(resp_item_row[0]) in (
                    "due",
                    "scheduled",
                    "running",
                    "waiting",
                ):
                    sync_duty_terminal(
                        connection,
                        str(open_peer[0]),
                        item_state="cancelled",
                        run_status="fenced",
                        now_ns=now_ns,
                    )
        audit_actor = (
            matched_actor if reason == "event" and matched_actor else "work_scheduler"
        )
        append_audit_event(
            connection,
            company_id=company_id,
            domain="work",
            entity_type="work_item",
            entity_id=work_item_id,
            event_type="work.wait_woken",
            actor_id=audit_actor,
            run_id=run_id,
            payload={
                "reason": reason,
                "event_key": wait_event_key if reason == "event" else event_key,
            },
            recorded_at_ns=now_ns,
        )
        woken_items.append(str(work_item_id))
    return tuple(woken_items)


def reclaim_expired_runs(
    connection: sqlite3.Connection, company_id: str, *, now_ns: int
) -> tuple[str, ...]:
    """Fence running work_runs whose lease expired and return items to due."""
    expired = connection.execute(
        """SELECT run_id, work_item_id, fencing_token, agent_id
           FROM work_runs
           WHERE company_id = ? AND status = 'running'
             AND lease_expires_at_ns IS NOT NULL AND lease_expires_at_ns <= ?
           ORDER BY created_at_ns, run_id""",
        (company_id, now_ns),
    ).fetchall()
    store_path: Path | None = None
    for _seq, db_name, db_file in connection.execute("PRAGMA database_list").fetchall():
        if (
            db_name == "main"
            and isinstance(db_file, str)
            and db_file
            and db_file != ":memory:"
        ):
            store_path = Path(db_file)
            break
    fenced_runs: list[str] = []
    for run_id, work_item_id, token, agent_id in expired:
        changed = connection.execute(
            """UPDATE work_runs
               SET status = 'fenced', lease_owner = NULL, updated_at_ns = ?
               WHERE run_id = ? AND fencing_token = ? AND status = 'running'""",
            (now_ns, run_id, token),
        )
        if changed.rowcount != 1:
            continue
        connection.execute(
            """UPDATE work_items
               SET state = 'due', updated_at_ns = ?
               WHERE work_item_id = ? AND state = 'running'""",
            (now_ns, work_item_id),
        )
        append_audit_event(
            connection,
            company_id=company_id,
            domain="work",
            entity_type="work_run",
            entity_id=run_id,
            event_type="work.run_fenced",
            actor_id="work_scheduler",
            run_id=run_id,
            payload={"work_item_id": work_item_id, "fencing_token": token},
            recorded_at_ns=now_ns,
        )
        if store_path is not None:
            try:
                from ..agent_desk import cleanup_run_scratch

                cleanup_run_scratch(store_path, str(agent_id), str(run_id))
            except Exception:
                pass
        fenced_runs.append(str(run_id))
    return tuple(fenced_runs)


def fail_work_run(
    connection: sqlite3.Connection,
    run_id: str,
    fencing_token: int,
    *,
    error_summary: str,
    return_to_due: bool = True,
    increment_attempt: bool = False,
    next_retry_at_ns: int | None = None,
    now_ns: int | None = None,
) -> WorkRun:
    """Record a failed active run and release the Agent's active execution slot."""
    if next_retry_at_ns is not None and (
        type(next_retry_at_ns) is not int or next_retry_at_ns < 0
    ):
        raise WorkKernelRefused("next_retry_at_ns must be a nonnegative integer")
    timestamp = time_ns() if now_ns is None else now_ns
    run = assert_run_fencing(connection, run_id, fencing_token, now_ns=timestamp)
    outcome_json = _canonical_json({"disposition": "failed", "error": error_summary})
    changed = connection.execute(
        """UPDATE work_runs
           SET status = 'failed', lease_owner = NULL, lease_expires_at_ns = NULL,
               outcome_json = ?, updated_at_ns = ?
           WHERE run_id = ? AND fencing_token = ? AND status = 'running'""",
        (outcome_json, timestamp, run_id, fencing_token),
    )
    if changed.rowcount != 1:
        raise RunFenced("Run was fenced before failure transition")
    next_state = "due" if return_to_due else "blocked"
    effective_retry_ns = next_retry_at_ns if return_to_due else None
    connection.execute(
        """UPDATE work_items
           SET state = ?,
               attempt_count = CASE WHEN ? THEN attempt_count + 1 ELSE attempt_count END,
               next_retry_at_ns = ?,
               updated_at_ns = ?
           WHERE work_item_id = ?""",
        (
            next_state,
            1 if increment_attempt else 0,
            effective_retry_ns,
            timestamp,
            run.work_item_id,
        ),
    )
    attempt_row = connection.execute(
        "SELECT attempt_count FROM work_items WHERE work_item_id = ?",
        (run.work_item_id,),
    ).fetchone()
    current_attempt = int(attempt_row[0]) if attempt_row is not None else 0
    append_audit_event(
        connection,
        company_id=run.company_id,
        domain="work",
        entity_type="work_run",
        entity_id=run_id,
        event_type="work.run_failed",
        actor_id=run.agent_id,
        run_id=run_id,
        payload={
            "work_item_id": run.work_item_id,
            "error": error_summary,
            "item_state": next_state,
            "attempt_count": current_attempt,
            "next_retry_at_ns": effective_retry_ns,
        },
        recorded_at_ns=timestamp,
    )
    return WorkRun(
        run.run_id,
        run.company_id,
        run.work_item_id,
        run.agent_id,
        run.duty_kind,
        "failed",
        run.fencing_token,
        None,
        None,
        None,
        None,
        run.budget_ceiling_minor,
        run.dedupe_key,
        outcome_json,
        run.created_at_ns,
        timestamp,
    )


def _resolve_next_condition_revision(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    next_condition: Mapping[str, Any],
) -> int:
    if "condition_revision" in next_condition:
        return int(next_condition["condition_revision"])
    source_identity = next_condition.get("source_identity")
    if isinstance(source_identity, str) and source_identity.strip():
        max_cond_rev = int(
            connection.execute(
                """SELECT COALESCE(MAX(condition_revision), 0)
                   FROM work_conditions
                   WHERE company_id = ? AND source_identity = ?""",
                (company_id, source_identity),
            ).fetchone()[0]
        )
        prefix = f"{source_identity}@"
        max_occ_rev = int(
            connection.execute(
                """SELECT COALESCE(MAX(condition_revision), 0)
                   FROM work_condition_occurrences
                   WHERE company_id = ?
                     AND (
                         source_identity = ?
                         OR substr(source_identity, 1, ?) = ?
                         OR condition_id IN (
                             SELECT condition_id
                             FROM work_conditions
                             WHERE company_id = ? AND source_identity = ?
                         )
                     )""",
                (
                    company_id,
                    source_identity,
                    len(prefix),
                    prefix,
                    company_id,
                    source_identity,
                ),
            ).fetchone()[0]
        )
        return max(max_cond_rev, max_occ_rev) + 1
    return 1


def _normalize_next_condition_work_item_id(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    work_item_id: str,
    next_condition: Mapping[str, Any],
    always_link_work_item: bool = False,
) -> str | None:
    """Normalize condition work_item_id consistently for both arming and replay verification."""
    resp_arg = next_condition.get("responsibility_id")
    is_discretionary_resp = False
    if isinstance(resp_arg, str) and resp_arg.strip():
        r_row = connection.execute(
            """SELECT duty_kind FROM work_responsibilities
               WHERE company_id = ? AND responsibility_id = ?""",
            (company_id, resp_arg.strip()),
        ).fetchone()
        if r_row is not None and str(r_row[0]) in (
            "work.discovery_review",
            "work.dreaming",
        ):
            is_discretionary_resp = True
    if is_discretionary_resp:
        return None
    if always_link_work_item:
        return work_item_id
    if resp_arg is None and next_condition.get("work_item_id") is None:
        return work_item_id
    raw_item = next_condition.get("work_item_id")
    return None if raw_item is None else str(raw_item)


def _arm_next_condition(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    owner_agent_id: str,
    work_item_id: str,
    next_condition: Mapping[str, Any],
    now_ns: int,
    always_link_work_item: bool = False,
) -> WorkCondition:
    next_revision = _resolve_next_condition_revision(
        connection, company_id=company_id, next_condition=next_condition
    )
    resp_arg = next_condition.get("responsibility_id")
    item_arg = _normalize_next_condition_work_item_id(
        connection,
        company_id=company_id,
        work_item_id=work_item_id,
        next_condition=next_condition,
        always_link_work_item=always_link_work_item,
    )
    if resp_arg is None and item_arg is not None:
        wi_row = connection.execute(
            """SELECT duty_kind, payload_json FROM work_items
               WHERE company_id = ? AND work_item_id = ?""",
            (company_id, item_arg),
        ).fetchone()
        if wi_row is not None and str(wi_row[0]) in (
            "work.discovery_review",
            "work.dreaming",
        ):
            try:
                wi_payload = json.loads(str(wi_row[1]))
            except json.JSONDecodeError:
                wi_payload = {}
            cand_resp = (
                wi_payload.get("responsibility_id")
                if isinstance(wi_payload, dict)
                else None
            )
            if isinstance(cand_resp, str) and cand_resp.strip():
                r_row = connection.execute(
                    """SELECT 1 FROM work_responsibilities
                       WHERE company_id = ? AND responsibility_id = ?
                         AND owner_agent_id = ? AND status = 'active'""",
                    (company_id, cand_resp.strip(), owner_agent_id),
                ).fetchone()
                if r_row is not None:
                    resp_arg = cand_resp.strip()
                    item_arg = None
    return set_condition(
        connection,
        company_id=company_id,
        owner_agent_id=owner_agent_id,
        trigger_kind=next_condition["trigger_kind"],
        responsibility_id=resp_arg,
        work_item_id=item_arg,
        due_at_ns=next_condition.get("due_at_ns"),
        source_identity=next_condition.get("source_identity"),
        expected_revision=next_condition.get("expected_revision"),
        expires_at_ns=next_condition.get("expires_at_ns"),
        cooldown_ns=next_condition.get("cooldown_ns", 0),
        max_fires=next_condition.get("max_fires"),
        condition_revision=next_revision,
        now_ns=now_ns,
    )


def complete_work_run(
    connection: sqlite3.Connection,
    run_id: str,
    fencing_token: int,
    *,
    outcome: dict[str, Any],
    item_state: str = "settled",
    next_condition: dict[str, Any] | None = None,
    now_ns: int | None = None,
) -> tuple[WorkRun, WorkCondition | None]:
    """Atomically settle an active run, its work item, and an optional next condition."""
    if item_state not in ("settled", "blocked", "cancelled"):
        raise WorkKernelRefused(f"Unsupported terminal item state: {item_state}")
    timestamp = time_ns() if now_ns is None else now_ns
    run = assert_run_fencing(connection, run_id, fencing_token, now_ns=timestamp)
    if not has_active_grant(connection, run.company_id, run.agent_id):
        raise RunFenced(f"Agent {run.agent_id} no longer holds an active grant")
    validated_next = (
        validate_next_condition_dict(next_condition)
        if next_condition is not None
        else None
    )
    outcome_json = _canonical_json(outcome)
    changed = connection.execute(
        """UPDATE work_runs
           SET status = 'completed', lease_owner = NULL, lease_expires_at_ns = NULL,
               outcome_json = ?, updated_at_ns = ?
           WHERE run_id = ? AND fencing_token = ? AND status = 'running'""",
        (outcome_json, timestamp, run_id, fencing_token),
    )
    if changed.rowcount != 1:
        raise RunFenced("Run was fenced before completion")
    connection.execute(
        "UPDATE work_items SET state = ?, next_retry_at_ns = NULL, updated_at_ns = ? WHERE work_item_id = ?",
        (item_state, timestamp, run.work_item_id),
    )
    armed: WorkCondition | None = None
    if validated_next is not None:
        armed = _arm_next_condition(
            connection,
            company_id=run.company_id,
            owner_agent_id=run.agent_id,
            work_item_id=run.work_item_id,
            next_condition=validated_next,
            now_ns=timestamp,
            always_link_work_item=(
                run.duty_kind not in ("work.discovery_review", "work.dreaming")
                and validated_next.get("responsibility_id") is None
            ),
        )
    append_audit_event(
        connection,
        company_id=run.company_id,
        domain="work",
        entity_type="work_run",
        entity_id=run_id,
        event_type="work.run_completed",
        actor_id=run.agent_id,
        run_id=run_id,
        payload={
            "work_item_id": run.work_item_id,
            "item_state": item_state,
            "next_condition_id": armed.condition_id if armed else None,
        },
        recorded_at_ns=timestamp,
    )
    completed = WorkRun(
        run.run_id,
        run.company_id,
        run.work_item_id,
        run.agent_id,
        run.duty_kind,
        "completed",
        run.fencing_token,
        None,
        None,
        None,
        None,
        run.budget_ceiling_minor,
        run.dedupe_key,
        outcome_json,
        run.created_at_ns,
        timestamp,
    )
    return completed, armed


def register_responsibility(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    owner_agent_id: str,
    duty_kind: str,
    remit: str,
    readable_scope: list[str] | dict[str, Any],
    responsibility_id: str | None = None,
    now_ns: int | None = None,
) -> WorkResponsibility:
    """Register a standing responsibility for proactive work discovery."""
    for value, label in (
        (company_id, "company_id"),
        (owner_agent_id, "owner_agent_id"),
        (duty_kind, "duty_kind"),
        (remit, "remit"),
    ):
        _require_nonempty(value, label)
    _require_agent(connection, company_id, owner_agent_id)
    rid = responsibility_id or uuid4().hex
    timestamp = time_ns() if now_ns is None else now_ns
    scope_json = _canonical_json(readable_scope)
    connection.execute(
        """INSERT INTO work_responsibilities (
               responsibility_id, company_id, owner_agent_id, duty_kind,
               remit, readable_scope_json, status, created_at_ns, updated_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (
            rid,
            company_id,
            owner_agent_id,
            duty_kind,
            remit,
            scope_json,
            timestamp,
            timestamp,
        ),
    )
    return WorkResponsibility(
        rid,
        company_id,
        owner_agent_id,
        duty_kind,
        remit,
        scope_json,
        "active",
        timestamp,
        timestamp,
    )


def revoke_responsibility(
    connection: sqlite3.Connection,
    company_id: str,
    responsibility_id: str,
    *,
    now_ns: int | None = None,
) -> None:
    timestamp = time_ns() if now_ns is None else now_ns
    changed = connection.execute(
        """UPDATE work_responsibilities
           SET status = 'revoked', updated_at_ns = ?
           WHERE company_id = ? AND responsibility_id = ?""",
        (timestamp, company_id, responsibility_id),
    )
    if changed.rowcount != 1:
        raise WorkKernelRefused("Responsibility not found")
    connection.execute(
        """UPDATE work_conditions
           SET status = 'cancelled', updated_at_ns = ?
           WHERE company_id = ? AND responsibility_id = ? AND status = 'armed'""",
        (timestamp, company_id, responsibility_id),
    )


def list_conditions(
    connection: sqlite3.Connection, company_id: str, owner_agent_id: str
) -> tuple[WorkCondition, ...]:
    rows = connection.execute(
        """SELECT condition_id, company_id, owner_agent_id, responsibility_id,
                  work_item_id, condition_revision, trigger_kind, due_at_ns,
                  source_identity, expected_revision, expires_at_ns, status,
                  created_at_ns, updated_at_ns, cooldown_ns, max_fires, fire_count
           FROM work_conditions
           WHERE company_id = ? AND owner_agent_id = ?
           ORDER BY created_at_ns, condition_id""",
        (company_id, owner_agent_id),
    ).fetchall()
    return tuple(WorkCondition(*row) for row in rows)


def _is_authorized_for_shared_publication(
    connection: sqlite3.Connection,
    company_id: str,
    agent_id: str,
    asset_id: str,
) -> bool | None:
    """Return True if agent may access existing shared_publication, False if unauthorized, None if nonexistent."""
    pub_row = connection.execute(
        """SELECT company_id, project_id, audience
           FROM shared_publication
           WHERE asset_id = ?""",
        (asset_id,),
    ).fetchone()
    if pub_row is None:
        return None
    pub_company, pub_project, audience_json = pub_row
    if str(pub_company) != company_id:
        return False
    try:
        audience = set(json.loads(str(audience_json)))
    except json.JSONDecodeError:
        return False
    proj_row = connection.execute(
        "SELECT members FROM shared_project WHERE company_id = ? AND project_id = ?",
        (company_id, str(pub_project)),
    ).fetchone()
    if proj_row is None:
        return False
    try:
        members = set(json.loads(str(proj_row[0])))
    except json.JSONDecodeError:
        return False
    return agent_id in audience and agent_id in members


def _insert_work_condition(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    owner_agent_id: str,
    trigger_kind: str,
    responsibility_id: str | None = None,
    work_item_id: str | None = None,
    due_at_ns: int | None = None,
    source_identity: str | None = None,
    expected_revision: str | None = None,
    expires_at_ns: int | None = None,
    cooldown_ns: int = 0,
    max_fires: int | None = None,
    condition_revision: int = 1,
    condition_id: str | None = None,
    now_ns: int | None = None,
    require_parent_link: bool = True,
    allow_reserved_source: bool = False,
) -> WorkCondition:
    """Internal helper to validate and insert a work_conditions row."""
    _require_agent(connection, company_id, owner_agent_id)
    if (
        not allow_reserved_source
        and isinstance(source_identity, str)
        and source_identity.startswith("result_dep:")
    ):
        raise WorkKernelRefused(
            "Reserved source_identity prefix 'result_dep:' may only be armed via arm_result_continuation"
        )
    if type(condition_revision) is not int or condition_revision < 1:
        raise WorkKernelRefused("condition_revision must be a positive integer")
    if type(cooldown_ns) is not int or cooldown_ns < 0:
        raise WorkKernelRefused("cooldown_ns must be a nonnegative integer")
    if max_fires is not None and (type(max_fires) is not int or max_fires < 1):
        raise WorkKernelRefused("max_fires must be a positive integer")
    if trigger_kind not in ("timer", "shared_asset_revision", "peer_response"):
        raise WorkKernelRefused(f"Unsupported condition trigger_kind: {trigger_kind}")
    if trigger_kind == "timer" and due_at_ns is None:
        raise WorkKernelRefused("Timer condition requires due_at_ns")
    if due_at_ns is not None and (type(due_at_ns) is not int or due_at_ns < 0):
        raise WorkKernelRefused("due_at_ns must be a nonnegative integer")
    if expires_at_ns is not None and (
        type(expires_at_ns) is not int or expires_at_ns < 0
    ):
        raise WorkKernelRefused("expires_at_ns must be a nonnegative integer")
    if trigger_kind == "shared_asset_revision":
        if not source_identity or not source_identity.strip():
            raise WorkKernelRefused("Asset revision condition requires source_identity")
        if (
            _is_authorized_for_shared_publication(
                connection, company_id, owner_agent_id, source_identity
            )
            is False
        ):
            raise WorkKernelRefused(
                "Agent is not authorized to watch shared_publication"
            )
    if trigger_kind == "peer_response" and (
        not source_identity or not source_identity.strip()
    ):
        raise WorkKernelRefused("Peer response condition requires source_identity")
    if require_parent_link and responsibility_id is None and work_item_id is None:
        raise WorkKernelRefused(
            "Condition must link a responsibility_id or work_item_id"
        )
    is_result_dep_cond = isinstance(
        source_identity, str
    ) and source_identity.startswith("result_dep:")
    if responsibility_id is not None:
        row = connection.execute(
            """SELECT owner_agent_id, status, duty_kind, readable_scope_json
               FROM work_responsibilities
               WHERE company_id = ? AND responsibility_id = ?""",
            (company_id, responsibility_id),
        ).fetchone()
        if row is None or row[0] != owner_agent_id or row[1] != "active":
            raise WorkKernelRefused(
                "Responsibility is inactive or owned by another Agent"
            )
        if is_result_dep_cond and str(row[2]) in (
            "work.discovery_review",
            "work.dreaming",
        ):
            wake_budget, _, _ = _parse_scope_policy(str(row[3]))
            armed_resp_deps = int(
                connection.execute(
                    """SELECT COUNT(*) FROM work_conditions
                       WHERE company_id = ? AND owner_agent_id = ?
                         AND responsibility_id = ? AND status = 'armed'
                         AND source_identity LIKE 'result_dep:%'""",
                    (company_id, owner_agent_id, responsibility_id),
                ).fetchone()[0]
            )
            if armed_resp_deps >= wake_budget:
                raise WorkKernelRefused(
                    "Standing responsibility armed result continuation cap reached"
                )
    if work_item_id is not None:
        item_owner = connection.execute(
            """SELECT owner_agent_id, duty_kind FROM work_items
               WHERE company_id = ? AND work_item_id = ?""",
            (company_id, work_item_id),
        ).fetchone()
        if item_owner is None or item_owner[0] != owner_agent_id:
            raise WorkKernelRefused(
                "Work item does not exist or belongs to another Agent"
            )
        if (
            responsibility_id is None
            and str(item_owner[1]) in ("work.discovery_review", "work.dreaming")
            and trigger_kind != "peer_response"
        ):
            existing_item_cond = connection.execute(
                """SELECT 1 FROM work_conditions
                   WHERE company_id = ? AND owner_agent_id = ?
                     AND work_item_id = ? AND responsibility_id IS NULL
                     AND status IN ('armed', 'triggered')
                     AND trigger_kind != 'peer_response'
                   LIMIT 1""",
                (company_id, owner_agent_id, work_item_id),
            ).fetchone()
            if existing_item_cond is not None:
                raise WorkKernelRefused(
                    "Discretionary discovery/dreaming work item may have at most one armed "
                    "item-linked continuation (use replace_condition or bind a standing responsibility)"
                )

    cid = condition_id or uuid4().hex
    timestamp = time_ns() if now_ns is None else now_ns
    effective_source = source_identity or f"timer:{cid}"
    connection.execute(
        """INSERT INTO work_conditions (
               condition_id, company_id, owner_agent_id, responsibility_id,
               work_item_id, condition_revision, trigger_kind, due_at_ns,
               source_identity, expected_revision, expires_at_ns,
               cooldown_ns, max_fires, fire_count, status,
               created_at_ns, updated_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'armed', ?, ?)""",
        (
            cid,
            company_id,
            owner_agent_id,
            responsibility_id,
            work_item_id,
            condition_revision,
            trigger_kind,
            due_at_ns,
            effective_source,
            expected_revision,
            expires_at_ns,
            cooldown_ns,
            max_fires,
            timestamp,
            timestamp,
        ),
    )
    return WorkCondition(
        cid,
        company_id,
        owner_agent_id,
        responsibility_id,
        work_item_id,
        condition_revision,
        trigger_kind,
        due_at_ns,
        effective_source,
        expected_revision,
        expires_at_ns,
        "armed",
        timestamp,
        timestamp,
        cooldown_ns=cooldown_ns,
        max_fires=max_fires,
        fire_count=0,
    )


def set_condition(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    owner_agent_id: str,
    trigger_kind: str,
    responsibility_id: str | None = None,
    work_item_id: str | None = None,
    due_at_ns: int | None = None,
    source_identity: str | None = None,
    expected_revision: str | None = None,
    expires_at_ns: int | None = None,
    cooldown_ns: int = 0,
    max_fires: int | None = None,
    condition_revision: int = 1,
    condition_id: str | None = None,
    now_ns: int | None = None,
) -> WorkCondition:
    """Arm an Agent-owned next condition (timer, shared_asset_revision, or peer_response)."""
    return _insert_work_condition(
        connection,
        company_id=company_id,
        owner_agent_id=owner_agent_id,
        trigger_kind=trigger_kind,
        responsibility_id=responsibility_id,
        work_item_id=work_item_id,
        due_at_ns=due_at_ns,
        source_identity=source_identity,
        expected_revision=expected_revision,
        expires_at_ns=expires_at_ns,
        cooldown_ns=cooldown_ns,
        max_fires=max_fires,
        condition_revision=condition_revision,
        condition_id=condition_id,
        now_ns=now_ns,
        require_parent_link=True,
    )


def arm_result_continuation(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    owner_agent_id: str,
    dependency_asset_id: str,
    due_at_ns: int,
    now_ns: int | None = None,
    responsibility_id: str | None = None,
    work_item_id: str | None = None,
    expires_at_ns: int | None = None,
    condition_id: str | None = None,
) -> WorkCondition:
    """Arm a timer continuation gated on a committed dependency publication."""
    _require_nonempty(dependency_asset_id, "dependency_asset_id")
    if type(due_at_ns) is not int or due_at_ns < 0:
        raise WorkKernelRefused("due_at_ns must be a nonnegative integer")
    if (
        _is_authorized_for_shared_publication(
            connection, company_id, owner_agent_id, dependency_asset_id
        )
        is False
    ):
        raise WorkKernelRefused(
            "Agent is not authorized to watch dependency shared_publication"
        )
    cid = condition_id or uuid4().hex
    return _insert_work_condition(
        connection,
        company_id=company_id,
        owner_agent_id=owner_agent_id,
        trigger_kind="timer",
        responsibility_id=responsibility_id,
        work_item_id=work_item_id,
        due_at_ns=due_at_ns,
        source_identity=f"result_dep:{cid}",
        expected_revision=dependency_asset_id,
        expires_at_ns=expires_at_ns,
        condition_id=cid,
        now_ns=now_ns,
        require_parent_link=False,
        allow_reserved_source=True,
    )


def replace_condition(
    connection: sqlite3.Connection,
    condition_id: str,
    *,
    owner_agent_id: str,
    trigger_kind: str,
    due_at_ns: int | None = None,
    source_identity: str | None = None,
    expected_revision: str | None = None,
    expires_at_ns: int | None = None,
    cooldown_ns: int | None = None,
    max_fires: int | None = None,
    expected_condition_revision: int | None = None,
    now_ns: int | None = None,
) -> WorkCondition:
    """Supersede an armed condition and arm a successor with incremented condition_revision."""
    row = connection.execute(
        """SELECT condition_id, company_id, owner_agent_id, responsibility_id,
                  work_item_id, condition_revision, status, cooldown_ns, max_fires
           FROM work_conditions WHERE condition_id = ?""",
        (condition_id,),
    ).fetchone()
    if row is None:
        raise WorkKernelRefused("Condition not found")
    (
        _,
        company_id,
        actual_owner,
        resp_id,
        item_id,
        revision,
        status,
        prev_cooldown_ns,
        prev_max_fires,
    ) = row
    if actual_owner != owner_agent_id:
        raise WorkKernelRefused("Condition belongs to another Agent")
    if status != "armed":
        raise WorkKernelRefused(f"Only armed conditions can be replaced (got {status})")
    if (
        expected_condition_revision is not None
        and int(revision) != expected_condition_revision
    ):
        raise WorkKernelRefused(
            f"Condition revision mismatch (expected {expected_condition_revision}, got {revision})"
        )
    timestamp = time_ns() if now_ns is None else now_ns
    changed = connection.execute(
        """UPDATE work_conditions
           SET status = 'superseded', updated_at_ns = ?
           WHERE condition_id = ? AND owner_agent_id = ?
             AND condition_revision = ? AND status = 'armed'""",
        (timestamp, condition_id, owner_agent_id, int(revision)),
    )
    if changed.rowcount != 1:
        raise WorkKernelRefused("Condition was modified concurrently before replace")
    return _insert_work_condition(
        connection,
        company_id=company_id,
        owner_agent_id=owner_agent_id,
        trigger_kind=trigger_kind,
        responsibility_id=resp_id,
        work_item_id=item_id,
        due_at_ns=due_at_ns,
        source_identity=source_identity,
        expected_revision=expected_revision,
        expires_at_ns=expires_at_ns,
        cooldown_ns=int(prev_cooldown_ns) if cooldown_ns is None else cooldown_ns,
        max_fires=prev_max_fires if max_fires is None else max_fires,
        condition_revision=int(revision) + 1,
        now_ns=timestamp,
        require_parent_link=(resp_id is not None or item_id is not None),
    )


def cancel_condition(
    connection: sqlite3.Connection,
    condition_id: str,
    *,
    owner_agent_id: str,
    expected_condition_revision: int | None = None,
    now_ns: int | None = None,
) -> None:
    timestamp = time_ns() if now_ns is None else now_ns
    if expected_condition_revision is not None:
        changed = connection.execute(
            """UPDATE work_conditions
               SET status = 'cancelled', updated_at_ns = ?
               WHERE condition_id = ? AND owner_agent_id = ?
                 AND condition_revision = ? AND status = 'armed'""",
            (timestamp, condition_id, owner_agent_id, expected_condition_revision),
        )
    else:
        changed = connection.execute(
            """UPDATE work_conditions
               SET status = 'cancelled', updated_at_ns = ?
               WHERE condition_id = ? AND owner_agent_id = ? AND status = 'armed'""",
            (timestamp, condition_id, owner_agent_id),
        )
    if changed.rowcount != 1:
        raise WorkKernelRefused("Armed condition not found for this Agent")


def _parse_scope_policy(
    raw_scope_json: str | None,
) -> tuple[int, int, int]:
    """Return (wake_budget, wake_window_ns, configured_cooldown_ns) from responsibility scope."""
    wake_budget = DEFAULT_DISCOVERY_WAKE_BUDGET
    wake_window_ns = DEFAULT_DISCOVERY_WAKE_WINDOW_NS
    configured_cooldown_ns = 0
    if not isinstance(raw_scope_json, str) or not raw_scope_json.strip():
        return wake_budget, wake_window_ns, configured_cooldown_ns
    try:
        parsed = json.loads(raw_scope_json)
    except json.JSONDecodeError:
        return wake_budget, wake_window_ns, configured_cooldown_ns
    if not isinstance(parsed, dict):
        return wake_budget, wake_window_ns, configured_cooldown_ns
    raw_budget = parsed.get("max_wakes_per_window")
    if type(raw_budget) is int and raw_budget >= 1:
        wake_budget = raw_budget
    raw_window = parsed.get("wake_window_ns")
    if type(raw_window) is int and raw_window >= 1:
        wake_window_ns = raw_window
    raw_cooldown = parsed.get("cooldown_ns", parsed.get("backoff_base_ns"))
    if type(raw_cooldown) is int and raw_cooldown >= 0:
        configured_cooldown_ns = raw_cooldown
    return wake_budget, wake_window_ns, configured_cooldown_ns


def drain_work_event_cursor(
    connection: sqlite3.Connection,
    company_id: str,
    *,
    now_ns: int,
    max_events: int = 256,
) -> WorkEventBatch:
    """Sequentially drain durable work-relevant audit events up to max_events."""
    _require_nonempty(company_id, "company_id")
    if type(max_events) is not int or max_events < 1:
        raise WorkKernelRefused("max_events must be a positive integer")
    cursor_row = connection.execute(
        "SELECT last_event_seq FROM work_event_cursor WHERE company_id = ?",
        (company_id,),
    ).fetchone()
    last_seq = int(cursor_row[0]) if cursor_row is not None else 0
    rows = connection.execute(
        """SELECT event_seq, event_type, entity_id, actor_id, payload_json
           FROM audit_event
           WHERE company_id = ? AND event_seq > ?
           ORDER BY event_seq ASC
           LIMIT ?""",
        (company_id, last_seq, max_events + 1),
    ).fetchall()
    has_more = len(rows) > max_events
    batch_rows = rows[:max_events]

    observed_assets: dict[str, str] = {}
    asset_authors: dict[str, str] = {}
    peer_events: dict[str, str] = {}
    max_seq = last_seq
    for event_seq, event_type, entity_id, actor_id, payload_json in batch_rows:
        seq = int(event_seq)
        if seq > max_seq:
            max_seq = seq
        try:
            payload = (
                json.loads(str(payload_json))
                if isinstance(payload_json, str) and payload_json.strip()
                else {}
            )
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if event_type in ("company.asset_published", "asset.published"):
            asset_id = str(payload.get("asset_id") or entity_id or "").strip()
            if not asset_id:
                continue
            pub_row = connection.execute(
                """SELECT author_id, content FROM shared_publication
                   WHERE company_id = ? AND asset_id = ?""",
                (company_id, asset_id),
            ).fetchone()
            raw_rev = payload.get("revision") or payload.get("excerpt_sha256")
            if isinstance(raw_rev, str) and raw_rev.strip():
                revision = raw_rev.strip()
            elif pub_row is not None:
                revision = sha256(str(pub_row[1]).encode("utf-8")).hexdigest()
            else:
                revision = f"event:{seq}"
            if pub_row is not None and str(pub_row[0]).strip():
                author = str(pub_row[0]).strip()
            elif isinstance(actor_id, str) and actor_id.strip():
                author = actor_id.strip()
            else:
                author = ""
            observed_assets[asset_id] = revision
            if author:
                asset_authors[asset_id] = author
        elif event_type in (
            "work.peer_request_answered",
            "work.peer_request_declined",
            "work.peer_request_expired",
            "work.peer_request_resolved",
        ):
            req_id = str(payload.get("request_id") or entity_id or "").strip()
            if not req_id:
                continue
            if event_type == "work.peer_request_expired":
                eff_actor = "work_scheduler"
            elif isinstance(actor_id, str) and actor_id.strip():
                eff_actor = actor_id.strip()
            else:
                eff_actor = ""
            if eff_actor:
                peer_events[f"peer_response:{req_id}"] = eff_actor

    connection.execute(
        """INSERT INTO work_event_cursor (company_id, last_event_seq, updated_at_ns)
           VALUES (?, ?, ?)
           ON CONFLICT(company_id) DO UPDATE SET
               last_event_seq = MAX(work_event_cursor.last_event_seq, excluded.last_event_seq),
               updated_at_ns = excluded.updated_at_ns""",
        (company_id, max_seq, now_ns),
    )
    return WorkEventBatch(
        last_event_seq=max_seq,
        observed_assets=observed_assets,
        asset_authors=asset_authors,
        peer_events=peer_events,
        events_processed=len(batch_rows),
        has_more=has_more,
    )


def _coalesce_open_condition_payload(
    open_payload: dict[str, Any],
    *,
    condition_id: str,
    condition_revision: int,
    trigger_kind: str,
    source_identity: str | None,
    observed_revision: str | None,
    responsibility_id: str | None = None,
) -> dict[str, Any]:
    """Preserve prior trigger(s) and append the incoming trigger when coalescing onto an open item."""
    raw_coalesced = open_payload.get("coalesced_revisions")
    coalesced: list[dict[str, Any]] = (
        [entry for entry in raw_coalesced if isinstance(entry, dict)]
        if isinstance(raw_coalesced, list)
        else []
    )
    existing_cond_id = open_payload.get("condition_id")
    existing_source = open_payload.get("source_identity")
    existing_rev = open_payload.get("observed_revision")
    if not coalesced and (
        existing_cond_id is not None
        or existing_source is not None
        or existing_rev is not None
    ):
        coalesced.append(
            {
                "condition_id": existing_cond_id,
                "condition_revision": open_payload.get("condition_revision"),
                "trigger_kind": open_payload.get("trigger_kind"),
                "source_identity": existing_source,
                "observed_revision": existing_rev,
            }
        )
    coalesced.append(
        {
            "condition_id": condition_id,
            "condition_revision": int(condition_revision),
            "trigger_kind": trigger_kind,
            "source_identity": source_identity,
            "observed_revision": observed_revision,
        }
    )
    if len(coalesced) > 32:
        deduped: list[dict[str, Any]] = []
        seen_keys: set[tuple[Any, Any]] = set()
        for entry in reversed(coalesced):
            key = (entry.get("condition_id"), entry.get("source_identity"))
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(entry)
        deduped.reverse()
        coalesced = deduped[-32:]

    open_payload["condition_id"] = condition_id
    open_payload["condition_revision"] = int(condition_revision)
    if responsibility_id is not None:
        open_payload["responsibility_id"] = responsibility_id
    open_payload["trigger_kind"] = trigger_kind
    open_payload["source_identity"] = source_identity
    open_payload["observed_revision"] = observed_revision
    open_payload["coalesced_revisions"] = coalesced
    return open_payload


def _read_discovery_window(
    connection: sqlite3.Connection,
    company_id: str,
    responsibility_id: str,
    *,
    now_ns: int,
    wake_window_ns: int | None = None,
) -> tuple[int, int, int, int]:
    """Return (consecutive_noops, cooldown_until_ns, window_started_ns, window_wakes)."""
    state_row = connection.execute(
        """SELECT consecutive_noops, cooldown_until_ns, window_started_ns, window_wakes
           FROM work_discovery_state
           WHERE company_id = ? AND responsibility_id = ?""",
        (company_id, responsibility_id),
    ).fetchone()
    if state_row is None:
        return 0, 0, now_ns, 0
    noops = int(state_row[0])
    cooldown_until = int(state_row[1])
    window_started = int(state_row[2])
    window_wakes = int(state_row[3])
    if wake_window_ns is not None and not (
        window_started > 0 and now_ns - window_started < wake_window_ns
    ):
        return noops, cooldown_until, now_ns, 0
    return noops, cooldown_until, window_started, window_wakes


def _upsert_discovery_state(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    responsibility_id: str,
    consecutive_noops: int,
    cooldown_until_ns: int,
    window_started_ns: int,
    window_wakes: int,
    now_ns: int,
) -> WorkDiscoveryState:
    connection.execute(
        """INSERT INTO work_discovery_state (
               company_id, responsibility_id, consecutive_noops,
               cooldown_until_ns, window_started_ns, window_wakes, updated_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(company_id, responsibility_id) DO UPDATE SET
               consecutive_noops = excluded.consecutive_noops,
               cooldown_until_ns = excluded.cooldown_until_ns,
               window_started_ns = excluded.window_started_ns,
               window_wakes = excluded.window_wakes,
               updated_at_ns = excluded.updated_at_ns""",
        (
            company_id,
            responsibility_id,
            consecutive_noops,
            cooldown_until_ns,
            window_started_ns,
            window_wakes,
            now_ns,
        ),
    )
    return WorkDiscoveryState(
        company_id=company_id,
        responsibility_id=responsibility_id,
        consecutive_noops=consecutive_noops,
        cooldown_until_ns=cooldown_until_ns,
        window_started_ns=window_started_ns,
        window_wakes=window_wakes,
        updated_at_ns=now_ns,
    )


def record_discovery_hysteresis(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    responsibility_id: str,
    action_kind: str,
    now_ns: int,
    base_cooldown_ns: int = 0,
    condition_id: str | None = None,
) -> WorkDiscoveryState:
    """Update work_discovery_state exponential backoff on defer/noop or reset on action."""
    _require_nonempty(company_id, "company_id")
    _require_nonempty(responsibility_id, "responsibility_id")
    _require_nonempty(action_kind, "action_kind")
    if type(now_ns) is not int or now_ns < 0:
        raise WorkKernelRefused("now_ns must be a nonnegative integer")
    if type(base_cooldown_ns) is not int or base_cooldown_ns < 0:
        raise WorkKernelRefused("base_cooldown_ns must be a nonnegative integer")

    resp_row = connection.execute(
        """SELECT readable_scope_json FROM work_responsibilities
           WHERE company_id = ? AND responsibility_id = ?""",
        (company_id, responsibility_id),
    ).fetchone()
    if resp_row is None:
        raise WorkKernelRefused("Responsibility not found for discovery hysteresis")
    _, wake_window_ns, scope_cooldown = _parse_scope_policy(str(resp_row[0]))
    cond_cooldown_row = connection.execute(
        """SELECT COALESCE(MAX(cooldown_ns), 0)
           FROM work_conditions
           WHERE company_id = ? AND responsibility_id = ? AND status = 'armed'""",
        (company_id, responsibility_id),
    ).fetchone()
    cond_cooldown = int(cond_cooldown_row[0]) if cond_cooldown_row is not None else 0
    if cond_cooldown == 0 and isinstance(condition_id, str) and condition_id.strip():
        fallback_row = connection.execute(
            """SELECT cooldown_ns
               FROM work_conditions
               WHERE company_id = ? AND responsibility_id = ? AND condition_id = ?""",
            (company_id, responsibility_id, condition_id.strip()),
        ).fetchone()
        if fallback_row is not None:
            cond_cooldown = int(fallback_row[0])
    effective_base = max(base_cooldown_ns, scope_cooldown, cond_cooldown)

    old_noops, old_cooldown_until, window_started_ns, window_wakes = (
        _read_discovery_window(
            connection,
            company_id,
            responsibility_id,
            now_ns=now_ns,
            wake_window_ns=wake_window_ns,
        )
    )

    normalized_action = action_kind.strip().lower()
    if normalized_action in ("defer", "noop"):
        new_noops = old_noops + 1
        if effective_base > 0:
            backoff = min(
                effective_base * (2 ** min(new_noops - 1, 6)),
                MAX_DISCOVERY_COOLDOWN_NS,
            )
            new_cooldown_until = max(old_cooldown_until, now_ns + backoff)
        else:
            new_cooldown_until = old_cooldown_until
    else:
        new_noops = 0
        new_cooldown_until = old_cooldown_until

    return _upsert_discovery_state(
        connection,
        company_id=company_id,
        responsibility_id=responsibility_id,
        consecutive_noops=new_noops,
        cooldown_until_ns=new_cooldown_until,
        window_started_ns=window_started_ns,
        window_wakes=window_wakes,
        now_ns=now_ns,
    )


def _is_result_committed(
    connection: sqlite3.Connection,
    company_id: str,
    dependency_asset_id: str,
    *,
    now_ns: int,
) -> bool:
    audit_row = connection.execute(
        """SELECT 1 FROM audit_event
           WHERE company_id = ?
             AND event_type IN ('asset.published', 'company.asset_published')
             AND recorded_at_ns <= ?
             AND (
                 entity_id = ?
                 OR json_extract(payload_json, '$.asset_id') = ?
             )
           LIMIT 1""",
        (company_id, now_ns, dependency_asset_id, dependency_asset_id),
    ).fetchone()
    if audit_row is not None:
        return True
    pub_row = connection.execute(
        """SELECT 1 FROM shared_publication
           WHERE company_id = ? AND asset_id = ?
           LIMIT 1""",
        (company_id, dependency_asset_id),
    ).fetchone()
    return pub_row is not None


def evaluate_conditions(
    connection: sqlite3.Connection,
    company_id: str,
    *,
    now_ns: int,
    observed_assets: dict[str, str] | None = None,
    triggering_actor_id: str | None = None,
    _cursor_batch: WorkEventBatch | None = None,
) -> tuple[WorkItem, ...]:
    """Evaluate armed conditions and admit deduplicated eligible work items."""
    cursor_batch = (
        drain_work_event_cursor(connection, company_id, now_ns=now_ns)
        if _cursor_batch is None
        else _cursor_batch
    )
    assets = dict(cursor_batch.observed_assets)
    if observed_assets:
        assets.update(observed_assets)

    rows = connection.execute(
        """SELECT c.condition_id, c.owner_agent_id, c.responsibility_id, c.work_item_id,
                  c.condition_revision, c.trigger_kind, c.due_at_ns, c.source_identity,
                  c.expected_revision, c.expires_at_ns,
                  c.cooldown_ns, c.max_fires, c.fire_count, c.updated_at_ns,
                  r.duty_kind, r.status, r.readable_scope_json
           FROM work_conditions c
           LEFT JOIN work_responsibilities r ON r.responsibility_id = c.responsibility_id
           WHERE c.company_id = ? AND c.status = 'armed'
           ORDER BY
               CASE
                   WHEN c.trigger_kind = 'peer_response'
                        OR c.source_identity LIKE 'result_dep:%'
                        OR (
                            c.work_item_id IS NOT NULL
                            AND COALESCE(r.duty_kind, '') NOT IN ('work.discovery_review', 'work.dreaming')
                        )
                        OR (
                            r.duty_kind IS NOT NULL
                            AND r.duty_kind NOT IN ('work.discovery_review', 'work.dreaming')
                        )
                   THEN 0
                   WHEN COALESCE(r.duty_kind, 'work.discovery_review') = 'work.discovery_review' THEN 1
                   ELSE 2
               END ASC,
               c.created_at_ns ASC,
               c.condition_id ASC""",
        (company_id,),
    ).fetchall()
    admitted: list[WorkItem] = []
    for (
        cid,
        owner_agent_id,
        resp_id,
        parent_item_id,
        cond_rev,
        trigger_kind,
        due_at_ns,
        source_identity,
        expected_revision,
        expires_at_ns,
        raw_cooldown_ns,
        raw_max_fires,
        raw_fire_count,
        cond_updated_at_ns,
        resp_duty_kind,
        resp_status,
        resp_scope_json,
    ) in rows:
        cooldown_ns = int(raw_cooldown_ns or 0)
        max_fires = None if raw_max_fires is None else int(raw_max_fires)
        fire_count = int(raw_fire_count or 0)

        if resp_id is not None and resp_status != "active":
            connection.execute(
                "UPDATE work_conditions SET status = 'cancelled', updated_at_ns = ? WHERE condition_id = ?",
                (now_ns, cid),
            )
            continue
        if parent_item_id is not None:
            p_state_row = connection.execute(
                "SELECT state FROM work_items WHERE company_id = ? AND work_item_id = ?",
                (company_id, parent_item_id),
            ).fetchone()
            if p_state_row is not None and str(p_state_row[0]) == "cancelled":
                connection.execute(
                    "UPDATE work_conditions SET status = 'cancelled', updated_at_ns = ? WHERE condition_id = ?",
                    (now_ns, cid),
                )
                continue
        if employment_cutoff(connection, owner_agent_id) is not None:
            connection.execute(
                "UPDATE work_conditions SET status = 'cancelled', updated_at_ns = ? WHERE condition_id = ?",
                (now_ns, cid),
            )
            continue
        if expires_at_ns is not None and now_ns >= int(expires_at_ns):
            connection.execute(
                "UPDATE work_conditions SET status = 'expired', updated_at_ns = ? WHERE condition_id = ?",
                (now_ns, cid),
            )
            continue
        if max_fires is not None and fire_count >= max_fires:
            connection.execute(
                """UPDATE work_conditions
                   SET status = 'triggered', updated_at_ns = ?
                   WHERE condition_id = ? AND condition_revision = ? AND status = 'armed'""",
                (now_ns, cid, int(cond_rev)),
            )
            continue

        eligible = False
        matched_revision: str | None = None
        resolved_author: str | None = None

        if (
            trigger_kind == "timer"
            and due_at_ns is not None
            and now_ns >= int(due_at_ns)
        ):
            if isinstance(source_identity, str) and source_identity.startswith(
                "result_dep:"
            ):
                dep_id = str(expected_revision or "").strip()
                if dep_id and _is_result_committed(
                    connection, company_id, dep_id, now_ns=now_ns
                ):
                    eligible = True
                    matched_revision = dep_id
            else:
                eligible = True
        elif trigger_kind == "shared_asset_revision" and source_identity:
            if (
                _is_authorized_for_shared_publication(
                    connection, company_id, owner_agent_id, source_identity
                )
                is False
            ):
                connection.execute(
                    "UPDATE work_conditions SET status = 'cancelled', updated_at_ns = ? WHERE condition_id = ?",
                    (now_ns, cid),
                )
                continue
            pub_row = connection.execute(
                """SELECT author_id, content FROM shared_publication
                   WHERE company_id = ? AND asset_id = ?""",
                (company_id, source_identity),
            ).fetchone()
            observed_rev: str | None = assets.get(source_identity)
            if observed_rev is None and pub_row is not None:
                observed_rev = sha256(str(pub_row[1]).encode("utf-8")).hexdigest()
            if observed_rev is not None:
                if pub_row is not None and str(pub_row[0]).strip():
                    resolved_author = str(pub_row[0]).strip()
                elif source_identity in cursor_batch.asset_authors:
                    resolved_author = cursor_batch.asset_authors[source_identity]
                elif (
                    isinstance(triggering_actor_id, str) and triggering_actor_id.strip()
                ):
                    resolved_author = triggering_actor_id.strip()

                rev_entity = f"{source_identity}@{observed_rev}"
                if resolved_author == owner_agent_id:
                    recorded = connection.execute(
                        """SELECT 1 FROM audit_event
                           WHERE company_id = ? AND domain = 'work'
                             AND entity_type = 'shared_asset_revision'
                             AND entity_id = ? AND actor_id = ?
                             AND event_type = 'work.self_authored_revision_suppressed'
                           LIMIT 1""",
                        (company_id, rev_entity, owner_agent_id),
                    ).fetchone()
                    if recorded is None:
                        append_audit_event(
                            connection,
                            company_id=company_id,
                            domain="work",
                            entity_type="shared_asset_revision",
                            entity_id=rev_entity,
                            event_type="work.self_authored_revision_suppressed",
                            actor_id=owner_agent_id,
                            payload={
                                "source_identity": source_identity,
                                "revision": observed_rev,
                                "condition_id": cid,
                            },
                            recorded_at_ns=now_ns,
                        )
                    continue
                self_authored = connection.execute(
                    """SELECT 1 FROM audit_event
                       WHERE company_id = ? AND domain = 'work'
                         AND entity_type = 'shared_asset_revision'
                         AND entity_id = ? AND actor_id = ?
                         AND event_type = 'work.self_authored_revision_suppressed'
                       LIMIT 1""",
                    (company_id, rev_entity, owner_agent_id),
                ).fetchone()
                if self_authored is not None:
                    continue
                if not resolved_author:
                    continue  # fail closed: asset revision wakes require non-self producer identity
                if expected_revision is None or observed_rev == expected_revision:
                    eligible = True
                    matched_revision = observed_rev
        elif trigger_kind == "peer_response" and source_identity:
            req_id = str(source_identity).removeprefix("peer_response:")
            peer_row = connection.execute(
                """SELECT state, requester_agent_id, recipient_agent_id FROM work_peer_requests
                   WHERE company_id = ? AND request_id = ?""",
                (company_id, req_id),
            ).fetchone()
            if peer_row is not None and str(peer_row[0]) != "open":
                res_state = str(peer_row[0])
                persisted_requester = str(peer_row[1])
                persisted_responder = str(peer_row[2])
                if owner_agent_id != persisted_requester:
                    continue  # only the originating requester may wake on peer_response
                if res_state in ("answered", "declined") and (
                    owner_agent_id == persisted_responder
                    or triggering_actor_id == owner_agent_id
                ):
                    continue  # anti-self-wake against persisted responder
                if expected_revision is None or res_state == expected_revision:
                    eligible = True
                    matched_revision = res_state
                    resolved_author = (
                        "work_scheduler"
                        if res_state == "expired"
                        else persisted_responder
                    )

        if not eligible:
            continue

        # Deduplicate strictly by (company_id, source_identity, condition_revision).
        # Scope shared_asset_revision by condition_id so multiple watchers never collide;
        # scope peer_response and result_dep per (owner_agent_id, target) to prevent
        # duplicate-condition wake amplification on already-resolved records.
        if trigger_kind == "shared_asset_revision":
            dedupe_source = (
                f"{cid}:{source_identity}@{matched_revision}"
                if matched_revision is not None
                else f"{cid}:{source_identity}"
            )
        elif trigger_kind == "peer_response":
            peer_target = str(source_identity or "").removeprefix("peer_response:")
            dedupe_source = (
                f"peer_response:{owner_agent_id}:{peer_target}@{matched_revision}"
                if matched_revision is not None
                else f"peer_response:{owner_agent_id}:{peer_target}"
            )
        elif (
            trigger_kind == "timer"
            and isinstance(source_identity, str)
            and source_identity.startswith("result_dep:")
            and matched_revision is not None
        ):
            dedupe_source = f"result_dep:{owner_agent_id}:{matched_revision}"
        else:
            dedupe_source = (
                f"{source_identity}@{matched_revision}"
                if matched_revision is not None
                else str(source_identity)
            )
        already = connection.execute(
            """SELECT 1 FROM work_condition_occurrences
               WHERE company_id = ? AND source_identity = ? AND condition_revision = ?""",
            (company_id, dedupe_source, int(cond_rev)),
        ).fetchone()
        is_multi_fire = trigger_kind == "shared_asset_revision" and (
            (max_fires is not None and max_fires > 1)
            or (max_fires is None and cooldown_ns > 0)
        )
        if already is not None:
            if not is_multi_fire:
                connection.execute(
                    """UPDATE work_conditions
                       SET status = 'triggered', updated_at_ns = ?
                       WHERE condition_id = ? AND condition_revision = ?""",
                    (now_ns, cid, int(cond_rev)),
                )
            continue

        if (
            trigger_kind == "shared_asset_revision"
            and fire_count > 0
            and cooldown_ns > 0
            and now_ns < int(cond_updated_at_ns) + cooldown_ns
        ):
            continue

        duty_kind = resp_duty_kind
        if not duty_kind and parent_item_id is not None:
            parent_kind = connection.execute(
                "SELECT duty_kind FROM work_items WHERE work_item_id = ?",
                (parent_item_id,),
            ).fetchone()
            if parent_kind is not None:
                duty_kind = str(parent_kind[0])
        duty_kind = duty_kind or "work.discovery_review"

        is_result_dep = isinstance(source_identity, str) and source_identity.startswith(
            "result_dep:"
        )
        effective_parent_id = (
            None
            if duty_kind in ("work.discovery_review", "work.dreaming")
            and not is_result_dep
            and trigger_kind != "peer_response"
            else parent_item_id
        )
        is_discretionary = (
            resp_id is not None
            and effective_parent_id is None
            and trigger_kind != "peer_response"
            and duty_kind in ("work.discovery_review", "work.dreaming")
            and not is_result_dep
        )
        is_unscoped_discretionary = (
            resp_id is None
            and effective_parent_id is None
            and trigger_kind != "peer_response"
            and duty_kind in ("work.discovery_review", "work.dreaming")
            and not is_result_dep
        )
        is_result_dep_discovery = is_result_dep and duty_kind in (
            "work.discovery_review",
            "work.dreaming",
        )
        disc_noops = 0
        disc_cooldown_until = 0
        disc_window_started = now_ns
        disc_window_wakes = 0
        configured_cooldown_ns = 0
        if is_result_dep_discovery:
            if resp_id is not None:
                wake_budget, wake_window_ns, configured_cooldown_ns = (
                    _parse_scope_policy(resp_scope_json)
                )
                (
                    disc_noops,
                    disc_cooldown_until,
                    disc_window_started,
                    disc_window_wakes,
                ) = _read_discovery_window(
                    connection,
                    company_id,
                    resp_id,
                    now_ns=now_ns,
                    wake_window_ns=wake_window_ns,
                )
                if disc_window_wakes >= wake_budget:
                    continue
            else:
                window_floor = max(0, now_ns - DEFAULT_DISCOVERY_WAKE_WINDOW_NS)
                recent_unscoped = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM work_items
                           WHERE company_id = ? AND owner_agent_id = ?
                             AND duty_kind = ? AND source_kind = 'condition'
                             AND json_extract(payload_json, '$.responsibility_id') IS NULL
                             AND created_at_ns >= ?""",
                        (company_id, owner_agent_id, duty_kind, window_floor),
                    ).fetchone()[0]
                )
                if recent_unscoped >= DEFAULT_DISCOVERY_WAKE_BUDGET:
                    continue
        if is_unscoped_discretionary:
            window_floor = max(0, now_ns - DEFAULT_DISCOVERY_WAKE_WINDOW_NS)
            recent_unscoped = int(
                connection.execute(
                    """SELECT COUNT(*) FROM work_items
                       WHERE company_id = ? AND owner_agent_id = ?
                         AND duty_kind = ? AND source_kind = 'condition'
                         AND json_extract(payload_json, '$.responsibility_id') IS NULL
                         AND created_at_ns >= ?""",
                    (company_id, owner_agent_id, duty_kind, window_floor),
                ).fetchone()[0]
            )
            if recent_unscoped >= DEFAULT_DISCOVERY_WAKE_BUDGET:
                continue
            open_unscoped = connection.execute(
                """SELECT work_item_id, state, payload_json
                   FROM work_items
                   WHERE company_id = ? AND owner_agent_id = ?
                     AND duty_kind = ?
                     AND state IN ('due', 'scheduled', 'running')
                     AND json_extract(payload_json, '$.responsibility_id') IS NULL
                   ORDER BY created_at_ns ASC, work_item_id ASC
                   LIMIT 1""",
                (company_id, owner_agent_id, duty_kind),
            ).fetchone()
            if open_unscoped is not None:
                open_item_id, open_state, open_payload_raw = open_unscoped
                if str(open_state) in ("due", "scheduled"):
                    try:
                        open_payload = json.loads(str(open_payload_raw))
                    except json.JSONDecodeError:
                        open_payload = {}
                    if not isinstance(open_payload, dict):
                        open_payload = {}
                    open_payload = _coalesce_open_condition_payload(
                        open_payload,
                        condition_id=cid,
                        condition_revision=int(cond_rev),
                        trigger_kind=trigger_kind,
                        source_identity=source_identity,
                        observed_revision=matched_revision,
                    )
                    connection.execute(
                        """UPDATE work_items
                           SET payload_json = ?, updated_at_ns = ?
                           WHERE work_item_id = ?""",
                        (_canonical_json(open_payload), now_ns, str(open_item_id)),
                    )
                    connection.execute(
                        """INSERT INTO work_condition_occurrences (
                               company_id, source_identity, condition_revision,
                               condition_id, work_item_id, admitted_at_ns
                           ) VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(company_id, source_identity, condition_revision) DO NOTHING""",
                        (
                            company_id,
                            dedupe_source,
                            int(cond_rev),
                            cid,
                            str(open_item_id),
                            now_ns,
                        ),
                    )
                    if not is_multi_fire:
                        connection.execute(
                            """UPDATE work_conditions
                               SET status = 'triggered', fire_count = fire_count + 1, updated_at_ns = ?
                               WHERE condition_id = ? AND condition_revision = ? AND status = 'armed'""",
                            (now_ns, cid, int(cond_rev)),
                        )
                continue
        if is_discretionary:
            wake_budget, wake_window_ns, configured_cooldown_ns = _parse_scope_policy(
                resp_scope_json
            )
            (
                disc_noops,
                disc_cooldown_until,
                disc_window_started,
                disc_window_wakes,
            ) = _read_discovery_window(
                connection,
                company_id,
                resp_id,
                now_ns=now_ns,
                wake_window_ns=wake_window_ns,
            )
            if now_ns < disc_cooldown_until:
                continue
            if disc_window_wakes >= wake_budget:
                continue

            open_row = connection.execute(
                """SELECT work_item_id, state, payload_json
                   FROM work_items
                   WHERE company_id = ? AND owner_agent_id = ?
                     AND duty_kind = ?
                     AND state IN ('due', 'scheduled', 'running')
                     AND json_extract(payload_json, '$.responsibility_id') = ?
                   ORDER BY created_at_ns ASC, work_item_id ASC
                   LIMIT 1""",
                (company_id, owner_agent_id, duty_kind, resp_id),
            ).fetchone()
            if open_row is not None:
                open_item_id, open_state, open_payload_raw = open_row
                if str(open_state) in ("due", "scheduled"):
                    try:
                        open_payload = json.loads(str(open_payload_raw))
                    except json.JSONDecodeError:
                        open_payload = {}
                    if not isinstance(open_payload, dict):
                        open_payload = {}
                    open_payload = _coalesce_open_condition_payload(
                        open_payload,
                        condition_id=cid,
                        condition_revision=int(cond_rev),
                        trigger_kind=trigger_kind,
                        source_identity=source_identity,
                        observed_revision=matched_revision,
                        responsibility_id=resp_id,
                    )
                    connection.execute(
                        """UPDATE work_items
                           SET payload_json = ?, updated_at_ns = ?
                           WHERE work_item_id = ?""",
                        (_canonical_json(open_payload), now_ns, str(open_item_id)),
                    )
                    connection.execute(
                        """INSERT INTO work_condition_occurrences (
                               company_id, source_identity, condition_revision,
                               condition_id, work_item_id, admitted_at_ns
                           ) VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(company_id, source_identity, condition_revision) DO NOTHING""",
                        (
                            company_id,
                            dedupe_source,
                            int(cond_rev),
                            cid,
                            str(open_item_id),
                            now_ns,
                        ),
                    )
                    if not is_multi_fire:
                        connection.execute(
                            """UPDATE work_conditions
                               SET status = 'triggered', fire_count = fire_count + 1, updated_at_ns = ?
                               WHERE condition_id = ? AND condition_revision = ? AND status = 'armed'""",
                            (now_ns, cid, int(cond_rev)),
                        )
                continue

        item, _ = create_work_item(
            connection,
            company_id=company_id,
            owner_agent_id=owner_agent_id,
            duty_kind=duty_kind,
            source_kind="condition",
            source_id=cid,
            parent_work_item_id=effective_parent_id,
            payload={
                "condition_id": cid,
                "condition_revision": int(cond_rev),
                "responsibility_id": resp_id,
                "trigger_kind": trigger_kind,
                "source_identity": source_identity,
                "observed_revision": matched_revision,
            },
            actor_id=resolved_author or triggering_actor_id or "work_scheduler",
            now_ns=now_ns,
        )
        connection.execute(
            """INSERT INTO work_condition_occurrences (
                   company_id, source_identity, condition_revision,
                   condition_id, work_item_id, admitted_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (company_id, dedupe_source, int(cond_rev), cid, item.work_item_id, now_ns),
        )
        new_fire_count = fire_count + 1
        keep_armed = trigger_kind == "shared_asset_revision" and (
            (max_fires is not None and new_fire_count < max_fires)
            or (max_fires is None and cooldown_ns > 0)
        )
        new_status = "armed" if keep_armed else "triggered"
        connection.execute(
            """UPDATE work_conditions
               SET status = ?, fire_count = ?, updated_at_ns = ?
               WHERE condition_id = ? AND condition_revision = ? AND status = 'armed'""",
            (new_status, new_fire_count, now_ns, cid, int(cond_rev)),
        )
        if is_discretionary or (is_result_dep_discovery and resp_id is not None):
            eff_cooldown = max(cooldown_ns, configured_cooldown_ns)
            if eff_cooldown > 0:
                step_cooldown = min(
                    eff_cooldown * (2 ** min(disc_noops, 6)),
                    MAX_DISCOVERY_COOLDOWN_NS,
                )
                new_cooldown_until = max(disc_cooldown_until, now_ns + step_cooldown)
            else:
                new_cooldown_until = disc_cooldown_until
            _upsert_discovery_state(
                connection,
                company_id=company_id,
                responsibility_id=resp_id,
                consecutive_noops=disc_noops,
                cooldown_until_ns=new_cooldown_until,
                window_started_ns=disc_window_started,
                window_wakes=disc_window_wakes + 1,
                now_ns=now_ns,
            )
        admitted.append(item)
    return tuple(admitted)


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


def validate_next_condition_dict(
    raw: Any,
    *,
    max_str_len: int | None = None,
    error_cls: type[Exception] = WorkKernelRefused,
) -> dict[str, Any]:
    """Validate and normalize a next_condition mapping against canonical schema rules."""
    if not isinstance(raw, Mapping):
        raise error_cls("next_condition must be a mapping")
    extra_keys = set(raw.keys()) - _NEXT_CONDITION_ALLOWED_KEYS
    if extra_keys:
        raise error_cls(f"Unsupported next_condition fields: {sorted(extra_keys)}")
    if "trigger_kind" not in raw:
        raise error_cls("next_condition requires trigger_kind")

    def _check_str(val: Any, key: str) -> str:
        if (
            not isinstance(val, str)
            or not val.strip()
            or (max_str_len is not None and len(val) > max_str_len)
        ):
            raise error_cls(f"next_condition.{key} must be a nonempty string")
        return val

    def _check_int(val: Any, key: str, minimum: int) -> int:
        if type(val) is not int or val < minimum or val >= 2**63:
            raise error_cls(f"next_condition.{key} must be a valid integer")
        return val

    parsed: dict[str, Any] = {
        "trigger_kind": _check_str(raw["trigger_kind"], "trigger_kind")
    }
    for key in (
        "responsibility_id",
        "work_item_id",
        "source_identity",
        "expected_revision",
    ):
        if key in raw and raw[key] is not None:
            parsed[key] = _check_str(raw[key], key)
    for key, minimum in (
        ("due_at_ns", 0),
        ("expires_at_ns", 0),
        ("cooldown_ns", 0),
        ("max_fires", 1),
        ("condition_revision", 1),
    ):
        if key in raw and raw[key] is not None:
            parsed[key] = _check_int(raw[key], key, minimum)
    return parsed


def _normalize_evidence(
    evidence: Sequence[DecisionEvidence | Mapping[str, Any]],
) -> tuple[DecisionEvidence, ...]:
    normalized: list[DecisionEvidence] = []
    for entry in evidence:
        if isinstance(entry, DecisionEvidence):
            ev = entry
        elif isinstance(entry, Mapping):
            allowed = {
                "kind",
                "resource_id",
                "revision",
                "observed_at_ns",
                "available_at_ns",
            }
            keys = set(entry.keys())
            if not allowed <= keys or not keys <= allowed:
                raise WorkKernelRefused("DecisionEvidence dict does not match schema")
            ev = DecisionEvidence(
                kind=entry["kind"],
                resource_id=entry["resource_id"],
                revision=entry["revision"],
                observed_at_ns=entry["observed_at_ns"],
                available_at_ns=entry["available_at_ns"],
            )
        else:
            raise WorkKernelRefused("DecisionEvidence entry has invalid type")
        _require_nonempty(ev.kind, "evidence.kind")
        _require_nonempty(ev.resource_id, "evidence.resource_id")
        if not isinstance(ev.revision, str) or not ev.revision.strip():
            raise WorkKernelRefused("evidence.revision must be a nonempty string")
        if type(ev.observed_at_ns) is not int or type(ev.available_at_ns) is not int:
            raise WorkKernelRefused("evidence timestamps must be integers")
        if ev.available_at_ns < 0 or ev.observed_at_ns < 0:
            raise WorkKernelRefused("evidence timestamps must be nonnegative")
        normalized.append(ev)
    return tuple(normalized)


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


def work_run_evidence_revision(
    run_id: str,
    *,
    company_id: str,
    work_item_id: str,
    agent_id: str,
    duty_kind: str,
    fencing_token: int,
    created_at_ns: int,
) -> str:
    """Compute canonical immutable revision digest for a work_run evidence citation."""
    return sha256(
        _canonical_json(
            {
                "kind": "work_run",
                "company_id": company_id,
                "run_id": run_id,
                "work_item_id": work_item_id,
                "agent_id": agent_id,
                "duty_kind": duty_kind,
                "fencing_token": int(fencing_token),
                "created_at_ns": int(created_at_ns),
            }
        ).encode("utf-8")
    ).hexdigest()


def work_item_evidence_revision(
    work_item_id: str,
    *,
    company_id: str,
    owner_agent_id: str,
    duty_kind: str,
    source_kind: str,
    source_id: str,
    payload_json: str,
    created_at_ns: int,
) -> str:
    """Compute canonical immutable revision digest for a work_item evidence citation."""
    return sha256(
        _canonical_json(
            {
                "kind": "work_item",
                "company_id": company_id,
                "work_item_id": work_item_id,
                "owner_agent_id": owner_agent_id,
                "duty_kind": duty_kind,
                "source_kind": source_kind,
                "source_id": source_id,
                "payload_json": payload_json,
                "created_at_ns": int(created_at_ns),
            }
        ).encode("utf-8")
    ).hexdigest()


def peer_request_evidence_revision(
    request_id: str,
    *,
    company_id: str,
    requester_agent_id: str,
    recipient_agent_id: str,
    state: str,
    question: str,
    expected_decision: str,
    response_json: str | None,
    created_at_ns: int,
) -> str:
    """Compute canonical revision digest for a peer_request evidence citation."""
    return sha256(
        _canonical_json(
            {
                "kind": "peer_request",
                "company_id": company_id,
                "request_id": request_id,
                "requester_agent_id": requester_agent_id,
                "recipient_agent_id": recipient_agent_id,
                "state": state,
                "question": question,
                "expected_decision": expected_decision,
                "response_json": response_json,
                "created_at_ns": int(created_at_ns),
            }
        ).encode("utf-8")
    ).hexdigest()


def _validate_core_evidence_entry(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    decision_at_ns: int,
    ev: DecisionEvidence,
    current_run_id: str | None = None,
) -> bool:
    if ev.available_at_ns > ev.observed_at_ns or ev.observed_at_ns > decision_at_ns:
        raise WorkKernelRefused(
            "Decision evidence timestamps violate availability/observation order"
        )
    if ev.kind == "private_draft":
        if "/" in ev.resource_id:
            owner_prefix, draft_name = ev.resource_id.split("/", 1)
            if owner_prefix != agent_id:
                raise WorkKernelRefused("Cannot cite another Agent's private draft")
        elif ":" in ev.resource_id:
            owner_prefix, draft_name = ev.resource_id.split(":", 1)
            if owner_prefix != agent_id:
                raise WorkKernelRefused("Cannot cite another Agent's private draft")
        else:
            draft_name = ev.resource_id
        if not draft_name or "/" in draft_name or ":" in draft_name:
            raise WorkKernelRefused("Invalid private_draft resource_id")
        desk_row = connection.execute(
            "SELECT 1 FROM private_desk WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if desk_row is None:
            raise WorkKernelRefused(
                "Agent has no private Desk for private_draft evidence"
            )
        db_file = ""
        for _seq, db_name, file_path in connection.execute(
            "PRAGMA database_list"
        ).fetchall():
            if db_name == "main" and file_path:
                db_file = str(file_path)
                break
        if not db_file:
            raise WorkKernelRefused(
                "Cannot validate private_draft without a file-backed company store"
            )
        from ..agent_desk import PRODUCTION_DESK_LIMITS, draft_at_revision
        from ..duty_run import DutyRefused

        try:
            resolved = draft_at_revision(
                Path(db_file),
                agent_id,
                draft_name,
                ev.revision,
                limits=PRODUCTION_DESK_LIMITS,
            )
        except DutyRefused as error:
            raise WorkKernelRefused(str(error)) from error
        if resolved is None:
            raise WorkKernelRefused(
                "Cited private_draft does not exist or revision does not match"
            )
    elif ev.kind == "shared_publication":
        pub_row = connection.execute(
            """SELECT company_id, project_id, audience, content
               FROM shared_publication
               WHERE asset_id = ?""",
            (ev.resource_id,),
        ).fetchone()
        if pub_row is None:
            raise WorkKernelRefused("Cited shared_publication does not exist")
        pub_company, pub_project, audience_json, content = pub_row
        if str(pub_company) != company_id:
            raise WorkKernelRefused(
                "Cited shared_publication belongs to another company"
            )
        try:
            audience = set(json.loads(str(audience_json)))
        except json.JSONDecodeError:
            audience = set()
        proj_row = connection.execute(
            "SELECT members FROM shared_project WHERE company_id = ? AND project_id = ?",
            (company_id, str(pub_project)),
        ).fetchone()
        try:
            members = set(json.loads(str(proj_row[0]))) if proj_row else set()
        except json.JSONDecodeError:
            members = set()
        if agent_id not in audience or agent_id not in members:
            raise WorkKernelRefused(
                "Agent is not authorized to cite shared_publication"
            )
        actual_rev = sha256(str(content).encode("utf-8")).hexdigest()
        if ev.revision != actual_rev:
            raise WorkKernelRefused(
                "Cited shared_publication revision does not match committed content"
            )
        pub_audit = connection.execute(
            """SELECT MIN(recorded_at_ns)
               FROM audit_event
               WHERE company_id = ?
                 AND event_type IN ('company.asset_published', 'asset.published')
                 AND (entity_id = ? OR json_extract(payload_json, '$.asset_id') = ?)""",
            (company_id, ev.resource_id, ev.resource_id),
        ).fetchone()
        if pub_audit is not None and pub_audit[0] is not None:
            pub_recorded_at_ns = int(pub_audit[0])
            if (
                ev.available_at_ns < pub_recorded_at_ns
                or pub_recorded_at_ns > decision_at_ns
            ):
                raise WorkKernelRefused(
                    "Cited shared_publication timestamps precede publication commit or follow decision"
                )
    elif ev.kind == "peer_request":
        req_row = connection.execute(
            """SELECT company_id, requester_agent_id, recipient_agent_id,
                      state, question, expected_decision, response_json,
                      created_at_ns, resolved_at_ns
               FROM work_peer_requests WHERE request_id = ?""",
            (ev.resource_id,),
        ).fetchone()
        if req_row is None:
            raise WorkKernelRefused("Cited peer_request does not exist")
        if str(req_row[0]) != company_id or agent_id not in (
            str(req_row[1]),
            str(req_row[2]),
        ):
            raise WorkKernelRefused("Agent is not authorized to cite peer_request")
        expected_peer_rev = peer_request_evidence_revision(
            ev.resource_id,
            company_id=str(req_row[0]),
            requester_agent_id=str(req_row[1]),
            recipient_agent_id=str(req_row[2]),
            state=str(req_row[3]),
            question=str(req_row[4]),
            expected_decision=str(req_row[5]),
            response_json=None if req_row[6] is None else str(req_row[6]),
            created_at_ns=int(req_row[7]),
        )
        if ev.revision != expected_peer_rev:
            raise WorkKernelRefused(
                "Cited peer_request revision does not match canonical request state"
            )
        min_avail = max(
            int(req_row[7]),
            int(req_row[8]) if req_row[8] is not None else 0,
        )
        if ev.available_at_ns < min_avail or min_avail > decision_at_ns:
            raise WorkKernelRefused(
                "Cited peer_request timestamps precede request creation/resolution or follow decision"
            )
    elif ev.kind == "work_run":
        run_row = connection.execute(
            """SELECT company_id, work_item_id, agent_id, duty_kind,
                      fencing_token, created_at_ns
               FROM work_runs WHERE run_id = ?""",
            (ev.resource_id,),
        ).fetchone()
        if run_row is None:
            raise WorkKernelRefused("Cited work_run does not exist")
        if str(run_row[0]) != company_id or str(run_row[2]) != agent_id:
            raise WorkKernelRefused("Agent is not authorized to cite work_run")
        expected_run_rev = work_run_evidence_revision(
            ev.resource_id,
            company_id=str(run_row[0]),
            work_item_id=str(run_row[1]),
            agent_id=str(run_row[2]),
            duty_kind=str(run_row[3]),
            fencing_token=int(run_row[4]),
            created_at_ns=int(run_row[5]),
        )
        if ev.revision != expected_run_rev:
            raise WorkKernelRefused(
                "Cited work_run revision does not match canonical run identity"
            )
        if ev.available_at_ns < int(run_row[5]) or int(run_row[5]) > decision_at_ns:
            raise WorkKernelRefused(
                "Cited work_run timestamps precede run creation or follow decision"
            )
    elif ev.kind == "work_item":
        item_row = connection.execute(
            """SELECT company_id, owner_agent_id, duty_kind, source_kind,
                      source_id, payload_json, created_at_ns
               FROM work_items WHERE work_item_id = ?""",
            (ev.resource_id,),
        ).fetchone()
        if item_row is None:
            raise WorkKernelRefused("Cited work_item does not exist")
        if str(item_row[0]) != company_id or str(item_row[1]) != agent_id:
            raise WorkKernelRefused("Agent is not authorized to cite work_item")
        expected_item_rev = work_item_evidence_revision(
            ev.resource_id,
            company_id=str(item_row[0]),
            owner_agent_id=str(item_row[1]),
            duty_kind=str(item_row[2]),
            source_kind=str(item_row[3]),
            source_id=str(item_row[4]),
            payload_json=str(item_row[5]),
            created_at_ns=int(item_row[6]),
        )
        if ev.revision != expected_item_rev:
            raise WorkKernelRefused(
                "Cited work_item revision does not match canonical item state"
            )
        if ev.available_at_ns < int(item_row[6]) or int(item_row[6]) > decision_at_ns:
            raise WorkKernelRefused(
                "Cited work_item timestamps precede item creation or follow decision"
            )
    elif ev.kind == "tool_call_receipt":
        target_run_id: str | None = None
        target_call_id = ev.resource_id
        if "/" in ev.resource_id:
            target_run_id, target_call_id = ev.resource_id.split("/", 1)
        elif ":" in ev.resource_id:
            target_run_id, target_call_id = ev.resource_id.split(":", 1)
        if not target_call_id or "/" in target_call_id or ":" in target_call_id:
            raise WorkKernelRefused("Invalid tool_call_receipt resource_id")
        if target_run_id is not None:
            rec_rows = connection.execute(
                """SELECT run_id, result_digest, recorded_at_ns
                   FROM tool_call_receipt
                   WHERE company_id = ? AND agent_id = ? AND run_id = ? AND call_id = ?""",
                (company_id, agent_id, target_run_id, target_call_id),
            ).fetchall()
        else:
            rec_rows = connection.execute(
                """SELECT run_id, result_digest, recorded_at_ns
                   FROM tool_call_receipt
                   WHERE company_id = ? AND agent_id = ? AND call_id = ?""",
                (company_id, agent_id, target_call_id),
            ).fetchall()
            if len(rec_rows) > 1 and current_run_id is not None:
                same_run = [r for r in rec_rows if str(r[0]) == current_run_id]
                if len(same_run) == 1:
                    rec_rows = same_run
        if not rec_rows:
            any_row = (
                connection.execute(
                    """SELECT 1 FROM tool_call_receipt
                       WHERE company_id = ? AND run_id = ? AND call_id = ?
                       LIMIT 1""",
                    (company_id, target_run_id, target_call_id),
                ).fetchone()
                if target_run_id is not None
                else connection.execute(
                    """SELECT 1 FROM tool_call_receipt
                       WHERE company_id = ? AND call_id = ?
                       LIMIT 1""",
                    (company_id, target_call_id),
                ).fetchone()
            )
            if any_row is None:
                raise WorkKernelRefused("Cited tool_call_receipt does not exist")
            raise WorkKernelRefused("Agent is not authorized to cite tool_call_receipt")
        if len(rec_rows) > 1:
            raise WorkKernelRefused(
                "Cited tool_call_receipt call_id is ambiguous across runs; qualify with run_id/call_id"
            )
        _rec_run_id, rec_digest, rec_recorded_at_ns = rec_rows[0]
        if ev.revision != str(rec_digest):
            raise WorkKernelRefused(
                "Cited tool_call_receipt revision does not match recorded result_digest"
            )
        if (
            ev.available_at_ns < int(rec_recorded_at_ns)
            or int(rec_recorded_at_ns) > decision_at_ns
        ):
            raise WorkKernelRefused(
                "Cited tool_call_receipt timestamps precede receipt recording or follow decision"
            )
    else:
        return False
    return True


def record_work_decision(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    work_item_id: str,
    run_id: str,
    fencing_token: int,
    agent_id: str,
    action_kind: str,
    rationale: str,
    evidence: Sequence[DecisionEvidence | Mapping[str, Any]] = (),
    decision_at_ns: int | None = None,
    now_ns: int | None = None,
    action_id: str | None = None,
    next_condition: Mapping[str, Any] | None = None,
    active_snapshot_id: str | None = None,
    domain_plugins: Sequence[Any] = (),
) -> WorkDecision:
    """Validate evidence, persist work_decision + optional next_condition atomically."""
    _require_nonempty(company_id, "company_id")
    _require_nonempty(work_item_id, "work_item_id")
    _require_nonempty(run_id, "run_id")
    _require_nonempty(agent_id, "agent_id")
    _require_nonempty(action_kind, "action_kind")
    if action_kind not in WORK_DECISION_ACTION_KINDS:
        raise WorkKernelRefused(
            f"Unsupported work_decision action_kind: {action_kind!r}"
        )
    _require_nonempty(rationale, "rationale")
    if action_id is not None:
        _require_nonempty(action_id, "action_id")
    effective_action_id = action_id or f"dec-{uuid4().hex}"
    if decision_at_ns is not None:
        effective_decision_ns = decision_at_ns
    elif now_ns is not None:
        effective_decision_ns = now_ns
    else:
        effective_decision_ns = time_ns()
    if type(effective_decision_ns) is not int or effective_decision_ns <= 0:
        raise WorkKernelRefused("decision_at_ns must be a positive integer")

    run = assert_run_fencing(
        connection, run_id, fencing_token, now_ns=effective_decision_ns
    )
    if (
        run.company_id != company_id
        or run.work_item_id != work_item_id
        or run.agent_id != agent_id
    ):
        raise WorkKernelRefused("Work decision does not match active run identity")
    if not has_active_grant(connection, company_id, agent_id):
        raise RunFenced(f"Agent {agent_id} no longer holds an active grant")
    item_row = connection.execute(
        """SELECT owner_agent_id, state, payload_json
           FROM work_items
           WHERE company_id = ? AND work_item_id = ?""",
        (company_id, work_item_id),
    ).fetchone()
    if (
        item_row is None
        or str(item_row[0]) != agent_id
        or str(item_row[1]) != "running"
    ):
        raise WorkKernelRefused("Work item is not actively running for this Agent")

    normalized_evidence = _normalize_evidence(evidence)
    for ev in normalized_evidence:
        validated = _validate_core_evidence_entry(
            connection,
            company_id=company_id,
            agent_id=agent_id,
            decision_at_ns=effective_decision_ns,
            ev=ev,
            current_run_id=run_id,
        )
        for plugin in domain_plugins:
            validator = getattr(plugin, "validate_decision_evidence", None)
            if validator is not None:
                if (
                    validator(
                        connection,
                        company_id,
                        agent_id,
                        effective_decision_ns,
                        ev,
                    )
                    is True
                ):
                    validated = True
        if not validated:
            raise WorkKernelRefused(
                f"Unsupported or unvalidated DecisionEvidence kind: {ev.kind!r}"
            )

    evidence_refs = [
        {
            "kind": ev.kind,
            "resource_id": (
                f"private_draft:opaque:{sha256(f'{agent_id}:{ev.resource_id}'.encode('utf-8')).hexdigest()[:24]}"
                if ev.kind == "private_draft"
                else ev.resource_id
            ),
            "revision": ev.revision,
            "observed_at_ns": ev.observed_at_ns,
            "available_at_ns": ev.available_at_ns,
        }
        for ev in normalized_evidence
    ]
    evidence_refs_json = _canonical_json(evidence_refs)
    evidence_digest = sha256(evidence_refs_json.encode("utf-8")).hexdigest()

    validated_next = (
        validate_next_condition_dict(next_condition)
        if next_condition is not None
        else None
    )

    prior_row = connection.execute(
        """SELECT decision_id, company_id, work_item_id, run_id, agent_id,
                  action_id, action_kind, evidence_refs_json, evidence_digest,
                  rationale, next_condition_id, active_snapshot_id, decision_at_ns
           FROM work_decision
           WHERE company_id = ? AND agent_id = ? AND action_id = ?""",
        (company_id, agent_id, effective_action_id),
    ).fetchone()
    if prior_row is not None:
        prior = WorkDecision(*prior_row)
        run_replay_ok = prior.run_id == run_id
        if not run_replay_ok and prior.work_item_id == work_item_id:
            prior_run_row = connection.execute(
                "SELECT status FROM work_runs WHERE company_id = ? AND run_id = ?",
                (company_id, prior.run_id),
            ).fetchone()
            run_replay_ok = prior_run_row is not None and str(prior_run_row[0]) in (
                "fenced",
                "failed",
            )
        if (
            prior.work_item_id != work_item_id
            or not run_replay_ok
            or prior.action_kind != action_kind
            or prior.evidence_digest != evidence_digest
            or prior.rationale != rationale
            or (
                decision_at_ns is not None
                and prior.decision_at_ns != effective_decision_ns
            )
            or prior.active_snapshot_id != active_snapshot_id
            or (prior.next_condition_id is None) != (validated_next is None)
        ):
            raise WorkKernelRefused(
                "Work decision conflicts with previously recorded action_id"
            )
        if validated_next is not None and prior.next_condition_id is not None:
            cond_row = connection.execute(
                """SELECT responsibility_id, work_item_id, trigger_kind, due_at_ns,
                          source_identity, expected_revision, expires_at_ns,
                          cooldown_ns, max_fires, condition_revision
                   FROM work_conditions WHERE condition_id = ?""",
                (prior.next_condition_id,),
            ).fetchone()
            if cond_row is None:
                raise WorkKernelRefused(
                    "Work decision conflicts with missing next_condition"
                )
            exp_resp = validated_next.get("responsibility_id")
            exp_item = _normalize_next_condition_work_item_id(
                connection,
                company_id=company_id,
                work_item_id=work_item_id,
                next_condition=validated_next,
                always_link_work_item=False,
            )
            req_source = validated_next.get("source_identity")
            exp_source = (
                f"timer:{prior.next_condition_id}" if req_source is None else req_source
            )
            if (
                cond_row[0] != exp_resp
                or cond_row[1] != exp_item
                or cond_row[2] != validated_next["trigger_kind"]
                or cond_row[3] != validated_next.get("due_at_ns")
                or cond_row[4] != exp_source
                or cond_row[5] != validated_next.get("expected_revision")
                or cond_row[6] != validated_next.get("expires_at_ns")
                or int(cond_row[7]) != int(validated_next.get("cooldown_ns", 0))
                or cond_row[8] != validated_next.get("max_fires")
                or (
                    "condition_revision" in validated_next
                    and int(cond_row[9]) != int(validated_next["condition_revision"])
                )
            ):
                raise WorkKernelRefused(
                    "Work decision conflicts with previously armed next_condition"
                )
        return prior

    next_condition_id: str | None = None
    if validated_next is not None:
        armed = _arm_next_condition(
            connection,
            company_id=company_id,
            owner_agent_id=agent_id,
            work_item_id=work_item_id,
            next_condition=validated_next,
            now_ns=effective_decision_ns,
            always_link_work_item=False,
        )
        next_condition_id = armed.condition_id

    decision_id = uuid4().hex
    connection.execute(
        """INSERT INTO work_decision (
               decision_id, company_id, work_item_id, run_id, agent_id,
               action_id, action_kind, evidence_refs_json, evidence_digest,
               rationale, next_condition_id, active_snapshot_id, decision_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            decision_id,
            company_id,
            work_item_id,
            run_id,
            agent_id,
            effective_action_id,
            action_kind,
            evidence_refs_json,
            evidence_digest,
            rationale,
            next_condition_id,
            active_snapshot_id,
            effective_decision_ns,
        ),
    )
    append_audit_event(
        connection,
        company_id=company_id,
        domain="work",
        entity_type="work_decision",
        entity_id=decision_id,
        event_type="work.decision_recorded",
        actor_id=agent_id,
        run_id=run_id,
        payload={
            "work_item_id": work_item_id,
            "action_id": opaque_decision_action_id(effective_action_id),
            "action_kind": action_kind,
            "evidence_digest": evidence_digest,
            "next_condition_id": next_condition_id,
        },
        recorded_at_ns=effective_decision_ns,
    )

    try:
        item_payload = json.loads(str(item_row[2]))
    except json.JSONDecodeError:
        item_payload = {}
    if isinstance(item_payload, dict):
        linked_resp = item_payload.get("responsibility_id")
        if isinstance(linked_resp, str) and linked_resp.strip():
            resp_info = connection.execute(
                """SELECT duty_kind FROM work_responsibilities
                   WHERE company_id = ? AND responsibility_id = ?""",
                (company_id, linked_resp),
            ).fetchone()
            if resp_info is not None and str(resp_info[0]) == "work.discovery_review":
                linked_cond_id = item_payload.get("condition_id")
                record_discovery_hysteresis(
                    connection,
                    company_id=company_id,
                    responsibility_id=linked_resp,
                    action_kind=action_kind,
                    now_ns=effective_decision_ns,
                    condition_id=(
                        linked_cond_id.strip()
                        if isinstance(linked_cond_id, str) and linked_cond_id.strip()
                        else None
                    ),
                )

    return WorkDecision(
        decision_id=decision_id,
        company_id=company_id,
        work_item_id=work_item_id,
        run_id=run_id,
        agent_id=agent_id,
        action_id=effective_action_id,
        action_kind=action_kind,
        evidence_refs_json=evidence_refs_json,
        evidence_digest=evidence_digest,
        rationale=rationale,
        next_condition_id=next_condition_id,
        active_snapshot_id=active_snapshot_id,
        decision_at_ns=effective_decision_ns,
    )


def open_peer_request(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    requester_work_item_id: str,
    requester_run_id: str,
    fencing_token: int,
    recipient_agent_id: str,
    question: str,
    expected_decision: str,
    evidence_refs: list[str],
    deadline_ns: int,
    request_id: str,
    request_key: str | None = None,
    project_id: str | None = None,
    now_ns: int | None = None,
) -> tuple[PeerRequest, bool]:
    """Record a blocking peer request, yield requester run to zero-compute wait, and create recipient duty."""
    for value, label in (
        (request_id, "request_id"),
        (recipient_agent_id, "recipient_agent_id"),
        (question, "question"),
        (expected_decision, "expected_decision"),
    ):
        _require_nonempty(value, label)
    if request_key is not None:
        _require_nonempty(request_key, "request_key")
    if project_id is not None:
        _require_nonempty(project_id, "project_id")
    _require_agent(connection, company_id, recipient_agent_id)
    evidence_json = _canonical_json(sorted(evidence_refs))
    timestamp = time_ns() if now_ns is None else now_ns

    existing = None
    if request_key is not None:
        existing = connection.execute(
            f"""SELECT {_PEER_REQUEST_COLUMNS}
                FROM work_peer_requests
                WHERE requester_work_item_id = ? AND request_key = ?""",
            (requester_work_item_id, request_key),
        ).fetchone()
    if existing is None:
        existing = connection.execute(
            f"SELECT {_PEER_REQUEST_COLUMNS} FROM work_peer_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    if existing is not None:
        prior = PeerRequest(*existing)
        run_row = connection.execute(
            """SELECT run_id, company_id, work_item_id, agent_id, duty_kind, status,
                      fencing_token, lease_owner, lease_expires_at_ns, wait_until_ns,
                      wait_event_key, budget_ceiling_minor, dedupe_key, outcome_json,
                      created_at_ns, updated_at_ns
               FROM work_runs
               WHERE run_id = ?""",
            (requester_run_id,),
        ).fetchone()
        if run_row is None:
            raise RunFenced("Work run does not exist")
        replay_run = WorkRun(*run_row)
        if replay_run.fencing_token != fencing_token:
            raise RunFenced(
                f"Run {requester_run_id} is fenced (token={replay_run.fencing_token} != {fencing_token})"
            )
        if replay_run.status == "running":
            replay_run = assert_run_fencing(
                connection, requester_run_id, fencing_token, now_ns=timestamp
            )
        elif (
            replay_run.status == "waiting"
            and prior.requester_run_id == requester_run_id
        ):
            if employment_cutoff(connection, replay_run.agent_id) is not None:
                raise RunFenced(
                    f"Agent {replay_run.agent_id} employment has been retired"
                )
            if not has_active_grant(connection, company_id, replay_run.agent_id):
                raise RunFenced(
                    f"Agent {replay_run.agent_id} no longer holds an active grant"
                )
        else:
            raise RunFenced(
                f"Run {requester_run_id} is fenced (status={replay_run.status})"
            )
        if (
            replay_run.work_item_id != requester_work_item_id
            or replay_run.company_id != company_id
        ):
            raise WorkKernelRefused("Peer request does not match active run identity")
        if (
            prior.company_id != company_id
            or prior.requester_work_item_id != requester_work_item_id
            or prior.requester_run_id != requester_run_id
            or prior.recipient_agent_id != recipient_agent_id
            or prior.question != question
            or prior.expected_decision != expected_decision
            or prior.evidence_refs_json != evidence_json
            or prior.deadline_ns != deadline_ns
            or (project_id is not None and prior.project_id != project_id)
            or (request_key is not None and prior.request_key != request_key)
        ):
            raise WorkKernelRefused("Peer request conflicts with the original receipt")
        return prior, True

    run = assert_run_fencing(
        connection, requester_run_id, fencing_token, now_ns=timestamp
    )
    if run.work_item_id != requester_work_item_id or run.company_id != company_id:
        raise WorkKernelRefused("Peer request does not match active run identity")
    if run.agent_id == recipient_agent_id:
        raise WorkKernelRefused("An Agent cannot open a peer request to itself")

    if deadline_ns <= timestamp:
        raise WorkKernelRefused("Peer request deadline must be in the future")

    resolved_project_id = _authorize_peer_route(
        connection,
        company_id=company_id,
        requester_agent_id=run.agent_id,
        recipient_agent_id=recipient_agent_id,
        evidence_refs=evidence_refs,
        project_id=project_id,
        check_open_quota=True,
        check_turn_capacity=True,
    )
    project_id = resolved_project_id

    parent_row = connection.execute(
        f"""SELECT {_PEER_REQUEST_COLUMNS}
            FROM work_peer_requests
            WHERE company_id = ? AND response_work_item_id = ?""",
        (company_id, requester_work_item_id),
    ).fetchone()
    if parent_row is None:
        chain_depth = 1
        hop_count = 1
        root_work_item_id = requester_work_item_id
        delegation_path = [run.agent_id]
    else:
        parent = PeerRequest(*parent_row)
        parent_depth = int(parent.chain_depth) if int(parent.chain_depth) > 0 else 1
        parent_hops = (
            int(parent.hop_count) if int(parent.hop_count) > 0 else parent_depth
        )
        chain_depth = parent_depth + 1
        hop_count = parent_hops + 1
        root_work_item_id = parent.root_work_item_id or parent.requester_work_item_id
        try:
            parsed_path = json.loads(parent.delegation_path_json)
        except json.JSONDecodeError:
            parsed_path = []
        raw_path = (
            [str(a) for a in parsed_path if isinstance(a, str) and a]
            if isinstance(parsed_path, list) and parsed_path
            else [parent.requester_agent_id]
        )
        delegation_path = [*raw_path, run.agent_id]
    if chain_depth > MAX_PEER_CHAIN_DEPTH or hop_count > MAX_PEER_CHAIN_DEPTH:
        raise WorkKernelRefused("Peer delegation chain depth exceeded")
    if recipient_agent_id in delegation_path:
        raise WorkKernelRefused("Peer delegation cycle detected")
    delegation_path_json = _canonical_json(delegation_path)

    open_edges = connection.execute(
        """SELECT requester_agent_id, recipient_agent_id
           FROM work_peer_requests
           WHERE company_id = ? AND state = 'open'
             AND (root_work_item_id = ? OR requester_work_item_id = ?)""",
        (company_id, root_work_item_id, root_work_item_id),
    ).fetchall()
    adj: dict[str, set[str]] = {}
    for src_agent, dst_agent in open_edges:
        adj.setdefault(str(src_agent), set()).add(str(dst_agent))
    queue = deque([recipient_agent_id])
    visited = {recipient_agent_id}
    while queue:
        current = queue.popleft()
        if current == run.agent_id:
            raise WorkKernelRefused("Mutual wait cycle detected")
        for nxt in adj.get(current, ()):
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)

    window_start = max(0, timestamp - PAIR_WINDOW_NS)
    pair_count = int(
        connection.execute(
            """SELECT COUNT(*) FROM work_peer_requests
               WHERE company_id = ? AND requester_agent_id = ?
                 AND recipient_agent_id = ? AND created_at_ns >= ?""",
            (company_id, run.agent_id, recipient_agent_id, window_start),
        ).fetchone()[0]
    )
    if pair_count >= PAIRWISE_RATE_LIMIT_COUNT:
        raise WorkKernelRefused("Pairwise peer request rate limit exceeded")

    response_item, _ = create_work_item(
        connection,
        company_id=company_id,
        owner_agent_id=recipient_agent_id,
        duty_kind="work.peer_response",
        source_kind="peer_request",
        source_id=request_id,
        parent_work_item_id=requester_work_item_id,
        dedupe_key=f"peer-response:{request_id}",
        payload={
            "request_id": request_id,
            "request_key": request_key,
            "project_id": project_id,
            "requester_agent_id": run.agent_id,
            "question": question,
            "expected_decision": expected_decision,
            "evidence_refs": sorted(evidence_refs),
            "deadline_ns": deadline_ns,
        },
        actor_id=run.agent_id,
        now_ns=timestamp,
    )
    try:
        connection.execute(
            """INSERT INTO work_peer_requests (
                   request_id, company_id, requester_work_item_id, requester_run_id,
                   requester_agent_id, recipient_agent_id, response_work_item_id,
                   request_key, project_id, question, expected_decision,
                   evidence_refs_json, deadline_ns, state, read_run_id,
                   answered_run_id, resumed_run_id, response_json, resolved_at_ns,
                   hop_count, chain_depth, root_work_item_id, delegation_path_json,
                   created_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?)""",
            (
                request_id,
                company_id,
                requester_work_item_id,
                requester_run_id,
                run.agent_id,
                recipient_agent_id,
                response_item.work_item_id,
                request_key,
                project_id,
                question,
                expected_decision,
                evidence_json,
                deadline_ns,
                hop_count,
                chain_depth,
                root_work_item_id,
                delegation_path_json,
                timestamp,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise WorkKernelRefused(
            "Requester work item already has an open blocking peer request"
        ) from error

    yield_run_to_wait(
        connection,
        requester_run_id,
        fencing_token,
        wait_until_ns=deadline_ns,
        wait_event_key=f"peer_response:{request_id}",
        reason="waiting_for_peer_response",
        now_ns=timestamp,
    )
    return (
        PeerRequest(
            request_id,
            company_id,
            requester_work_item_id,
            requester_run_id,
            run.agent_id,
            recipient_agent_id,
            response_item.work_item_id,
            question,
            expected_decision,
            evidence_json,
            deadline_ns,
            "open",
            None,
            None,
            timestamp,
            request_key=request_key,
            project_id=project_id,
            hop_count=hop_count,
            chain_depth=chain_depth,
            root_work_item_id=root_work_item_id,
            delegation_path_json=delegation_path_json,
        ),
        False,
    )


def record_peer_request_read(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    request_id: str,
    actor_id: str,
    response_run_id: str,
    response_fencing_token: int,
    now_ns: int | None = None,
) -> PeerRequest:
    """Record that the named recipient read an open peer request during the active response run."""
    row = connection.execute(
        f"""SELECT {_PEER_REQUEST_COLUMNS}
            FROM work_peer_requests
            WHERE company_id = ? AND request_id = ?""",
        (company_id, request_id),
    ).fetchone()
    if row is None:
        raise WorkKernelRefused("Peer request not found")
    prior = PeerRequest(*row)
    if prior.state != "open":
        raise WorkKernelRefused("Peer question is not open for this run")
    if actor_id != prior.recipient_agent_id:
        raise WorkKernelRefused(
            "Only the named recipient Agent may read a peer request"
        )
    timestamp = time_ns() if now_ns is None else now_ns
    if timestamp >= prior.deadline_ns:
        raise WorkKernelRefused("Peer request deadline has elapsed")
    resp_run = assert_run_fencing(
        connection, response_run_id, response_fencing_token, now_ns=timestamp
    )
    if (
        resp_run.company_id != company_id
        or resp_run.work_item_id != prior.response_work_item_id
        or resp_run.agent_id != prior.recipient_agent_id
    ):
        raise WorkKernelRefused(
            "Response run does not match the named recipient's response duty"
        )
    refs = json.loads(prior.evidence_refs_json)
    _authorize_peer_route(
        connection,
        company_id=company_id,
        requester_agent_id=prior.requester_agent_id,
        recipient_agent_id=prior.recipient_agent_id,
        evidence_refs=refs if isinstance(refs, list) else [],
        project_id=prior.project_id,
        check_open_quota=False,
        check_turn_capacity=False,
    )
    connection.execute(
        """UPDATE work_peer_requests
           SET read_run_id = ?
           WHERE company_id = ? AND request_id = ? AND state = 'open'""",
        (response_run_id, company_id, request_id),
    )
    return PeerRequest(
        prior.request_id,
        prior.company_id,
        prior.requester_work_item_id,
        prior.requester_run_id,
        prior.requester_agent_id,
        prior.recipient_agent_id,
        prior.response_work_item_id,
        prior.question,
        prior.expected_decision,
        prior.evidence_refs_json,
        prior.deadline_ns,
        prior.state,
        prior.response_json,
        prior.resolved_at_ns,
        prior.created_at_ns,
        request_key=prior.request_key,
        project_id=prior.project_id,
        read_run_id=response_run_id,
        answered_run_id=prior.answered_run_id,
        resumed_run_id=prior.resumed_run_id,
        hop_count=prior.hop_count,
        chain_depth=prior.chain_depth,
        root_work_item_id=prior.root_work_item_id,
        delegation_path_json=prior.delegation_path_json,
    )


def resolve_peer_request(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    request_id: str,
    actor_id: str,
    resolution_state: str,
    response_payload: dict[str, Any],
    response_run_id: str | None = None,
    response_fencing_token: int | None = None,
    caller_response_run_id: str | None = None,
    now_ns: int | None = None,
) -> tuple[PeerRequest, bool]:
    """Answer, decline, or expire an open peer request and wake the requester once."""
    if resolution_state not in ("answered", "declined", "expired"):
        raise WorkKernelRefused(
            f"Unsupported peer resolution state: {resolution_state}"
        )
    row = connection.execute(
        f"""SELECT {_PEER_REQUEST_COLUMNS}
            FROM work_peer_requests
            WHERE company_id = ? AND request_id = ?""",
        (company_id, request_id),
    ).fetchone()
    if row is None:
        raise WorkKernelRefused("Peer request not found")
    prior = PeerRequest(*row)
    if prior.state != "open":
        return prior, False

    timestamp = time_ns() if now_ns is None else now_ns
    if resolution_state in ("answered", "declined"):
        if actor_id != prior.recipient_agent_id:
            raise WorkKernelRefused(
                "Only the named recipient Agent may answer or decline a peer request"
            )
        if timestamp >= prior.deadline_ns:
            raise WorkKernelRefused("Peer request deadline has elapsed")
        if not response_run_id or response_fencing_token is None:
            raise WorkKernelRefused(
                "Peer response requires an active response_run_id and response_fencing_token"
            )
        resp_run = assert_run_fencing(
            connection, response_run_id, response_fencing_token, now_ns=timestamp
        )
        if (
            resp_run.lease_expires_at_ns is not None
            and timestamp >= resp_run.lease_expires_at_ns
        ):
            raise RunFenced("Response run lease has expired")
        if (
            resp_run.company_id != company_id
            or resp_run.work_item_id != prior.response_work_item_id
            or resp_run.agent_id != prior.recipient_agent_id
        ):
            raise WorkKernelRefused(
                "Response run does not match the named recipient's response duty"
            )
        if prior.read_run_id != response_run_id:
            raise WorkKernelRefused(
                "Peer outcome requires a current read before answering"
            )
        try:
            refs = json.loads(prior.evidence_refs_json)
        except json.JSONDecodeError as error:
            raise WorkKernelRefused("Malformed peer request evidence refs") from error
        _authorize_peer_route(
            connection,
            company_id=company_id,
            requester_agent_id=prior.requester_agent_id,
            recipient_agent_id=prior.recipient_agent_id,
            evidence_refs=refs if isinstance(refs, list) else [],
            project_id=prior.project_id,
            check_open_quota=False,
            check_turn_capacity=False,
        )
    elif timestamp < prior.deadline_ns:
        raise WorkKernelRefused("Peer request has not reached its deadline")

    response_json = _canonical_json(response_payload)
    answered_run = (
        response_run_id if resolution_state in ("answered", "declined") else None
    )
    if resolution_state in ("answered", "declined"):
        changed = connection.execute(
            """UPDATE work_peer_requests
               SET state = ?, answered_run_id = ?, response_json = ?, resolved_at_ns = ?
               WHERE company_id = ? AND request_id = ? AND state = 'open'
                 AND ? < deadline_ns AND read_run_id = ?""",
            (
                resolution_state,
                answered_run,
                response_json,
                timestamp,
                company_id,
                request_id,
                timestamp,
                response_run_id,
            ),
        )
    else:
        changed = connection.execute(
            """UPDATE work_peer_requests
               SET state = ?, answered_run_id = ?, response_json = ?, resolved_at_ns = ?
               WHERE company_id = ? AND request_id = ? AND state = 'open'
                 AND ? >= deadline_ns""",
            (
                resolution_state,
                answered_run,
                response_json,
                timestamp,
                company_id,
                request_id,
                timestamp,
            ),
        )
    if changed.rowcount != 1:
        refetched = connection.execute(
            f"""SELECT {_PEER_REQUEST_COLUMNS}
                FROM work_peer_requests
                WHERE company_id = ? AND request_id = ?""",
            (company_id, request_id),
        ).fetchone()
        return (PeerRequest(*refetched) if refetched is not None else prior), False

    if resolution_state == "expired":
        skip_self_terminal = False
        if caller_response_run_id is not None:
            running_row = connection.execute(
                """SELECT run_id FROM work_runs
                   WHERE company_id = ? AND work_item_id = ? AND status = 'running'""",
                (company_id, prior.response_work_item_id),
            ).fetchone()
            skip_self_terminal = (
                running_row is not None
                and str(running_row[0]) == caller_response_run_id
            )
        if not skip_self_terminal:
            resp_item_row = connection.execute(
                """SELECT state FROM work_items
                   WHERE company_id = ? AND work_item_id = ?""",
                (company_id, prior.response_work_item_id),
            ).fetchone()
            if resp_item_row is not None and str(resp_item_row[0]) in (
                "due",
                "scheduled",
                "running",
                "waiting",
            ):
                sync_duty_terminal(
                    connection,
                    prior.response_work_item_id,
                    item_state="cancelled",
                    run_status="fenced",
                    now_ns=timestamp,
                )

    # Wake the waiting requester work item once (anti-self-wake respected)
    wake_waiting_runs(
        connection,
        company_id,
        now_ns=timestamp,
        event_key=f"peer_response:{request_id}",
        triggering_actor_id=(
            "work_scheduler" if resolution_state == "expired" else actor_id
        ),
    )
    append_audit_event(
        connection,
        company_id=company_id,
        domain="work",
        entity_type="peer_request",
        entity_id=request_id,
        event_type=f"work.peer_request_{resolution_state}",
        actor_id=actor_id,
        payload={"requester_work_item_id": prior.requester_work_item_id},
        recorded_at_ns=timestamp,
    )
    return (
        PeerRequest(
            prior.request_id,
            prior.company_id,
            prior.requester_work_item_id,
            prior.requester_run_id,
            prior.requester_agent_id,
            prior.recipient_agent_id,
            prior.response_work_item_id,
            prior.question,
            prior.expected_decision,
            prior.evidence_refs_json,
            prior.deadline_ns,
            resolution_state,
            response_json,
            timestamp,
            prior.created_at_ns,
            request_key=prior.request_key,
            project_id=prior.project_id,
            read_run_id=prior.read_run_id,
            answered_run_id=answered_run,
            resumed_run_id=prior.resumed_run_id,
            hop_count=prior.hop_count,
            chain_depth=prior.chain_depth,
            root_work_item_id=prior.root_work_item_id,
            delegation_path_json=prior.delegation_path_json,
        ),
        True,
    )


def advance_peer_read_on_resume(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    response_work_item_id: str,
    old_run_id: str,
    new_run_id: str,
) -> None:
    """Carry forward prior read proof when a multi-hop peer_response item resumes on a new run."""
    connection.execute(
        """UPDATE work_peer_requests
           SET read_run_id = ?
           WHERE company_id = ? AND response_work_item_id = ?
             AND state = 'open' AND read_run_id = ?""",
        (new_run_id, company_id, response_work_item_id, old_run_id),
    )


def mark_peer_request_resumed(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    request_id: str,
    resumed_run_id: str,
) -> None:
    """Bind the resumed requester run to its resolved peer request."""
    changed = connection.execute(
        """UPDATE work_peer_requests
           SET resumed_run_id = ?
           WHERE company_id = ? AND request_id = ?
             AND state != 'open' AND resumed_run_id IS NULL""",
        (resumed_run_id, company_id, request_id),
    )
    if changed.rowcount != 1:
        raise WorkKernelRefused("Peer request is not eligible for resume binding")


def read_peer_request_outcome(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    requester_work_item_id: str,
    actor_id: str,
    include_provenance: bool = False,
) -> dict[str, Any] | None:
    """Read the resolved peer outcome for a resumed requester, guarding project removal."""
    row = connection.execute(
        """SELECT request_key, request_id, state, response_json, project_id,
                  requester_agent_id, recipient_agent_id, evidence_refs_json
           FROM work_peer_requests
           WHERE company_id = ? AND requester_work_item_id = ?
             AND resumed_run_id IS NOT NULL
           ORDER BY rowid DESC LIMIT 1""",
        (company_id, requester_work_item_id),
    ).fetchone()
    if row is None:
        return None
    (
        request_key,
        request_id,
        state,
        response_json,
        project_id,
        requester_agent_id,
        recipient_agent_id,
        evidence_refs_json,
    ) = row
    label = str(request_key) if request_key is not None else str(request_id)
    if project_id is not None:
        proj = connection.execute(
            "SELECT members FROM shared_project WHERE company_id = ? AND project_id = ?",
            (company_id, str(project_id)),
        ).fetchone()
        members = set(json.loads(str(proj[0]))) if proj is not None else set()
        if actor_id not in members:
            unavailable: dict[str, Any] = {
                "request_key": label,
                "outcome": "unavailable",
                "answer": None,
            }
            if include_provenance:
                unavailable["request_id"] = str(request_id)
                unavailable["project_id"] = str(project_id)
                unavailable["requester_agent_id"] = str(requester_agent_id)
                unavailable["recipient_agent_id"] = str(recipient_agent_id)
                unavailable["evidence_refs"] = []
            return unavailable
    payload = json.loads(str(response_json)) if response_json is not None else {}
    answer = payload.get("answer") if isinstance(payload, dict) else None
    result: dict[str, Any] = {
        "request_key": label,
        "outcome": str(state),
        "answer": answer,
    }
    if include_provenance:
        combined_refs: list[str] = []
        for raw_source in (
            json.loads(str(evidence_refs_json)) if evidence_refs_json else (),
            payload.get("evidence_refs") if isinstance(payload, dict) else (),
        ):
            if isinstance(raw_source, list):
                for item in raw_source:
                    if (
                        isinstance(item, str)
                        and item.strip()
                        and item.strip() not in combined_refs
                    ):
                        combined_refs.append(item.strip())
        result["request_id"] = str(request_id)
        result["project_id"] = None if project_id is None else str(project_id)
        result["requester_agent_id"] = str(requester_agent_id)
        result["recipient_agent_id"] = str(recipient_agent_id)
        result["evidence_refs"] = combined_refs
    return result


def interrupt_work_run(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    run_id: str,
    expected_fencing_token: int,
    actor_id: str,
    reason: str,
    command_id: str | None = None,
    now_ns: int | None = None,
) -> tuple[bool, int]:
    """Cancel an active or waiting work_run with optimistic fencing_token check.

    Returns (applied, current_fencing_token). Returns (False, current_token) on
    fencing mismatch or already-terminal state so callers can persist a
    conflicting_revision receipt cleanly.
    """
    _require_nonempty(company_id, "company_id")
    _require_nonempty(run_id, "run_id")
    _require_nonempty(actor_id, "actor_id")
    row = connection.execute(
        """SELECT work_item_id, status, fencing_token
           FROM work_runs
           WHERE company_id = ? AND run_id = ?""",
        (company_id, run_id),
    ).fetchone()
    if row is None:
        raise WorkKernelRefused("Work run not found for company")
    work_item_id, status, current_token = str(row[0]), str(row[1]), int(row[2])
    if (
        status not in ("queued", "running", "waiting")
        or current_token != expected_fencing_token
    ):
        return False, current_token
    timestamp = time_ns() if now_ns is None else now_ns
    outcome = json.dumps({"interrupted": True}, sort_keys=True)
    changed = connection.execute(
        """UPDATE work_runs
           SET status = 'cancelled',
               lease_owner = NULL,
               lease_expires_at_ns = NULL,
               wait_until_ns = NULL,
               wait_event_key = NULL,
               outcome_json = ?,
               updated_at_ns = ?
           WHERE company_id = ? AND run_id = ?
             AND status IN ('queued', 'running', 'waiting')
             AND fencing_token = ?""",
        (outcome, timestamp, company_id, run_id, expected_fencing_token),
    )
    if changed.rowcount != 1:
        return False, current_token
    connection.execute(
        """UPDATE work_items
           SET state = 'cancelled', updated_at_ns = ?
           WHERE company_id = ? AND work_item_id = ?
             AND state IN ('due', 'scheduled', 'running', 'waiting', 'blocked')""",
        (timestamp, company_id, work_item_id),
    )
    append_audit_event(
        connection,
        company_id=company_id,
        domain="work",
        entity_type="work_run",
        entity_id=run_id,
        event_type="work.run_interrupted",
        actor_id=actor_id,
        run_id=run_id,
        command_id=command_id,
        payload={"work_item_id": work_item_id, "fencing_token": current_token},
        recorded_at_ns=timestamp,
    )
    return True, current_token


def cancel_work_item(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    work_item_id: str,
    expected_state: str,
    actor_id: str,
    reason: str,
    command_id: str | None = None,
    now_ns: int | None = None,
) -> tuple[bool, str]:
    """Cancel a non-terminal work_item and any active/waiting work_runs."""
    _require_nonempty(company_id, "company_id")
    _require_nonempty(work_item_id, "work_item_id")
    _require_nonempty(actor_id, "actor_id")
    row = connection.execute(
        """SELECT state FROM work_items
           WHERE company_id = ? AND work_item_id = ?""",
        (company_id, work_item_id),
    ).fetchone()
    if row is None:
        raise WorkKernelRefused("Work item not found for company")
    current_state = str(row[0])
    if current_state in ("settled", "cancelled") or current_state != expected_state:
        return False, current_state
    timestamp = time_ns() if now_ns is None else now_ns
    outcome = json.dumps({"cancelled": True}, sort_keys=True)
    connection.execute(
        """UPDATE work_runs
           SET status = 'cancelled',
               lease_owner = NULL,
               lease_expires_at_ns = NULL,
               wait_until_ns = NULL,
               wait_event_key = NULL,
               outcome_json = COALESCE(outcome_json, ?),
               updated_at_ns = ?
           WHERE company_id = ? AND work_item_id = ?
             AND status IN ('queued', 'running', 'waiting')""",
        (outcome, timestamp, company_id, work_item_id),
    )
    connection.execute(
        """UPDATE work_items
           SET state = 'cancelled', updated_at_ns = ?
           WHERE company_id = ? AND work_item_id = ?""",
        (timestamp, company_id, work_item_id),
    )
    append_audit_event(
        connection,
        company_id=company_id,
        domain="work",
        entity_type="work_item",
        entity_id=work_item_id,
        event_type="work.item_cancelled",
        actor_id=actor_id,
        command_id=command_id,
        payload={"previous_state": current_state},
        recorded_at_ns=timestamp,
    )
    return True, "cancelled"
