"""Blocking peer requests, read/answer/resume handshakes, and delegation cycle checks."""

import json
import sqlite3
from collections import deque
from typing import Any

from ...duty_run import employment_cutoff
from ...storage.kernel import append_audit_event
from .items import create_work_item, sync_duty_terminal
from .peer_auth import _authorize_peer_route
from .runs import assert_run_fencing, wake_waiting_runs, yield_run_to_wait
from .types import (
    _PEER_REQUEST_COLUMNS,
    MAX_PEER_CHAIN_DEPTH,
    PAIR_WINDOW_NS,
    PAIRWISE_RATE_LIMIT_COUNT,
    PeerRequest,
    RunFenced,
    WorkKernelRefused,
    WorkRun,
    _canonical_json,
    _current_time_ns as time_ns,
    _require_agent,
    _require_nonempty,
    has_active_grant,
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
