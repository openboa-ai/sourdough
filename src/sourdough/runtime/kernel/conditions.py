"""Work conditions, event cursor draining, next-condition normalization, and condition evaluation."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import uuid4

from ...duty_run import employment_cutoff
from ...storage.kernel import append_audit_event
from .items import create_work_item
from .peer_auth import _is_authorized_for_shared_publication
from .responsibilities import (
    _parse_scope_policy,
    _read_discovery_window,
    _upsert_discovery_state,
)
from .types import (
    _NEXT_CONDITION_ALLOWED_KEYS,
    DEFAULT_DISCOVERY_WAKE_BUDGET,
    DEFAULT_DISCOVERY_WAKE_WINDOW_NS,
    MAX_DISCOVERY_COOLDOWN_NS,
    _current_time_ns as time_ns,
    WorkCondition,
    WorkEventBatch,
    WorkItem,
    WorkKernelRefused,
    _canonical_json,
    _require_agent,
    _require_nonempty,
)


@dataclass(frozen=True)
class _ArmedConditionRow:
    condition_id: str
    owner_agent_id: str
    responsibility_id: str | None
    work_item_id: str | None
    condition_revision: int
    trigger_kind: str
    due_at_ns: int | None
    source_identity: str | None
    expected_revision: str | None
    expires_at_ns: int | None
    cooldown_ns: int
    max_fires: int | None
    fire_count: int
    updated_at_ns: int
    resp_duty_kind: str | None
    resp_status: str | None
    resp_scope_json: str | None


@dataclass(frozen=True)
class _TriggerMatch:
    eligible: bool
    matched_revision: str | None = None
    resolved_author: str | None = None


@dataclass(frozen=True)
class _DiscoveryGateState:
    consecutive_noops: int = 0
    cooldown_until_ns: int = 0
    window_started_ns: int = 0
    window_wakes: int = 0
    configured_cooldown_ns: int = 0


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


def _prune_ineligible_condition(
    connection: sqlite3.Connection,
    company_id: str,
    cond: _ArmedConditionRow,
    *,
    now_ns: int,
) -> bool:
    """Cancel, expire, or retire conditions that are no longer eligible; return True if pruned."""
    if cond.responsibility_id is not None and cond.resp_status != "active":
        connection.execute(
            "UPDATE work_conditions SET status = 'cancelled', updated_at_ns = ? WHERE condition_id = ?",
            (now_ns, cond.condition_id),
        )
        return True
    if cond.work_item_id is not None:
        p_state_row = connection.execute(
            "SELECT state FROM work_items WHERE company_id = ? AND work_item_id = ?",
            (company_id, cond.work_item_id),
        ).fetchone()
        if p_state_row is not None and str(p_state_row[0]) == "cancelled":
            connection.execute(
                "UPDATE work_conditions SET status = 'cancelled', updated_at_ns = ? WHERE condition_id = ?",
                (now_ns, cond.condition_id),
            )
            return True
    if employment_cutoff(connection, cond.owner_agent_id) is not None:
        connection.execute(
            "UPDATE work_conditions SET status = 'cancelled', updated_at_ns = ? WHERE condition_id = ?",
            (now_ns, cond.condition_id),
        )
        return True
    if cond.expires_at_ns is not None and now_ns >= int(cond.expires_at_ns):
        connection.execute(
            "UPDATE work_conditions SET status = 'expired', updated_at_ns = ? WHERE condition_id = ?",
            (now_ns, cond.condition_id),
        )
        return True
    if cond.max_fires is not None and cond.fire_count >= cond.max_fires:
        connection.execute(
            """UPDATE work_conditions
               SET status = 'triggered', updated_at_ns = ?
               WHERE condition_id = ? AND condition_revision = ? AND status = 'armed'""",
            (now_ns, cond.condition_id, cond.condition_revision),
        )
        return True
    return False


def _eval_timer_condition(cond: _ArmedConditionRow, *, now_ns: int) -> _TriggerMatch:
    if cond.due_at_ns is not None and now_ns >= int(cond.due_at_ns):
        return _TriggerMatch(eligible=True)
    return _TriggerMatch(eligible=False)


def _eval_result_continuation_condition(
    connection: sqlite3.Connection,
    company_id: str,
    cond: _ArmedConditionRow,
    *,
    now_ns: int,
) -> _TriggerMatch:
    if cond.due_at_ns is None or now_ns < int(cond.due_at_ns):
        return _TriggerMatch(eligible=False)
    dep_id = str(cond.expected_revision or "").strip()
    if dep_id and _is_result_committed(connection, company_id, dep_id, now_ns=now_ns):
        return _TriggerMatch(eligible=True, matched_revision=dep_id)
    return _TriggerMatch(eligible=False)


def _eval_shared_publication_condition(
    connection: sqlite3.Connection,
    company_id: str,
    cond: _ArmedConditionRow,
    *,
    now_ns: int,
    assets: Mapping[str, str],
    cursor_batch: WorkEventBatch,
    triggering_actor_id: str | None,
) -> _TriggerMatch:
    source_identity = cond.source_identity
    if not source_identity:
        return _TriggerMatch(eligible=False)
    if (
        _is_authorized_for_shared_publication(
            connection, company_id, cond.owner_agent_id, source_identity
        )
        is False
    ):
        connection.execute(
            "UPDATE work_conditions SET status = 'cancelled', updated_at_ns = ? WHERE condition_id = ?",
            (now_ns, cond.condition_id),
        )
        return _TriggerMatch(eligible=False)
    pub_row = connection.execute(
        """SELECT author_id, content FROM shared_publication
           WHERE company_id = ? AND asset_id = ?""",
        (company_id, source_identity),
    ).fetchone()
    observed_rev: str | None = assets.get(source_identity)
    if observed_rev is None and pub_row is not None:
        observed_rev = sha256(str(pub_row[1]).encode("utf-8")).hexdigest()
    if observed_rev is None:
        return _TriggerMatch(eligible=False)

    resolved_author: str | None = None
    if pub_row is not None and str(pub_row[0]).strip():
        resolved_author = str(pub_row[0]).strip()
    elif source_identity in cursor_batch.asset_authors:
        resolved_author = cursor_batch.asset_authors[source_identity]
    elif isinstance(triggering_actor_id, str) and triggering_actor_id.strip():
        resolved_author = triggering_actor_id.strip()

    rev_entity = f"{source_identity}@{observed_rev}"
    if resolved_author == cond.owner_agent_id:
        recorded = connection.execute(
            """SELECT 1 FROM audit_event
               WHERE company_id = ? AND domain = 'work'
                 AND entity_type = 'shared_asset_revision'
                 AND entity_id = ? AND actor_id = ?
                 AND event_type = 'work.self_authored_revision_suppressed'
               LIMIT 1""",
            (company_id, rev_entity, cond.owner_agent_id),
        ).fetchone()
        if recorded is None:
            append_audit_event(
                connection,
                company_id=company_id,
                domain="work",
                entity_type="shared_asset_revision",
                entity_id=rev_entity,
                event_type="work.self_authored_revision_suppressed",
                actor_id=cond.owner_agent_id,
                payload={
                    "source_identity": source_identity,
                    "revision": observed_rev,
                    "condition_id": cond.condition_id,
                },
                recorded_at_ns=now_ns,
            )
        return _TriggerMatch(eligible=False)
    self_authored = connection.execute(
        """SELECT 1 FROM audit_event
           WHERE company_id = ? AND domain = 'work'
             AND entity_type = 'shared_asset_revision'
             AND entity_id = ? AND actor_id = ?
             AND event_type = 'work.self_authored_revision_suppressed'
           LIMIT 1""",
        (company_id, rev_entity, cond.owner_agent_id),
    ).fetchone()
    if self_authored is not None:
        return _TriggerMatch(eligible=False)
    if not resolved_author:
        return _TriggerMatch(
            eligible=False
        )  # fail closed: asset revision wakes require non-self producer identity
    if cond.expected_revision is None or observed_rev == cond.expected_revision:
        return _TriggerMatch(
            eligible=True,
            matched_revision=observed_rev,
            resolved_author=resolved_author,
        )
    return _TriggerMatch(eligible=False)


def _eval_peer_response_condition(
    connection: sqlite3.Connection,
    company_id: str,
    cond: _ArmedConditionRow,
    *,
    triggering_actor_id: str | None,
) -> _TriggerMatch:
    if not cond.source_identity:
        return _TriggerMatch(eligible=False)
    req_id = str(cond.source_identity).removeprefix("peer_response:")
    peer_row = connection.execute(
        """SELECT state, requester_agent_id, recipient_agent_id FROM work_peer_requests
           WHERE company_id = ? AND request_id = ?""",
        (company_id, req_id),
    ).fetchone()
    if peer_row is None or str(peer_row[0]) == "open":
        return _TriggerMatch(eligible=False)
    res_state = str(peer_row[0])
    persisted_requester = str(peer_row[1])
    persisted_responder = str(peer_row[2])
    if cond.owner_agent_id != persisted_requester:
        return _TriggerMatch(
            eligible=False
        )  # only the originating requester may wake on peer_response
    if res_state in ("answered", "declined") and (
        cond.owner_agent_id == persisted_responder
        or triggering_actor_id == cond.owner_agent_id
    ):
        return _TriggerMatch(
            eligible=False
        )  # anti-self-wake against persisted responder
    if cond.expected_revision is None or res_state == cond.expected_revision:
        return _TriggerMatch(
            eligible=True,
            matched_revision=res_state,
            resolved_author=(
                "work_scheduler" if res_state == "expired" else persisted_responder
            ),
        )
    return _TriggerMatch(eligible=False)


def _evaluate_trigger(
    connection: sqlite3.Connection,
    company_id: str,
    cond: _ArmedConditionRow,
    *,
    now_ns: int,
    assets: Mapping[str, str],
    cursor_batch: WorkEventBatch,
    triggering_actor_id: str | None,
) -> _TriggerMatch:
    if cond.trigger_kind == "timer":
        if isinstance(cond.source_identity, str) and cond.source_identity.startswith(
            "result_dep:"
        ):
            return _eval_result_continuation_condition(
                connection, company_id, cond, now_ns=now_ns
            )
        return _eval_timer_condition(cond, now_ns=now_ns)
    if cond.trigger_kind == "shared_asset_revision":
        return _eval_shared_publication_condition(
            connection,
            company_id,
            cond,
            now_ns=now_ns,
            assets=assets,
            cursor_batch=cursor_batch,
            triggering_actor_id=triggering_actor_id,
        )
    if cond.trigger_kind == "peer_response":
        return _eval_peer_response_condition(
            connection,
            company_id,
            cond,
            triggering_actor_id=triggering_actor_id,
        )
    return _TriggerMatch(eligible=False)


def _condition_dedupe_source(
    cond: _ArmedConditionRow, matched_revision: str | None
) -> str:
    """Compute canonical dedupe source_identity for work_condition_occurrences."""
    if cond.trigger_kind == "shared_asset_revision":
        return (
            f"{cond.condition_id}:{cond.source_identity}@{matched_revision}"
            if matched_revision is not None
            else f"{cond.condition_id}:{cond.source_identity}"
        )
    if cond.trigger_kind == "peer_response":
        peer_target = str(cond.source_identity or "").removeprefix("peer_response:")
        return (
            f"peer_response:{cond.owner_agent_id}:{peer_target}@{matched_revision}"
            if matched_revision is not None
            else f"peer_response:{cond.owner_agent_id}:{peer_target}"
        )
    if (
        cond.trigger_kind == "timer"
        and isinstance(cond.source_identity, str)
        and cond.source_identity.startswith("result_dep:")
        and matched_revision is not None
    ):
        return f"result_dep:{cond.owner_agent_id}:{matched_revision}"
    return (
        f"{cond.source_identity}@{matched_revision}"
        if matched_revision is not None
        else str(cond.source_identity)
    )


def _unscoped_discretionary_budget_exhausted(
    connection: sqlite3.Connection,
    company_id: str,
    owner_agent_id: str,
    duty_kind: str,
    *,
    now_ns: int,
) -> bool:
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
    return recent_unscoped >= DEFAULT_DISCOVERY_WAKE_BUDGET


def _coalesce_onto_open_item(
    connection: sqlite3.Connection,
    company_id: str,
    cond: _ArmedConditionRow,
    *,
    open_row: tuple[Any, Any, Any],
    dedupe_source: str,
    matched_revision: str | None,
    is_multi_fire: bool,
    responsibility_id: str | None,
    now_ns: int,
) -> None:
    open_item_id, open_state, open_payload_raw = open_row
    if str(open_state) not in ("due", "scheduled"):
        return
    try:
        open_payload = json.loads(str(open_payload_raw))
    except json.JSONDecodeError:
        open_payload = {}
    if not isinstance(open_payload, dict):
        open_payload = {}
    open_payload = _coalesce_open_condition_payload(
        open_payload,
        condition_id=cond.condition_id,
        condition_revision=cond.condition_revision,
        trigger_kind=cond.trigger_kind,
        source_identity=cond.source_identity,
        observed_revision=matched_revision,
        responsibility_id=responsibility_id,
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
            cond.condition_revision,
            cond.condition_id,
            str(open_item_id),
            now_ns,
        ),
    )
    if not is_multi_fire:
        connection.execute(
            """UPDATE work_conditions
               SET status = 'triggered', fire_count = fire_count + 1, updated_at_ns = ?
               WHERE condition_id = ? AND condition_revision = ? AND status = 'armed'""",
            (now_ns, cond.condition_id, cond.condition_revision),
        )


def _check_discretionary_gate(
    connection: sqlite3.Connection,
    company_id: str,
    cond: _ArmedConditionRow,
    *,
    duty_kind: str,
    dedupe_source: str,
    matched_revision: str | None,
    is_multi_fire: bool,
    is_result_dep_discovery: bool,
    is_unscoped_discretionary: bool,
    is_discretionary: bool,
    now_ns: int,
) -> tuple[bool, _DiscoveryGateState]:
    """Enforce discretionary wake budgets, cooldowns, and open-item coalescing."""
    disc_state = _DiscoveryGateState(window_started_ns=now_ns)
    if is_result_dep_discovery:
        if cond.responsibility_id is not None:
            wake_budget, wake_window_ns, configured_cooldown_ns = _parse_scope_policy(
                cond.resp_scope_json
            )
            noops, cooldown_until, window_started, window_wakes = (
                _read_discovery_window(
                    connection,
                    company_id,
                    cond.responsibility_id,
                    now_ns=now_ns,
                    wake_window_ns=wake_window_ns,
                )
            )
            disc_state = _DiscoveryGateState(
                consecutive_noops=noops,
                cooldown_until_ns=cooldown_until,
                window_started_ns=window_started,
                window_wakes=window_wakes,
                configured_cooldown_ns=configured_cooldown_ns,
            )
            if window_wakes >= wake_budget:
                return False, disc_state
        elif _unscoped_discretionary_budget_exhausted(
            connection, company_id, cond.owner_agent_id, duty_kind, now_ns=now_ns
        ):
            return False, disc_state

    if is_unscoped_discretionary:
        if _unscoped_discretionary_budget_exhausted(
            connection, company_id, cond.owner_agent_id, duty_kind, now_ns=now_ns
        ):
            return False, disc_state
        open_unscoped = connection.execute(
            """SELECT work_item_id, state, payload_json
               FROM work_items
               WHERE company_id = ? AND owner_agent_id = ?
                 AND duty_kind = ?
                 AND state IN ('due', 'scheduled', 'running')
                 AND json_extract(payload_json, '$.responsibility_id') IS NULL
               ORDER BY created_at_ns ASC, work_item_id ASC
               LIMIT 1""",
            (company_id, cond.owner_agent_id, duty_kind),
        ).fetchone()
        if open_unscoped is not None:
            _coalesce_onto_open_item(
                connection,
                company_id,
                cond,
                open_row=open_unscoped,
                dedupe_source=dedupe_source,
                matched_revision=matched_revision,
                is_multi_fire=is_multi_fire,
                responsibility_id=None,
                now_ns=now_ns,
            )
            return False, disc_state

    if is_discretionary and cond.responsibility_id is not None:
        wake_budget, wake_window_ns, configured_cooldown_ns = _parse_scope_policy(
            cond.resp_scope_json
        )
        noops, cooldown_until, window_started, window_wakes = _read_discovery_window(
            connection,
            company_id,
            cond.responsibility_id,
            now_ns=now_ns,
            wake_window_ns=wake_window_ns,
        )
        disc_state = _DiscoveryGateState(
            consecutive_noops=noops,
            cooldown_until_ns=cooldown_until,
            window_started_ns=window_started,
            window_wakes=window_wakes,
            configured_cooldown_ns=configured_cooldown_ns,
        )
        if now_ns < cooldown_until or window_wakes >= wake_budget:
            return False, disc_state
        open_row = connection.execute(
            """SELECT work_item_id, state, payload_json
               FROM work_items
               WHERE company_id = ? AND owner_agent_id = ?
                 AND duty_kind = ?
                 AND state IN ('due', 'scheduled', 'running')
                 AND json_extract(payload_json, '$.responsibility_id') = ?
               ORDER BY created_at_ns ASC, work_item_id ASC
               LIMIT 1""",
            (company_id, cond.owner_agent_id, duty_kind, cond.responsibility_id),
        ).fetchone()
        if open_row is not None:
            _coalesce_onto_open_item(
                connection,
                company_id,
                cond,
                open_row=open_row,
                dedupe_source=dedupe_source,
                matched_revision=matched_revision,
                is_multi_fire=is_multi_fire,
                responsibility_id=cond.responsibility_id,
                now_ns=now_ns,
            )
            return False, disc_state

    return True, disc_state


def _admit_condition_work_item(
    connection: sqlite3.Connection,
    company_id: str,
    cond: _ArmedConditionRow,
    *,
    duty_kind: str,
    effective_parent_id: str | None,
    dedupe_source: str,
    match: _TriggerMatch,
    triggering_actor_id: str | None,
    update_discovery_state: bool,
    disc_state: _DiscoveryGateState,
    now_ns: int,
) -> WorkItem:
    item, _ = create_work_item(
        connection,
        company_id=company_id,
        owner_agent_id=cond.owner_agent_id,
        duty_kind=duty_kind,
        source_kind="condition",
        source_id=cond.condition_id,
        parent_work_item_id=effective_parent_id,
        payload={
            "condition_id": cond.condition_id,
            "condition_revision": cond.condition_revision,
            "responsibility_id": cond.responsibility_id,
            "trigger_kind": cond.trigger_kind,
            "source_identity": cond.source_identity,
            "observed_revision": match.matched_revision,
        },
        actor_id=match.resolved_author or triggering_actor_id or "work_scheduler",
        now_ns=now_ns,
    )
    connection.execute(
        """INSERT INTO work_condition_occurrences (
               company_id, source_identity, condition_revision,
               condition_id, work_item_id, admitted_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            company_id,
            dedupe_source,
            cond.condition_revision,
            cond.condition_id,
            item.work_item_id,
            now_ns,
        ),
    )
    new_fire_count = cond.fire_count + 1
    keep_armed = cond.trigger_kind == "shared_asset_revision" and (
        (cond.max_fires is not None and new_fire_count < cond.max_fires)
        or (cond.max_fires is None and cond.cooldown_ns > 0)
    )
    new_status = "armed" if keep_armed else "triggered"
    connection.execute(
        """UPDATE work_conditions
           SET status = ?, fire_count = ?, updated_at_ns = ?
           WHERE condition_id = ? AND condition_revision = ? AND status = 'armed'""",
        (
            new_status,
            new_fire_count,
            now_ns,
            cond.condition_id,
            cond.condition_revision,
        ),
    )
    if update_discovery_state and cond.responsibility_id is not None:
        eff_cooldown = max(cond.cooldown_ns, disc_state.configured_cooldown_ns)
        if eff_cooldown > 0:
            step_cooldown = min(
                eff_cooldown * (2 ** min(disc_state.consecutive_noops, 6)),
                MAX_DISCOVERY_COOLDOWN_NS,
            )
            new_cooldown_until = max(
                disc_state.cooldown_until_ns, now_ns + step_cooldown
            )
        else:
            new_cooldown_until = disc_state.cooldown_until_ns
        _upsert_discovery_state(
            connection,
            company_id=company_id,
            responsibility_id=cond.responsibility_id,
            consecutive_noops=disc_state.consecutive_noops,
            cooldown_until_ns=new_cooldown_until,
            window_started_ns=disc_state.window_started_ns,
            window_wakes=disc_state.window_wakes + 1,
            now_ns=now_ns,
        )
    return item


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
    for raw in rows:
        cond = _ArmedConditionRow(
            condition_id=raw[0],
            owner_agent_id=raw[1],
            responsibility_id=raw[2],
            work_item_id=raw[3],
            condition_revision=int(raw[4]),
            trigger_kind=raw[5],
            due_at_ns=raw[6],
            source_identity=raw[7],
            expected_revision=raw[8],
            expires_at_ns=raw[9],
            cooldown_ns=int(raw[10] or 0),
            max_fires=None if raw[11] is None else int(raw[11]),
            fire_count=int(raw[12] or 0),
            updated_at_ns=int(raw[13]),
            resp_duty_kind=raw[14],
            resp_status=raw[15],
            resp_scope_json=raw[16],
        )
        if _prune_ineligible_condition(connection, company_id, cond, now_ns=now_ns):
            continue

        match = _evaluate_trigger(
            connection,
            company_id,
            cond,
            now_ns=now_ns,
            assets=assets,
            cursor_batch=cursor_batch,
            triggering_actor_id=triggering_actor_id,
        )
        if not match.eligible:
            continue

        # Deduplicate strictly by (company_id, source_identity, condition_revision).
        # Scope shared_asset_revision by condition_id so multiple watchers never collide;
        # scope peer_response and result_dep per (owner_agent_id, target) to prevent
        # duplicate-condition wake amplification on already-resolved records.
        dedupe_source = _condition_dedupe_source(cond, match.matched_revision)
        already = connection.execute(
            """SELECT 1 FROM work_condition_occurrences
               WHERE company_id = ? AND source_identity = ? AND condition_revision = ?""",
            (company_id, dedupe_source, cond.condition_revision),
        ).fetchone()
        is_multi_fire = cond.trigger_kind == "shared_asset_revision" and (
            (cond.max_fires is not None and cond.max_fires > 1)
            or (cond.max_fires is None and cond.cooldown_ns > 0)
        )
        if already is not None:
            if not is_multi_fire:
                connection.execute(
                    """UPDATE work_conditions
                       SET status = 'triggered', updated_at_ns = ?
                       WHERE condition_id = ? AND condition_revision = ?""",
                    (now_ns, cond.condition_id, cond.condition_revision),
                )
            continue

        if (
            cond.trigger_kind == "shared_asset_revision"
            and cond.fire_count > 0
            and cond.cooldown_ns > 0
            and now_ns < cond.updated_at_ns + cond.cooldown_ns
        ):
            continue

        duty_kind = cond.resp_duty_kind
        if not duty_kind and cond.work_item_id is not None:
            parent_kind = connection.execute(
                "SELECT duty_kind FROM work_items WHERE work_item_id = ?",
                (cond.work_item_id,),
            ).fetchone()
            if parent_kind is not None:
                duty_kind = str(parent_kind[0])
        duty_kind = duty_kind or "work.discovery_review"

        is_result_dep = isinstance(
            cond.source_identity, str
        ) and cond.source_identity.startswith("result_dep:")
        effective_parent_id = (
            None
            if duty_kind in ("work.discovery_review", "work.dreaming")
            and not is_result_dep
            and cond.trigger_kind != "peer_response"
            else cond.work_item_id
        )
        is_discretionary = (
            cond.responsibility_id is not None
            and effective_parent_id is None
            and cond.trigger_kind != "peer_response"
            and duty_kind in ("work.discovery_review", "work.dreaming")
            and not is_result_dep
        )
        is_unscoped_discretionary = (
            cond.responsibility_id is None
            and effective_parent_id is None
            and cond.trigger_kind != "peer_response"
            and duty_kind in ("work.discovery_review", "work.dreaming")
            and not is_result_dep
        )
        is_result_dep_discovery = is_result_dep and duty_kind in (
            "work.discovery_review",
            "work.dreaming",
        )

        allowed, disc_state = _check_discretionary_gate(
            connection,
            company_id,
            cond,
            duty_kind=duty_kind,
            dedupe_source=dedupe_source,
            matched_revision=match.matched_revision,
            is_multi_fire=is_multi_fire,
            is_result_dep_discovery=is_result_dep_discovery,
            is_unscoped_discretionary=is_unscoped_discretionary,
            is_discretionary=is_discretionary,
            now_ns=now_ns,
        )
        if not allowed:
            continue

        item = _admit_condition_work_item(
            connection,
            company_id,
            cond,
            duty_kind=duty_kind,
            effective_parent_id=effective_parent_id,
            dedupe_source=dedupe_source,
            match=match,
            triggering_actor_id=triggering_actor_id,
            update_discovery_state=(
                is_discretionary
                or (is_result_dep_discovery and cond.responsibility_id is not None)
            ),
            disc_state=disc_state,
            now_ns=now_ns,
        )
        admitted.append(item)
    return tuple(admitted)
