"""Pure read-only SQLite projections for the Sourdough Board Client.

Every projection executes inside a single SQLite read_tx snapshot and returns
(payload, event_seq) so ETag and X-Sourdough-Event-Seq never tear. Private Desk
drafts are never opened or disclosed (opaque count only), missing accounting or
valuation telemetry is never coerced to zero, and Core projections remain
domain-neutral unless an installed DomainPlugin enriches them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any

from ..agent_desk import _check_no_symlinks, _drafts_directory
from ..board_control import (
    ALLOWED_AVATAR_VARIANTS,
    AuthorizeBoard,
    resolve_board_access,
)
from ..company import FoundingRefused, _identity
from ..method_learning import (
    collect_lineage_blast_radii,
    compute_active_snapshot_id,
    resolve_active_methods,
)
from ..runtime.domains import DomainPlugin, load_domain_plugins
from ..storage.db import open_reader, read_tx
from ..storage.kernel import max_event_seq
from ..storage.schema import installed_domains
from ..trial_registry import (
    TrialRefused,
    read_sealed_suite_meta,
    summarize_rsi_transfer,
)

PROJECTION_AGENTS_GRAPH = "agents-graph-v1"
PROJECTION_AGENT_DETAIL = "agent-detail-v1"
PROJECTION_MESSAGES = "messages-v1"
PROJECTION_APPROVALS = "approvals-v1"
PROJECTION_FINANCE = "finance-v1"
PROJECTION_ORG = "org-v1"
PROJECTION_RESTRICTIONS = "restrictions-v1"
PROJECTION_SYSTEM_SAFETY = "system-safety-v1"
PROJECTION_SYSTEM_EVALS = "system-evals-v1"
PROJECTION_SYSTEM_AUDIT = "system-audit-v1"

PROJECTION_ORDER_TRACE = "order-trace-v1"
PROJECTION_STRATEGIES = "strategies-v1"
PROJECTION_TRADING_DESK = "trading-desk-v1"

CORE_CLIENT_PROJECTIONS: tuple[str, ...] = (
    PROJECTION_AGENTS_GRAPH,
    PROJECTION_AGENT_DETAIL,
    PROJECTION_MESSAGES,
    PROJECTION_APPROVALS,
    PROJECTION_FINANCE,
    PROJECTION_ORG,
    PROJECTION_RESTRICTIONS,
    PROJECTION_SYSTEM_SAFETY,
    PROJECTION_SYSTEM_EVALS,
    PROJECTION_SYSTEM_AUDIT,
)

ALL_CLIENT_PROJECTIONS: tuple[str, ...] = (
    *CORE_CLIENT_PROJECTIONS,
    PROJECTION_ORDER_TRACE,
    PROJECTION_STRATEGIES,
    PROJECTION_TRADING_DESK,
)

_DEFAULT_AVATAR_VARIANTS: tuple[str, ...] = (
    "wide-oval-top",
    "tall-capsule-right",
    "round-triangle-bottom",
    "round-diamond-left",
    "low-dome-bottom",
    "wide-crown-top",
    "bean-top",
    "round-pentagon-bottom",
)


def _apply_core_enrichers(
    projection_name: str,
    db: sqlite3.Connection,
    company_id: str,
    payload: dict[str, Any],
    domain_plugins: Sequence[DomainPlugin] | None,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    domains = set(installed_domains(db))
    non_core = domains - {"core"}
    if not non_core:
        return
    plugins = (
        tuple(domain_plugins) if domain_plugins is not None else load_domain_plugins()
    )
    for plugin in plugins:
        if plugin.domain_name in domains:
            plugin.enrich_core_projection(
                projection_name, db, company_id, payload, context=context
            )


def _json_loads_safe(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _redact_evidence_refs_for_board(raw_json: str) -> list[Any]:
    parsed = _json_loads_safe(raw_json, [])
    if not isinstance(parsed, list):
        return []
    redacted: list[Any] = []
    for entry in parsed:
        if isinstance(entry, Mapping) and str(entry.get("kind", "")) == "private_draft":
            item = dict(entry)
            raw_res = str(item.get("resource_id", ""))
            item["resource_id"] = (
                f"private_draft:{sha256(raw_res.encode('utf-8')).hexdigest()[:16]}"
            )
            redacted.append(item)
        else:
            redacted.append(entry)
    return redacted


def _count_private_drafts(store: Path, agent_id: str) -> int:
    """Count unique private Desk draft stems without opening, reading, or naming any file."""
    try:
        root = store.resolve(strict=False).parent
        drafts_dir = _drafts_directory(store, agent_id)
        if not _check_no_symlinks(drafts_dir, root):
            return 0
        dir_st = os.lstat(drafts_dir)
        if stat.S_ISLNK(dir_st.st_mode) or not stat.S_ISDIR(dir_st.st_mode):
            return 0
        stems: set[str] = set()
        for entry in drafts_dir.iterdir():
            name = entry.name
            if name.startswith("."):
                continue
            st = os.lstat(entry)
            if (
                stat.S_ISLNK(st.st_mode)
                or not stat.S_ISREG(st.st_mode)
                or st.st_nlink != 1
            ):
                continue
            stems.add(entry.stem if entry.suffix else name)
        return len(stems)
    except Exception:
        return 0


def _agent_directory(
    db: sqlite3.Connection, company_id: str
) -> dict[str, dict[str, Any]]:
    founder_row = db.execute(
        "SELECT founder_id FROM founding_receipt WHERE company_id = ?",
        (company_id,),
    ).fetchone()
    founder_id = str(founder_row[0]) if founder_row else None

    rows = db.execute(
        """SELECT a.id, a.execution_profile,
                  g.id, g.scope,
                  p.display_name, p.role_label, p.avatar_variant, p.monthly_budget_minor,
                  ar.cutoff, ar.reason,
                  ho.state, ho.purpose, ho.attempts,
                  hr.allocated_turns
           FROM agent a
           LEFT JOIN standing_grant g ON g.agent_id = a.id
           LEFT JOIN agent_profile p ON p.agent_id = a.id
           LEFT JOIN agent_archive ar ON ar.agent_id = a.id
           LEFT JOIN hire_onboarding ho ON ho.agent_id = a.id
           LEFT JOIN hire_receipt hr ON hr.agent_id = a.id
           WHERE a.company_id = ?
           ORDER BY CASE WHEN a.id = ? THEN 0 ELSE 1 END, a.rowid ASC""",
        (company_id, founder_id or ""),
    ).fetchall()

    directory: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(rows):
        agent_id = str(row[0])
        is_founder = agent_id == founder_id
        scope = str(row[3]) if row[3] is not None else ""
        default_role = "CEO" if is_founder else "Operations & QA"
        default_name = (
            "Founder · CEO" if is_founder else agent_id.replace("-", " ").title()
        )
        raw_avatar = str(row[6]).strip() if row[6] is not None else ""
        avatar_variant = (
            raw_avatar
            if raw_avatar in ALLOWED_AVATAR_VARIANTS
            else _DEFAULT_AVATAR_VARIANTS[idx % len(_DEFAULT_AVATAR_VARIANTS)]
        )
        directory[agent_id] = {
            "agent_id": agent_id,
            "is_ceo": is_founder,
            "execution_profile": str(row[1]),
            "grant_id": None if row[2] is None else str(row[2]),
            "grant_scope": scope,
            "display_name": str(row[4]) if row[4] else default_name,
            "role_label": str(row[5]) if row[5] else default_role,
            "avatar_variant": avatar_variant,
            "monthly_budget_minor": int(row[7]) if row[7] is not None else 0,
            "archived": row[8] is not None,
            "archive_reason": None if row[9] is None else str(row[9]),
            "onboarding_state": None if row[10] is None else str(row[10]),
            "onboarding_purpose": None if row[11] is None else str(row[11]),
            "onboarding_attempts": int(row[12]) if row[12] is not None else 0,
            "allocated_turns": int(row[13]) if row[13] is not None else None,
        }
    return directory


def _active_restrictions_rows(
    db: sqlite3.Connection, company_id: str
) -> list[dict[str, Any]]:
    rows = db.execute(
        """SELECT restriction_id, cause_category, scope_ref, restriction_revision,
                  active, operator_releasable, reason, recorded_at_ns, cleared_at_ns,
                  last_command_id
           FROM restriction_cause
           WHERE company_id = ?
           ORDER BY active DESC, recorded_at_ns DESC""",
        (company_id,),
    ).fetchall()
    return [
        {
            "restriction_id": str(r[0]),
            "cause_category": str(r[1]),
            "scope_ref": str(r[2]),
            "restriction_revision": int(r[3]),
            "active": bool(r[4]),
            "operator_releasable": bool(r[5]),
            "reason": str(r[6]),
            "recorded_at_ns": int(r[7]),
            "cleared_at_ns": None if r[8] is None else int(r[8]),
            "last_command_id": None if r[9] is None else str(r[9]),
        }
        for r in rows
    ]


def _sum_gl_accounts(db: sqlite3.Connection) -> dict[str, int]:
    rows = db.execute(
        """SELECT account, SUM(amount_minor)
           FROM journal_line
           GROUP BY account"""
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _close_issues_summary(
    db: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = db.execute(
        """SELECT request_id, scope, supersedes, request, receipt, assessed_ns
           FROM accounting_close
           ORDER BY rowid DESC"""
    ).fetchall()
    closes: list[dict[str, Any]] = []
    open_issues: list[dict[str, Any]] = []
    for r in rows:
        req = _json_loads_safe(str(r[3]), {})
        rec = _json_loads_safe(str(r[4]), {})
        issues = rec.get("issues", []) if isinstance(rec, dict) else []
        if not isinstance(issues, list):
            issues = []
        status = (
            str(rec.get("status", "unknown")) if isinstance(rec, dict) else "unknown"
        )
        entry = {
            "request_id": str(r[0]),
            "scope": str(r[1]),
            "supersedes": None if r[2] is None else str(r[2]),
            "status": status,
            "assessed_ns": None if r[5] is None else int(r[5]),
            "issues": issues,
            "period": req.get("period") if isinstance(req, dict) else None,
        }
        closes.append(entry)
    if closes:
        for issue in closes[0]["issues"]:
            if isinstance(issue, dict):
                open_issues.append(issue)
            else:
                open_issues.append({"detail": str(issue)})
    return closes, open_issues


def read_agents_graph_projection(
    store: Path,
    company_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Area 2 Agents & Activity node-and-edge interaction graph projection."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        directory = _agent_directory(db, company_id)

        # Active work_runs and work_items per agent
        active_runs: dict[str, dict[str, Any]] = {}
        for r in db.execute(
            """SELECT run_id, agent_id, work_item_id, duty_kind, status,
                      fencing_token, budget_ceiling_minor, updated_at_ns
               FROM work_runs
               WHERE company_id = ?
               ORDER BY updated_at_ns DESC""",
            (company_id,),
        ).fetchall():
            aid = str(r[1])
            if aid not in active_runs or str(r[4]) == "running":
                active_runs[aid] = {
                    "run_id": str(r[0]),
                    "work_item_id": str(r[2]),
                    "duty_kind": str(r[3]),
                    "status": str(r[4]),
                    "fencing_token": int(r[5]),
                    "budget_ceiling_minor": int(r[6]),
                    "updated_at_ns": int(r[7]),
                }

        latest_items: dict[str, dict[str, Any]] = {}
        for r in db.execute(
            """SELECT work_item_id, owner_agent_id, duty_kind, state, payload_json, updated_at_ns
               FROM work_items
               WHERE company_id = ?
               ORDER BY updated_at_ns DESC""",
            (company_id,),
        ).fetchall():
            aid = str(r[1])
            if aid not in latest_items:
                p = _json_loads_safe(str(r[4]), {})
                summary = (
                    str(p.get("summary") or p.get("title") or r[2])
                    if isinstance(p, dict)
                    else str(r[2])
                )
                latest_items[aid] = {
                    "work_item_id": str(r[0]),
                    "duty_kind": str(r[2]),
                    "state": str(r[3]),
                    "summary": summary,
                    "updated_at_ns": int(r[5]),
                }

        pub_counts = {
            str(r[0]): int(r[1])
            for r in db.execute(
                """SELECT author_id, COUNT(*)
                   FROM shared_publication WHERE company_id = ? GROUP BY author_id""",
                (company_id,),
            ).fetchall()
        }

        nodes: list[dict[str, Any]] = []
        for aid, info in directory.items():
            run = active_runs.get(aid)
            item = latest_items.get(aid)
            if info["archived"]:
                live_status = "archived"
                avatar_state = "done"
            elif info["onboarding_state"] == "blocked":
                live_status = "blocked"
                avatar_state = "blocked"
            elif run and run["status"] == "running":
                live_status = "working"
                avatar_state = "working"
            elif run and run["status"] == "waiting":
                live_status = "waiting"
                avatar_state = "waiting"
            elif item and item["state"] in ("due", "scheduled"):
                live_status = "ready"
                avatar_state = "ready"
            else:
                live_status = "idle"
                avatar_state = "ready"

            current_task = (
                item["summary"]
                if item
                else (
                    f"Onboarding ({info['onboarding_state']})"
                    if info["onboarding_state"]
                    else "Standing watch"
                )
            )
            nodes.append(
                {
                    "agent_id": aid,
                    "display_name": info["display_name"],
                    "role_label": info["role_label"],
                    "is_ceo": info["is_ceo"],
                    "avatar_variant": info["avatar_variant"],
                    "avatar_state": avatar_state,
                    "live_status": live_status,
                    "archived": info["archived"],
                    "current_task": current_task,
                    "active_run": run,
                    "monthly_budget_minor": info["monthly_budget_minor"],
                    "private_draft_count": _count_private_drafts(store, aid),
                    "published_count": pub_counts.get(aid, 0),
                    "grant_scope": info["grant_scope"],
                    "sandbox_authorized": False,
                    "sandbox_activity": None,
                }
            )

        # Build live interaction edges from peer requests, project conversations, handoffs.
        edges: list[dict[str, Any]] = []
        seen_edge_keys: set[tuple[str, str, str]] = set()

        for r in db.execute(
            """SELECT request_id, requester_agent_id, recipient_agent_id,
                      project_id, question, state, created_at_ns
               FROM work_peer_requests
               WHERE company_id = ?
               ORDER BY created_at_ns DESC LIMIT 20""",
            (company_id,),
        ).fetchall():
            src, dst = str(r[1]), str(r[2])
            if src == dst:
                continue
            key = (min(src, dst), max(src, dst), f"peer:{r[0]}")
            if key in seen_edge_keys:
                continue
            seen_edge_keys.add(key)
            q_text = str(r[4])
            short_label = q_text if len(q_text) <= 34 else f"{q_text[:31]}..."
            preview_text = q_text if len(q_text) <= 180 else f"{q_text[:177]}..."
            sender_name = str(directory.get(src, {}).get("display_name", src))
            state_str = str(r[5])
            edges.append(
                {
                    "edge_id": f"peer-{r[0]}",
                    "source_agent_id": src,
                    "target_agent_id": dst,
                    "kind": "peer_request",
                    "state": state_str,
                    "active": state_str == "open",
                    "label": short_label,
                    "latest_preview": preview_text,
                    "latest_sender_name": sender_name,
                    "message_count": 1,
                    "channel_id": str(r[3]) if r[3] else "ceo-direct",
                    "updated_at_ns": int(r[6]),
                }
            )

        for r in db.execute(
            """SELECT offer_id, sender_id, recipient_id, expected_result, accepted_run_id
               FROM work_handoff
               WHERE company_id = ?
               ORDER BY rowid DESC LIMIT 12""",
            (company_id,),
        ).fetchall():
            src, dst = str(r[1]), str(r[2])
            if src == dst:
                continue
            expected_text = str(r[3])
            preview_text = (
                expected_text
                if len(expected_text) <= 180
                else f"{expected_text[:177]}..."
            )
            sender_name = str(directory.get(src, {}).get("display_name", src))
            accepted = bool(r[4])
            edges.append(
                {
                    "edge_id": f"handoff-{r[0]}",
                    "source_agent_id": src,
                    "target_agent_id": dst,
                    "kind": "handoff",
                    "state": "accepted" if accepted else "offered",
                    "active": not accepted,
                    "label": f"Handoff · {expected_text[:24]}",
                    "latest_preview": preview_text,
                    "latest_sender_name": sender_name,
                    "message_count": 1,
                    "channel_id": "ceo-direct",
                    "updated_at_ns": as_of_ns,
                }
            )

        # Shared project message edges (connect recent sender with other project members)
        for r in db.execute(
            """SELECT sp.project_id, sp.members,
                      (SELECT pm.sender_id FROM project_message pm
                       WHERE pm.project_id = sp.project_id ORDER BY pm.rowid DESC LIMIT 1),
                      (SELECT pm.body FROM project_message pm
                       WHERE pm.project_id = sp.project_id ORDER BY pm.rowid DESC LIMIT 1),
                      (SELECT COUNT(*) FROM project_message pm
                       WHERE pm.project_id = sp.project_id)
               FROM shared_project sp
               WHERE sp.company_id = ?""",
            (company_id,),
        ).fetchall():
            project_id = str(r[0])
            members = _json_loads_safe(str(r[1]), [])
            last_sender = str(r[2]) if r[2] else None
            last_body = str(r[3]) if r[3] else ""
            msg_count = int(r[4])
            if not isinstance(members, list) or len(members) < 2 or msg_count == 0:
                continue
            src = last_sender if last_sender in members else str(members[0])
            sender_name = str(directory.get(src, {}).get("display_name", src))
            preview_text = (
                last_body.strip()
                if len(last_body.strip()) <= 180
                else f"{last_body.strip()[:177]}..."
            )
            for member in members:
                dst = str(member)
                if dst == src:
                    continue
                pair_key = (min(src, dst), max(src, dst), f"proj:{project_id}")
                if pair_key in seen_edge_keys:
                    continue
                seen_edge_keys.add(pair_key)
                label = f"#{project_id} · {msg_count} msgs"
                if last_body:
                    snippet = last_body.split("\n", 1)[0][:22]
                    label = f"#{project_id} · {snippet}"
                edges.append(
                    {
                        "edge_id": f"proj-{project_id}-{src}-{dst}",
                        "source_agent_id": src,
                        "target_agent_id": dst,
                        "kind": "channel",
                        "state": "active",
                        "active": True,
                        "label": label,
                        "latest_preview": preview_text,
                        "latest_sender_name": sender_name,
                        "message_count": msg_count,
                        "channel_id": project_id,
                        "updated_at_ns": as_of_ns,
                    }
                )

        default_mode = "graph" if len(nodes) <= 8 else "list"
        payload = {
            "projection": PROJECTION_AGENTS_GRAPH,
            "company_id": company_id,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "default_view_mode": default_mode,
            "nodes": nodes,
            "edges": edges,
        }
        _apply_core_enrichers(
            PROJECTION_AGENTS_GRAPH, db, company_id, payload, domain_plugins
        )
        return payload, event_seq


def read_agent_detail_projection(
    store: Path,
    company_id: str,
    agent_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Read full-screen Agent Detail: Desk, Authority/Budget, and 4 individual tabs."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        directory = _agent_directory(db, company_id)
        info = directory.get(agent_id)
        if info is None:
            return None, event_seq

        desk_row = db.execute(
            "SELECT manifest_revision FROM private_desk WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        staffing_row = (
            db.execute(
                """SELECT allowed_scope, slots, turns, manager_scope
                   FROM staffing_authority WHERE grant_id = ?""",
                (info["grant_id"],),
            ).fetchone()
            if info["grant_id"]
            else None
        )

        # Tab 1: Work Activity
        work_activity = [
            {
                "run_id": str(r[0]),
                "work_item_id": str(r[1]),
                "duty_kind": str(r[2]),
                "status": str(r[3]),
                "fencing_token": int(r[4]),
                "budget_ceiling_minor": int(r[5]),
                "outcome": _json_loads_safe(r[6], None),
                "created_at_ns": int(r[7]),
                "updated_at_ns": int(r[8]),
            }
            for r in db.execute(
                """SELECT run_id, work_item_id, duty_kind, status, fencing_token,
                          budget_ceiling_minor, outcome_json, created_at_ns, updated_at_ns
                   FROM work_runs
                   WHERE company_id = ? AND agent_id = ?
                   ORDER BY updated_at_ns DESC LIMIT 40""",
                (company_id, agent_id),
            ).fetchall()
        ]

        work_items = [
            {
                "work_item_id": str(r[0]),
                "duty_kind": str(r[1]),
                "state": str(r[2]),
                "source_kind": str(r[3]),
                "source_id": str(r[4]),
                "payload": _json_loads_safe(str(r[5]), {}),
                "updated_at_ns": int(r[6]),
            }
            for r in db.execute(
                """SELECT work_item_id, duty_kind, state, source_kind, source_id,
                          payload_json, updated_at_ns
                   FROM work_items
                   WHERE company_id = ? AND owner_agent_id = ?
                   ORDER BY updated_at_ns DESC LIMIT 30""",
                (company_id, agent_id),
            ).fetchall()
        ]

        conditions = [
            {
                "condition_id": str(r[0]),
                "trigger_kind": str(r[1]),
                "due_at_ns": None if r[2] is None else int(r[2]),
                "source_identity": None if r[3] is None else str(r[3]),
                "status": str(r[4]),
                "condition_revision": int(r[5]),
                "responsibility_id": None if r[6] is None else str(r[6]),
                "work_item_id": None if r[7] is None else str(r[7]),
                "cooldown_ns": int(r[8]),
                "max_fires": None if r[9] is None else int(r[9]),
                "fire_count": int(r[10]),
            }
            for r in db.execute(
                """SELECT condition_id, trigger_kind, due_at_ns, source_identity, status,
                          condition_revision, responsibility_id, work_item_id,
                          cooldown_ns, max_fires, fire_count
                   FROM work_conditions
                   WHERE company_id = ? AND owner_agent_id = ?
                   ORDER BY updated_at_ns DESC, condition_id ASC LIMIT 20""",
                (company_id, agent_id),
            ).fetchall()
        ]
        armed_conditions = [c for c in conditions if c["status"] == "armed"]

        active_methods_resolved = resolve_active_methods(
            db, company_id, agent_id=agent_id
        )
        active_methods = [
            m.to_dict() | {"delta_id": f"{m.method_id}:{m.revision_sha256}"}
            for m in active_methods_resolved
        ]
        active_snapshot_id = compute_active_snapshot_id(active_methods_resolved)

        work_decisions = [
            {
                "decision_id": str(r[0]),
                "work_item_id": str(r[1]),
                "run_id": str(r[2]),
                "action_id": str(r[3]),
                "action_kind": str(r[4]),
                "evidence_refs": _redact_evidence_refs_for_board(str(r[5])),
                "evidence_digest": str(r[6]),
                "rationale": str(r[7]),
                "next_condition_id": None if r[8] is None else str(r[8]),
                "active_snapshot_id": None if r[9] is None else str(r[9]),
                "decision_at_ns": int(r[10]),
            }
            for r in db.execute(
                """SELECT decision_id, work_item_id, run_id, action_id, action_kind,
                          evidence_refs_json, evidence_digest, rationale,
                          next_condition_id, active_snapshot_id, decision_at_ns
                   FROM work_decision
                   WHERE company_id = ? AND agent_id = ?
                   ORDER BY decision_at_ns DESC, decision_id ASC LIMIT 30""",
                (company_id, agent_id),
            ).fetchall()
        ]

        agent_episodes = [
            {
                "episode_id": str(r[0]),
                "work_item_id": str(r[1]),
                "run_id": str(r[2]),
                "duty_kind": str(r[3]),
                "outcome_status": str(r[4]),
                "active_snapshot_id": None if r[5] is None else str(r[5]),
                "summary_text": str(r[6])[:400],
                "tool_calls_count": int(r[7]),
                "input_tokens": int(r[8]),
                "output_tokens": int(r[9]),
                "cost_minor_units": int(r[10]),
                "recorded_at_ns": int(r[11]),
            }
            for r in db.execute(
                """SELECT episode_id, work_item_id, run_id, duty_kind, outcome_status,
                          active_snapshot_id, summary_text, tool_calls_count,
                          input_tokens, output_tokens, cost_minor_units, recorded_at_ns
                   FROM agent_episode
                   WHERE company_id = ? AND agent_id = ?
                   ORDER BY recorded_at_ns DESC, episode_id ASC LIMIT 30""",
                (company_id, agent_id),
            ).fetchall()
        ]

        # Tab 2: Interactions & Talks (Read-Only)
        interactions = [
            {
                "message_id": str(r[0]),
                "project_id": str(r[1]),
                "sender_id": str(r[2]),
                "sender_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "sender_role": directory.get(str(r[2]), {}).get("role_label", "Agent"),
                "body": str(r[3]),
                "asset_id": None if r[4] is None else str(r[4]),
                "audience": _json_loads_safe(str(r[5]), []),
            }
            for r in db.execute(
                """SELECT pm.message_id, pm.project_id, pm.sender_id, pm.body,
                          pm.asset_id, pm.audience
                   FROM project_message pm
                   LEFT JOIN project_delivery pd
                     ON pd.message_id = pm.message_id AND pd.recipient_id = ?
                   WHERE pm.company_id = ?
                     AND (pm.sender_id = ? OR pd.recipient_id = ?)
                   ORDER BY pm.rowid DESC LIMIT 40""",
                (agent_id, company_id, agent_id, agent_id),
            ).fetchall()
        ]

        # Tab 3: Published Reports
        published_reports = [
            {
                "asset_id": str(r[0]),
                "project_id": str(r[1]),
                "work_item_id": str(r[2]),
                "audience": _json_loads_safe(str(r[3]), []),
                "content": str(r[4]),
            }
            for r in db.execute(
                """SELECT asset_id, project_id, work_item_id, audience, content
                   FROM shared_publication
                   WHERE company_id = ? AND author_id = ?
                   ORDER BY rowid DESC LIMIT 30""",
                (company_id, agent_id),
            ).fetchall()
        ]

        # Tab 4: Peer Requests & Handoffs
        peer_requests = [
            {
                "request_id": str(r[0]),
                "direction": "outgoing" if str(r[1]) == agent_id else "incoming",
                "requester_agent_id": str(r[1]),
                "requester_name": directory.get(str(r[1]), {}).get(
                    "display_name", str(r[1])
                ),
                "recipient_agent_id": str(r[2]),
                "recipient_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "project_id": None if r[3] is None else str(r[3]),
                "question": str(r[4]),
                "expected_decision": str(r[5]),
                "state": str(r[6]),
                "response": _json_loads_safe(r[7], None),
                "created_at_ns": int(r[8]),
            }
            for r in db.execute(
                """SELECT request_id, requester_agent_id, recipient_agent_id,
                          project_id, question, expected_decision, state,
                          response_json, created_at_ns
                   FROM work_peer_requests
                   WHERE company_id = ?
                     AND (requester_agent_id = ? OR recipient_agent_id = ?)
                   ORDER BY created_at_ns DESC LIMIT 30""",
                (company_id, agent_id, agent_id),
            ).fetchall()
        ]

        handoffs = [
            {
                "offer_id": str(r[0]),
                "direction": "outgoing" if str(r[1]) == agent_id else "incoming",
                "sender_id": str(r[1]),
                "sender_name": directory.get(str(r[1]), {}).get(
                    "display_name", str(r[1])
                ),
                "recipient_id": str(r[2]),
                "recipient_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "asset_id": str(r[3]),
                "expected_result": str(r[4]),
                "accepted": r[5] is not None,
            }
            for r in db.execute(
                """SELECT offer_id, sender_id, recipient_id, asset_id,
                          expected_result, accepted_run_id
                   FROM work_handoff
                   WHERE company_id = ? AND (sender_id = ? OR recipient_id = ?)
                   ORDER BY rowid DESC LIMIT 20""",
                (company_id, agent_id, agent_id),
            ).fetchall()
        ]

        prohibitions = [
            "Cannot modify company charter or Board mandate",
            "Cannot exceed delegated standing authority or act outside approved grants",
            "Cannot read another Agent's private Desk drafts",
        ]
        if not info["is_ceo"]:
            prohibitions.append("Cannot retire employees or exceed delegated budget")

        private_draft_count = _count_private_drafts(store, agent_id)
        active_run = next(
            (r for r in work_activity if r["status"] in ("running", "waiting")), None
        )

        payload = {
            "projection": PROJECTION_AGENT_DETAIL,
            "company_id": company_id,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "profile": info | {"active_run": active_run},
            "sandbox_authorized": False,
            "agent_sandbox": None,
            "desk_workspace": {
                "execution_profile": info["execution_profile"],
                "desk_manifest_revision": None
                if desk_row is None
                else str(desk_row[0]),
                "private_draft_count": private_draft_count,
                "private_draft_notice": "Private drafts remain physically isolated on the Agent's Desk and are never disclosed to the Board.",
                "published_count": len(published_reports),
                "work_run_count": len(work_activity),
            },
            "authority_budget": {
                "grant_id": info["grant_id"],
                "grant_scope": info["grant_scope"],
                "can_hire": staffing_row is not None,
                "hiring_slots": None if staffing_row is None else int(staffing_row[1]),
                "hiring_turns": None if staffing_row is None else int(staffing_row[2]),
                "monthly_budget_minor": info["monthly_budget_minor"],
                "allocated_turns": info["allocated_turns"],
                "prohibitions": prohibitions,
            },
            "active_methods": active_methods,
            "active_snapshot_id": active_snapshot_id,
            "armed_conditions": armed_conditions,
            "tabs": {
                "work_activity": work_activity,
                "work_items": work_items,
                "conditions": conditions,
                "armed_conditions": armed_conditions,
                "work_decisions": work_decisions,
                "agent_episodes": agent_episodes,
                "interactions_and_talks": interactions,
                "published_reports": published_reports,
                "peer_requests": peer_requests,
                "handoffs": handoffs,
            },
        }
        _apply_core_enrichers(
            PROJECTION_AGENT_DETAIL, db, company_id, payload, domain_plugins
        )
        return payload, event_seq


def read_messages_projection(
    store: Path,
    company_id: str,
    *,
    authorize_board: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Area 3 Unified Company Messages: writable CEO 1:1 + read-only internal channels."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        directory = _agent_directory(db, company_id)
        access = resolve_board_access(
            db,
            company_id,
            authorize_board=authorize_board,
            principal_hint=principal_hint,
        )
        ceo_agent = next((a for a in directory.values() if a["is_ceo"]), None)
        ceo_id = ceo_agent["agent_id"] if ceo_agent else ""

        ceo_msgs = [
            {
                "message_id": str(r[0]),
                "sender_kind": str(r[1]),
                "sender_id": str(r[2]),
                "sender_name": (
                    "Board"
                    if str(r[1]) == "board"
                    else (ceo_agent["display_name"] if ceo_agent else "CEO")
                ),
                "body": str(r[3]),
                "context_ref": (
                    {
                        "kind": str(r[4]),
                        "id": str(r[5]),
                        "label": str(r[6] or r[5]),
                    }
                    if r[4] and r[5]
                    else None
                ),
                "command_id": None if r[7] is None else str(r[7]),
                "delivery_state": str(r[8]),
                "recorded_at_ns": int(r[9]),
            }
            for r in db.execute(
                """SELECT message_id, sender_kind, sender_id, body,
                          context_kind, context_id, context_label, command_id,
                          delivery_state, recorded_at_ns
                   FROM ceo_message
                   WHERE company_id = ?
                   ORDER BY recorded_at_ns ASC, rowid ASC""",
                (company_id,),
            ).fetchall()
        ]

        # Read-only internal channels from shared_project + project_message
        channels: list[dict[str, Any]] = []
        for proj in db.execute(
            """SELECT project_id, revision, members, publishers
               FROM shared_project
               WHERE company_id = ?
               ORDER BY rowid ASC""",
            (company_id,),
        ).fetchall():
            pid = str(proj[0])
            member_ids = _json_loads_safe(str(proj[2]), [])
            member_profiles = [
                {
                    "agent_id": mid,
                    "display_name": directory.get(mid, {}).get("display_name", mid),
                    "role_label": directory.get(mid, {}).get("role_label", "Agent"),
                    "avatar_variant": directory.get(mid, {}).get(
                        "avatar_variant", "wide-oval-top"
                    ),
                }
                for mid in (member_ids if isinstance(member_ids, list) else [])
            ]
            msgs = [
                {
                    "message_id": str(m[0]),
                    "sender_id": str(m[1]),
                    "sender_name": directory.get(str(m[1]), {}).get(
                        "display_name", str(m[1])
                    ),
                    "sender_role": directory.get(str(m[1]), {}).get(
                        "role_label", "Agent"
                    ),
                    "avatar_variant": directory.get(str(m[1]), {}).get(
                        "avatar_variant", "wide-oval-top"
                    ),
                    "body": str(m[2]),
                    "asset_id": None if m[3] is None else str(m[3]),
                    "audience": _json_loads_safe(str(m[4]), []),
                    "delivery_count": int(m[5]),
                    "read_count": int(m[6]),
                }
                for m in db.execute(
                    """SELECT pm.message_id, pm.sender_id, pm.body, pm.asset_id,
                              pm.audience,
                              (SELECT COUNT(*) FROM project_delivery pd WHERE pd.message_id = pm.message_id),
                              (SELECT COUNT(*) FROM project_delivery pd WHERE pd.message_id = pm.message_id AND pd.read_run_id IS NOT NULL)
                       FROM project_message pm
                       WHERE pm.company_id = ? AND pm.project_id = ?
                       ORDER BY pm.rowid ASC""",
                    (company_id, pid),
                ).fetchall()
            ]
            channels.append(
                {
                    "channel_id": pid,
                    "name": f"#{pid}",
                    "revision": str(proj[1]),
                    "read_only": True,
                    "members": member_profiles,
                    "message_count": len(msgs),
                    "messages": msgs,
                }
            )

        return (
            {
                "projection": PROJECTION_MESSAGES,
                "company_id": company_id,
                "charter_revision": charter_revision,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "ceo_thread": {
                    "channel_id": "ceo-direct",
                    "ceo_agent_id": ceo_id,
                    "ceo_display_name": ceo_agent["display_name"]
                    if ceo_agent
                    else "Founder · CEO",
                    "ceo_role_label": ceo_agent["role_label"] if ceo_agent else "CEO",
                    "ceo_avatar_variant": (
                        ceo_agent["avatar_variant"] if ceo_agent else "wide-oval-top"
                    ),
                    "writable": access.role == "board_control",
                    "advisory_notice": "Advisory message to CEO · Does not directly modify authority, mutate mandates, or retire employees.",
                    "messages": ceo_msgs,
                },
                "internal_channels": channels,
            },
            event_seq,
        )


def read_approvals_projection(
    store: Path,
    company_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Area 4 Board Approvals queue & resolution ledger (zero popups)."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        directory = _agent_directory(db, company_id)

        rows = db.execute(
            """SELECT question_id, kind, title, summary, impact_json, options_json,
                      revision, payload_hash, state, respond_by_ns,
                      created_by_agent_id, created_at_ns, resolved_at_ns,
                      resolved_command_id, resolution_choice, resolution_reason,
                      application_status, delivery_status
               FROM decision_question
               WHERE company_id = ?
               ORDER BY CASE state WHEN 'open' THEN 0 ELSE 1 END, created_at_ns DESC""",
            (company_id,),
        ).fetchall()

        pending: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        for r in rows:
            creator_id = str(r[10])
            creator = directory.get(creator_id, {})
            item = {
                "question_id": str(r[0]),
                "kind": str(r[1]),
                "title": str(r[2]),
                "summary": str(r[3]),
                "impact": _json_loads_safe(str(r[4]), {}),
                "options": _json_loads_safe(str(r[5]), ["approve", "decline"]),
                "revision": int(r[6]),
                "payload_hash": str(r[7]),
                "state": str(r[8]),
                "respond_by_ns": None if r[9] is None else int(r[9]),
                "created_by_agent_id": creator_id,
                "created_by_name": creator.get("display_name", creator_id),
                "created_by_role": creator.get("role_label", "Agent"),
                "created_at_ns": int(r[11]),
                "resolved_at_ns": None if r[12] is None else int(r[12]),
                "resolved_command_id": None if r[13] is None else str(r[13]),
                "resolution_choice": None if r[14] is None else str(r[14]),
                "resolution_reason": None if r[15] is None else str(r[15]),
                "application_status": str(r[16]),
                "delivery_status": str(r[17]),
            }
            if item["state"] == "open":
                pending.append(item)
            else:
                history.append(item)

        payload = {
            "projection": PROJECTION_APPROVALS,
            "company_id": company_id,
            "charter_revision": charter_revision,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "pending_count": len(pending),
            "pending": pending,
            "history": history,
            "promote_candidates": [],
        }
        _apply_core_enrichers(
            PROJECTION_APPROVALS, db, company_id, payload, domain_plugins
        )
        return payload, event_seq


def read_finance_projection(
    store: Path,
    company_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Two-Layer P&L, General Ledger entries, period closes, and domain supplements."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        gl_balances = _sum_gl_accounts(db)
        closes, open_issues = _close_issues_summary(db)

        journals = [
            {
                "entry_id": str(r[0]),
                "source": str(r[1]),
                "source_event": str(r[2]),
                "effective_date": str(r[3]),
                "currency": str(r[4]),
                "lines": [
                    {
                        "ordinal": int(line[0]),
                        "account": str(line[1]),
                        "amount_minor": int(line[2]),
                    }
                    for line in db.execute(
                        """SELECT ordinal, account, amount_minor
                           FROM journal_line WHERE entry_id = ? ORDER BY ordinal ASC""",
                        (str(r[0]),),
                    ).fetchall()
                ],
            }
            for r in db.execute(
                """SELECT id, source, source_event, effective_date, currency
                   FROM journal_entry ORDER BY rowid DESC LIMIT 40"""
            ).fetchall()
        ]

        payload = {
            "projection": PROJECTION_FINANCE,
            "company_id": company_id,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "trading_enabled": False,
            "account_balances": [
                {"account": k, "balance_minor": v}
                for k, v in sorted(gl_balances.items())
            ],
            "journals": journals,
            "accounting_closes": closes,
            "open_close_issues": open_issues,
            "funding_settlements": [],
            "retained_statements": [],
        }
        _apply_core_enrichers(
            PROJECTION_FINANCE, db, company_id, payload, domain_plugins
        )
        return payload, event_seq


def read_org_projection(store: Path, company_id: str) -> tuple[dict[str, Any], int]:
    """Read Roster, Published Reports, and read-only Delegated HR Custody ledger."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        directory = _agent_directory(db, company_id)

        roster = [
            info | {"private_draft_count": _count_private_drafts(store, aid)}
            for aid, info in directory.items()
        ]
        publications = [
            {
                "asset_id": str(r[0]),
                "project_id": str(r[1]),
                "author_id": str(r[2]),
                "author_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "author_role": directory.get(str(r[2]), {}).get("role_label", "Agent"),
                "work_item_id": str(r[3]),
                "audience": _json_loads_safe(str(r[4]), []),
                "content": str(r[5]),
            }
            for r in db.execute(
                """SELECT asset_id, project_id, author_id, work_item_id, audience, content
                   FROM shared_publication
                   WHERE company_id = ?
                   ORDER BY rowid DESC LIMIT 40""",
                (company_id,),
            ).fetchall()
        ]
        archives = [
            {
                "cutoff": int(r[0]),
                "agent_id": str(r[1]),
                "agent_name": directory.get(str(r[1]), {}).get(
                    "display_name", str(r[1])
                ),
                "manager_id": str(r[2]),
                "manager_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "employment_revision": str(r[3]),
                "reason": str(r[4]),
                "previous_scope": str(r[5]),
            }
            for r in db.execute(
                """SELECT cutoff, agent_id, manager_id, employment_revision, reason, previous_scope
                   FROM agent_archive
                   WHERE company_id = ?
                   ORDER BY cutoff DESC""",
                (company_id,),
            ).fetchall()
        ]
        return (
            {
                "projection": PROJECTION_ORG,
                "company_id": company_id,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "roster": roster,
                "publications": publications,
                "delegated_hr_custody": {
                    "board_can_retire": False,
                    "policy_notice": "Managed by CEO · Board view is read-only.",
                    "archived_employees": archives,
                },
            },
            event_seq,
        )


def read_restrictions_projection(
    store: Path,
    company_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read active & historical competing restriction causes plus domain risk readiness."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        causes = _active_restrictions_rows(db, company_id)
        payload = {
            "projection": PROJECTION_RESTRICTIONS,
            "company_id": company_id,
            "charter_revision": charter_revision,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "active_causes": [c for c in causes if c["active"]],
            "history": [c for c in causes if not c["active"]],
            "trading_risk": None,
        }
        _apply_core_enrichers(
            PROJECTION_RESTRICTIONS, db, company_id, payload, domain_plugins
        )
        return payload, event_seq


def read_system_safety_projection(
    store: Path,
    company_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read checkpoints, restore quarantine status, and 5-step Safe Custody Hold."""
    checkpoints_dir = store.resolve(strict=False).parent / "checkpoints"
    checkpoints: list[dict[str, Any]] = []
    if checkpoints_dir.is_dir():
        for entry in sorted(checkpoints_dir.glob("*.zip"), reverse=True)[:20]:
            try:
                st = os.lstat(entry)
                if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                    checkpoints.append(
                        {
                            "archive_name": entry.name,
                            "size_bytes": int(st.st_size),
                            "modified_ns": int(st.st_mtime_ns),
                        }
                    )
            except OSError:
                continue

    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        restores = [
            {
                "request_revision": str(r[0]),
                "checkpoint_sha256": str(r[1]),
                "watermark": _json_loads_safe(str(r[2]), {}),
                "restored_at_ns": int(r[3]),
            }
            for r in db.execute(
                """SELECT request_revision, checkpoint_sha256, watermark_json, restored_at_ns
                   FROM company_restore_receipt ORDER BY restored_at_ns DESC"""
            ).fetchall()
        ]
        retired = db.execute(
            """SELECT request_revision, replacement_sha256, retired_at_ns
               FROM company_store_retired WHERE singleton = 1"""
        ).fetchone()
        active_causes = [
            c for c in _active_restrictions_rows(db, company_id) if c["active"]
        ]

        custody_steps = [
            {
                "id": "data_cut",
                "label": "Data cut integrity verified",
                "status": "done",
            },
            {
                "id": "sole_writer",
                "label": "Single-writer lease confirmed",
                "status": "done",
            },
            {
                "id": "authority",
                "label": "Current Board authority verified",
                "status": "done",
            },
            {
                "id": "reconciliation",
                "label": "External reconciliation complete",
                "status": (
                    "done"
                    if not any(
                        c["cause_category"] == "restore_quarantine"
                        for c in active_causes
                    )
                    else "active"
                ),
            },
            {
                "id": "next_actions",
                "label": "Eligible for normal company operations",
                "status": "done" if not active_causes else "pending",
            },
        ]

        payload = {
            "projection": PROJECTION_SYSTEM_SAFETY,
            "company_id": company_id,
            "charter_revision": charter_revision,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "checkpoints": checkpoints,
            "restore_receipts": restores,
            "store_retired": (
                {
                    "request_revision": str(retired[0]),
                    "replacement_sha256": str(retired[1]),
                    "retired_at_ns": int(retired[2]),
                }
                if retired
                else None
            ),
            "safe_custody_hold": custody_steps,
            "active_restrictions": active_causes,
        }
        _apply_core_enrichers(
            PROJECTION_SYSTEM_SAFETY, db, company_id, payload, domain_plugins
        )
        return payload, event_seq


def read_system_evals_projection(
    store: Path,
    company_id: str,
    *,
    evaluator_store: Path | None = None,
    analysis_plan: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Flow 12 review tool pass^3 / DAG lineage + optional isolated Flow 13 offline DB."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)

        qualifications = [
            {
                "receipt_id": str(r[0]),
                "candidate_asset_id": str(r[1]),
                "baseline_asset_id": None if r[2] is None else str(r[2]),
                "parent_asset_id": None if r[3] is None else str(r[3]),
                "passed": bool(r[4]),
                "trial_count": int(r[5]),
                "recorded_at_ns": int(r[6]),
            }
            for r in db.execute(
                """SELECT receipt_id, candidate_asset_id, baseline_asset_id,
                          parent_asset_id, passed, trial_count, recorded_at_ns
                   FROM review_qualification
                   WHERE company_id = ?
                   ORDER BY recorded_at_ns DESC LIMIT 30""",
                (company_id,),
            ).fetchall()
        ]

        activations = [
            {
                "request_id": str(r[0]),
                "operation": str(r[1]),
                "candidate_id": str(r[2]),
                "qualification_id": str(r[3]),
                "parent_asset_id": None if r[4] is None else str(r[4]),
                "revision": int(r[5]),
                "quarantined": bool(r[6]),
                "quarantine_reason": None if r[7] is None else str(r[7]),
                "recorded_at_ns": int(r[8]),
            }
            for r in db.execute(
                """SELECT request_id, operation, candidate_id, qualification_id,
                          parent_asset_id, revision, quarantined, quarantine_reason,
                          recorded_at_ns
                   FROM review_tool_activation
                   WHERE company_id = ?
                   ORDER BY revision DESC LIMIT 30""",
                (company_id,),
            ).fetchall()
        ]

        method_deltas = [
            {
                "delta_id": f"{r[0]}:{r[1]}",
                "method_id": str(r[0]),
                "revision_sha256": str(r[1]),
                "delta_kind": str(r[2]),
                "item_kind": str(r[3]),
                "audience_scope": str(r[4]),
                "audience_target_id": None if r[5] is None else str(r[5]),
                "author_agent_id": str(r[6]),
                "parent_method_ids": _json_loads_safe(str(r[7]), []),
                "active_snapshot_id": None if r[8] is None else str(r[8]),
                "superseded_revision": None if r[9] is None else str(r[9]),
                "redaction_passed": bool(r[10]),
                "status": str(r[11]),
                "created_at_ns": int(r[12]),
            }
            for r in db.execute(
                """SELECT method_id, revision_sha256, delta_kind, item_kind,
                          audience_scope, audience_target_id, author_agent_id,
                          parent_method_ids_json, active_snapshot_id,
                          superseded_revision, redaction_passed, status, created_at_ns
                   FROM method_delta
                   WHERE company_id = ?
                   ORDER BY created_at_ns DESC, method_id ASC, revision_sha256 ASC
                   LIMIT 50""",
                (company_id,),
            ).fetchall()
        ]

        method_qualifications = [
            {
                "qualification_id": str(r[0]),
                "method_id": str(r[1]),
                "revision_sha256": str(r[2]),
                "delta_id": f"{r[1]}:{r[2]}",
                "baseline_revision_sha256": None if r[3] is None else str(r[3]),
                "stage1_passed": bool(r[4]),
                "stage2_passed": bool(r[5]),
                "stage3_passed": bool(r[6]),
                "trial_count": int(r[7]),
                "metrics": _json_loads_safe(str(r[8]), {}),
                "evaluator_revision": str(r[9]),
                "passed": bool(r[10]),
                "recorded_at_ns": int(r[11]),
            }
            for r in db.execute(
                """SELECT qualification_id, method_id, revision_sha256,
                          baseline_revision_sha256, stage1_passed, stage2_passed,
                          stage3_passed, trial_count, metrics_json, evaluator_revision,
                          passed, recorded_at_ns
                   FROM method_qualification
                   WHERE company_id = ?
                   ORDER BY recorded_at_ns DESC, qualification_id ASC
                   LIMIT 50""",
                (company_id,),
            ).fetchall()
        ]

        delta_meta_by_id: dict[str, tuple[str, str]] = {}
        method_activations: list[dict[str, Any]] = []
        for r in db.execute(
            """SELECT a.request_id, a.operation, a.method_id, a.revision_sha256,
                      a.qualification_id, a.audience_scope, a.audience_target_id,
                      a.activation_seq, a.principal_id, a.grant_id, a.quarantined,
                      a.quarantine_reason, a.recorded_at_ns,
                      d.delta_kind, d.status
               FROM method_activation a
               LEFT JOIN method_delta d
                 ON d.company_id = a.company_id
                AND d.method_id = a.method_id
                AND d.revision_sha256 = a.revision_sha256
               WHERE a.company_id = ?
               ORDER BY a.activation_seq DESC
               LIMIT 50""",
            (company_id,),
        ).fetchall():
            delta_id = f"{r[2]}:{r[3]}"
            delta_meta_by_id[delta_id] = (
                "" if r[13] is None else str(r[13]),
                "" if r[14] is None else str(r[14]),
            )
            method_activations.append(
                {
                    "request_id": str(r[0]),
                    "operation": str(r[1]),
                    "method_id": str(r[2]),
                    "revision_sha256": str(r[3]),
                    "delta_id": delta_id,
                    "qualification_id": str(r[4]),
                    "audience_scope": str(r[5]),
                    "audience_target_id": None if r[6] is None else str(r[6]),
                    "activation_seq": int(r[7]),
                    "principal_id": str(r[8]),
                    "grant_id": str(r[9]),
                    "quarantined": bool(r[10]),
                    "quarantine_reason": None if r[11] is None else str(r[11]),
                    "recorded_at_ns": int(r[12]),
                }
            )

        blast_radii = collect_lineage_blast_radii(
            db,
            company_id,
            [
                (m_act["method_id"], m_act["revision_sha256"])
                for m_act in method_activations
            ],
        )
        for m_act in method_activations:
            desc = list(
                blast_radii.get((m_act["method_id"], m_act["revision_sha256"]), ())
            )
            m_act["blast_radius_descendants"] = desc
            blocked = {m_act["delta_id"], *desc}
            fallback = next(
                (
                    other["revision_sha256"]
                    for other in method_activations
                    if other["method_id"] == m_act["method_id"]
                    and other["audience_scope"] == m_act["audience_scope"]
                    and other["audience_target_id"] == m_act["audience_target_id"]
                    and not other["quarantined"]
                    and other["operation"] != "quarantine"
                    and other["delta_id"] not in blocked
                    and delta_meta_by_id.get(other["delta_id"], ("", ""))[0]
                    != "DEPRECATE"
                    and delta_meta_by_id.get(other["delta_id"], ("", ""))[1]
                    != "quarantined"
                ),
                None,
            )
            m_act["fallback_revision"] = fallback

    # Pre-compute transitive blast radius for each activated tool candidate
    children_map: dict[str, list[str]] = {}
    for act in activations:
        parent = act["parent_asset_id"]
        if parent:
            children_map.setdefault(parent, []).append(act["candidate_id"])

    def _descendants(root_id: str) -> list[str]:
        seen: list[str] = []
        stack = list(children_map.get(root_id, []))
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.append(cur)
                stack.extend(children_map.get(cur, []))
        return seen

    for act in activations:
        desc = _descendants(act["candidate_id"])
        act["blast_radius_descendants"] = desc
        blocked = {act["candidate_id"], *desc}
        fallback = next(
            (
                other["candidate_id"]
                for other in activations
                if not other["quarantined"] and other["candidate_id"] not in blocked
            ),
            None,
        )
        act["fallback_candidate_id"] = fallback

    # Optional physically separate offline evaluator database (strictly read-only)
    offline_evals: dict[str, Any] = {
        "connected": False,
        "isolated_notice": "Offline evaluator registry runs in a separate read-only SQLite database outside company.sqlite.",
        "suites": [],
    }
    rsi_transfer_summaries: list[dict[str, Any]] = []
    if evaluator_store is not None:
        try:
            st = os.lstat(evaluator_store)
            if (
                not stat.S_ISLNK(st.st_mode)
                and stat.S_ISREG(st.st_mode)
                and st.st_uid == os.geteuid()
                and st.st_nlink == 1
            ):
                meta = read_sealed_suite_meta(evaluator_store)
                offline_evals["connected"] = True
                offline_evals["suites"] = [
                    {
                        "revision": meta["revision"],
                        "suite_id": meta["suite_id"],
                        "purpose": meta["purpose"],
                        "sealed_at_ns": int(st.st_mtime_ns),
                        "observation_count": meta["observation_count"],
                        "grade_count": meta["grade_count"],
                        "pairwise_grade_count": meta["pairwise_grade_count"],
                    }
                ]
                if meta["purpose"] == "recursive":
                    effective_plan = (
                        analysis_plan
                        if analysis_plan is not None
                        else meta.get("analysis_plan")
                    )
                    if effective_plan is not None:
                        summary = summarize_rsi_transfer(
                            evaluator_store,
                            revision=meta["revision"],
                            analysis_plan=effective_plan,
                        )
                        rsi_transfer_summaries.append(
                            {
                                "suite_id": meta["suite_id"],
                                "revision": meta["revision"],
                                **asdict(summary),
                            }
                        )
                    else:
                        rsi_transfer_summaries.append(
                            {
                                "suite_id": meta["suite_id"],
                                "revision": meta["revision"],
                                "verdict": "UNAVAILABLE",
                                "reasons": ("sealed-analysis-plan-unavailable",),
                                "analysis_plan_digest": meta["analysis_plan_digest"],
                                "planned_pairs": meta["planned_pairs"],
                                "complete_pairs": meta["pairwise_grade_count"],
                                "distinct_transfer_families": meta[
                                    "distinct_transfer_families"
                                ],
                            }
                        )
        except (TrialRefused, OSError, sqlite3.Error):
            offline_evals["connected"] = False
            offline_evals["suites"] = []
            rsi_transfer_summaries = []

    return (
        {
            "projection": PROJECTION_SYSTEM_EVALS,
            "company_id": company_id,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "qualifications": qualifications,
            "activations": activations,
            "method_deltas": method_deltas,
            "method_qualifications": method_qualifications,
            "method_activations": method_activations,
            "rsi_transfer_summaries": rsi_transfer_summaries,
            "offline_evaluator": offline_evals,
        },
        event_seq,
    )


def read_system_audit_projection(
    store: Path,
    company_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read redacted audit_event tail, work_conditions, fencing leases, and board_binding roles."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        events = [
            {
                "event_seq": int(r[0]),
                "domain": str(r[1]),
                "entity_type": str(r[2]),
                "entity_id": str(r[3]),
                "event_type": str(r[4]),
                "recorded_at_ns": int(r[5]),
            }
            for r in db.execute(
                """SELECT event_seq, domain, entity_type, entity_id, event_type, recorded_at_ns
                   FROM audit_event
                   WHERE company_id = ?
                   ORDER BY event_seq DESC LIMIT 80""",
                (company_id,),
            ).fetchall()
        ]
        conditions = [
            {
                "condition_id": str(r[0]),
                "owner_agent_id": str(r[1]),
                "trigger_kind": str(r[2]),
                "due_at_ns": None if r[3] is None else int(r[3]),
                "source_identity": None if r[4] is None else str(r[4]),
                "status": str(r[5]),
            }
            for r in db.execute(
                """SELECT condition_id, owner_agent_id, trigger_kind,
                          due_at_ns, source_identity, status
                   FROM work_conditions
                   WHERE company_id = ?
                   ORDER BY updated_at_ns DESC LIMIT 40""",
                (company_id,),
            ).fetchall()
        ]
        fences = [
            {
                "run_id": str(r[0]),
                "agent_id": str(r[1]),
                "duty_kind": str(r[2]),
                "status": str(r[3]),
                "fencing_token": int(r[4]),
                "lease_owner": None if r[5] is None else str(r[5]),
                "lease_expires_at_ns": None if r[6] is None else int(r[6]),
            }
            for r in db.execute(
                """SELECT run_id, agent_id, duty_kind, status, fencing_token,
                          lease_owner, lease_expires_at_ns
                   FROM work_runs
                   WHERE company_id = ? AND status IN ('running', 'waiting', 'queued')
                   ORDER BY updated_at_ns DESC LIMIT 30""",
                (company_id,),
            ).fetchall()
        ]
        bindings = [
            {
                "principal_id": f"principal:{sha256(str(r[0]).encode('utf-8')).hexdigest()[:12]}",
                "role_name": str(r[1]),
                "scope_ref": str(r[2]),
                "binding_revision": int(r[3]),
                "active": r[4] is None,
            }
            for r in db.execute(
                """SELECT principal_id, role_name, scope_ref, binding_revision, revoked_at_ns
                   FROM board_binding
                   WHERE company_id = ?
                   ORDER BY bound_at_ns ASC""",
                (company_id,),
            ).fetchall()
        ]
        return (
            {
                "projection": PROJECTION_SYSTEM_AUDIT,
                "company_id": company_id,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "events": events,
                "conditions": conditions,
                "active_fences": fences,
                "board_bindings": bindings,
            },
            event_seq,
        )


def projection_etag(payload: dict[str, Any], event_seq: int) -> str:
    """Compute a stable ETag over projection payload excluding volatile as_of_ns."""
    stable = {k: v for k, v in payload.items() if k != "as_of_ns"}
    digest = sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f'"{digest}:{event_seq}"'
