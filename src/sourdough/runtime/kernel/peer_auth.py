"""Peer route authorization, project evidence scope verification, and shared-publication access."""

import json
import sqlite3
from hashlib import sha256

from ...duty_run import has_turn_capacity
from .types import MAX_PEER_EVIDENCE_REFS, WorkKernelRefused, has_active_grant


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
