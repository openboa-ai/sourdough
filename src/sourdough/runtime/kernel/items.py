"""Durable work item creation, cancellation, and legacy duty synchronization."""

import json
import sqlite3
from typing import Any
from uuid import uuid4

from ...storage.kernel import append_audit_event
from .types import (
    _VALID_WORK_ITEM_STATES,
    DUTY_TO_WORK_ITEM_STATE,
    WorkItem,
    WorkKernelRefused,
    _canonical_json,
    _current_time_ns as time_ns,
    _require_agent,
    _require_nonempty,
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
