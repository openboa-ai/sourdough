"""Evidence-backed work decisions, core evidence validators, and replay verification."""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ...storage.kernel import append_audit_event
from .conditions import (
    _arm_next_condition,
    _normalize_next_condition_work_item_id,
    validate_next_condition_dict,
)
from .responsibilities import record_discovery_hysteresis
from .runs import assert_run_fencing
from .types import (
    WORK_DECISION_ACTION_KINDS,
    DecisionEvidence,
    RunFenced,
    WorkDecision,
    WorkKernelRefused,
    _current_time_ns as time_ns,
    _canonical_json,
    _require_nonempty,
    has_active_grant,
    opaque_decision_action_id,
)


def _normalize_evidence(
    evidence: Sequence[DecisionEvidence | Mapping[str, Any]],
) -> tuple[DecisionEvidence, ...]:
    normalized: list[DecisionEvidence] = []
    for entry in evidence:
        if isinstance(entry, DecisionEvidence):
            ev = entry
        elif isinstance(entry, Mapping):
            allowed = {
                "kind",
                "resource_id",
                "revision",
                "observed_at_ns",
                "available_at_ns",
            }
            keys = set(entry.keys())
            if not allowed <= keys or not keys <= allowed:
                raise WorkKernelRefused("DecisionEvidence dict does not match schema")
            ev = DecisionEvidence(
                kind=entry["kind"],
                resource_id=entry["resource_id"],
                revision=entry["revision"],
                observed_at_ns=entry["observed_at_ns"],
                available_at_ns=entry["available_at_ns"],
            )
        else:
            raise WorkKernelRefused("DecisionEvidence entry has invalid type")
        _require_nonempty(ev.kind, "evidence.kind")
        _require_nonempty(ev.resource_id, "evidence.resource_id")
        if not isinstance(ev.revision, str) or not ev.revision.strip():
            raise WorkKernelRefused("evidence.revision must be a nonempty string")
        if type(ev.observed_at_ns) is not int or type(ev.available_at_ns) is not int:
            raise WorkKernelRefused("evidence timestamps must be integers")
        if ev.available_at_ns < 0 or ev.observed_at_ns < 0:
            raise WorkKernelRefused("evidence timestamps must be nonnegative")
        normalized.append(ev)
    return tuple(normalized)


def work_run_evidence_revision(
    run_id: str,
    *,
    company_id: str,
    work_item_id: str,
    agent_id: str,
    duty_kind: str,
    fencing_token: int,
    created_at_ns: int,
) -> str:
    """Compute canonical immutable revision digest for a work_run evidence citation."""
    return sha256(
        _canonical_json(
            {
                "kind": "work_run",
                "company_id": company_id,
                "run_id": run_id,
                "work_item_id": work_item_id,
                "agent_id": agent_id,
                "duty_kind": duty_kind,
                "fencing_token": int(fencing_token),
                "created_at_ns": int(created_at_ns),
            }
        ).encode("utf-8")
    ).hexdigest()


def work_item_evidence_revision(
    work_item_id: str,
    *,
    company_id: str,
    owner_agent_id: str,
    duty_kind: str,
    source_kind: str,
    source_id: str,
    payload_json: str,
    created_at_ns: int,
) -> str:
    """Compute canonical immutable revision digest for a work_item evidence citation."""
    return sha256(
        _canonical_json(
            {
                "kind": "work_item",
                "company_id": company_id,
                "work_item_id": work_item_id,
                "owner_agent_id": owner_agent_id,
                "duty_kind": duty_kind,
                "source_kind": source_kind,
                "source_id": source_id,
                "payload_json": payload_json,
                "created_at_ns": int(created_at_ns),
            }
        ).encode("utf-8")
    ).hexdigest()


def peer_request_evidence_revision(
    request_id: str,
    *,
    company_id: str,
    requester_agent_id: str,
    recipient_agent_id: str,
    state: str,
    question: str,
    expected_decision: str,
    response_json: str | None,
    created_at_ns: int,
) -> str:
    """Compute canonical revision digest for a peer_request evidence citation."""
    return sha256(
        _canonical_json(
            {
                "kind": "peer_request",
                "company_id": company_id,
                "request_id": request_id,
                "requester_agent_id": requester_agent_id,
                "recipient_agent_id": recipient_agent_id,
                "state": state,
                "question": question,
                "expected_decision": expected_decision,
                "response_json": response_json,
                "created_at_ns": int(created_at_ns),
            }
        ).encode("utf-8")
    ).hexdigest()


class _CoreEvidenceValidator(Protocol):
    def __call__(
        self,
        connection: sqlite3.Connection,
        *,
        company_id: str,
        agent_id: str,
        decision_at_ns: int,
        ev: DecisionEvidence,
        current_run_id: str | None = None,
    ) -> None: ...


def _validate_private_draft_evidence(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    decision_at_ns: int,
    ev: DecisionEvidence,
    current_run_id: str | None = None,
) -> None:
    del company_id, decision_at_ns, current_run_id
    if "/" in ev.resource_id:
        owner_prefix, draft_name = ev.resource_id.split("/", 1)
        if owner_prefix != agent_id:
            raise WorkKernelRefused("Cannot cite another Agent's private draft")
    elif ":" in ev.resource_id:
        owner_prefix, draft_name = ev.resource_id.split(":", 1)
        if owner_prefix != agent_id:
            raise WorkKernelRefused("Cannot cite another Agent's private draft")
    else:
        draft_name = ev.resource_id
    if not draft_name or "/" in draft_name or ":" in draft_name:
        raise WorkKernelRefused("Invalid private_draft resource_id")
    desk_row = connection.execute(
        "SELECT 1 FROM private_desk WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if desk_row is None:
        raise WorkKernelRefused("Agent has no private Desk for private_draft evidence")
    db_file = ""
    for _seq, db_name, file_path in connection.execute(
        "PRAGMA database_list"
    ).fetchall():
        if db_name == "main" and file_path:
            db_file = str(file_path)
            break
    if not db_file:
        raise WorkKernelRefused(
            "Cannot validate private_draft without a file-backed company store"
        )
    from ...agent_desk import PRODUCTION_DESK_LIMITS, draft_at_revision
    from ...duty_run import DutyRefused

    try:
        resolved = draft_at_revision(
            Path(db_file),
            agent_id,
            draft_name,
            ev.revision,
            limits=PRODUCTION_DESK_LIMITS,
        )
    except DutyRefused as error:
        raise WorkKernelRefused(str(error)) from error
    if resolved is None:
        raise WorkKernelRefused(
            "Cited private_draft does not exist or revision does not match"
        )


def _validate_shared_publication_evidence(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    decision_at_ns: int,
    ev: DecisionEvidence,
    current_run_id: str | None = None,
) -> None:
    del current_run_id
    pub_row = connection.execute(
        """SELECT company_id, project_id, audience, content
           FROM shared_publication
           WHERE asset_id = ?""",
        (ev.resource_id,),
    ).fetchone()
    if pub_row is None:
        raise WorkKernelRefused("Cited shared_publication does not exist")
    pub_company, pub_project, audience_json, content = pub_row
    if str(pub_company) != company_id:
        raise WorkKernelRefused("Cited shared_publication belongs to another company")
    try:
        audience = set(json.loads(str(audience_json)))
    except json.JSONDecodeError:
        audience = set()
    proj_row = connection.execute(
        "SELECT members FROM shared_project WHERE company_id = ? AND project_id = ?",
        (company_id, str(pub_project)),
    ).fetchone()
    try:
        members = set(json.loads(str(proj_row[0]))) if proj_row else set()
    except json.JSONDecodeError:
        members = set()
    if agent_id not in audience or agent_id not in members:
        raise WorkKernelRefused("Agent is not authorized to cite shared_publication")
    actual_rev = sha256(str(content).encode("utf-8")).hexdigest()
    if ev.revision != actual_rev:
        raise WorkKernelRefused(
            "Cited shared_publication revision does not match committed content"
        )
    pub_audit = connection.execute(
        """SELECT MIN(recorded_at_ns)
           FROM audit_event
           WHERE company_id = ?
             AND event_type IN ('company.asset_published', 'asset.published')
             AND (entity_id = ? OR json_extract(payload_json, '$.asset_id') = ?)""",
        (company_id, ev.resource_id, ev.resource_id),
    ).fetchone()
    if pub_audit is not None and pub_audit[0] is not None:
        pub_recorded_at_ns = int(pub_audit[0])
        if (
            ev.available_at_ns < pub_recorded_at_ns
            or pub_recorded_at_ns > decision_at_ns
        ):
            raise WorkKernelRefused(
                "Cited shared_publication timestamps precede publication commit or follow decision"
            )


def _validate_peer_request_evidence(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    decision_at_ns: int,
    ev: DecisionEvidence,
    current_run_id: str | None = None,
) -> None:
    del current_run_id
    req_row = connection.execute(
        """SELECT company_id, requester_agent_id, recipient_agent_id,
                  state, question, expected_decision, response_json,
                  created_at_ns, resolved_at_ns
           FROM work_peer_requests WHERE request_id = ?""",
        (ev.resource_id,),
    ).fetchone()
    if req_row is None:
        raise WorkKernelRefused("Cited peer_request does not exist")
    if str(req_row[0]) != company_id or agent_id not in (
        str(req_row[1]),
        str(req_row[2]),
    ):
        raise WorkKernelRefused("Agent is not authorized to cite peer_request")
    expected_peer_rev = peer_request_evidence_revision(
        ev.resource_id,
        company_id=str(req_row[0]),
        requester_agent_id=str(req_row[1]),
        recipient_agent_id=str(req_row[2]),
        state=str(req_row[3]),
        question=str(req_row[4]),
        expected_decision=str(req_row[5]),
        response_json=None if req_row[6] is None else str(req_row[6]),
        created_at_ns=int(req_row[7]),
    )
    if ev.revision != expected_peer_rev:
        raise WorkKernelRefused(
            "Cited peer_request revision does not match canonical request state"
        )
    min_avail = max(
        int(req_row[7]),
        int(req_row[8]) if req_row[8] is not None else 0,
    )
    if ev.available_at_ns < min_avail or min_avail > decision_at_ns:
        raise WorkKernelRefused(
            "Cited peer_request timestamps precede request creation/resolution or follow decision"
        )


def _validate_work_run_evidence(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    decision_at_ns: int,
    ev: DecisionEvidence,
    current_run_id: str | None = None,
) -> None:
    del current_run_id
    run_row = connection.execute(
        """SELECT company_id, work_item_id, agent_id, duty_kind,
                  fencing_token, created_at_ns
           FROM work_runs WHERE run_id = ?""",
        (ev.resource_id,),
    ).fetchone()
    if run_row is None:
        raise WorkKernelRefused("Cited work_run does not exist")
    if str(run_row[0]) != company_id or str(run_row[2]) != agent_id:
        raise WorkKernelRefused("Agent is not authorized to cite work_run")
    expected_run_rev = work_run_evidence_revision(
        ev.resource_id,
        company_id=str(run_row[0]),
        work_item_id=str(run_row[1]),
        agent_id=str(run_row[2]),
        duty_kind=str(run_row[3]),
        fencing_token=int(run_row[4]),
        created_at_ns=int(run_row[5]),
    )
    if ev.revision != expected_run_rev:
        raise WorkKernelRefused(
            "Cited work_run revision does not match canonical run identity"
        )
    if ev.available_at_ns < int(run_row[5]) or int(run_row[5]) > decision_at_ns:
        raise WorkKernelRefused(
            "Cited work_run timestamps precede run creation or follow decision"
        )


def _validate_work_item_evidence(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    decision_at_ns: int,
    ev: DecisionEvidence,
    current_run_id: str | None = None,
) -> None:
    del current_run_id
    item_row = connection.execute(
        """SELECT company_id, owner_agent_id, duty_kind, source_kind,
                  source_id, payload_json, created_at_ns
           FROM work_items WHERE work_item_id = ?""",
        (ev.resource_id,),
    ).fetchone()
    if item_row is None:
        raise WorkKernelRefused("Cited work_item does not exist")
    if str(item_row[0]) != company_id or str(item_row[1]) != agent_id:
        raise WorkKernelRefused("Agent is not authorized to cite work_item")
    expected_item_rev = work_item_evidence_revision(
        ev.resource_id,
        company_id=str(item_row[0]),
        owner_agent_id=str(item_row[1]),
        duty_kind=str(item_row[2]),
        source_kind=str(item_row[3]),
        source_id=str(item_row[4]),
        payload_json=str(item_row[5]),
        created_at_ns=int(item_row[6]),
    )
    if ev.revision != expected_item_rev:
        raise WorkKernelRefused(
            "Cited work_item revision does not match canonical item state"
        )
    if ev.available_at_ns < int(item_row[6]) or int(item_row[6]) > decision_at_ns:
        raise WorkKernelRefused(
            "Cited work_item timestamps precede item creation or follow decision"
        )


def _validate_tool_call_receipt_evidence(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    decision_at_ns: int,
    ev: DecisionEvidence,
    current_run_id: str | None = None,
) -> None:
    target_run_id: str | None = None
    target_call_id = ev.resource_id
    if "/" in ev.resource_id:
        target_run_id, target_call_id = ev.resource_id.split("/", 1)
    elif ":" in ev.resource_id:
        target_run_id, target_call_id = ev.resource_id.split(":", 1)
    if not target_call_id or "/" in target_call_id or ":" in target_call_id:
        raise WorkKernelRefused("Invalid tool_call_receipt resource_id")
    if target_run_id is not None:
        rec_rows = connection.execute(
            """SELECT run_id, result_digest, recorded_at_ns
               FROM tool_call_receipt
               WHERE company_id = ? AND agent_id = ? AND run_id = ? AND call_id = ?""",
            (company_id, agent_id, target_run_id, target_call_id),
        ).fetchall()
    else:
        rec_rows = connection.execute(
            """SELECT run_id, result_digest, recorded_at_ns
               FROM tool_call_receipt
               WHERE company_id = ? AND agent_id = ? AND call_id = ?""",
            (company_id, agent_id, target_call_id),
        ).fetchall()
        if len(rec_rows) > 1 and current_run_id is not None:
            same_run = [r for r in rec_rows if str(r[0]) == current_run_id]
            if len(same_run) == 1:
                rec_rows = same_run
    if not rec_rows:
        any_row = (
            connection.execute(
                """SELECT 1 FROM tool_call_receipt
                   WHERE company_id = ? AND run_id = ? AND call_id = ?
                   LIMIT 1""",
                (company_id, target_run_id, target_call_id),
            ).fetchone()
            if target_run_id is not None
            else connection.execute(
                """SELECT 1 FROM tool_call_receipt
                   WHERE company_id = ? AND call_id = ?
                   LIMIT 1""",
                (company_id, target_call_id),
            ).fetchone()
        )
        if any_row is None:
            raise WorkKernelRefused("Cited tool_call_receipt does not exist")
        raise WorkKernelRefused("Agent is not authorized to cite tool_call_receipt")
    if len(rec_rows) > 1:
        raise WorkKernelRefused(
            "Cited tool_call_receipt call_id is ambiguous across runs; qualify with run_id/call_id"
        )
    _rec_run_id, rec_digest, rec_recorded_at_ns = rec_rows[0]
    if ev.revision != str(rec_digest):
        raise WorkKernelRefused(
            "Cited tool_call_receipt revision does not match recorded result_digest"
        )
    if (
        ev.available_at_ns < int(rec_recorded_at_ns)
        or int(rec_recorded_at_ns) > decision_at_ns
    ):
        raise WorkKernelRefused(
            "Cited tool_call_receipt timestamps precede receipt recording or follow decision"
        )


_CORE_EVIDENCE_VALIDATORS: dict[str, _CoreEvidenceValidator] = {
    "private_draft": _validate_private_draft_evidence,
    "shared_publication": _validate_shared_publication_evidence,
    "peer_request": _validate_peer_request_evidence,
    "work_run": _validate_work_run_evidence,
    "work_item": _validate_work_item_evidence,
    "tool_call_receipt": _validate_tool_call_receipt_evidence,
}


def _validate_core_evidence_entry(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    decision_at_ns: int,
    ev: DecisionEvidence,
    current_run_id: str | None = None,
) -> bool:
    if ev.available_at_ns > ev.observed_at_ns or ev.observed_at_ns > decision_at_ns:
        raise WorkKernelRefused(
            "Decision evidence timestamps violate availability/observation order"
        )
    validator = _CORE_EVIDENCE_VALIDATORS.get(ev.kind)
    if validator is None:
        return False
    validator(
        connection,
        company_id=company_id,
        agent_id=agent_id,
        decision_at_ns=decision_at_ns,
        ev=ev,
        current_run_id=current_run_id,
    )
    return True


def _verify_decision_replay(
    connection: sqlite3.Connection,
    *,
    prior: WorkDecision,
    company_id: str,
    work_item_id: str,
    run_id: str,
    action_kind: str,
    evidence_digest: str,
    rationale: str,
    decision_at_ns: int | None,
    effective_decision_ns: int,
    active_snapshot_id: str | None,
    validated_next: dict[str, Any] | None,
) -> WorkDecision:
    """Verify idempotent decision replay against an existing action_id record."""
    run_replay_ok = prior.run_id == run_id
    if not run_replay_ok and prior.work_item_id == work_item_id:
        prior_run_row = connection.execute(
            "SELECT status FROM work_runs WHERE company_id = ? AND run_id = ?",
            (company_id, prior.run_id),
        ).fetchone()
        run_replay_ok = prior_run_row is not None and str(prior_run_row[0]) in (
            "fenced",
            "failed",
        )
    if (
        prior.work_item_id != work_item_id
        or not run_replay_ok
        or prior.action_kind != action_kind
        or prior.evidence_digest != evidence_digest
        or prior.rationale != rationale
        or (
            decision_at_ns is not None and prior.decision_at_ns != effective_decision_ns
        )
        or prior.active_snapshot_id != active_snapshot_id
        or (prior.next_condition_id is None) != (validated_next is None)
    ):
        raise WorkKernelRefused(
            "Work decision conflicts with previously recorded action_id"
        )
    if validated_next is not None and prior.next_condition_id is not None:
        cond_row = connection.execute(
            """SELECT responsibility_id, work_item_id, trigger_kind, due_at_ns,
                      source_identity, expected_revision, expires_at_ns,
                      cooldown_ns, max_fires, condition_revision
               FROM work_conditions WHERE condition_id = ?""",
            (prior.next_condition_id,),
        ).fetchone()
        if cond_row is None:
            raise WorkKernelRefused(
                "Work decision conflicts with missing next_condition"
            )
        exp_resp = validated_next.get("responsibility_id")
        exp_item = _normalize_next_condition_work_item_id(
            connection,
            company_id=company_id,
            work_item_id=work_item_id,
            next_condition=validated_next,
            always_link_work_item=False,
        )
        req_source = validated_next.get("source_identity")
        exp_source = (
            f"timer:{prior.next_condition_id}" if req_source is None else req_source
        )
        if (
            cond_row[0] != exp_resp
            or cond_row[1] != exp_item
            or cond_row[2] != validated_next["trigger_kind"]
            or cond_row[3] != validated_next.get("due_at_ns")
            or cond_row[4] != exp_source
            or cond_row[5] != validated_next.get("expected_revision")
            or cond_row[6] != validated_next.get("expires_at_ns")
            or int(cond_row[7]) != int(validated_next.get("cooldown_ns", 0))
            or cond_row[8] != validated_next.get("max_fires")
            or (
                "condition_revision" in validated_next
                and int(cond_row[9]) != int(validated_next["condition_revision"])
            )
        ):
            raise WorkKernelRefused(
                "Work decision conflicts with previously armed next_condition"
            )
    return prior


def record_work_decision(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    work_item_id: str,
    run_id: str,
    fencing_token: int,
    agent_id: str,
    action_kind: str,
    rationale: str,
    evidence: Sequence[DecisionEvidence | Mapping[str, Any]] = (),
    decision_at_ns: int | None = None,
    now_ns: int | None = None,
    action_id: str | None = None,
    next_condition: Mapping[str, Any] | None = None,
    active_snapshot_id: str | None = None,
    domain_plugins: Sequence[Any] = (),
) -> WorkDecision:
    """Validate evidence, persist work_decision + optional next_condition atomically."""
    _require_nonempty(company_id, "company_id")
    _require_nonempty(work_item_id, "work_item_id")
    _require_nonempty(run_id, "run_id")
    _require_nonempty(agent_id, "agent_id")
    _require_nonempty(action_kind, "action_kind")
    if action_kind not in WORK_DECISION_ACTION_KINDS:
        raise WorkKernelRefused(
            f"Unsupported work_decision action_kind: {action_kind!r}"
        )
    _require_nonempty(rationale, "rationale")
    if action_id is not None:
        _require_nonempty(action_id, "action_id")
    effective_action_id = action_id or f"dec-{uuid4().hex}"
    if decision_at_ns is not None:
        effective_decision_ns = decision_at_ns
    elif now_ns is not None:
        effective_decision_ns = now_ns
    else:
        effective_decision_ns = time_ns()
    if type(effective_decision_ns) is not int or effective_decision_ns <= 0:
        raise WorkKernelRefused("decision_at_ns must be a positive integer")

    run = assert_run_fencing(
        connection, run_id, fencing_token, now_ns=effective_decision_ns
    )
    if (
        run.company_id != company_id
        or run.work_item_id != work_item_id
        or run.agent_id != agent_id
    ):
        raise WorkKernelRefused("Work decision does not match active run identity")
    if not has_active_grant(connection, company_id, agent_id):
        raise RunFenced(f"Agent {agent_id} no longer holds an active grant")
    item_row = connection.execute(
        """SELECT owner_agent_id, state, payload_json
           FROM work_items
           WHERE company_id = ? AND work_item_id = ?""",
        (company_id, work_item_id),
    ).fetchone()
    if (
        item_row is None
        or str(item_row[0]) != agent_id
        or str(item_row[1]) != "running"
    ):
        raise WorkKernelRefused("Work item is not actively running for this Agent")

    normalized_evidence = _normalize_evidence(evidence)
    for ev in normalized_evidence:
        validated = _validate_core_evidence_entry(
            connection,
            company_id=company_id,
            agent_id=agent_id,
            decision_at_ns=effective_decision_ns,
            ev=ev,
            current_run_id=run_id,
        )
        for plugin in domain_plugins:
            validator = getattr(plugin, "validate_decision_evidence", None)
            if validator is not None:
                if (
                    validator(
                        connection,
                        company_id,
                        agent_id,
                        effective_decision_ns,
                        ev,
                    )
                    is True
                ):
                    validated = True
        if not validated:
            raise WorkKernelRefused(
                f"Unsupported or unvalidated DecisionEvidence kind: {ev.kind!r}"
            )

    evidence_refs = [
        {
            "kind": ev.kind,
            "resource_id": (
                f"private_draft:opaque:{sha256(f'{agent_id}:{ev.resource_id}'.encode('utf-8')).hexdigest()[:24]}"
                if ev.kind == "private_draft"
                else ev.resource_id
            ),
            "revision": ev.revision,
            "observed_at_ns": ev.observed_at_ns,
            "available_at_ns": ev.available_at_ns,
        }
        for ev in normalized_evidence
    ]
    evidence_refs_json = _canonical_json(evidence_refs)
    evidence_digest = sha256(evidence_refs_json.encode("utf-8")).hexdigest()

    validated_next = (
        validate_next_condition_dict(next_condition)
        if next_condition is not None
        else None
    )

    prior_row = connection.execute(
        """SELECT decision_id, company_id, work_item_id, run_id, agent_id,
                  action_id, action_kind, evidence_refs_json, evidence_digest,
                  rationale, next_condition_id, active_snapshot_id, decision_at_ns
           FROM work_decision
           WHERE company_id = ? AND agent_id = ? AND action_id = ?""",
        (company_id, agent_id, effective_action_id),
    ).fetchone()
    if prior_row is not None:
        return _verify_decision_replay(
            connection,
            prior=WorkDecision(*prior_row),
            company_id=company_id,
            work_item_id=work_item_id,
            run_id=run_id,
            action_kind=action_kind,
            evidence_digest=evidence_digest,
            rationale=rationale,
            decision_at_ns=decision_at_ns,
            effective_decision_ns=effective_decision_ns,
            active_snapshot_id=active_snapshot_id,
            validated_next=validated_next,
        )

    next_condition_id: str | None = None
    if validated_next is not None:
        armed = _arm_next_condition(
            connection,
            company_id=company_id,
            owner_agent_id=agent_id,
            work_item_id=work_item_id,
            next_condition=validated_next,
            now_ns=effective_decision_ns,
            always_link_work_item=False,
        )
        next_condition_id = armed.condition_id

    decision_id = uuid4().hex
    connection.execute(
        """INSERT INTO work_decision (
               decision_id, company_id, work_item_id, run_id, agent_id,
               action_id, action_kind, evidence_refs_json, evidence_digest,
               rationale, next_condition_id, active_snapshot_id, decision_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            decision_id,
            company_id,
            work_item_id,
            run_id,
            agent_id,
            effective_action_id,
            action_kind,
            evidence_refs_json,
            evidence_digest,
            rationale,
            next_condition_id,
            active_snapshot_id,
            effective_decision_ns,
        ),
    )
    append_audit_event(
        connection,
        company_id=company_id,
        domain="work",
        entity_type="work_decision",
        entity_id=decision_id,
        event_type="work.decision_recorded",
        actor_id=agent_id,
        run_id=run_id,
        payload={
            "work_item_id": work_item_id,
            "action_id": opaque_decision_action_id(effective_action_id),
            "action_kind": action_kind,
            "evidence_digest": evidence_digest,
            "next_condition_id": next_condition_id,
        },
        recorded_at_ns=effective_decision_ns,
    )

    try:
        item_payload = json.loads(str(item_row[2]))
    except json.JSONDecodeError:
        item_payload = {}
    if isinstance(item_payload, dict):
        linked_resp = item_payload.get("responsibility_id")
        if isinstance(linked_resp, str) and linked_resp.strip():
            resp_info = connection.execute(
                """SELECT duty_kind FROM work_responsibilities
                   WHERE company_id = ? AND responsibility_id = ?""",
                (company_id, linked_resp),
            ).fetchone()
            if resp_info is not None and str(resp_info[0]) == "work.discovery_review":
                linked_cond_id = item_payload.get("condition_id")
                record_discovery_hysteresis(
                    connection,
                    company_id=company_id,
                    responsibility_id=linked_resp,
                    action_kind=action_kind,
                    now_ns=effective_decision_ns,
                    condition_id=(
                        linked_cond_id.strip()
                        if isinstance(linked_cond_id, str) and linked_cond_id.strip()
                        else None
                    ),
                )

    return WorkDecision(
        decision_id=decision_id,
        company_id=company_id,
        work_item_id=work_item_id,
        run_id=run_id,
        agent_id=agent_id,
        action_id=effective_action_id,
        action_kind=action_kind,
        evidence_refs_json=evidence_refs_json,
        evidence_digest=evidence_digest,
        rationale=rationale,
        next_condition_id=next_condition_id,
        active_snapshot_id=active_snapshot_id,
        decision_at_ns=effective_decision_ns,
    )
