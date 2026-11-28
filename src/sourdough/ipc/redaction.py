"""Board Client privacy and redaction helpers for Sourdough projections and event streams.

All helpers in this module are strictly read-only and ensure Agent-private Desk
drafts, free-form execution prose, arbitrary condition labels, and one_agent
method identifiers/lineage never cross the Board visibility boundary.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from ..method_learning import (
    opaque_one_agent_delta_id,
    opaque_one_agent_method_id,
)
from ..runtime.work_kernel import WORK_DECISION_ACTION_KINDS

AUDIT_ONE_AGENT_ENTITY_TYPES: frozenset[str] = frozenset(
    {"method_delta", "method_activation", "method_qualification"}
)

_OPAQUE_TIMER_SOURCE_RE = re.compile(r"^(?:timer|result_dep):[0-9a-f]{12,64}$")


def _parse_json_safe(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _redact_one_agent_ref_for_board(
    ref: str,
    *,
    force_opaque: bool,
    one_agent_deltas: set[tuple[str, str]],
    one_agent_mids: set[str],
) -> str:
    """Opaque-map a method/delta reference when it belongs to a one_agent method."""
    if ":" in ref:
        mid, rev = ref.rsplit(":", 1)
        if force_opaque or (mid, rev) in one_agent_deltas or mid in one_agent_mids:
            return opaque_one_agent_delta_id(mid, rev)
        return ref
    if force_opaque or ref in one_agent_mids:
        return opaque_one_agent_method_id(ref)
    return ref


def redact_audit_entity_id_for_board(
    entity_type: str,
    entity_id: str,
    *,
    one_agent_deltas: set[tuple[str, str]],
    one_agent_mids: set[str],
) -> str:
    """Opaque-map one_agent method/delta entity_ids in Board-facing audit event streams."""
    if entity_type not in AUDIT_ONE_AGENT_ENTITY_TYPES:
        return entity_id
    return _redact_one_agent_ref_for_board(
        entity_id,
        force_opaque=False,
        one_agent_deltas=one_agent_deltas,
        one_agent_mids=one_agent_mids,
    )


def load_one_agent_method_sets(
    db: sqlite3.Connection, company_id: str
) -> tuple[set[tuple[str, str]], set[str]]:
    """Return (one_agent_deltas, one_agent_only_method_ids) for Board projection redaction."""
    one_agent_deltas: set[tuple[str, str]] = set()
    one_agent_mids: set[str] = set()
    shared_mids: set[str] = set()
    for mid, rev, scope in db.execute(
        """SELECT method_id, revision_sha256, audience_scope
           FROM method_delta
           WHERE company_id = ?""",
        (company_id,),
    ).fetchall():
        mid_s, rev_s, scope_s = str(mid), str(rev), str(scope)
        if scope_s == "one_agent":
            one_agent_deltas.add((mid_s, rev_s))
            one_agent_mids.add(mid_s)
        else:
            shared_mids.add(mid_s)
    return one_agent_deltas, (one_agent_mids - shared_mids)


_load_one_agent_method_sets = load_one_agent_method_sets


def _redact_active_method_for_board(method_dict: dict[str, Any]) -> dict[str, Any]:
    """Keep Agent-only (one_agent) method identifiers, bodies, lineage, and notes out of Board projections."""
    projected = dict(method_dict)
    if str(projected.get("audience_scope", "")) != "one_agent":
        return projected
    raw_mid = str(projected.get("method_id", "") or "")
    raw_rev = str(projected.get("revision_sha256", "") or "")
    opaque_mid = opaque_one_agent_method_id(raw_mid)
    projected["method_id"] = opaque_mid
    projected["method_id_redacted"] = True
    if "delta_id" in projected:
        projected["delta_id"] = (
            opaque_one_agent_delta_id(raw_mid, raw_rev) if raw_rev else opaque_mid
        )
    if isinstance(projected.get("parent_method_ids"), (list, tuple)):
        projected["parent_method_ids"] = [
            _redact_one_agent_ref_for_board(
                str(p),
                force_opaque=True,
                one_agent_deltas=set(),
                one_agent_mids=set(),
            )
            for p in projected["parent_method_ids"]
        ]
    raw_body = str(projected.get("body_text", "") or "")
    projected["body_text"] = None
    projected["body_redacted"] = True
    projected["body_digest"] = sha256(raw_body.encode("utf-8")).hexdigest()
    if "uncertainty_note" in projected:
        projected["uncertainty_note"] = None
    if "falsification_criteria" in projected:
        projected["falsification_criteria"] = None
    if "trigger_spec" in projected:
        projected["trigger_spec"] = {"redacted": True}
    if "procedure_meta" in projected:
        projected["procedure_meta"] = None
    return projected


def _sanitize_action_kind_for_board(action_kind: Any) -> str:
    """Allow only server-defined decision action kinds in Board projections; redact legacy values."""
    val = str(action_kind or "")
    return val if val in WORK_DECISION_ACTION_KINDS else "redacted"


def _redact_condition_source_for_board(
    db: sqlite3.Connection,
    company_id: str,
    trigger_kind: str | None,
    source_identity: str | None,
) -> str | None:
    """Keep arbitrary Agent-supplied condition labels out of Board projections."""
    if source_identity is None:
        return None
    raw = str(source_identity)
    if _OPAQUE_TIMER_SOURCE_RE.fullmatch(raw):
        return raw
    if trigger_kind == "shared_asset_revision":
        pub = db.execute(
            "SELECT 1 FROM shared_publication WHERE company_id = ? AND asset_id = ? LIMIT 1",
            (company_id, raw),
        ).fetchone()
        if pub is not None:
            return raw
    elif trigger_kind == "peer_response":
        req_id = raw.removeprefix("peer_response:")
        req = db.execute(
            "SELECT 1 FROM work_peer_requests WHERE company_id = ? AND request_id = ? LIMIT 1",
            (company_id, req_id),
        ).fetchone()
        if req is not None:
            return raw
    return f"redacted:{sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _redact_work_item_payload_for_board(
    db: sqlite3.Connection, company_id: str, raw_json: str
) -> dict[str, Any]:
    """Sanitize condition/trigger source identities inside projected work_item payloads."""
    parsed = _parse_json_safe(raw_json, {})
    if not isinstance(parsed, dict):
        return {}
    projected = dict(parsed)
    trig_kind = (
        str(projected.get("trigger_kind"))
        if projected.get("trigger_kind") is not None
        else None
    )
    if isinstance(projected.get("source_identity"), str):
        projected["source_identity"] = _redact_condition_source_for_board(
            db, company_id, trig_kind, projected["source_identity"]
        )
    if isinstance(projected.get("coalesced_revisions"), list):
        safe_coal: list[Any] = []
        for entry in projected["coalesced_revisions"]:
            if isinstance(entry, Mapping):
                e_copy = dict(entry)
                e_trig = (
                    str(e_copy.get("trigger_kind"))
                    if e_copy.get("trigger_kind") is not None
                    else trig_kind
                )
                if isinstance(e_copy.get("source_identity"), str):
                    e_copy["source_identity"] = _redact_condition_source_for_board(
                        db, company_id, e_trig, e_copy["source_identity"]
                    )
                safe_coal.append(e_copy)
        projected["coalesced_revisions"] = safe_coal
    return projected


def _redact_run_outcome_for_board(
    raw_json: str | None,
    *,
    one_agent_deltas: set[tuple[str, str]] | None = None,
    one_agent_mids: set[str] | None = None,
) -> Any:
    """Redact free-form prose, non-enum action_kind, and one_agent method/delta IDs from work_runs outcomes."""
    parsed = _parse_json_safe(raw_json, None)
    if not isinstance(parsed, dict):
        return parsed
    oa_deltas = one_agent_deltas or set()
    oa_mids = one_agent_mids or set()
    projected = dict(parsed)
    if "output_text" in projected:
        raw_out = projected.pop("output_text", None)
        projected["output_text"] = None
        projected["output_redacted"] = True
        if raw_out is not None and "output_digest" not in projected:
            projected["output_digest"] = sha256(
                str(raw_out).encode("utf-8")
            ).hexdigest()
    if "action_kind" in projected and projected["action_kind"] is not None:
        raw_act = str(projected["action_kind"])
        safe_act = _sanitize_action_kind_for_board(raw_act)
        projected["action_kind"] = safe_act
        if safe_act != raw_act:
            projected["action_kind_redacted"] = True
    raw_trace = projected.get("causal_method_trace")
    if isinstance(raw_trace, Mapping) and (oa_deltas or oa_mids):
        safe_trace = dict(raw_trace)
        for key in ("available", "retrieved", "invoked"):
            vals = safe_trace.get(key)
            if isinstance(vals, list):
                safe_trace[key] = [
                    _redact_one_agent_ref_for_board(
                        str(v),
                        force_opaque=False,
                        one_agent_deltas=oa_deltas,
                        one_agent_mids=oa_mids,
                    )
                    for v in vals
                ]
        projected["causal_method_trace"] = safe_trace
    oa_any_mids = oa_mids | {mid for mid, _ in oa_deltas}
    raw_proposals = projected.get("proposals")
    if isinstance(raw_proposals, list):
        safe_proposals: list[Any] = []
        for entry in raw_proposals:
            if isinstance(entry, str):
                safe_proposals.append(
                    _redact_one_agent_ref_for_board(
                        entry,
                        force_opaque=False,
                        one_agent_deltas=oa_deltas,
                        one_agent_mids=oa_any_mids,
                    )
                )
                continue
            if not isinstance(entry, Mapping):
                safe_proposals.append(entry)
                continue
            item = dict(entry)
            raw_mid = str(item.get("method_id") or "")
            raw_rev = str(item.get("revision_sha256") or "")
            raw_did = str(item.get("delta_id") or "")
            explicit_scope = str(item.get("audience_scope") or "")
            is_oa = (
                explicit_scope == "one_agent"
                or (raw_mid, raw_rev) in oa_deltas
                or raw_mid in oa_mids
                or (not explicit_scope and not raw_rev and raw_mid in oa_any_mids)
                or (":" in raw_did and tuple(raw_did.rsplit(":", 1)) in oa_deltas)
            )
            if is_oa:
                if raw_mid:
                    item["method_id"] = opaque_one_agent_method_id(raw_mid)
                if raw_did:
                    item["delta_id"] = (
                        opaque_one_agent_delta_id(raw_mid, raw_rev)
                        if raw_mid and raw_rev
                        else _redact_one_agent_ref_for_board(
                            raw_did,
                            force_opaque=True,
                            one_agent_deltas=oa_deltas,
                            one_agent_mids=oa_any_mids,
                        )
                    )
                for secret_key in ("body", "body_text", "trigger_spec"):
                    if secret_key in item:
                        item[secret_key] = None
                item["method_id_redacted"] = True
            safe_proposals.append(item)
        projected["proposals"] = safe_proposals
    raw_patterns = projected.get("patterns")
    if isinstance(raw_patterns, list):
        safe_patterns: list[Any] = []
        for entry in raw_patterns:
            if isinstance(entry, str):
                safe_patterns.append(
                    _redact_one_agent_ref_for_board(
                        entry,
                        force_opaque=False,
                        one_agent_deltas=oa_deltas,
                        one_agent_mids=oa_any_mids,
                    )
                )
                continue
            if not isinstance(entry, Mapping):
                safe_patterns.append(entry)
                continue
            item = dict(entry)
            raw_mid = str(item.get("method_id") or "")
            if raw_mid and (
                str(item.get("audience_scope") or "") == "one_agent"
                or raw_mid in oa_any_mids
            ):
                item["method_id"] = opaque_one_agent_method_id(raw_mid)
                item["method_id_redacted"] = True
            safe_patterns.append(item)
        projected["patterns"] = safe_patterns
    return projected


def _redact_evidence_refs_for_board(raw_json: str) -> list[Any]:
    parsed = _parse_json_safe(raw_json, [])
    if not isinstance(parsed, list):
        return []
    redacted: list[Any] = []
    for entry in parsed:
        if isinstance(entry, Mapping) and str(entry.get("kind", "")) == "private_draft":
            item = dict(entry)
            raw_res = str(item.get("resource_id", ""))
            if not raw_res.startswith("private_draft:opaque:"):
                item["resource_id"] = (
                    f"private_draft:{sha256(raw_res.encode('utf-8')).hexdigest()[:16]}"
                )
            redacted.append(item)
        else:
            redacted.append(entry)
    return redacted


__all__ = [
    "AUDIT_ONE_AGENT_ENTITY_TYPES",
    "_OPAQUE_TIMER_SOURCE_RE",
    "_load_one_agent_method_sets",
    "_redact_active_method_for_board",
    "_redact_condition_source_for_board",
    "_redact_evidence_refs_for_board",
    "_redact_one_agent_ref_for_board",
    "_redact_run_outcome_for_board",
    "_redact_work_item_payload_for_board",
    "_sanitize_action_kind_for_board",
    "load_one_agent_method_sets",
    "redact_audit_entity_id_for_board",
]
