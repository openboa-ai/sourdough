"""Read-only SQLite projections for system safety, evaluations, and audit telemetry."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any

from ...company import FoundingRefused, _identity
from ...method_learning import (
    collect_lineage_blast_radii,
    opaque_one_agent_method_id,
)
from ...runtime.domains import DomainPlugin
from ...storage.kernel import max_event_seq
from ...trial_registry import (
    TrialRefused,
    read_sealed_suite_meta,
    summarize_rsi_transfer,
)
from ..redaction import (
    _redact_condition_source_for_board,
    _redact_one_agent_ref_for_board,
    load_one_agent_method_sets,
    redact_audit_entity_id_for_board,
)
from ._shared import (
    PROJECTION_SYSTEM_AUDIT,
    PROJECTION_SYSTEM_EVALS,
    PROJECTION_SYSTEM_SAFETY,
    _active_restrictions_rows,
    _apply_core_enrichers,
    _json_loads_safe,
    _open_projection_snapshot,
)


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

    with _open_projection_snapshot(store) as db:
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
    with _open_projection_snapshot(store) as db:
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

        one_agent_deltas, one_agent_mids = load_one_agent_method_sets(db, company_id)

        method_deltas: list[dict[str, Any]] = []
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
        ).fetchall():
            raw_mid, raw_rev, scope_s = str(r[0]), str(r[1]), str(r[4])
            is_one_agent = scope_s == "one_agent"
            proj_mid = opaque_one_agent_method_id(raw_mid) if is_one_agent else raw_mid
            raw_parents = _json_loads_safe(str(r[7]), [])
            proj_parents = (
                [
                    _redact_one_agent_ref_for_board(
                        str(p),
                        force_opaque=is_one_agent,
                        one_agent_deltas=one_agent_deltas,
                        one_agent_mids=one_agent_mids,
                    )
                    for p in raw_parents
                ]
                if isinstance(raw_parents, list)
                else []
            )
            method_deltas.append(
                {
                    "delta_id": f"{proj_mid}:{raw_rev}",
                    "method_id": proj_mid,
                    "method_id_redacted": is_one_agent,
                    "revision_sha256": raw_rev,
                    "delta_kind": str(r[2]),
                    "item_kind": str(r[3]),
                    "audience_scope": scope_s,
                    "audience_target_id": None if r[5] is None else str(r[5]),
                    "author_agent_id": str(r[6]),
                    "parent_method_ids": proj_parents,
                    "active_snapshot_id": None if r[8] is None else str(r[8]),
                    "superseded_revision": None if r[9] is None else str(r[9]),
                    "redaction_passed": bool(r[10]),
                    "status": str(r[11]),
                    "created_at_ns": int(r[12]),
                }
            )

        method_qualifications: list[dict[str, Any]] = []
        for r in db.execute(
            """SELECT q.qualification_id, q.method_id, q.revision_sha256,
                      q.baseline_revision_sha256, q.stage1_passed, q.stage2_passed,
                      q.stage3_passed, q.trial_count, q.metrics_json, q.evaluator_revision,
                      q.passed, q.recorded_at_ns, d.audience_scope
               FROM method_qualification q
               LEFT JOIN method_delta d
                 ON d.company_id = q.company_id
                AND d.method_id = q.method_id
                AND d.revision_sha256 = q.revision_sha256
               WHERE q.company_id = ?
               ORDER BY q.recorded_at_ns DESC, q.qualification_id ASC
               LIMIT 50""",
            (company_id,),
        ).fetchall():
            raw_mid, raw_rev = str(r[1]), str(r[2])
            is_one_agent = (
                str(r[12] or "") == "one_agent"
                or (raw_mid, raw_rev) in one_agent_deltas
            )
            proj_mid = opaque_one_agent_method_id(raw_mid) if is_one_agent else raw_mid
            method_qualifications.append(
                {
                    "qualification_id": str(r[0]),
                    "method_id": proj_mid,
                    "method_id_redacted": is_one_agent,
                    "revision_sha256": raw_rev,
                    "delta_id": f"{proj_mid}:{raw_rev}",
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
            )

        delta_meta_by_id: dict[str, tuple[str, str]] = {}
        raw_act_keys: list[tuple[str, str]] = []
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
            raw_mid, raw_rev, scope_s = str(r[2]), str(r[3]), str(r[5])
            is_one_agent = scope_s == "one_agent"
            proj_mid = opaque_one_agent_method_id(raw_mid) if is_one_agent else raw_mid
            raw_delta_id = f"{raw_mid}:{raw_rev}"
            proj_delta_id = f"{proj_mid}:{raw_rev}"
            raw_act_keys.append((raw_mid, raw_rev))
            delta_meta_by_id[raw_delta_id] = (
                "" if r[13] is None else str(r[13]),
                "" if r[14] is None else str(r[14]),
            )
            method_activations.append(
                {
                    "request_id": str(r[0]),
                    "operation": str(r[1]),
                    "method_id": proj_mid,
                    "method_id_redacted": is_one_agent,
                    "revision_sha256": raw_rev,
                    "delta_id": proj_delta_id,
                    "qualification_id": str(r[4]),
                    "audience_scope": scope_s,
                    "audience_target_id": None if r[6] is None else str(r[6]),
                    "activation_seq": int(r[7]),
                    "principal_id": str(r[8]),
                    "grant_id": str(r[9]),
                    "quarantined": bool(r[10]),
                    "quarantine_reason": (
                        "redacted_one_agent_reason"
                        if is_one_agent and r[11] is not None
                        else (None if r[11] is None else str(r[11]))
                    ),
                    "recorded_at_ns": int(r[12]),
                    "_raw_delta_id": raw_delta_id,
                    "_raw_key": (raw_mid, raw_rev),
                }
            )

        blast_radii = collect_lineage_blast_radii(
            db,
            company_id,
            raw_act_keys,
        )
        for m_act in method_activations:
            raw_key = m_act.pop("_raw_key")
            raw_delta_id = m_act.pop("_raw_delta_id")
            is_one_agent = m_act["audience_scope"] == "one_agent"
            raw_desc = list(blast_radii.get(raw_key, ()))
            m_act["blast_radius_descendants"] = [
                _redact_one_agent_ref_for_board(
                    str(d_ref),
                    force_opaque=is_one_agent,
                    one_agent_deltas=one_agent_deltas,
                    one_agent_mids=one_agent_mids,
                )
                for d_ref in raw_desc
            ]
            blocked = {raw_delta_id, *raw_desc}
            fallback = next(
                (
                    other["revision_sha256"]
                    for other, other_raw in zip(method_activations, raw_act_keys)
                    if other["method_id"] == m_act["method_id"]
                    and other["audience_scope"] == m_act["audience_scope"]
                    and other["audience_target_id"] == m_act["audience_target_id"]
                    and not other["quarantined"]
                    and other["operation"] != "quarantine"
                    and f"{other_raw[0]}:{other_raw[1]}" not in blocked
                    and delta_meta_by_id.get(
                        f"{other_raw[0]}:{other_raw[1]}", ("", "")
                    )[0]
                    != "DEPRECATE"
                    and delta_meta_by_id.get(
                        f"{other_raw[0]}:{other_raw[1]}", ("", "")
                    )[1]
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
    with _open_projection_snapshot(store) as db:
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        one_agent_deltas, one_agent_mids = load_one_agent_method_sets(db, company_id)
        events = [
            {
                "event_seq": int(r[0]),
                "domain": str(r[1]),
                "entity_type": str(r[2]),
                "entity_id": redact_audit_entity_id_for_board(
                    str(r[2]),
                    str(r[3]),
                    one_agent_deltas=one_agent_deltas,
                    one_agent_mids=one_agent_mids,
                ),
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
                "source_identity": _redact_condition_source_for_board(
                    db, company_id, str(r[2]), None if r[4] is None else str(r[4])
                ),
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
