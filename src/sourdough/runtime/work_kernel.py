"""Three-entity Work kernel: items, fenced runs, responsibilities, conditions, and peer requests."""

import json
import sqlite3
from dataclasses import dataclass
from time import time_ns
from typing import Any
from uuid import uuid4

from ..duty_run import has_turn_capacity
from ..storage.kernel import append_audit_event


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
    """Return True iff the Agent holds a current non-revoked standing grant in this company."""
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
                      payload_json, created_at_ns, updated_at_ns
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


def _authorize_peer_route(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    requester_agent_id: str,
    recipient_agent_id: str,
    evidence_refs: list[str],
    check_open_quota: bool = False,
    check_turn_capacity: bool = True,
) -> None:
    """Atomically verify peer-request route, recipient grant/responsibility, evidence scope, and budget."""
    if not has_active_grant(connection, company_id, requester_agent_id):
        raise WorkKernelRefused("Requester Agent has no active standing grant")
    if not has_active_grant(connection, company_id, recipient_agent_id):
        raise WorkKernelRefused("Recipient Agent has no active standing grant")
    if check_turn_capacity and not has_turn_capacity(connection, recipient_agent_id):
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
        return

    raise WorkKernelRefused(
        "Peer request route or evidence scope is not authorized by recipient responsibility"
    )


def has_authorized_peer_response_item(
    connection: sqlite3.Connection, company_id: str, work_item_id: str
) -> bool:
    """Verify a peer_response work item's linked request is open and currently authorized."""
    row = connection.execute(
        """SELECT requester_agent_id, recipient_agent_id, evidence_refs_json, state
           FROM work_peer_requests
           WHERE company_id = ? AND response_work_item_id = ?""",
        (company_id, work_item_id),
    ).fetchone()
    if row is None:
        return True
    requester_id, recipient_id, evidence_json, state = row
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
            check_open_quota=False,
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
    """Cancel a pending peer_response work item if its linked peer request is closed."""
    req_state = connection.execute(
        """SELECT state FROM work_peer_requests
           WHERE company_id = ? AND response_work_item_id = ?""",
        (company_id, work_item_id),
    ).fetchone()
    if req_state is None or str(req_state[0]) == "open":
        return False
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
    if duty_kind == "work.peer_response":
        req_row = connection.execute(
            """SELECT requester_agent_id, recipient_agent_id, evidence_refs_json, state
               FROM work_peer_requests
               WHERE company_id = ? AND response_work_item_id = ?""",
            (company_id, work_item_id),
        ).fetchone()
        if req_row is not None:
            requester_id, recipient_id, evidence_json, req_state = req_row
            if str(req_state) != "open":
                raise WorkKernelRefused("Linked peer request is no longer open")
            refs = json.loads(str(evidence_json))
            _authorize_peer_route(
                connection,
                company_id=company_id,
                requester_agent_id=str(requester_id),
                recipient_agent_id=str(recipient_id),
                evidence_refs=refs if isinstance(refs, list) else [],
                check_open_quota=False,
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
    timestamp = time_ns() if now_ns is None else now_ns
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
           SET state = 'running', updated_at_ns = ?
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
    connection: sqlite3.Connection, run_id: str, fencing_token: int
) -> WorkRun:
    """Verify the run is still active at the exact fencing_token."""
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
    run = assert_run_fencing(connection, run_id, fencing_token)
    timestamp = time_ns() if now_ns is None else now_ns
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
        "UPDATE work_items SET state = 'waiting', updated_at_ns = ? WHERE work_item_id = ?",
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
) -> tuple[str, ...]:
    """Wake eligible waiting runs while enforcing the anti-self-wake invariant."""
    rows = connection.execute(
        """SELECT run_id, work_item_id, agent_id, wait_until_ns, wait_event_key
           FROM work_runs
           WHERE company_id = ? AND status = 'waiting'
           ORDER BY created_at_ns, run_id""",
        (company_id,),
    ).fetchall()
    woken_items: list[str] = []
    for run_id, work_item_id, agent_id, wait_until_ns, wait_event_key in rows:
        timer_due = wait_until_ns is not None and now_ns >= int(wait_until_ns)
        event_matched = (
            event_key is not None
            and wait_event_key == event_key
            and triggering_actor_id != agent_id
        )
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
               SET state = 'due', updated_at_ns = ?
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
                connection.execute(
                    """UPDATE work_items
                       SET state = 'cancelled', updated_at_ns = ?
                       WHERE company_id = ? AND work_item_id = ?
                         AND state IN ('due', 'scheduled')""",
                    (now_ns, company_id, str(open_peer[0])),
                )
        audit_actor = (
            triggering_actor_id
            if reason == "event" and triggering_actor_id
            else "work_scheduler"
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
            payload={"reason": reason, "event_key": event_key},
            recorded_at_ns=now_ns,
        )
        woken_items.append(str(work_item_id))
    return tuple(woken_items)


def reclaim_expired_runs(
    connection: sqlite3.Connection, company_id: str, *, now_ns: int
) -> tuple[str, ...]:
    """Fence running work_runs whose lease expired and return items to due."""
    expired = connection.execute(
        """SELECT run_id, work_item_id, fencing_token
           FROM work_runs
           WHERE company_id = ? AND status = 'running'
             AND lease_expires_at_ns IS NOT NULL AND lease_expires_at_ns <= ?
           ORDER BY created_at_ns, run_id""",
        (company_id, now_ns),
    ).fetchall()
    fenced_runs: list[str] = []
    for run_id, work_item_id, token in expired:
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
        fenced_runs.append(str(run_id))
    return tuple(fenced_runs)


def fail_work_run(
    connection: sqlite3.Connection,
    run_id: str,
    fencing_token: int,
    *,
    error_summary: str,
    return_to_due: bool = True,
    now_ns: int | None = None,
) -> WorkRun:
    """Record a failed active run and release the Agent's active execution slot."""
    run = assert_run_fencing(connection, run_id, fencing_token)
    timestamp = time_ns() if now_ns is None else now_ns
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
    connection.execute(
        "UPDATE work_items SET state = ?, updated_at_ns = ? WHERE work_item_id = ?",
        (next_state, timestamp, run.work_item_id),
    )
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
    run = assert_run_fencing(connection, run_id, fencing_token)
    timestamp = time_ns() if now_ns is None else now_ns
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
        "UPDATE work_items SET state = ?, updated_at_ns = ? WHERE work_item_id = ?",
        (item_state, timestamp, run.work_item_id),
    )
    armed: WorkCondition | None = None
    if next_condition is not None:
        armed = set_condition(
            connection,
            company_id=run.company_id,
            owner_agent_id=run.agent_id,
            trigger_kind=next_condition["trigger_kind"],
            responsibility_id=next_condition.get("responsibility_id"),
            work_item_id=run.work_item_id,
            due_at_ns=next_condition.get("due_at_ns"),
            source_identity=next_condition.get("source_identity"),
            expected_revision=next_condition.get("expected_revision"),
            expires_at_ns=next_condition.get("expires_at_ns"),
            now_ns=timestamp,
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
                  created_at_ns, updated_at_ns
           FROM work_conditions
           WHERE company_id = ? AND owner_agent_id = ?
           ORDER BY created_at_ns, condition_id""",
        (company_id, owner_agent_id),
    ).fetchall()
    return tuple(WorkCondition(*row) for row in rows)


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
    condition_revision: int = 1,
    condition_id: str | None = None,
    now_ns: int | None = None,
) -> WorkCondition:
    """Arm an Agent-owned next condition (timer or shared_asset_revision)."""
    _require_agent(connection, company_id, owner_agent_id)
    if type(condition_revision) is not int or condition_revision < 1:
        raise WorkKernelRefused("condition_revision must be a positive integer")
    if trigger_kind not in ("timer", "shared_asset_revision", "peer_response"):
        raise WorkKernelRefused(f"Unsupported condition trigger_kind: {trigger_kind}")
    if trigger_kind == "timer" and due_at_ns is None:
        raise WorkKernelRefused("Timer condition requires due_at_ns")
    if trigger_kind == "shared_asset_revision" and (
        not source_identity or not source_identity.strip()
    ):
        raise WorkKernelRefused("Asset revision condition requires source_identity")
    if trigger_kind == "peer_response" and (
        not source_identity or not source_identity.strip()
    ):
        raise WorkKernelRefused("Peer response condition requires source_identity")
    if responsibility_id is None and work_item_id is None:
        raise WorkKernelRefused(
            "Condition must link a responsibility_id or work_item_id"
        )
    if responsibility_id is not None:
        row = connection.execute(
            """SELECT owner_agent_id, status FROM work_responsibilities
               WHERE company_id = ? AND responsibility_id = ?""",
            (company_id, responsibility_id),
        ).fetchone()
        if row is None or row[0] != owner_agent_id or row[1] != "active":
            raise WorkKernelRefused(
                "Responsibility is inactive or owned by another Agent"
            )
    if work_item_id is not None:
        item_owner = connection.execute(
            """SELECT owner_agent_id FROM work_items
               WHERE company_id = ? AND work_item_id = ?""",
            (company_id, work_item_id),
        ).fetchone()
        if item_owner is None or item_owner[0] != owner_agent_id:
            raise WorkKernelRefused(
                "Work item does not exist or belongs to another Agent"
            )

    cid = condition_id or uuid4().hex
    timestamp = time_ns() if now_ns is None else now_ns
    effective_source = source_identity or f"timer:{cid}"
    connection.execute(
        """INSERT INTO work_conditions (
               condition_id, company_id, owner_agent_id, responsibility_id,
               work_item_id, condition_revision, trigger_kind, due_at_ns,
               source_identity, expected_revision, expires_at_ns, status,
               created_at_ns, updated_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'armed', ?, ?)""",
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
    now_ns: int | None = None,
) -> WorkCondition:
    """Supersede an armed condition and arm a successor with incremented condition_revision."""
    row = connection.execute(
        """SELECT condition_id, company_id, owner_agent_id, responsibility_id,
                  work_item_id, condition_revision, status
           FROM work_conditions WHERE condition_id = ?""",
        (condition_id,),
    ).fetchone()
    if row is None:
        raise WorkKernelRefused("Condition not found")
    _, company_id, actual_owner, resp_id, item_id, revision, status = row
    if actual_owner != owner_agent_id:
        raise WorkKernelRefused("Condition belongs to another Agent")
    if status != "armed":
        raise WorkKernelRefused(f"Only armed conditions can be replaced (got {status})")
    timestamp = time_ns() if now_ns is None else now_ns
    connection.execute(
        "UPDATE work_conditions SET status = 'superseded', updated_at_ns = ? WHERE condition_id = ?",
        (timestamp, condition_id),
    )
    return set_condition(
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
        condition_revision=int(revision) + 1,
        now_ns=timestamp,
    )


def cancel_condition(
    connection: sqlite3.Connection,
    condition_id: str,
    *,
    owner_agent_id: str,
    now_ns: int | None = None,
) -> None:
    timestamp = time_ns() if now_ns is None else now_ns
    changed = connection.execute(
        """UPDATE work_conditions
           SET status = 'cancelled', updated_at_ns = ?
           WHERE condition_id = ? AND owner_agent_id = ? AND status = 'armed'""",
        (timestamp, condition_id, owner_agent_id),
    )
    if changed.rowcount != 1:
        raise WorkKernelRefused("Armed condition not found for this Agent")


def evaluate_conditions(
    connection: sqlite3.Connection,
    company_id: str,
    *,
    now_ns: int,
    observed_assets: dict[str, str] | None = None,
    triggering_actor_id: str | None = None,
) -> tuple[WorkItem, ...]:
    """Evaluate armed conditions and admit deduplicated eligible work items."""
    assets = observed_assets or {}
    rows = connection.execute(
        """SELECT c.condition_id, c.owner_agent_id, c.responsibility_id, c.work_item_id,
                  c.condition_revision, c.trigger_kind, c.due_at_ns, c.source_identity,
                  c.expected_revision, c.expires_at_ns, r.duty_kind, r.status
           FROM work_conditions c
           LEFT JOIN work_responsibilities r ON r.responsibility_id = c.responsibility_id
           WHERE c.company_id = ? AND c.status = 'armed'
           ORDER BY c.created_at_ns, c.condition_id""",
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
        resp_duty_kind,
        resp_status,
    ) in rows:
        if resp_id is not None and resp_status != "active":
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

        eligible = False
        matched_revision = None
        if (
            trigger_kind == "timer"
            and due_at_ns is not None
            and now_ns >= int(due_at_ns)
        ):
            eligible = True
        elif trigger_kind == "shared_asset_revision" and source_identity in assets:
            observed_rev = assets[source_identity]
            rev_entity = f"{source_identity}@{observed_rev}"
            if triggering_actor_id == owner_agent_id:
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
                continue  # anti-self-wake persisted for subsequent actor-less sweeps
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
            if expected_revision is None or observed_rev == expected_revision:
                eligible = True
                matched_revision = observed_rev
        elif trigger_kind == "peer_response" and source_identity:
            req_id = str(source_identity).removeprefix("peer_response:")
            peer_row = connection.execute(
                """SELECT state, recipient_agent_id FROM work_peer_requests
                   WHERE company_id = ? AND request_id = ?""",
                (company_id, req_id),
            ).fetchone()
            if peer_row is not None and str(peer_row[0]) != "open":
                res_state = str(peer_row[0])
                persisted_responder = str(peer_row[1])
                if res_state in ("answered", "declined") and (
                    owner_agent_id == persisted_responder
                    or triggering_actor_id == owner_agent_id
                ):
                    continue  # anti-self-wake against persisted responder
                if expected_revision is None or res_state == expected_revision:
                    eligible = True
                    matched_revision = res_state

        if not eligible:
            continue

        # Deduplicate strictly by (company_id, source_identity, condition_revision)
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
        if already is not None:
            connection.execute(
                "UPDATE work_conditions SET status = 'triggered', updated_at_ns = ? WHERE condition_id = ?",
                (now_ns, cid),
            )
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
        item, _ = create_work_item(
            connection,
            company_id=company_id,
            owner_agent_id=owner_agent_id,
            duty_kind=duty_kind,
            source_kind="condition",
            source_id=cid,
            parent_work_item_id=parent_item_id,
            payload={
                "condition_id": cid,
                "condition_revision": int(cond_rev),
                "responsibility_id": resp_id,
                "trigger_kind": trigger_kind,
                "source_identity": source_identity,
                "observed_revision": matched_revision,
            },
            actor_id=triggering_actor_id or "work_scheduler",
            now_ns=now_ns,
        )
        connection.execute(
            """INSERT INTO work_condition_occurrences (
                   company_id, source_identity, condition_revision,
                   condition_id, work_item_id, admitted_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (company_id, dedupe_source, int(cond_rev), cid, item.work_item_id, now_ns),
        )
        connection.execute(
            "UPDATE work_conditions SET status = 'triggered', updated_at_ns = ? WHERE condition_id = ?",
            (now_ns, cid),
        )
        admitted.append(item)
    return tuple(admitted)


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
    _require_agent(connection, company_id, recipient_agent_id)
    evidence_json = _canonical_json(sorted(evidence_refs))

    existing = connection.execute(
        """SELECT request_id, company_id, requester_work_item_id, requester_run_id,
                  requester_agent_id, recipient_agent_id, response_work_item_id,
                  question, expected_decision, evidence_refs_json, deadline_ns,
                  state, response_json, resolved_at_ns, created_at_ns
           FROM work_peer_requests WHERE request_id = ?""",
        (request_id,),
    ).fetchone()
    if existing is not None:
        prior = PeerRequest(*existing)
        if (
            prior.company_id != company_id
            or prior.requester_work_item_id != requester_work_item_id
            or prior.recipient_agent_id != recipient_agent_id
            or prior.question != question
            or prior.expected_decision != expected_decision
            or prior.evidence_refs_json != evidence_json
            or prior.deadline_ns != deadline_ns
        ):
            raise WorkKernelRefused("Peer request ID reused with conflicting terms")
        return prior, True

    run = assert_run_fencing(connection, requester_run_id, fencing_token)
    if run.work_item_id != requester_work_item_id or run.company_id != company_id:
        raise WorkKernelRefused("Peer request does not match active run identity")
    if run.agent_id == recipient_agent_id:
        raise WorkKernelRefused("An Agent cannot open a peer request to itself")

    timestamp = time_ns() if now_ns is None else now_ns
    if deadline_ns <= timestamp:
        raise WorkKernelRefused("Peer request deadline must be in the future")

    _authorize_peer_route(
        connection,
        company_id=company_id,
        requester_agent_id=run.agent_id,
        recipient_agent_id=recipient_agent_id,
        evidence_refs=evidence_refs,
        check_open_quota=True,
    )

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
                   question, expected_decision, evidence_refs_json, deadline_ns,
                   state, response_json, resolved_at_ns, created_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, ?)""",
            (
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
        ),
        False,
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
    now_ns: int | None = None,
) -> tuple[PeerRequest, bool]:
    """Answer, decline, or expire an open peer request and wake the requester once."""
    if resolution_state not in ("answered", "declined", "expired"):
        raise WorkKernelRefused(
            f"Unsupported peer resolution state: {resolution_state}"
        )
    row = connection.execute(
        """SELECT request_id, company_id, requester_work_item_id, requester_run_id,
                  requester_agent_id, recipient_agent_id, response_work_item_id,
                  question, expected_decision, evidence_refs_json, deadline_ns,
                  state, response_json, resolved_at_ns, created_at_ns
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
        if not response_run_id or response_fencing_token is None:
            raise WorkKernelRefused(
                "Peer response requires an active response_run_id and response_fencing_token"
            )
        resp_run = assert_run_fencing(
            connection, response_run_id, response_fencing_token
        )
        if (
            resp_run.company_id != company_id
            or resp_run.work_item_id != prior.response_work_item_id
            or resp_run.agent_id != prior.recipient_agent_id
        ):
            raise WorkKernelRefused(
                "Response run does not match the named recipient's response duty"
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
            check_open_quota=False,
            check_turn_capacity=False,
        )
    elif timestamp < prior.deadline_ns:
        raise WorkKernelRefused("Peer request has not reached its deadline")

    response_json = _canonical_json(response_payload)
    changed = connection.execute(
        """UPDATE work_peer_requests
           SET state = ?, response_json = ?, resolved_at_ns = ?
           WHERE company_id = ? AND request_id = ? AND state = 'open'""",
        (resolution_state, response_json, timestamp, company_id, request_id),
    )
    if changed.rowcount != 1:
        return prior, False

    if resolution_state == "expired":
        connection.execute(
            """UPDATE work_items
               SET state = 'cancelled', updated_at_ns = ?
               WHERE company_id = ? AND work_item_id = ?
                 AND state IN ('due', 'scheduled')""",
            (timestamp, company_id, prior.response_work_item_id),
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
        ),
        True,
    )
