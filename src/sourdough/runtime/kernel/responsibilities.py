"""Standing work responsibilities, project peer policies, and discovery hysteresis state."""

import json
import sqlite3
from collections.abc import Sequence
from hashlib import sha256
from typing import Any
from uuid import uuid4

from .types import (
    DEFAULT_DISCOVERY_WAKE_BUDGET,
    DEFAULT_DISCOVERY_WAKE_WINDOW_NS,
    MAX_DISCOVERY_COOLDOWN_NS,
    WorkDiscoveryState,
    WorkKernelRefused,
    WorkResponsibility,
    _canonical_json,
    _current_time_ns as time_ns,
    _require_agent,
    _require_nonempty,
)


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
