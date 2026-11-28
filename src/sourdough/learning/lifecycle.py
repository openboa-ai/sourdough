"""Method delta distillation, 3-stage pre-commit qualification, activation, and lineage quarantine."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from time import time_ns
from typing import Any
from uuid import uuid4

from ..company import _identity
from ..duty_run import ExecutionClaim, employment_cutoff
from ..storage.db import open_writer, write_tx
from ..storage.kernel import (
    CommandConflict,
    admit_command_receipt,
    append_audit_event,
)
from ..writer_lease import WriterLease
from .lineage import (
    _agent_authorized_for_delta_sources,
    _all_lineage_edges,
    _has_quarantined_ancestor,
    _node_has_quarantined_parent_or_snapshot,
    _quarantined_nodes,
)
from .models import (
    _DELTA_COLUMNS,
    _MAX_NOTE_CHARS,
    EVALUATOR_REVISION,
    METHOD_CONTRACT_VERSION,
    VALID_AUDIENCE_SCOPES,
    VALID_DELTA_KINDS,
    VALID_ITEM_KINDS,
    MethodActivation,
    MethodDelta,
    MethodQualification,
    MethodQuarantineReceipt,
    MethodRefused,
    _canonical_json,
    _format_delta_id,
    _lookup_delta,
    _require_nonempty_str,
    _require_safe_id,
    _row_to_delta,
    _sha256_json,
    _validate_claim,
    opaque_one_agent_delta_id,
    opaque_one_agent_method_id,
)
from .projections import sync_all_company_desk_projections
from .redaction import (
    _check_no_prompt_injection,
    _find_unredacted_shared_leaks,
    _iter_strings,
    _verify_source_refs,
)


def _verify_promotion_authority(
    db: sqlite3.Connection,
    *,
    company_id: str,
    actor_id: str,
    grant_id: str,
    audience_scope: str,
    audience_target_id: str | None,
) -> None:
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise MethodRefused("actor_id is required")
    if not isinstance(grant_id, str) or not grant_id.strip():
        raise MethodRefused("grant_id is required")
    if employment_cutoff(db, actor_id) is not None:
        raise MethodRefused("Promotion actor employment is fenced")
    row = db.execute(
        """SELECT g.scope
           FROM standing_grant g
           JOIN agent a ON a.id = g.agent_id
           WHERE a.company_id = ? AND a.id = ? AND g.id = ?""",
        (company_id, actor_id, grant_id),
    ).fetchone()
    if row is None:
        raise MethodRefused("Promotion grant does not belong to actor in this company")
    scope = str(row[0]).strip()
    if not scope or scope.lower() == "revoked":
        raise MethodRefused("Promotion grant is revoked")
    if audience_scope == "one_agent":
        if scope != "manage-company" and actor_id != audience_target_id:
            raise MethodRefused(
                "Activating one_agent method requires own active grant or manage-company authority"
            )
    elif audience_scope == "project":
        if (
            scope != "manage-company"
            and scope != f"manage-project:{audience_target_id}"
        ):
            raise MethodRefused(
                "Activating project-wide method requires company/project promotion authority"
            )
    elif audience_scope == "company":
        if scope != "manage-company":
            raise MethodRefused(
                "Activating company-wide method requires manage-company promotion authority"
            )


def distill_method_delta(
    writer_or_conn: WriterLease | sqlite3.Connection,
    claim: ExecutionClaim,
    *,
    delta_kind: str,
    method_id: str,
    trigger_spec: Mapping[str, Any],
    body: str,
    audience_scope: str,
    source_refs: Sequence[str],
    target_kind: str | None = None,
    item_kind: str | None = None,
    audience_target_id: str | None = None,
    parent_method_ids: Sequence[str] = (),
    active_snapshot_id: str | None = None,
    superseded_revision: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    procedure_meta: Mapping[str, Any] | None = None,
    uncertainty_note: str = "",
    falsification_criteria: str = "",
    now_ns: int | None = None,
) -> MethodDelta:
    """Distill an itemized, content-addressed candidate (or disputed) method delta.

    `item_kind` (`'work_method' | 'improvement_procedure' | 'helper_script'`) is
    canonical (matching `method_delta.item_kind`); `target_kind` is retained as a
    compatibility alias when `item_kind` is omitted.
    """
    if delta_kind not in VALID_DELTA_KINDS:
        raise MethodRefused(f"Unsupported delta_kind: {delta_kind!r}")
    if target_kind is not None and item_kind is not None and target_kind != item_kind:
        raise MethodRefused("Conflicting target_kind and item_kind arguments")
    eff_item_kind = item_kind if item_kind is not None else target_kind
    if eff_item_kind not in VALID_ITEM_KINDS:
        raise MethodRefused(f"Unsupported item_kind/target_kind: {eff_item_kind!r}")
    if audience_scope not in VALID_AUDIENCE_SCOPES:
        raise MethodRefused(f"Unsupported audience_scope: {audience_scope!r}")

    clean_method_id = _require_safe_id(method_id, "method_id")
    if not isinstance(trigger_spec, Mapping) or not trigger_spec:
        raise MethodRefused("trigger_spec must be a nonempty mapping")
    trigger_dict = dict(trigger_spec)
    _check_no_prompt_injection(_canonical_json(trigger_dict), label="trigger_spec")
    for text_part in _iter_strings(trigger_dict):
        _check_no_prompt_injection(text_part, label="trigger_spec")

    clean_body = _require_nonempty_str(body, "body")
    _check_no_prompt_injection(clean_body, label="body")

    if not isinstance(uncertainty_note, str) or len(uncertainty_note) > _MAX_NOTE_CHARS:
        raise MethodRefused("uncertainty_note must be a bounded string")
    _check_no_prompt_injection(uncertainty_note, label="uncertainty_note")
    if (
        not isinstance(falsification_criteria, str)
        or len(falsification_criteria) > _MAX_NOTE_CHARS
    ):
        raise MethodRefused("falsification_criteria must be a bounded string")
    _check_no_prompt_injection(falsification_criteria, label="falsification_criteria")

    if metadata is not None and procedure_meta is not None:
        raise MethodRefused("Provide either metadata or procedure_meta, not both")
    raw_meta = procedure_meta if procedure_meta is not None else metadata
    if raw_meta is not None and not isinstance(raw_meta, Mapping):
        raise MethodRefused("metadata / procedure_meta must be a mapping")
    if raw_meta is not None:
        for text_part in _iter_strings(raw_meta):
            _check_no_prompt_injection(text_part, label="metadata")

    if isinstance(parent_method_ids, (str, bytes)) or not isinstance(
        parent_method_ids, Sequence
    ):
        raise MethodRefused("parent_method_ids must be a sequence of method IDs")
    clean_parents: list[str] = []
    for p in parent_method_ids:
        if not isinstance(p, str) or not p.strip():
            raise MethodRefused("parent_method_ids must contain nonempty strings")
        p_str = p.strip()
        base_p = p_str.rsplit(":", 1)[0].rsplit("@", 1)[0]
        if base_p == clean_method_id and delta_kind == "ADD":
            raise MethodRefused("ADD method delta cannot cite itself as a parent")
        clean_parents.append(p_str)
    parents_tuple = tuple(dict.fromkeys(clean_parents))

    if active_snapshot_id is not None and (
        not isinstance(active_snapshot_id, str) or not active_snapshot_id.strip()
    ):
        raise MethodRefused("active_snapshot_id must be a nonempty string or None")

    if audience_scope == "one_agent":
        eff_target_id = (
            claim.founder_id
            if audience_target_id is None
            else _require_safe_id(audience_target_id, "audience_target_id")
        )
        if eff_target_id != claim.founder_id:
            raise MethodRefused(
                "one_agent delta cannot target a different Agent's Desk"
            )
    elif audience_scope == "project":
        if audience_target_id is None:
            raise MethodRefused("project audience_scope requires audience_target_id")
        eff_target_id = _require_safe_id(audience_target_id, "audience_target_id")
    else:
        if audience_target_id is not None:
            raise MethodRefused(
                "company audience_scope must not specify audience_target_id"
            )
        eff_target_id = None

    redaction_passed = True
    redaction_report: dict[str, Any] | None = None
    if audience_scope in ("project", "company"):
        leaks: list[str] = []
        shared_texts = [
            clean_body,
            uncertainty_note,
            falsification_criteria,
            *_iter_strings(trigger_dict),
            *(_iter_strings(raw_meta) if raw_meta is not None else ()),
        ]
        for text_part in shared_texts:
            leaks.extend(_find_unredacted_shared_leaks(text_part))
        if leaks:
            raise MethodRefused(
                "Shared method delta rejected due to unredacted private Desk path or secret: "
                + ", ".join(sorted(set(leaks)))
            )
        redaction_report = {
            "screened_scope": audience_scope,
            "leaks_found": [],
            "injection_screened": True,
        }
    else:
        redaction_report = {
            "screened_scope": "one_agent",
            "leaks_found": [],
            "injection_screened": True,
        }

    if delta_kind in ("PATCH", "DEPRECATE"):
        if not isinstance(superseded_revision, str) or not superseded_revision.strip():
            raise MethodRefused(f"{delta_kind} delta requires superseded_revision")
        eff_superseded = superseded_revision.strip()
    else:
        if superseded_revision is not None:
            raise MethodRefused("ADD delta must not specify superseded_revision")
        eff_superseded = None

    eff_meta: dict[str, Any] | None = dict(raw_meta) if raw_meta is not None else None
    if eff_item_kind == "improvement_procedure":
        meta_obj = dict(eff_meta or {})
        meta_obj.setdefault("predecessor_procedure", eff_superseded)
        meta_obj.setdefault("parent_method_ids", list(parents_tuple))
        meta_obj.setdefault("active_snapshot_id", active_snapshot_id)
        meta_obj.setdefault("rejected_attempts", [])
        meta_obj.setdefault("predicted_effect", "")
        meta_obj.setdefault("observed_effect", "")
        meta_obj.setdefault("evaluation_conditions", trigger_dict)
        meta_obj.setdefault("proposed_change", clean_body[:240])
        eff_meta = meta_obj
    elif eff_item_kind == "helper_script":
        meta_obj = dict(eff_meta or {})
        script_name = meta_obj.get("script_name")
        if script_name is not None:
            _require_safe_id(script_name, "script_name")
        else:
            meta_obj["script_name"] = f"{clean_method_id}.py"
        eff_meta = meta_obj

    def _run(db: sqlite3.Connection, store_path: Path | None) -> MethodDelta:
        company_id = _identity(db)
        _validate_claim(db, claim, company_id=company_id)

        if audience_scope == "project":
            proj_row = db.execute(
                "SELECT members FROM shared_project WHERE company_id = ? AND project_id = ?",
                (company_id, eff_target_id),
            ).fetchone()
            if proj_row is None:
                raise MethodRefused(f"Shared project '{eff_target_id}' does not exist")
            try:
                members = set(json.loads(str(proj_row[0])))
            except json.JSONDecodeError:
                members = set()
            if claim.founder_id not in members:
                raise MethodRefused(
                    f"Agent '{claim.founder_id}' is not a member of project '{eff_target_id}'"
                )

        if delta_kind in ("PATCH", "DEPRECATE"):
            active_row = db.execute(
                """SELECT revision_sha256 FROM method_delta
                   WHERE company_id = ? AND method_id = ?
                     AND audience_scope = ?
                     AND (audience_target_id IS ? OR audience_target_id = ?)
                     AND status = 'active'
                   LIMIT 1""",
                (
                    company_id,
                    clean_method_id,
                    audience_scope,
                    eff_target_id,
                    eff_target_id,
                ),
            ).fetchone()
            if active_row is not None:
                if str(active_row[0]) != eff_superseded:
                    raise MethodRefused(
                        f"superseded_revision '{eff_superseded}' does not match active revision '{active_row[0]}'"
                    )
            else:
                known_row = db.execute(
                    """SELECT 1 FROM method_delta
                       WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?
                         AND audience_scope = ?
                         AND (audience_target_id IS ? OR audience_target_id = ?)""",
                    (
                        company_id,
                        clean_method_id,
                        eff_superseded,
                        audience_scope,
                        eff_target_id,
                        eff_target_id,
                    ),
                ).fetchone()
                if known_row is None:
                    raise MethodRefused(
                        f"superseded_revision '{eff_superseded}' does not exist for '{clean_method_id}'"
                    )

        for parent_ref in parents_tuple:
            if ":" in parent_ref:
                p_mid, p_rev = parent_ref.rsplit(":", 1)
                p_rows = db.execute(
                    """SELECT audience_scope, audience_target_id, author_agent_id
                       FROM method_delta
                       WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
                    (company_id, p_mid, p_rev),
                ).fetchall()
            else:
                p_rows = db.execute(
                    """SELECT audience_scope, audience_target_id, author_agent_id
                       FROM method_delta
                       WHERE company_id = ? AND method_id = ?""",
                    (company_id, parent_ref),
                ).fetchall()
            if not p_rows:
                raise MethodRefused(
                    "parent_method_ids must reference existing method_delta records"
                )
            if not any(
                str(r[0]) != "one_agent"
                or str(r[1] or r[2]) == claim.founder_id
                or str(r[2]) == claim.founder_id
                for r in p_rows
            ):
                raise MethodRefused(
                    "parent_method_ids cannot reference another Agent's private method"
                )

        verified_refs, missing_refs = _verify_source_refs(
            db,
            store_path=store_path,
            company_id=company_id,
            agent_id=claim.founder_id,
            audience_scope=audience_scope,
            audience_target_id=eff_target_id,
            source_refs=source_refs,
            allow_private_refs=(audience_scope == "one_agent"),
        )
        status = "disputed" if missing_refs else "candidate"
        eff_uncertainty = uncertainty_note
        if missing_refs and not eff_uncertainty:
            eff_uncertainty = "Unverified or missing source references: " + ", ".join(
                missing_refs
            )

        rev_payload = {
            "method_id": clean_method_id,
            "delta_kind": delta_kind,
            "item_kind": eff_item_kind,
            "trigger_spec": trigger_dict,
            "body_text": clean_body,
            "audience_scope": audience_scope,
            "audience_target_id": eff_target_id,
            "author_agent_id": claim.founder_id,
            "parent_method_ids": list(parents_tuple),
            "active_snapshot_id": active_snapshot_id,
            "source_refs": list(verified_refs),
            "procedure_meta": eff_meta,
            "uncertainty_note": eff_uncertainty,
            "falsification_criteria": falsification_criteria,
            "superseded_revision": eff_superseded,
        }
        revision_sha256 = _sha256_json(rev_payload)

        existing = db.execute(
            f"""SELECT {_DELTA_COLUMNS}
                FROM method_delta
                WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
            (company_id, clean_method_id, revision_sha256),
        ).fetchone()
        if existing is not None:
            return _row_to_delta(existing)

        ts = max(1, time_ns() if now_ns is None else int(now_ns))
        trigger_json = _canonical_json(trigger_dict)
        parents_json = _canonical_json(list(parents_tuple))
        refs_json = _canonical_json(list(verified_refs))
        meta_json = _canonical_json(eff_meta) if eff_meta is not None else None
        report_json = (
            _canonical_json(redaction_report) if redaction_report is not None else None
        )

        db.execute(
            """INSERT INTO method_delta (
                   company_id, method_id, revision_sha256, delta_kind, item_kind,
                   trigger_spec_json, body_text, audience_scope, audience_target_id,
                   author_agent_id, parent_method_ids_json, active_snapshot_id,
                   source_refs_json, procedure_meta_json, uncertainty_note,
                   falsification_criteria, superseded_revision, redaction_passed,
                   redaction_report_json, status, created_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id,
                clean_method_id,
                revision_sha256,
                delta_kind,
                eff_item_kind,
                trigger_json,
                clean_body,
                audience_scope,
                eff_target_id,
                claim.founder_id,
                parents_json,
                active_snapshot_id,
                refs_json,
                meta_json,
                eff_uncertainty,
                falsification_criteria,
                eff_superseded,
                1 if redaction_passed else 0,
                report_json,
                status,
                ts,
            ),
        )
        audit_mid = (
            opaque_one_agent_method_id(clean_method_id)
            if audience_scope == "one_agent"
            else clean_method_id
        )
        append_audit_event(
            db,
            company_id=company_id,
            domain="learning",
            entity_type="method_delta",
            entity_id=_format_delta_id(audit_mid, revision_sha256),
            event_type=(
                "learning.method_delta_disputed"
                if status == "disputed"
                else "learning.method_delta_distilled"
            ),
            actor_id=claim.founder_id,
            run_id=claim.run_id,
            payload={
                "method_id": audit_mid,
                "revision_sha256": revision_sha256,
                "delta_kind": delta_kind,
                "item_kind": eff_item_kind,
                "audience_scope": audience_scope,
                "status": status,
                "missing_refs": list(missing_refs),
            },
            recorded_at_ns=ts,
        )
        row = db.execute(
            f"""SELECT {_DELTA_COLUMNS}
                FROM method_delta
                WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
            (company_id, clean_method_id, revision_sha256),
        ).fetchone()
        assert row is not None
        return _row_to_delta(row)

    if isinstance(writer_or_conn, WriterLease):
        writer_or_conn.verify()
        with (
            closing(open_writer(writer_or_conn.store, lease=writer_or_conn)) as db,
            write_tx(db),
        ):
            delta = _run(db, writer_or_conn.store)
            writer_or_conn.verify()
            return delta
    if isinstance(writer_or_conn, sqlite3.Connection):
        return _run(writer_or_conn, None)
    raise MethodRefused("Expected WriterLease or sqlite3.Connection")


def qualify_method_delta(
    writer_or_conn: WriterLease | sqlite3.Connection,
    delta_id: str,
    *,
    passed_k: int,
    total_k: int | None = None,
    negative_trigger_passed: bool,
    safety_passed: bool,
    holdout_margin: float = 0.0,
    baseline_revision_sha256: str | None = None,
    evaluator_revision: str = EVALUATOR_REVISION,
    metrics: Mapping[str, Any] | None = None,
    qualification_id: str | None = None,
    now_ns: int | None = None,
) -> MethodQualification:
    """Evaluate a candidate method delta through the 3-stage pre-commit gate."""
    eff_total_k = passed_k if total_k is None else total_k
    if type(passed_k) is not int or type(eff_total_k) is not int:
        raise MethodRefused("passed_k and total_k must be integers")
    if passed_k < 0 or eff_total_k < 1 or passed_k > eff_total_k:
        raise MethodRefused("Invalid pass^k trial counts")
    if eff_total_k < 3:
        raise MethodRefused("Stage C requires pass^k with total_k >= 3")
    if isinstance(holdout_margin, bool) or not isinstance(holdout_margin, (int, float)):
        raise MethodRefused("holdout_margin must be a real number")
    margin_f = float(holdout_margin)
    if not math.isfinite(margin_f):
        raise MethodRefused("holdout_margin must be finite")
    if not isinstance(evaluator_revision, str) or not evaluator_revision.strip():
        raise MethodRefused("evaluator_revision must be a nonempty string")

    def _run(db: sqlite3.Connection) -> MethodQualification:
        company_id = _identity(db)
        delta = _lookup_delta(db, company_id, delta_id)
        if delta.status in ("disputed", "quarantined"):
            raise MethodRefused(
                f"Cannot qualify method delta in '{delta.status}' status"
            )
        if _node_has_quarantined_parent_or_snapshot(db, company_id, delta):
            raise MethodRefused(
                "Cannot qualify method delta with quarantined parent or snapshot lineage"
            )

        stage1_passed = (
            bool(delta.trigger_spec)
            and bool(negative_trigger_passed)
            and (delta.redaction_passed or delta.audience_scope == "one_agent")
        )
        # Safety/harmlessness cannot be waived by holdout_margin or pass^k score.
        stage2_passed = bool(safety_passed)
        stage3_passed = eff_total_k >= 3 and passed_k == eff_total_k and margin_f >= 0.0
        overall_passed = stage1_passed and stage2_passed and stage3_passed

        qid = qualification_id or f"mqual-{uuid4().hex[:16]}"
        ts = max(1, time_ns() if now_ns is None else int(now_ns))
        metrics_dict = dict(metrics or {})
        metrics_dict.update(
            {
                "passed_k": passed_k,
                "total_k": eff_total_k,
                "negative_trigger_passed": bool(negative_trigger_passed),
                "safety_passed": bool(safety_passed),
                "holdout_margin": margin_f,
            }
        )
        metrics_json = _canonical_json(metrics_dict)

        db.execute(
            """INSERT INTO method_qualification (
                   company_id, qualification_id, method_id, revision_sha256,
                   baseline_revision_sha256, stage1_passed, stage2_passed,
                   stage3_passed, trial_count, metrics_json, evaluator_revision,
                   passed, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id,
                qid,
                delta.method_id,
                delta.revision_sha256,
                baseline_revision_sha256,
                1 if stage1_passed else 0,
                1 if stage2_passed else 0,
                1 if stage3_passed else 0,
                eff_total_k,
                metrics_json,
                evaluator_revision,
                1 if overall_passed else 0,
                ts,
            ),
        )
        audit_delta_id = (
            opaque_one_agent_delta_id(delta.method_id, delta.revision_sha256)
            if delta.audience_scope == "one_agent"
            else delta.delta_id
        )
        append_audit_event(
            db,
            company_id=company_id,
            domain="learning",
            entity_type="method_qualification",
            entity_id=qid,
            event_type="learning.method_qualified",
            actor_id=delta.author_agent_id,
            payload={
                "qualification_id": qid,
                "delta_id": audit_delta_id,
                "stage1_passed": stage1_passed,
                "stage2_passed": stage2_passed,
                "stage3_passed": stage3_passed,
                "passed": overall_passed,
                "trial_count": eff_total_k,
            },
            recorded_at_ns=ts,
        )
        return MethodQualification(
            company_id=company_id,
            qualification_id=qid,
            method_id=delta.method_id,
            revision_sha256=delta.revision_sha256,
            delta_id=delta.delta_id,
            baseline_revision_sha256=baseline_revision_sha256,
            stage1_passed=stage1_passed,
            stage2_passed=stage2_passed,
            stage3_passed=stage3_passed,
            trial_count=eff_total_k,
            metrics=metrics_dict,
            evaluator_revision=evaluator_revision,
            passed=overall_passed,
            recorded_at_ns=ts,
        )

    if isinstance(writer_or_conn, WriterLease):
        writer_or_conn.verify()
        with (
            closing(open_writer(writer_or_conn.store, lease=writer_or_conn)) as db,
            write_tx(db),
        ):
            qual = _run(db)
            writer_or_conn.verify()
            return qual
    if isinstance(writer_or_conn, sqlite3.Connection):
        return _run(writer_or_conn)
    raise MethodRefused("Expected WriterLease or sqlite3.Connection")


def activate_method_delta(
    writer_or_conn: WriterLease | sqlite3.Connection,
    delta_id: str,
    *,
    actor_id: str,
    grant_id: str,
    qualification_id: str | None = None,
    request_id: str | None = None,
    operation: str = "activate",
    now_ns: int | None = None,
) -> MethodActivation:
    """Activate (or revert) a qualified method delta under explicit promotion grant."""
    if operation not in ("activate", "revert"):
        raise MethodRefused(f"Unsupported activation operation: {operation!r}")

    def _run(db: sqlite3.Connection) -> tuple[MethodActivation, str]:
        company_id = _identity(db)
        delta = _lookup_delta(db, company_id, delta_id)
        _verify_promotion_authority(
            db,
            company_id=company_id,
            actor_id=actor_id,
            grant_id=grant_id,
            audience_scope=delta.audience_scope,
            audience_target_id=delta.audience_target_id,
        )
        if request_id is not None:
            existing_act = db.execute(
                """SELECT request_id, operation, method_id, revision_sha256,
                          qualification_id, audience_scope, audience_target_id,
                          activation_seq, principal_id, grant_id, quarantined,
                          quarantine_reason, recorded_at_ns
                   FROM method_activation
                   WHERE company_id = ? AND request_id = ?""",
                (company_id, request_id),
            ).fetchone()
            if existing_act is not None:
                (
                    e_req,
                    e_op,
                    e_mid,
                    e_rev,
                    e_qid,
                    e_scope,
                    e_target,
                    e_seq,
                    e_actor,
                    e_grant,
                    e_quar,
                    e_qreason,
                    e_ts,
                ) = existing_act
                if (
                    str(e_op) != operation
                    or str(e_mid) != delta.method_id
                    or str(e_rev) != delta.revision_sha256
                    or str(e_actor) != actor_id
                    or str(e_grant) != grant_id
                    or (qualification_id is not None and str(e_qid) != qualification_id)
                ):
                    raise MethodRefused("Activation request identity conflict")
                cmd_row = db.execute(
                    """SELECT result_json FROM command_receipt
                       WHERE company_id = ? AND command_id = ?
                         AND command_type = ?""",
                    (company_id, request_id, f"method_learning.{operation}"),
                ).fetchone()
                snap_id_replayed = ""
                if cmd_row is not None and cmd_row[0]:
                    try:
                        parsed_res = json.loads(str(cmd_row[0]))
                        if isinstance(parsed_res, dict) and isinstance(
                            parsed_res.get("active_snapshot_id"), str
                        ):
                            snap_id_replayed = parsed_res["active_snapshot_id"]
                    except json.JSONDecodeError:
                        pass
                return (
                    MethodActivation(
                        activation_id=str(e_req),
                        request_id=str(e_req),
                        company_id=company_id,
                        operation=str(e_op),
                        method_id=str(e_mid),
                        revision_sha256=str(e_rev),
                        delta_id=f"{e_mid}:{e_rev}",
                        qualification_id=str(e_qid),
                        audience_scope=str(e_scope),
                        audience_target_id=None if e_target is None else str(e_target),
                        activation_seq=int(e_seq),
                        active_snapshot_id=snap_id_replayed,
                        principal_id=str(e_actor),
                        grant_id=str(e_grant),
                        quarantined=bool(e_quar),
                        quarantine_reason=None if e_qreason is None else str(e_qreason),
                        recorded_at_ns=int(e_ts),
                    ),
                    company_id,
                )
        if delta.status != "candidate":
            raise MethodRefused(
                f"Cannot activate method delta in '{delta.status}' status"
            )
        if (
            delta.audience_scope in ("project", "company")
            and not delta.redaction_passed
        ):
            raise MethodRefused("Shared method delta has not passed redaction gate")
        if _node_has_quarantined_parent_or_snapshot(db, company_id, delta):
            raise MethodRefused(
                "Candidate or its lineage ancestor/snapshot is quarantined"
            )
        quarantined_target = db.execute(
            """SELECT 1 FROM method_activation
               WHERE company_id = ? AND method_id = ?
                 AND audience_scope = ?
                 AND (audience_target_id IS ? OR audience_target_id = ?)
                 AND quarantined = 1
               LIMIT 1""",
            (
                company_id,
                delta.method_id,
                delta.audience_scope,
                delta.audience_target_id,
                delta.audience_target_id,
            ),
        ).fetchone()
        if quarantined_target is None:
            quarantined_target = db.execute(
                """SELECT 1 FROM method_delta
                   WHERE company_id = ? AND method_id = ?
                     AND audience_scope = ?
                     AND (audience_target_id IS ? OR audience_target_id = ?)
                     AND status = 'quarantined'
                   LIMIT 1""",
                (
                    company_id,
                    delta.method_id,
                    delta.audience_scope,
                    delta.audience_target_id,
                    delta.audience_target_id,
                ),
            ).fetchone()
        if quarantined_target is not None:
            raise MethodRefused(
                f"Method '{delta.method_id}' has an active quarantine and cannot be activated"
            )
        active_row = db.execute(
            """SELECT revision_sha256 FROM method_delta
               WHERE company_id = ? AND method_id = ?
                 AND audience_scope = ?
                 AND (audience_target_id IS ? OR audience_target_id = ?)
                 AND status = 'active'
               LIMIT 1""",
            (
                company_id,
                delta.method_id,
                delta.audience_scope,
                delta.audience_target_id,
                delta.audience_target_id,
            ),
        ).fetchone()
        if delta.delta_kind == "ADD":
            if active_row is not None:
                raise MethodRefused(
                    f"CAS conflict: method '{delta.method_id}' already has active revision "
                    f"'{active_row[0]}' (use PATCH or DEPRECATE)"
                )
        elif delta.delta_kind in ("PATCH", "DEPRECATE"):
            if active_row is None or str(active_row[0]) != delta.superseded_revision:
                raise MethodRefused(
                    f"CAS conflict: superseded_revision '{delta.superseded_revision}' "
                    f"does not match active revision '{active_row[0] if active_row is not None else None}'"
                )

        if qualification_id is not None:
            q_row = db.execute(
                """SELECT qualification_id, passed, stage1_passed, stage2_passed,
                          stage3_passed, evaluator_revision
                   FROM method_qualification
                   WHERE company_id = ? AND qualification_id = ?
                     AND method_id = ? AND revision_sha256 = ?""",
                (company_id, qualification_id, delta.method_id, delta.revision_sha256),
            ).fetchone()
        else:
            q_row = db.execute(
                """SELECT qualification_id, passed, stage1_passed, stage2_passed,
                          stage3_passed, evaluator_revision
                   FROM method_qualification
                   WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?
                   ORDER BY recorded_at_ns DESC, rowid DESC
                   LIMIT 1""",
                (company_id, delta.method_id, delta.revision_sha256),
            ).fetchone()
        if (
            q_row is None
            or not bool(q_row[1])
            or not bool(q_row[2])
            or not bool(q_row[3])
            or not bool(q_row[4])
        ):
            raise MethodRefused(
                "Method delta must pass all 3 pre-commit qualification stages before activation"
            )
        eff_qual_id = str(q_row[0])

        if delta.delta_kind != "DEPRECATE" and delta.audience_scope in (
            "project",
            "company",
        ):
            if delta.audience_scope == "project":
                if not delta.audience_target_id:
                    raise MethodRefused(
                        "Project-scoped delta requires audience_target_id"
                    )
                proj_row = db.execute(
                    "SELECT members FROM shared_project WHERE company_id = ? AND project_id = ?",
                    (company_id, delta.audience_target_id),
                ).fetchone()
                try:
                    raw_members = json.loads(str(proj_row[0])) if proj_row else []
                except json.JSONDecodeError:
                    raw_members = []
                target_agents = [
                    str(m)
                    for m in raw_members
                    if isinstance(m, str) and employment_cutoff(db, m) is None
                ]
            else:
                target_agents = [
                    str(r[0])
                    for r in db.execute(
                        "SELECT id FROM agent WHERE company_id = ?", (company_id,)
                    ).fetchall()
                    if employment_cutoff(db, str(r[0])) is None
                ]
            for target_agent in target_agents:
                if not _agent_authorized_for_delta_sources(
                    db, company_id, target_agent, delta
                ):
                    raise MethodRefused(
                        f"Audience member '{target_agent}' is not authorized to read all cited "
                        f"shared_publication sources for '{delta.method_id}'"
                    )

        req_id = request_id or f"mact-{uuid4().hex[:16]}"
        seq_row = db.execute(
            "SELECT COALESCE(MAX(activation_seq), 0) FROM method_activation WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        next_seq = int(seq_row[0]) + 1

        # Retire any prior active revision of this method in the same audience scope.
        db.execute(
            """UPDATE method_delta
               SET status = 'retired'
               WHERE company_id = ? AND method_id = ?
                 AND audience_scope = ?
                 AND (audience_target_id IS ? OR audience_target_id = ?)
                 AND status = 'active'""",
            (
                company_id,
                delta.method_id,
                delta.audience_scope,
                delta.audience_target_id,
                delta.audience_target_id,
            ),
        )
        new_status = "retired" if delta.delta_kind == "DEPRECATE" else "active"
        db.execute(
            """UPDATE method_delta
               SET status = ?
               WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
            (new_status, company_id, delta.method_id, delta.revision_sha256),
        )

        active_pairs = [
            (str(r[0]), str(r[1]))
            for r in db.execute(
                """SELECT method_id, revision_sha256
                   FROM method_delta
                   WHERE company_id = ? AND status = 'active'
                   ORDER BY method_id, revision_sha256""",
                (company_id,),
            ).fetchall()
        ]
        snap_id = (
            "snap-"
            + _sha256_json(
                {
                    "activation_seq": next_seq,
                    "active_pairs": active_pairs,
                    "method_id": delta.method_id,
                    "revision_sha256": delta.revision_sha256,
                }
            )[:24]
        )

        ts = max(1, time_ns() if now_ns is None else int(now_ns))
        payload_digest = _sha256_json(
            {
                "operation": operation,
                "method_id": delta.method_id,
                "revision_sha256": delta.revision_sha256,
                "qualification_id": eff_qual_id,
            }
        )
        preconditions = {
            "method_id": delta.method_id,
            "revision_sha256": delta.revision_sha256,
            "qualification_id": eff_qual_id,
        }
        snapshot_members = [
            [m, r]
            for m, r in sorted(
                {*active_pairs, (delta.method_id, delta.revision_sha256)}
            )
        ]
        content_snap_id = (
            "snap-" + _sha256_json(active_pairs)[:24] if active_pairs else None
        )
        receipt_result = {
            "activation_id": req_id,
            "operation": operation,
            "method_id": delta.method_id,
            "revision_sha256": delta.revision_sha256,
            "qualification_id": eff_qual_id,
            "audience_scope": delta.audience_scope,
            "audience_target_id": delta.audience_target_id,
            "activation_seq": next_seq,
            "active_snapshot_id": snap_id,
            "content_snapshot_id": content_snap_id,
            "snapshot_members": snapshot_members,
        }
        one_agent_snap_pairs = {
            (str(r[0]), str(r[1]))
            for r in db.execute(
                """SELECT method_id, revision_sha256 FROM method_delta
                   WHERE company_id = ? AND audience_scope = 'one_agent'""",
                (company_id,),
            ).fetchall()
        }
        audit_act_mid = (
            opaque_one_agent_method_id(delta.method_id)
            if delta.audience_scope == "one_agent"
            else delta.method_id
        )
        audit_act_delta_id = _format_delta_id(audit_act_mid, delta.revision_sha256)
        audit_act_payload = {
            **receipt_result,
            "method_id": audit_act_mid,
            "snapshot_members": [
                [
                    opaque_one_agent_method_id(m)
                    if (m, r) in one_agent_snap_pairs
                    else m,
                    r,
                ]
                for m, r in snapshot_members
            ],
        }
        event_seq = append_audit_event(
            db,
            company_id=company_id,
            domain="learning",
            entity_type="method_activation",
            entity_id=audit_act_delta_id,
            event_type=f"learning.method_{operation}d",
            actor_id=actor_id,
            command_id=req_id,
            payload=audit_act_payload,
            recorded_at_ns=ts,
        )
        try:
            _cmd, replayed = admit_command_receipt(
                db,
                company_id=company_id,
                command_id=req_id,
                actor_id=actor_id,
                peer_pid=None,
                command_type=f"method_learning.{operation}",
                contract_version=METHOD_CONTRACT_VERSION,
                payload_sha256=payload_digest,
                preconditions=preconditions,
                state="applied",
                result=receipt_result,
                applied_seq=event_seq,
                now_ns=ts,
            )
        except CommandConflict as exc:
            raise MethodRefused("Activation request identity conflict") from exc
        if replayed:
            raise MethodRefused("Activation request identity already in use")

        db.execute(
            """INSERT INTO method_activation (
                   company_id, request_id, operation, method_id, revision_sha256,
                   qualification_id, audience_scope, audience_target_id,
                   activation_seq, principal_id, grant_id, quarantined,
                   quarantine_reason, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)""",
            (
                company_id,
                req_id,
                operation,
                delta.method_id,
                delta.revision_sha256,
                eff_qual_id,
                delta.audience_scope,
                delta.audience_target_id,
                next_seq,
                actor_id,
                grant_id,
                ts,
            ),
        )
        return (
            MethodActivation(
                activation_id=req_id,
                request_id=req_id,
                company_id=company_id,
                operation=operation,
                method_id=delta.method_id,
                revision_sha256=delta.revision_sha256,
                delta_id=delta.delta_id,
                qualification_id=eff_qual_id,
                audience_scope=delta.audience_scope,
                audience_target_id=delta.audience_target_id,
                activation_seq=next_seq,
                active_snapshot_id=snap_id,
                principal_id=actor_id,
                grant_id=grant_id,
                quarantined=False,
                quarantine_reason=None,
                recorded_at_ns=ts,
            ),
            company_id,
        )

    if isinstance(writer_or_conn, WriterLease):
        writer_or_conn.verify()
        with (
            closing(open_writer(writer_or_conn.store, lease=writer_or_conn)) as db,
            write_tx(db),
        ):
            activation, cid = _run(db)
            writer_or_conn.verify()
        sync_all_company_desk_projections(writer_or_conn.store, cid)
        return activation
    if isinstance(writer_or_conn, sqlite3.Connection):
        activation, _ = _run(writer_or_conn)
        return activation
    raise MethodRefused("Expected WriterLease or sqlite3.Connection")


def quarantine_method_lineage(
    writer_or_conn: WriterLease | sqlite3.Connection,
    company_id: str,
    *,
    method_id: str,
    revision_sha256: str | None = None,
    reason: str,
    actor_id: str | None = None,
    grant_id: str | None = None,
    request_id: str | None = None,
    now_ns: int | None = None,
) -> MethodQuarantineReceipt:
    """BFS-quarantine a faulted method and all transitive descendants, restoring any clean incumbent."""
    clean_method_id = _require_safe_id(method_id, "method_id")
    clean_reason = _require_nonempty_str(reason, "reason", max_bytes=2048)

    def _run(db: sqlite3.Connection) -> MethodQuarantineReceipt:
        db_company_id = _identity(db)
        if db_company_id != company_id:
            raise MethodRefused("Company identity mismatch")

        if actor_id is None and grant_id is None:
            founder_row = db.execute(
                "SELECT founder_id, grant_id FROM founding_receipt WHERE company_id = ?",
                (company_id,),
            ).fetchone()
            if founder_row is None:
                raise MethodRefused(
                    "Company founding_receipt unavailable for quarantine"
                )
            eff_actor_id = str(founder_row[0])
            eff_grant_id = str(founder_row[1])
        else:
            if actor_id is None or grant_id is None:
                raise MethodRefused(
                    "Both actor_id and grant_id must be provided together"
                )
            eff_actor_id = actor_id
            eff_grant_id = grant_id

        if revision_sha256 is not None:
            target_row = db.execute(
                f"""SELECT {_DELTA_COLUMNS}
                    FROM method_delta
                    WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
                (company_id, clean_method_id, revision_sha256),
            ).fetchone()
        else:
            target_row = db.execute(
                f"""SELECT {_DELTA_COLUMNS}
                    FROM method_delta
                    WHERE company_id = ? AND method_id = ?
                    ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
                             created_at_ns DESC
                    LIMIT 1""",
                (company_id, clean_method_id),
            ).fetchone()
        if target_row is None:
            raise MethodRefused(f"Method '{clean_method_id}' not found for quarantine")
        target_delta = _row_to_delta(target_row)

        _verify_promotion_authority(
            db,
            company_id=company_id,
            actor_id=eff_actor_id,
            grant_id=eff_grant_id,
            audience_scope=target_delta.audience_scope,
            audience_target_id=target_delta.audience_target_id,
        )

        children_by_node, parents_by_node = _all_lineage_edges(db, company_id)
        start_nodes: list[tuple[str, str]] = [
            (target_delta.method_id, target_delta.revision_sha256)
        ]
        if revision_sha256 is None:
            for extra_rev in db.execute(
                """SELECT revision_sha256 FROM method_delta
                   WHERE company_id = ? AND method_id = ?
                     AND audience_scope = ?
                     AND (audience_target_id IS ? OR audience_target_id = ?)""",
                (
                    company_id,
                    clean_method_id,
                    target_delta.audience_scope,
                    target_delta.audience_target_id,
                    target_delta.audience_target_id,
                ),
            ).fetchall():
                start_nodes.append((clean_method_id, str(extra_rev[0])))

        visited: set[tuple[str, str]] = set(start_nodes)
        queue: deque[tuple[str, str]] = deque(start_nodes)
        while queue:
            cur = queue.popleft()
            for child in children_by_node.get(cur, ()):
                if child not in visited:
                    visited.add(child)
                    queue.append(child)

        quarantined_revisions = tuple(sorted(visited))
        quarantined_methods = tuple(sorted({m for m, _ in quarantined_revisions}))

        # Capture scopes of currently effective methods (active revisions OR currently
        # effective adopted DEPRECATE activations) being quarantined before status change.
        affected_scopes: list[tuple[str, str, str | None]] = []
        seen_affected_scopes: set[tuple[str, str, str | None]] = set()
        for q_mid, q_rev in quarantined_revisions:
            row = db.execute(
                """SELECT audience_scope, audience_target_id, status, delta_kind
                   FROM method_delta
                   WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
                (company_id, q_mid, q_rev),
            ).fetchone()
            if row is not None:
                r_scope = str(row[0])
                r_target = None if row[1] is None else str(row[1])
                r_status = str(row[2])
                r_dkind = str(row[3])
                is_effective = r_status == "active"
                if (
                    not is_effective
                    and r_dkind == "DEPRECATE"
                    and r_status == "retired"
                ):
                    latest_act = db.execute(
                        """SELECT method_id, revision_sha256
                           FROM method_activation
                           WHERE company_id = ?
                             AND method_id = ?
                             AND audience_scope = ?
                             AND (audience_target_id IS ? OR audience_target_id = ?)
                             AND quarantined = 0
                           ORDER BY activation_seq DESC
                           LIMIT 1""",
                        (company_id, q_mid, r_scope, r_target, r_target),
                    ).fetchone()
                    if latest_act is not None and (
                        str(latest_act[0]),
                        str(latest_act[1]),
                    ) == (q_mid, q_rev):
                        is_effective = True
                if is_effective:
                    scope_key = (q_mid, r_scope, r_target)
                    if scope_key not in seen_affected_scopes:
                        seen_affected_scopes.add(scope_key)
                        affected_scopes.append(scope_key)
            db.execute(
                """UPDATE method_delta
                   SET status = 'quarantined'
                   WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
                (company_id, q_mid, q_rev),
            )
            db.execute(
                """UPDATE method_activation
                   SET quarantined = 1, quarantine_reason = ?
                   WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
                (clean_reason, company_id, q_mid, q_rev),
            )

        all_q_nodes = _quarantined_nodes(db, company_id)
        restored_method_id: str | None = None
        restored_revision_sha256: str | None = None
        restored_qual_id: str | None = None
        restored_scope: str | None = None
        restored_target: str | None = None

        for inc_mid, inc_scope, inc_target in affected_scopes:
            prior_rows = db.execute(
                """SELECT a.method_id, a.revision_sha256, a.qualification_id,
                          d.delta_kind, d.status, q.passed, q.evaluator_revision
                   FROM method_activation a
                   JOIN method_delta d
                     ON d.company_id = a.company_id
                    AND d.method_id = a.method_id
                    AND d.revision_sha256 = a.revision_sha256
                   JOIN method_qualification q
                     ON q.company_id = a.company_id
                    AND q.qualification_id = a.qualification_id
                   WHERE a.company_id = ?
                     AND a.method_id = ?
                     AND a.audience_scope = ?
                     AND (a.audience_target_id IS ? OR a.audience_target_id = ?)
                     AND a.quarantined = 0
                   ORDER BY a.activation_seq DESC""",
                (company_id, inc_mid, inc_scope, inc_target, inc_target),
            ).fetchall()
            for p_mid, p_rev, p_qid, p_dkind, p_status, p_passed, _ in prior_rows:
                node = (str(p_mid), str(p_rev))
                if (
                    str(p_status) == "quarantined"
                    or not bool(p_passed)
                    or _has_quarantined_ancestor(
                        db,
                        company_id,
                        node,
                        quarantined=all_q_nodes,
                        parents_by_node=parents_by_node,
                    )
                ):
                    continue
                if str(p_dkind) == "DEPRECATE":
                    # Clean deprecation is a valid incumbent state (intentionally removed);
                    # stop restoration here instead of resurrecting an older pre-deprecation ADD.
                    break
                db.execute(
                    """UPDATE method_delta
                       SET status = 'active'
                       WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
                    (company_id, str(p_mid), str(p_rev)),
                )
                if restored_method_id is None:
                    restored_method_id = str(p_mid)
                    restored_revision_sha256 = str(p_rev)
                    restored_qual_id = str(p_qid)
                    restored_scope = inc_scope
                    restored_target = inc_target
                break

        ts = max(1, time_ns() if now_ns is None else int(now_ns))
        req_id = request_id or f"mquar-{uuid4().hex[:16]}"
        seq_row = db.execute(
            "SELECT COALESCE(MAX(activation_seq), 0) FROM method_activation WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        next_seq = int(seq_row[0]) + 1

        active_pairs = [
            (str(r[0]), str(r[1]))
            for r in db.execute(
                """SELECT method_id, revision_sha256
                   FROM method_delta
                   WHERE company_id = ? AND status = 'active'
                   ORDER BY method_id, revision_sha256""",
                (company_id,),
            ).fetchall()
        ]
        snap_id = (
            "snap-"
            + _sha256_json(
                {
                    "activation_seq": next_seq,
                    "active_pairs": active_pairs,
                    "quarantine": clean_method_id,
                }
            )[:24]
            if active_pairs
            else None
        )

        payload_digest = _sha256_json(
            {
                "operation": "quarantine",
                "method_id": target_delta.method_id,
                "revision_sha256": target_delta.revision_sha256,
                "reason": clean_reason,
            }
        )
        preconditions = {
            "method_id": target_delta.method_id,
            "revision_sha256": target_delta.revision_sha256,
        }
        content_snap_id = (
            "snap-" + _sha256_json(active_pairs)[:24] if active_pairs else None
        )
        snapshot_members = [[m, r] for m, r in active_pairs]
        receipt_payload = {
            "request_id": req_id,
            "faulted_method_id": target_delta.method_id,
            "faulted_revision_sha256": target_delta.revision_sha256,
            "quarantined_methods": list(quarantined_methods),
            "quarantined_revisions": [list(pair) for pair in quarantined_revisions],
            "restored_method_id": restored_method_id,
            "restored_revision_sha256": restored_revision_sha256,
            "active_snapshot_id": snap_id,
            "content_snapshot_id": content_snap_id,
            "snapshot_members": snapshot_members,
            "reason": clean_reason,
        }
        one_agent_q_pairs = {
            (str(r[0]), str(r[1]))
            for r in db.execute(
                """SELECT method_id, revision_sha256 FROM method_delta
                   WHERE company_id = ? AND audience_scope = 'one_agent'""",
                (company_id,),
            ).fetchall()
        }
        one_agent_q_mids = {m for m, _r in one_agent_q_pairs}
        audit_q_mid = (
            opaque_one_agent_method_id(target_delta.method_id)
            if target_delta.audience_scope == "one_agent"
            else target_delta.method_id
        )
        audit_q_delta_id = _format_delta_id(audit_q_mid, target_delta.revision_sha256)
        audit_q_payload = {
            **receipt_payload,
            "faulted_method_id": audit_q_mid,
            "quarantined_methods": [
                opaque_one_agent_method_id(m)
                if target_delta.audience_scope == "one_agent" or m in one_agent_q_mids
                else m
                for m in quarantined_methods
            ],
            "quarantined_revisions": [
                [
                    opaque_one_agent_method_id(m) if (m, r) in one_agent_q_pairs else m,
                    r,
                ]
                for m, r in quarantined_revisions
            ],
            "restored_method_id": (
                None
                if restored_method_id is None
                else (
                    opaque_one_agent_method_id(restored_method_id)
                    if (restored_method_id, restored_revision_sha256 or "")
                    in one_agent_q_pairs
                    else restored_method_id
                )
            ),
            "snapshot_members": [
                [
                    opaque_one_agent_method_id(m) if (m, r) in one_agent_q_pairs else m,
                    r,
                ]
                for m, r in snapshot_members
            ],
        }
        event_seq = append_audit_event(
            db,
            company_id=company_id,
            domain="learning",
            entity_type="method_activation",
            entity_id=audit_q_delta_id,
            event_type="learning.method_lineage_quarantined",
            actor_id=eff_actor_id,
            command_id=req_id,
            payload=audit_q_payload,
            recorded_at_ns=ts,
        )
        try:
            _cmd, replayed = admit_command_receipt(
                db,
                company_id=company_id,
                command_id=req_id,
                actor_id=eff_actor_id,
                peer_pid=None,
                command_type="method_learning.quarantine",
                contract_version=METHOD_CONTRACT_VERSION,
                payload_sha256=payload_digest,
                preconditions=preconditions,
                state="applied",
                result=receipt_payload,
                applied_seq=event_seq,
                now_ns=ts,
            )
        except CommandConflict as exc:
            raise MethodRefused("Quarantine request identity conflict") from exc
        if replayed:
            raise MethodRefused("Quarantine request identity already in use")

        if (
            restored_method_id is not None
            and restored_revision_sha256 is not None
            and restored_qual_id is not None
            and restored_scope is not None
        ):
            db.execute(
                """INSERT INTO method_activation (
                       company_id, request_id, operation, method_id, revision_sha256,
                       qualification_id, audience_scope, audience_target_id,
                       activation_seq, principal_id, grant_id, quarantined,
                       quarantine_reason, recorded_at_ns
                   ) VALUES (?, ?, 'quarantine', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (
                    company_id,
                    req_id,
                    restored_method_id,
                    restored_revision_sha256,
                    restored_qual_id,
                    restored_scope,
                    restored_target,
                    next_seq,
                    eff_actor_id,
                    eff_grant_id,
                    clean_reason,
                    ts,
                ),
            )

        return MethodQuarantineReceipt(
            request_id=req_id,
            company_id=company_id,
            faulted_method_id=target_delta.method_id,
            faulted_revision_sha256=target_delta.revision_sha256,
            quarantined_methods=quarantined_methods,
            quarantined_revisions=quarantined_revisions,
            restored_method_id=restored_method_id,
            restored_revision_sha256=restored_revision_sha256,
            active_snapshot_id=snap_id,
            reason=clean_reason,
            principal_id=eff_actor_id,
            grant_id=eff_grant_id,
        )

    if isinstance(writer_or_conn, WriterLease):
        writer_or_conn.verify()
        with (
            closing(open_writer(writer_or_conn.store, lease=writer_or_conn)) as db,
            write_tx(db),
        ):
            receipt = _run(db)
            writer_or_conn.verify()
        sync_all_company_desk_projections(writer_or_conn.store, company_id)
        return receipt
    if isinstance(writer_or_conn, sqlite3.Connection):
        return _run(writer_or_conn)
    raise MethodRefused("Expected WriterLease or sqlite3.Connection")
