"""Read-only SQLite projections for the Agents interaction graph and Agent detail views."""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any

from ...company import FoundingRefused, _identity
from ...method_learning import (
    compute_active_snapshot_id,
    resolve_active_methods,
)
from ...runtime.domains import DomainPlugin
from ...runtime.work_kernel import WORK_DECISION_ACTION_KINDS
from ...storage.kernel import max_event_seq
from ..redaction import (
    _redact_active_method_for_board,
    _redact_condition_source_for_board,
    _redact_evidence_refs_for_board,
    _redact_run_outcome_for_board,
    _redact_work_item_payload_for_board,
    _sanitize_action_kind_for_board,
    load_one_agent_method_sets,
)
from ._shared import (
    PROJECTION_AGENT_DETAIL,
    PROJECTION_AGENTS_GRAPH,
    _agent_directory,
    _apply_core_enrichers,
    _count_private_drafts_for_projection,
    _json_loads_safe,
    _open_projection_snapshot,
)


def read_agents_graph_projection(
    store: Path,
    company_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Area 2 Agents & Activity node-and-edge interaction graph projection."""
    with _open_projection_snapshot(store) as db:
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
                    "private_draft_count": 0,
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
            """SELECT offer_id, sender_id, recipient_id, expected_result, accepted_run_id, state
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
            state_str = (
                str(r[5])
                if r[5] is not None and str(r[5]).strip()
                else ("accepted" if r[4] else "offered")
            )
            edges.append(
                {
                    "edge_id": f"handoff-{r[0]}",
                    "source_agent_id": src,
                    "target_agent_id": dst,
                    "kind": "handoff",
                    "state": state_str,
                    "active": state_str == "offered",
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
    for node in payload["nodes"]:
        node["private_draft_count"] = _count_private_drafts_for_projection(
            store, str(node["agent_id"])
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
    with _open_projection_snapshot(store) as db:
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
        runtime_binding_row = db.execute(
            """SELECT provider, model, revision, sandbox_backend, profile_id, updated_at_ns
               FROM agent_runtime_binding
               WHERE company_id = ? AND agent_id = ?""",
            (company_id, agent_id),
        ).fetchone()
        runtime_binding = (
            {
                "provider": str(runtime_binding_row[0]),
                "model": str(runtime_binding_row[1]),
                "revision": str(runtime_binding_row[2]),
                "sandbox_backend": str(runtime_binding_row[3]),
                "profile_id": str(runtime_binding_row[4]),
                "updated_at_ns": int(runtime_binding_row[5]),
            }
            if runtime_binding_row is not None
            else None
        )
        staffing_row = (
            db.execute(
                """SELECT allowed_scope, slots, turns, manager_scope
                   FROM staffing_authority WHERE grant_id = ?""",
                (info["grant_id"],),
            ).fetchone()
            if info["grant_id"]
            else None
        )

        responsibilities = [
            {
                "responsibility_id": str(r[0]),
                "duty_kind": str(r[1]),
                "remit": str(r[2]),
                "status": str(r[3]),
                "consecutive_noops": int(r[4]) if r[4] is not None else 0,
                "cooldown_until_ns": int(r[5]) if r[5] is not None else 0,
                "window_started_ns": int(r[6]) if r[6] is not None else 0,
                "window_wakes": int(r[7]) if r[7] is not None else 0,
                "updated_at_ns": int(r[8]),
            }
            for r in db.execute(
                """SELECT r.responsibility_id, r.duty_kind, r.remit, r.status,
                          d.consecutive_noops, d.cooldown_until_ns,
                          d.window_started_ns, d.window_wakes, r.updated_at_ns
                   FROM work_responsibilities r
                   LEFT JOIN work_discovery_state d
                     ON d.company_id = r.company_id
                    AND d.responsibility_id = r.responsibility_id
                   WHERE r.company_id = ? AND r.owner_agent_id = ?
                   ORDER BY r.updated_at_ns DESC, r.responsibility_id ASC
                   LIMIT 20""",
                (company_id, agent_id),
            ).fetchall()
        ]

        # Tab 1: Work Activity
        one_agent_deltas, one_agent_mids = load_one_agent_method_sets(db, company_id)
        work_activity = [
            {
                "run_id": str(r[0]),
                "work_item_id": str(r[1]),
                "duty_kind": str(r[2]),
                "status": str(r[3]),
                "fencing_token": int(r[4]),
                "budget_ceiling_minor": int(r[5]),
                "outcome": _redact_run_outcome_for_board(
                    r[6],
                    one_agent_deltas=one_agent_deltas,
                    one_agent_mids=one_agent_mids,
                ),
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
                "payload": _redact_work_item_payload_for_board(
                    db, company_id, str(r[5])
                ),
                "attempt_count": int(r[6]),
                "next_retry_at_ns": None if r[7] is None else int(r[7]),
                "updated_at_ns": int(r[8]),
            }
            for r in db.execute(
                """SELECT work_item_id, duty_kind, state, source_kind, source_id,
                          payload_json, attempt_count, next_retry_at_ns, updated_at_ns
                   FROM work_items
                   WHERE company_id = ? AND owner_agent_id = ?
                   ORDER BY updated_at_ns DESC LIMIT 30""",
                (company_id, agent_id),
            ).fetchall()
        ]

        dreaming_runs = [r for r in work_activity if r["duty_kind"] == "work.dreaming"]
        dreaming_stats_row = db.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status IN ('failed', 'fenced') THEN 1 ELSE 0 END),
                      MAX(updated_at_ns)
               FROM work_runs
               WHERE company_id = ? AND agent_id = ? AND duty_kind = 'work.dreaming'""",
            (company_id, agent_id),
        ).fetchone()
        dreaming_latest_row = db.execute(
            """SELECT run_id, status, outcome_json, updated_at_ns
               FROM work_runs
               WHERE company_id = ? AND agent_id = ? AND duty_kind = 'work.dreaming'
               ORDER BY updated_at_ns DESC, run_id ASC
               LIMIT 1""",
            (company_id, agent_id),
        ).fetchone()
        dreaming_summary = {
            "total_runs": int(dreaming_stats_row[0]) if dreaming_stats_row else 0,
            "completed_runs": (
                int(dreaming_stats_row[1] or 0) if dreaming_stats_row else 0
            ),
            "failed_runs": (
                int(dreaming_stats_row[2] or 0) if dreaming_stats_row else 0
            ),
            "last_run_id": (
                None if dreaming_latest_row is None else str(dreaming_latest_row[0])
            ),
            "last_status": (
                None if dreaming_latest_row is None else str(dreaming_latest_row[1])
            ),
            "last_run_at_ns": (
                None
                if dreaming_stats_row is None or dreaming_stats_row[3] is None
                else int(dreaming_stats_row[3])
            ),
            "last_outcome": (
                None
                if dreaming_latest_row is None
                else _redact_run_outcome_for_board(
                    dreaming_latest_row[2],
                    one_agent_deltas=one_agent_deltas,
                    one_agent_mids=one_agent_mids,
                )
            ),
            "recent_runs_count": len(dreaming_runs),
        }

        conditions = [
            {
                "condition_id": str(r[0]),
                "trigger_kind": str(r[1]),
                "due_at_ns": None if r[2] is None else int(r[2]),
                "source_identity": _redact_condition_source_for_board(
                    db, company_id, str(r[1]), None if r[3] is None else str(r[3])
                ),
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
            _redact_active_method_for_board(
                m.to_dict() | {"delta_id": f"{m.method_id}:{m.revision_sha256}"}
            )
            for m in active_methods_resolved
        ]
        active_snapshot_id = compute_active_snapshot_id(active_methods_resolved)

        work_decisions = [
            {
                "decision_id": str(r[0]),
                "work_item_id": str(r[1]),
                "run_id": str(r[2]),
                "action_id": f"action:{sha256(str(r[3]).encode('utf-8')).hexdigest()[:16]}",
                "action_id_redacted": True,
                "action_kind": _sanitize_action_kind_for_board(r[4]),
                "action_kind_redacted": str(r[4]) not in WORK_DECISION_ACTION_KINDS,
                "evidence_refs": _redact_evidence_refs_for_board(str(r[5])),
                "evidence_digest": str(r[6]),
                "rationale": None,
                "rationale_redacted": True,
                "rationale_digest": sha256(str(r[7]).encode("utf-8")).hexdigest(),
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
                "summary_text": f"{r[4]}:{r[3]}",
                "summary_redacted": True,
                "summary_digest": sha256(str(r[6]).encode("utf-8")).hexdigest(),
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
        peer_requests = []
        for r in db.execute(
            """SELECT request_id, requester_agent_id, recipient_agent_id,
                      project_id, question, expected_decision, state,
                      response_json, created_at_ns, hop_count, chain_depth,
                      root_work_item_id, delegation_path_json
               FROM work_peer_requests
               WHERE company_id = ?
                 AND (requester_agent_id = ? OR recipient_agent_id = ?)
               ORDER BY created_at_ns DESC LIMIT 30""",
            (company_id, agent_id, agent_id),
        ).fetchall():
            delegation_path = _json_loads_safe(str(r[12] or "[]"), [])
            peer_requests.append(
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
                    "hop_count": int(r[9]),
                    "chain_depth": int(r[10]),
                    "root_work_item_id": None if r[11] is None else str(r[11]),
                    "delegation_path_length": (
                        len(delegation_path) if isinstance(delegation_path, list) else 0
                    ),
                }
            )

        handoffs = []
        for r in db.execute(
            """SELECT offer_id, sender_id, recipient_id, asset_id,
                      expected_result, accepted_run_id, state, expires_at,
                      successor_work_item_id
               FROM work_handoff
               WHERE company_id = ? AND (sender_id = ? OR recipient_id = ?)
               ORDER BY rowid DESC LIMIT 20""",
            (company_id, agent_id, agent_id),
        ).fetchall():
            state_str = (
                str(r[6])
                if r[6] is not None and str(r[6]).strip()
                else ("accepted" if r[5] is not None else "offered")
            )
            handoffs.append(
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
                    "state": state_str,
                    "accepted": state_str == "accepted",
                    "expires_at": int(r[7]),
                    "successor_work_item_id": None if r[8] is None else str(r[8]),
                }
            )

        prohibitions = [
            "Cannot modify company charter or Board mandate",
            "Cannot exceed delegated standing authority or act outside approved grants",
            "Cannot read another Agent's private Desk drafts",
        ]
        if not info["is_ceo"]:
            prohibitions.append("Cannot retire employees or exceed delegated budget")

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
                "runtime_binding": runtime_binding,
                "desk_manifest_revision": None
                if desk_row is None
                else str(desk_row[0]),
                "private_draft_count": 0,
                "private_draft_notice": "Private drafts remain physically isolated on the Agent's Desk and are never disclosed to the Board.",
                "published_count": len(published_reports),
                "work_run_count": len(work_activity),
            },
            "runtime_binding": runtime_binding,
            "responsibilities": responsibilities,
            "dreaming_summary": dreaming_summary,
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
                "responsibilities": responsibilities,
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
    payload["desk_workspace"]["private_draft_count"] = (
        _count_private_drafts_for_projection(store, agent_id)
    )
    return payload, event_seq
