"""Episodic dreaming audit for recurring workflow and failure pattern consolidation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from time import time_ns
from typing import Any

from ..company import _identity
from ..duty_run import ExecutionClaim
from ..storage.db import open_writer, write_tx
from ..storage.kernel import append_audit_event
from ..writer_lease import WriterLease
from .lifecycle import distill_method_delta
from .models import (
    DreamingAuditResult,
    MethodDelta,
    MethodRefused,
    _safe_token,
    _validate_claim,
    compute_active_snapshot_id,
    opaque_one_agent_delta_id,
    opaque_one_agent_method_id,
)
from .projections import _resolve_active_methods_on_db


def audit_episodes_for_dreaming(
    writer_or_conn: WriterLease | sqlite3.Connection,
    claim: ExecutionClaim,
    *,
    episode_limit: int = 20,
    min_pattern_count: int = 2,
    max_proposals: int = 3,
    now_ns: int | None = None,
) -> DreamingAuditResult:
    """Audit recent raw agent_episode records and draft candidate Skill deltas without deleting history."""
    if type(episode_limit) is not int or episode_limit < 1:
        raise MethodRefused("episode_limit must be a positive integer")
    if type(min_pattern_count) is not int or min_pattern_count < 2:
        raise MethodRefused("min_pattern_count must be an integer >= 2")
    if type(max_proposals) is not int or max_proposals < 1:
        raise MethodRefused("max_proposals must be a positive integer")
    bounded_limit = min(episode_limit, 50)
    bounded_proposals = min(max_proposals, 10)

    def _run(db: sqlite3.Connection, store_path: Path | None) -> DreamingAuditResult:
        del store_path
        company_id = _identity(db)
        _validate_claim(db, claim, company_id=company_id)
        agent_id = claim.founder_id
        ts = max(1, time_ns() if now_ns is None else int(now_ns))

        ep_rows = db.execute(
            """SELECT episode_id, run_id, duty_kind, outcome_status,
                      active_snapshot_id, decisions_json, receipts_json,
                      tool_calls_count, recorded_at_ns
               FROM agent_episode
               WHERE company_id = ? AND agent_id = ?
                 AND duty_kind != 'work.dreaming'
               ORDER BY recorded_at_ns DESC, episode_id DESC
               LIMIT ?""",
            (company_id, agent_id, bounded_limit),
        ).fetchall()

        if not ep_rows:
            append_audit_event(
                db,
                company_id=company_id,
                domain="learning",
                entity_type="work_dreaming",
                entity_id=claim.run_id,
                event_type="learning.dreaming_completed",
                actor_id=agent_id,
                run_id=claim.run_id,
                payload={
                    "episodes_audited": 0,
                    "patterns_detected": 0,
                    "proposals_drafted": [],
                    "skipped_reason": "no_episodes",
                },
                recorded_at_ns=ts,
            )
            return DreamingAuditResult(
                company_id=company_id,
                agent_id=agent_id,
                episodes_audited=0,
                patterns_detected=(),
                drafted_deltas=(),
                skipped_reason="no_episodes",
            )

        active_methods = _resolve_active_methods_on_db(
            db, company_id, agent_id=agent_id, duty_kind=None
        )
        active_by_id = {m.method_id: m for m in active_methods}
        snap_id = compute_active_snapshot_id(active_methods)

        run_ids = [str(r[1]) for r in ep_rows]
        receipts_by_run: dict[str, list[tuple[str, str, str, str | None]]] = {}
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            rec_rows = db.execute(
                f"""SELECT run_id, call_id, tool_name, status, error_type
                    FROM tool_call_receipt
                    WHERE company_id = ? AND agent_id = ? AND run_id IN ({placeholders})
                    ORDER BY recorded_at_ns ASC, call_id ASC""",
                (company_id, agent_id, *run_ids),
            ).fetchall()
            for r_id, c_id, t_name, st, err_type in rec_rows:
                receipts_by_run.setdefault(str(r_id), []).append(
                    (
                        str(c_id),
                        str(t_name),
                        str(st),
                        None if err_type is None else str(err_type),
                    )
                )

        # 1. Detect recurring failure/refusal patterns & recurring successful workflows.
        failure_groups: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
        success_groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        method_outcomes: dict[str, dict[str, list[str]]] = {}

        for (
            ep_id,
            run_id,
            duty_kind,
            outcome_status,
            _ep_snap,
            decisions_json,
            _receipts_json,
            tool_calls_count,
            _rec_ns,
        ) in ep_rows:
            ep_s = str(ep_id)
            run_s = str(run_id)
            dk_s = str(duty_kind)
            st_s = str(outcome_status)
            run_rcpts = receipts_by_run.get(run_s, [])

            failed_rcpts = [
                (cid, tname, err)
                for cid, tname, rst, err in run_rcpts
                if rst in ("refused", "error", "fatal")
            ]
            if st_s in ("failed", "blocked") or failed_rcpts:
                if failed_rcpts:
                    for cid, tname, err in failed_rcpts:
                        sig = f"{tname}:{err or 'refused'}"
                        failure_groups.setdefault((dk_s, sig), []).append((ep_s, cid))
                else:
                    failure_groups.setdefault((dk_s, f"outcome:{st_s}"), []).append(
                        (ep_s, None)
                    )
            elif st_s == "completed" and int(tool_calls_count or 0) >= 1:
                ok_tools = tuple(
                    dict.fromkeys(
                        tname for _cid, tname, rst, _err in run_rcpts if rst == "ok"
                    )
                )
                if ok_tools:
                    success_groups.setdefault((dk_s, ok_tools), []).append(ep_s)

            try:
                dec_list = json.loads(str(decisions_json))
            except json.JSONDecodeError:
                dec_list = []
            if isinstance(dec_list, list):
                for entry in dec_list:
                    if not isinstance(entry, Mapping):
                        continue
                    invoked = entry.get("invoked")
                    if not isinstance(invoked, list):
                        continue
                    for mid in invoked:
                        if not isinstance(mid, str) or not mid:
                            continue
                        bucket = method_outcomes.setdefault(mid, {"ok": [], "fail": []})
                        if st_s == "completed" and not failed_rcpts:
                            bucket["ok"].append(ep_s)
                        elif st_s in ("failed", "blocked") or failed_rcpts:
                            bucket["fail"].append(ep_s)

        patterns: list[dict[str, Any]] = []
        drafted: list[MethodDelta] = []

        # Contradicted / stale one_agent active methods -> PATCH or DEPRECATE candidate
        for mid in sorted(method_outcomes):
            if len(drafted) >= bounded_proposals:
                break
            active = active_by_id.get(mid)
            if active is None or active.audience_scope != "one_agent":
                continue
            counts = method_outcomes[mid]
            fail_eps = list(dict.fromkeys(counts["fail"]))
            ok_eps = list(dict.fromkeys(counts["ok"]))
            if len(fail_eps) < min_pattern_count:
                continue
            delta_kind = "DEPRECATE" if not ok_eps else "PATCH"
            patterns.append(
                {
                    "kind": "stale_or_contradicted_skill",
                    "method_id": mid,
                    "delta_kind": delta_kind,
                    "failure_episodes": len(fail_eps),
                    "success_episodes": len(ok_eps),
                }
            )
            refs = [f"agent_episode:{eid}" for eid in fail_eps[:6]]
            body_text = (
                f"Dreaming audit flagged {len(fail_eps)} failed/refused episodes "
                f"and {len(ok_eps)} completed episodes while invoking '{mid}'. "
                f"Recommended {delta_kind.lower()} pending 3-stage evaluation."
            )
            delta = distill_method_delta(
                db,
                claim,
                delta_kind=delta_kind,
                item_kind=active.item_kind,
                method_id=mid,
                trigger_spec=dict(active.trigger_spec),
                body=body_text,
                audience_scope="one_agent",
                audience_target_id=agent_id,
                parent_method_ids=(f"{active.method_id}:{active.revision_sha256}",),
                active_snapshot_id=snap_id,
                superseded_revision=active.revision_sha256,
                source_refs=refs,
                uncertainty_note=(
                    f"Observed {len(fail_eps)} failures vs {len(ok_eps)} successes across recent episodes."
                ),
                falsification_criteria=(
                    f"Reject {delta_kind} if qualified holdout trials show {mid} improves pass^k."
                ),
                now_ns=ts,
            )
            drafted.append(delta)

        # Recurring failure/refusal patterns -> ADD or PATCH candidate
        for (dk_s, err_sig), occs in sorted(
            failure_groups.items(), key=lambda kv: (-len(kv[1]), kv[0])
        ):
            if len(drafted) >= bounded_proposals:
                break
            unique_eps = list(dict.fromkeys(ep for ep, _cid in occs))
            if len(unique_eps) < min_pattern_count:
                continue
            mid = f"dream-fix-{_safe_token(dk_s)}-{_safe_token(err_sig)}"[:96]
            existing_active = active_by_id.get(mid)
            if (
                existing_active is not None
                and existing_active.audience_scope != "one_agent"
            ):
                continue
            patterns.append(
                {
                    "kind": "recurring_failure",
                    "duty_kind": dk_s,
                    "signature": err_sig,
                    "occurrences": len(unique_eps),
                    "method_id": mid,
                }
            )
            refs: list[str] = [f"agent_episode:{eid}" for eid in unique_eps[:6]]
            for _ep, cid in occs:
                if cid is not None and f"tool_call_receipt:{cid}" not in refs:
                    refs.append(f"tool_call_receipt:{cid}")
                    if len(refs) >= 10:
                        break
            body_text = (
                f"Guard against recurring '{err_sig}' during '{dk_s}' "
                f"(observed across {len(unique_eps)} episodes): validate inputs, "
                "grants, and preconditions before invoking tools."
            )
            delta = distill_method_delta(
                db,
                claim,
                delta_kind="PATCH" if existing_active is not None else "ADD",
                item_kind="work_method",
                method_id=mid,
                trigger_spec={"duty_kind": dk_s, "guard_signature": err_sig},
                body=body_text,
                audience_scope="one_agent",
                audience_target_id=agent_id,
                parent_method_ids=(
                    (f"{existing_active.method_id}:{existing_active.revision_sha256}",)
                    if existing_active is not None
                    else ()
                ),
                active_snapshot_id=snap_id,
                superseded_revision=(
                    existing_active.revision_sha256
                    if existing_active is not None
                    else None
                ),
                source_refs=refs,
                uncertainty_note=f"Drafted from {len(unique_eps)} recent episodes; requires 3-stage gate.",
                falsification_criteria=f"Falsified if '{err_sig}' persists when guard is applied.",
                now_ns=ts,
            )
            drafted.append(delta)

        # Recurring successful tool workflows -> ADD candidate
        for (dk_s, tool_seq), eps in sorted(
            success_groups.items(), key=lambda kv: (-len(kv[1]), kv[0])
        ):
            if len(drafted) >= bounded_proposals:
                break
            unique_eps = list(dict.fromkeys(eps))
            if len(unique_eps) < min_pattern_count:
                continue
            mid = f"dream-sop-{_safe_token(dk_s)}"[:96]
            if mid in active_by_id:
                continue
            patterns.append(
                {
                    "kind": "recurring_success_workflow",
                    "duty_kind": dk_s,
                    "tools": list(tool_seq),
                    "occurrences": len(unique_eps),
                    "method_id": mid,
                }
            )
            refs = [f"agent_episode:{eid}" for eid in unique_eps[:6]]
            tool_chain = " -> ".join(tool_seq[:6])
            body_text = (
                f"Standard workflow for '{dk_s}' verified across {len(unique_eps)} "
                f"completed episodes: execute bounded sequence ({tool_chain}) and "
                "record grounded evidence."
            )
            delta = distill_method_delta(
                db,
                claim,
                delta_kind="ADD",
                item_kind="work_method",
                method_id=mid,
                trigger_spec={"duty_kind": dk_s, "workflow_tools": list(tool_seq[:6])},
                body=body_text,
                audience_scope="one_agent",
                audience_target_id=agent_id,
                active_snapshot_id=snap_id,
                source_refs=refs,
                uncertainty_note=f"Consolidated from {len(unique_eps)} completed episodes; requires 3-stage gate.",
                falsification_criteria=f"Falsified if workflow ({tool_chain}) degrades completion rate.",
                now_ns=ts,
            )
            drafted.append(delta)

        append_audit_event(
            db,
            company_id=company_id,
            domain="learning",
            entity_type="work_dreaming",
            entity_id=claim.run_id,
            event_type="learning.dreaming_completed",
            actor_id=agent_id,
            run_id=claim.run_id,
            payload={
                "episodes_audited": len(ep_rows),
                "patterns_detected": len(patterns),
                "proposals_drafted": [
                    {
                        "delta_id": (
                            opaque_one_agent_delta_id(d.method_id, d.revision_sha256)
                            if d.audience_scope == "one_agent"
                            else d.delta_id
                        ),
                        "method_id": (
                            opaque_one_agent_method_id(d.method_id)
                            if d.audience_scope == "one_agent"
                            else d.method_id
                        ),
                        "delta_kind": d.delta_kind,
                        "status": d.status,
                    }
                    for d in drafted
                ],
            },
            recorded_at_ns=ts,
        )
        return DreamingAuditResult(
            company_id=company_id,
            agent_id=agent_id,
            episodes_audited=len(ep_rows),
            patterns_detected=tuple(patterns),
            drafted_deltas=tuple(drafted),
            skipped_reason=None,
        )

    if isinstance(writer_or_conn, WriterLease):
        writer_or_conn.verify()
        with (
            closing(open_writer(writer_or_conn.store, lease=writer_or_conn)) as db,
            write_tx(db),
        ):
            res = _run(db, writer_or_conn.store)
            writer_or_conn.verify()
            return res
    if isinstance(writer_or_conn, sqlite3.Connection):
        return _run(writer_or_conn, None)
    raise MethodRefused("Expected WriterLease or sqlite3.Connection")
