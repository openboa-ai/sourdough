"""Core OuterToolBroker tool suite for Proactive Work Discovery, Condition Scheduling, and Decisions."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from typing import Any

from ..agent_runtime.protocol import ToolHandler, ToolSpec
from ..duty_run import DutyRefused, WorkRunClaim
from ..writer_lease import WriterLease
from .domains import DomainPlugin, ToolContribution
from .tool_kit import (
    FencedToolBase,
    optional_int as _optional_int,
    optional_str as _optional_str,
    require_int as _require_int,
    require_object as _require_object,
    require_str as _require_str,
)
from .work_kernel import (
    WORK_DECISION_ACTION_KINDS,
    DecisionEvidence,
    WorkCondition,
    WorkKernelRefused,
    arm_result_continuation,
    cancel_condition,
    list_conditions,
    opaque_decision_action_id,
    record_work_decision,
    replace_condition,
    set_condition,
    validate_next_condition_dict,
)


def _serialize_condition(cond: WorkCondition) -> dict[str, Any]:
    return {
        "condition_id": cond.condition_id,
        "responsibility_id": cond.responsibility_id,
        "work_item_id": cond.work_item_id,
        "condition_revision": cond.condition_revision,
        "trigger_kind": cond.trigger_kind,
        "due_at_ns": cond.due_at_ns,
        "source_identity": cond.source_identity,
        "expected_revision": cond.expected_revision,
        "expires_at_ns": cond.expires_at_ns,
        "cooldown_ns": cond.cooldown_ns,
        "max_fires": cond.max_fires,
        "fire_count": cond.fire_count,
        "status": cond.status,
        "created_at_ns": cond.created_at_ns,
        "updated_at_ns": cond.updated_at_ns,
    }


def _parse_next_condition_arg(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    return validate_next_condition_dict(raw, max_str_len=256, error_cls=DutyRefused)


def build_core_proactive_tool_contribution(
    *,
    writer: WriterLease,
    claim: WorkRunClaim,
    tx_lock: threading.RLock | None = None,
    domain_plugins: Sequence[DomainPlugin] = (),
    active_snapshot_id: str | None = None,
) -> ToolContribution:
    """Build Core proactive condition-scheduling and decision OuterToolBroker tools."""
    kit = FencedToolBase(writer=writer, claim=claim, tx_lock=tx_lock)
    _now_ns = kit.now_ns

    def _list_conditions(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"status"},
            required=set(),
            label="list_conditions",
        )
        status_filter = _optional_str(obj.get("status"), "status")
        with kit.fenced_reader(verify_desk=True, verify_writer=True) as db:
            items = list_conditions(db, claim.company_id, claim.founder_id)
        if status_filter is not None:
            items = tuple(c for c in items if c.status == status_filter)
        return {
            "ok": True,
            "conditions": [_serialize_condition(c) for c in items],
        }

    def _set_condition(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={
                "responsibility_id",
                "work_item_id",
                "trigger_kind",
                "due_at_ns",
                "source_identity",
                "expected_revision",
                "expires_at_ns",
                "cooldown_ns",
                "max_fires",
            },
            required={"trigger_kind"},
            label="set_condition",
        )
        trigger_kind = _require_str(obj["trigger_kind"], "trigger_kind")
        resp_id = _optional_str(obj.get("responsibility_id"), "responsibility_id")
        item_id = _optional_str(obj.get("work_item_id"), "work_item_id")
        due_at_ns = _optional_int(obj.get("due_at_ns"), "due_at_ns", minimum=0)
        source_identity = _optional_str(obj.get("source_identity"), "source_identity")
        if source_identity is not None and source_identity.startswith("result_dep:"):
            raise WorkKernelRefused(
                "Reserved source_identity prefix 'result_dep:' may only be armed via arm_result_continuation"
            )
        if trigger_kind == "timer" and source_identity is not None:
            raise WorkKernelRefused(
                "Timer conditions assign source_identity automatically; omit source_identity"
            )
        expected_revision = _optional_str(
            obj.get("expected_revision"), "expected_revision"
        )
        expires_at_ns = _optional_int(
            obj.get("expires_at_ns"), "expires_at_ns", minimum=0
        )
        cooldown_ns = _optional_int(obj.get("cooldown_ns"), "cooldown_ns", minimum=0)
        max_fires = _optional_int(obj.get("max_fires"), "max_fires", minimum=1)

        def _infer_active_responsibility(db: Any) -> str | None:
            wi_row = db.execute(
                """SELECT payload_json FROM work_items
                   WHERE company_id = ? AND work_item_id = ?""",
                (claim.company_id, claim.duty_id),
            ).fetchone()
            if wi_row is None:
                return None
            try:
                wi_payload = json.loads(str(wi_row[0]))
            except json.JSONDecodeError:
                wi_payload = {}
            cand_resp = (
                wi_payload.get("responsibility_id")
                if isinstance(wi_payload, dict)
                else None
            )
            if isinstance(cand_resp, str) and cand_resp.strip():
                r_row = db.execute(
                    """SELECT 1 FROM work_responsibilities
                       WHERE company_id = ? AND responsibility_id = ?
                         AND owner_agent_id = ? AND status = 'active'""",
                    (claim.company_id, cand_resp.strip(), claim.founder_id),
                ).fetchone()
                if r_row is not None:
                    return cand_resp.strip()
            return None

        now = _now_ns()
        with kit.fenced_write_uow(verify_desk=True) as db:
            if resp_id is None and item_id is not None and item_id != claim.duty_id:
                raise WorkKernelRefused(
                    "set_condition may only link the current work item or an active standing responsibility"
                )
            if resp_id is None and (item_id is None or item_id == claim.duty_id):
                # Prefer binding to the work item's active standing responsibility so
                # discovery/dreaming timers cannot escape responsibility budgets/cooldowns.
                inferred_resp = _infer_active_responsibility(db)
                if inferred_resp is not None:
                    resp_id = inferred_resp
                    item_id = None
                else:
                    item_id = claim.duty_id
            cond = set_condition(
                db,
                company_id=claim.company_id,
                owner_agent_id=claim.founder_id,
                trigger_kind=trigger_kind,
                responsibility_id=resp_id,
                work_item_id=item_id,
                due_at_ns=due_at_ns,
                source_identity=source_identity,
                expected_revision=expected_revision,
                expires_at_ns=expires_at_ns,
                cooldown_ns=0 if cooldown_ns is None else cooldown_ns,
                max_fires=max_fires,
                now_ns=now,
            )
            writer.verify()
        return {"ok": True, **_serialize_condition(cond)}

    def _replace_condition(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={
                "condition_id",
                "trigger_kind",
                "due_at_ns",
                "source_identity",
                "expected_revision",
                "expires_at_ns",
                "cooldown_ns",
                "max_fires",
                "expected_condition_revision",
            },
            required={"condition_id", "trigger_kind"},
            label="replace_condition",
        )
        condition_id = _require_str(obj["condition_id"], "condition_id")
        trigger_kind = _require_str(obj["trigger_kind"], "trigger_kind")
        due_at_ns = _optional_int(obj.get("due_at_ns"), "due_at_ns", minimum=0)
        source_identity = _optional_str(obj.get("source_identity"), "source_identity")
        if source_identity is not None and source_identity.startswith("result_dep:"):
            raise WorkKernelRefused(
                "Reserved source_identity prefix 'result_dep:' may only be armed via arm_result_continuation"
            )
        if trigger_kind == "timer" and source_identity is not None:
            raise WorkKernelRefused(
                "Timer conditions assign source_identity automatically; omit source_identity"
            )
        expected_revision = _optional_str(
            obj.get("expected_revision"), "expected_revision"
        )
        expires_at_ns = _optional_int(
            obj.get("expires_at_ns"), "expires_at_ns", minimum=0
        )
        cooldown_ns = _optional_int(obj.get("cooldown_ns"), "cooldown_ns", minimum=0)
        max_fires = _optional_int(obj.get("max_fires"), "max_fires", minimum=1)
        expected_cond_rev = _optional_int(
            obj.get("expected_condition_revision"),
            "expected_condition_revision",
            minimum=1,
        )
        now = _now_ns()
        with kit.fenced_write_uow(verify_desk=True) as db:
            cond = replace_condition(
                db,
                condition_id,
                owner_agent_id=claim.founder_id,
                trigger_kind=trigger_kind,
                due_at_ns=due_at_ns,
                source_identity=source_identity,
                expected_revision=expected_revision,
                expires_at_ns=expires_at_ns,
                cooldown_ns=cooldown_ns,
                max_fires=max_fires,
                expected_condition_revision=expected_cond_rev,
                now_ns=now,
            )
            writer.verify()
        return {"ok": True, **_serialize_condition(cond)}

    def _cancel_condition(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"condition_id", "expected_condition_revision"},
            required={"condition_id"},
            label="cancel_condition",
        )
        condition_id = _require_str(obj["condition_id"], "condition_id")
        expected_cond_rev = _optional_int(
            obj.get("expected_condition_revision"),
            "expected_condition_revision",
            minimum=1,
        )
        now = _now_ns()
        with kit.fenced_write_uow(verify_desk=True) as db:
            cancel_condition(
                db,
                condition_id,
                owner_agent_id=claim.founder_id,
                expected_condition_revision=expected_cond_rev,
                now_ns=now,
            )
            writer.verify()
        return {
            "ok": True,
            "condition_id": condition_id,
            "status": "cancelled",
        }

    def _record_work_decision(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={
                "action_kind",
                "rationale",
                "evidence",
                "decision_at_ns",
                "action_id",
                "next_condition",
            },
            required={"action_kind", "rationale"},
            label="record_work_decision",
        )
        action_kind = _require_str(obj["action_kind"], "action_kind")
        rationale = _require_str(obj["rationale"], "rationale", max_len=4000)
        raw_action_id = _optional_str(obj.get("action_id"), "action_id")
        action_id = (
            opaque_decision_action_id(raw_action_id)
            if raw_action_id is not None
            else None
        )
        decision_at_ns = _optional_int(
            obj.get("decision_at_ns"), "decision_at_ns", minimum=1
        )
        fallback_now_ns = max(1, _now_ns())
        raw_evidence = obj.get("evidence", [])
        if not isinstance(raw_evidence, list):
            raise DutyRefused("evidence must be an array")
        parsed_evidence: list[DecisionEvidence] = []
        for idx, item in enumerate(raw_evidence):
            ev_obj = _require_object(
                item,
                allowed={
                    "kind",
                    "resource_id",
                    "revision",
                    "observed_at_ns",
                    "available_at_ns",
                },
                required={
                    "kind",
                    "resource_id",
                    "revision",
                    "observed_at_ns",
                    "available_at_ns",
                },
                label=f"evidence[{idx}]",
            )
            parsed_evidence.append(
                DecisionEvidence(
                    kind=_require_str(ev_obj["kind"], f"evidence[{idx}].kind"),
                    resource_id=_require_str(
                        ev_obj["resource_id"], f"evidence[{idx}].resource_id"
                    ),
                    revision=_require_str(
                        ev_obj["revision"], f"evidence[{idx}].revision"
                    ),
                    observed_at_ns=_require_int(
                        ev_obj["observed_at_ns"],
                        f"evidence[{idx}].observed_at_ns",
                        minimum=0,
                    ),
                    available_at_ns=_require_int(
                        ev_obj["available_at_ns"],
                        f"evidence[{idx}].available_at_ns",
                        minimum=0,
                    ),
                )
            )
        next_cond = _parse_next_condition_arg(obj.get("next_condition"))
        with kit.fenced_write_uow(verify_desk=True) as db:
            dec = record_work_decision(
                db,
                company_id=claim.company_id,
                work_item_id=claim.duty_id,
                run_id=claim.run_id,
                fencing_token=claim.fencing_token,
                agent_id=claim.founder_id,
                action_kind=action_kind,
                rationale=rationale,
                evidence=tuple(parsed_evidence),
                decision_at_ns=decision_at_ns,
                now_ns=fallback_now_ns,
                action_id=action_id,
                next_condition=next_cond,
                active_snapshot_id=active_snapshot_id,
                domain_plugins=domain_plugins,
            )
            writer.verify()
        return {
            "ok": True,
            "decision_id": dec.decision_id,
            "action_id": dec.action_id,
            "action_kind": dec.action_kind,
            "evidence_digest": dec.evidence_digest,
            "next_condition_id": dec.next_condition_id,
            "decision_at_ns": dec.decision_at_ns,
        }

    def _arm_result_continuation(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={
                "dependency_asset_id",
                "due_at_ns",
                "responsibility_id",
                "work_item_id",
                "expires_at_ns",
            },
            required={"dependency_asset_id", "due_at_ns"},
            label="arm_result_continuation",
        )
        dependency_asset_id = _require_str(
            obj["dependency_asset_id"], "dependency_asset_id"
        )
        due_at_ns = _require_int(obj["due_at_ns"], "due_at_ns", minimum=0)
        resp_id = _optional_str(obj.get("responsibility_id"), "responsibility_id")
        item_id = _optional_str(obj.get("work_item_id"), "work_item_id")
        expires_at_ns = _optional_int(
            obj.get("expires_at_ns"), "expires_at_ns", minimum=0
        )
        now = _now_ns()
        with kit.fenced_write_uow(verify_desk=True) as db:
            if resp_id is None and item_id is not None and item_id != claim.duty_id:
                raise WorkKernelRefused(
                    "arm_result_continuation may only link the current work item or an active standing responsibility"
                )
            if resp_id is None and (item_id is None or item_id == claim.duty_id):
                wi_row = db.execute(
                    """SELECT payload_json FROM work_items
                       WHERE company_id = ? AND work_item_id = ?""",
                    (claim.company_id, claim.duty_id),
                ).fetchone()
                inferred_resp: str | None = None
                if wi_row is not None:
                    try:
                        wi_payload = json.loads(str(wi_row[0]))
                    except json.JSONDecodeError:
                        wi_payload = {}
                    cand_resp = (
                        wi_payload.get("responsibility_id")
                        if isinstance(wi_payload, dict)
                        else None
                    )
                    if isinstance(cand_resp, str) and cand_resp.strip():
                        r_row = db.execute(
                            """SELECT 1 FROM work_responsibilities
                               WHERE company_id = ? AND responsibility_id = ?
                                 AND owner_agent_id = ? AND status = 'active'""",
                            (claim.company_id, cand_resp.strip(), claim.founder_id),
                        ).fetchone()
                        if r_row is not None:
                            inferred_resp = cand_resp.strip()
                if inferred_resp is not None:
                    resp_id = inferred_resp
                    item_id = None
                else:
                    item_id = claim.duty_id
            cond = arm_result_continuation(
                db,
                company_id=claim.company_id,
                owner_agent_id=claim.founder_id,
                dependency_asset_id=dependency_asset_id,
                due_at_ns=due_at_ns,
                now_ns=now,
                responsibility_id=resp_id,
                work_item_id=item_id,
                expires_at_ns=expires_at_ns,
            )
            writer.verify()
        return {"ok": True, **_serialize_condition(cond)}

    specs: tuple[ToolSpec, ...] = (
        ToolSpec(
            name="list_conditions",
            description="List active and historical watched conditions owned by this Employee/Agent.",
            parameters={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="set_condition",
            description=(
                "Watch a schedule or company condition "
                "(timer, shared_asset_revision, or peer_response) for your standing responsibility or work item."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "responsibility_id": {"type": "string"},
                    "work_item_id": {"type": "string"},
                    "trigger_kind": {
                        "type": "string",
                        "enum": ["timer", "shared_asset_revision", "peer_response"],
                    },
                    "due_at_ns": {"type": "integer", "minimum": 0},
                    "source_identity": {"type": "string"},
                    "expected_revision": {"type": "string"},
                    "expires_at_ns": {"type": "integer", "minimum": 0},
                    "cooldown_ns": {"type": "integer", "minimum": 0},
                    "max_fires": {"type": "integer", "minimum": 1},
                },
                "required": ["trigger_kind"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="replace_condition",
            description="Supersede an active watched condition and arm a successor revision.",
            parameters={
                "type": "object",
                "properties": {
                    "condition_id": {"type": "string"},
                    "trigger_kind": {
                        "type": "string",
                        "enum": ["timer", "shared_asset_revision", "peer_response"],
                    },
                    "due_at_ns": {"type": "integer", "minimum": 0},
                    "source_identity": {"type": "string"},
                    "expected_revision": {"type": "string"},
                    "expires_at_ns": {"type": "integer", "minimum": 0},
                    "cooldown_ns": {"type": "integer", "minimum": 0},
                    "max_fires": {"type": "integer", "minimum": 1},
                    "expected_condition_revision": {"type": "integer", "minimum": 1},
                },
                "required": ["condition_id", "trigger_kind"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="cancel_condition",
            description="Cancel an active watched condition owned by this Employee/Agent.",
            parameters={
                "type": "object",
                "properties": {
                    "condition_id": {"type": "string"},
                    "expected_condition_revision": {"type": "integer", "minimum": 1},
                },
                "required": ["condition_id"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="record_work_decision",
            description=(
                "Record a grounded work decision backed by verified evidence references "
                "and an optional follow-up condition (next_condition)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action_kind": {
                        "type": "string",
                        "enum": sorted(WORK_DECISION_ACTION_KINDS),
                    },
                    "rationale": {"type": "string"},
                    "action_id": {"type": "string"},
                    "decision_at_ns": {"type": "integer", "minimum": 1},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string"},
                                "resource_id": {"type": "string"},
                                "revision": {"type": "string"},
                                "observed_at_ns": {"type": "integer", "minimum": 0},
                                "available_at_ns": {"type": "integer", "minimum": 0},
                            },
                            "required": [
                                "kind",
                                "resource_id",
                                "revision",
                                "observed_at_ns",
                                "available_at_ns",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "next_condition": {
                        "type": "object",
                        "properties": {
                            "responsibility_id": {"type": "string"},
                            "work_item_id": {"type": "string"},
                            "trigger_kind": {
                                "type": "string",
                                "enum": [
                                    "timer",
                                    "shared_asset_revision",
                                    "peer_response",
                                ],
                            },
                            "due_at_ns": {"type": "integer", "minimum": 0},
                            "source_identity": {"type": "string"},
                            "expected_revision": {"type": "string"},
                            "expires_at_ns": {"type": "integer", "minimum": 0},
                            "cooldown_ns": {"type": "integer", "minimum": 0},
                            "max_fires": {"type": "integer", "minimum": 1},
                            "condition_revision": {"type": "integer", "minimum": 1},
                        },
                        "required": ["trigger_kind"],
                        "additionalProperties": False,
                    },
                },
                "required": ["action_kind", "rationale"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="arm_result_continuation",
            description="Schedule a follow-up check gated on a committed shared deliverable publication.",
            parameters={
                "type": "object",
                "properties": {
                    "dependency_asset_id": {"type": "string"},
                    "due_at_ns": {"type": "integer", "minimum": 0},
                    "responsibility_id": {"type": "string"},
                    "work_item_id": {"type": "string"},
                    "expires_at_ns": {"type": "integer", "minimum": 0},
                },
                "required": ["dependency_asset_id", "due_at_ns"],
                "additionalProperties": False,
            },
        ),
    )

    handlers: dict[str, ToolHandler] = {
        "list_conditions": _list_conditions,
        "set_condition": _set_condition,
        "replace_condition": _replace_condition,
        "cancel_condition": _cancel_condition,
        "record_work_decision": _record_work_decision,
        "arm_result_continuation": _arm_result_continuation,
    }
    return ToolContribution(
        specs=specs,
        handlers=handlers,
        recoverable_exceptions=(DutyRefused, WorkKernelRefused),
    )
