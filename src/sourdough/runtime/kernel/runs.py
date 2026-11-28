"""Fenced work run lifecycle: claim, lease fencing, durable waits, completion, failure, and retirement."""

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...duty_run import employment_cutoff, has_turn_capacity
from ...storage.kernel import append_audit_event
from .conditions import _arm_next_condition, validate_next_condition_dict
from .items import sync_duty_terminal
from .peer_auth import _authorize_peer_route
from .types import (
    RunFenced,
    WorkCondition,
    WorkKernelRefused,
    WorkRun,
    _canonical_json,
    _current_time_ns as time_ns,
    _require_nonempty,
    has_active_grant,
)


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
                from ...agent_desk import cleanup_run_scratch

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
