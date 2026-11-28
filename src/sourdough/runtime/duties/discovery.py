"""Proactive work discovery review duty handler."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from ...company_assets import shared_asset_for
from ...duty_run import DutyRefused, work_run_claim_from_context
from ...method_learning import compute_active_snapshot_id
from ...storage.db import scoped_reader, write_uow
from ..collab_tools import build_core_collab_tool_contribution
from ..domains import merge_tool_contributions
from ..proactive_tools import build_core_proactive_tool_contribution
from ..registry import DutyContext, DutyOutcome, RuntimeRefused
from ..shift_runner import reconcile_shift_episode_outcome
from ..work_kernel import assert_run_fencing
from .deps import CoreDutyDeps, hydrate_resolved_child_peer_outcome


def _find_committed_work_decision(
    db: Any,
    *,
    company_id: str,
    work_item_id: str,
    run_id: str,
) -> tuple[str, str, str | None, str] | None:
    row = db.execute(
        """SELECT d.decision_id, d.action_kind, d.next_condition_id, d.run_id
           FROM work_decision d
           JOIN work_runs r ON r.run_id = d.run_id
           WHERE d.company_id = ? AND d.work_item_id = ?
             AND (d.run_id = ? OR r.status IN ('fenced', 'failed'))
           ORDER BY CASE WHEN d.run_id = ? THEN 0 ELSE 1 END,
                    d.decision_at_ns DESC, d.decision_id DESC
           LIMIT 1""",
        (company_id, work_item_id, run_id, run_id),
    ).fetchone()
    if row is None:
        return None
    return (
        str(row[0]),
        str(row[1]),
        None if row[2] is None else str(row[2]),
        str(row[3]),
    )


class DiscoveryDuties:
    """Executes proactive discovery reviews and enforces grounded work_decision commits."""

    def __init__(self, deps: CoreDutyDeps) -> None:
        self._deps = deps

    def run_work_discovery_review(self, ctx: DutyContext) -> DutyOutcome:
        agent_id = ctx.work_item.owner_agent_id
        receipts: list[dict[str, Any]] = []
        notes: list[str] = []
        try:
            payload = json.loads(ctx.work_item.payload_json)
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        with write_uow(ctx.writer, tx_lock=ctx.tx_lock) as db:
            assert_run_fencing(
                db, ctx.run.run_id, ctx.run.fencing_token, now_ns=ctx.now_ns
            )
            committed = _find_committed_work_decision(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
            )
            if committed is not None:
                dec_id, act_kind, next_cid, dec_run_id = committed
                return DutyOutcome(
                    disposition="completed",
                    result={
                        "decision_id": dec_id,
                        "action_kind": act_kind,
                        "next_condition_id": next_cid,
                        "recovered_committed_decision": True,
                        "recovered_from_run_id": dec_run_id,
                        "cost_minor_units": 0,
                    },
                )
            hydrate_resolved_child_peer_outcome(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
                agent_id=agent_id,
                receipts=receipts,
                notes=notes,
            )
            if payload:
                receipts.append({"kind": "discovery_trigger", **payload})
                notes.append(
                    f"Watched condition trigger ({payload.get('trigger_kind', 'manual')}): "
                    f"source={payload.get('source_identity')} rev={payload.get('observed_revision')}"
                )
                if "case_inputs" in payload:
                    notes.append(
                        f"Trial case inputs: {json.dumps(payload['case_inputs'], sort_keys=True)[:1000]}"
                    )
                if "worker_context" in payload:
                    notes.append(
                        f"Trial worker context: {json.dumps(payload['worker_context'], sort_keys=True)[:1000]}"
                    )
                coalesced_entries = payload.get("coalesced_revisions")
                if isinstance(coalesced_entries, list) and len(coalesced_entries) > 1:
                    summary_parts = [
                        f"{e.get('trigger_kind', 'condition')}:{e.get('source_identity')}@{e.get('observed_revision')}"
                        for e in coalesced_entries
                        if isinstance(e, dict)
                    ]
                    if summary_parts:
                        notes.append(
                            f"Coalesced triggers ({len(summary_parts)}): {', '.join(summary_parts)}"
                        )
            resp_id = payload.get("responsibility_id")
            if isinstance(resp_id, str) and resp_id.strip():
                resp_row = db.execute(
                    """SELECT remit, readable_scope_json
                       FROM work_responsibilities
                       WHERE company_id = ? AND responsibility_id = ?""",
                    (ctx.company_id, resp_id),
                ).fetchone()
                if resp_row is not None:
                    receipts.append(
                        {
                            "kind": "responsibility",
                            "responsibility_id": resp_id,
                            "remit": str(resp_row[0]),
                            "readable_scope_json": str(resp_row[1]),
                        }
                    )
                    notes.append(f"Standing responsibility remit: {resp_row[0]}")
            source_ids: list[str] = []
            coalesced_entries = payload.get("coalesced_revisions")
            if isinstance(coalesced_entries, list):
                for entry in coalesced_entries:
                    if isinstance(entry, dict):
                        entry_sid = entry.get("source_identity")
                        if (
                            isinstance(entry_sid, str)
                            and entry_sid.strip()
                            and entry_sid not in source_ids
                        ):
                            source_ids.append(entry_sid)
            top_source_id = payload.get("source_identity")
            if (
                isinstance(top_source_id, str)
                and top_source_id.strip()
                and top_source_id not in source_ids
            ):
                source_ids.append(top_source_id)
            for source_id in source_ids:
                try:
                    asset = shared_asset_for(db, agent_id, source_id)
                except DutyRefused:
                    asset = None
                if asset is not None:
                    asset_rev = sha256(asset.content.encode("utf-8")).hexdigest()
                    receipts.append(
                        {
                            "kind": "shared_asset",
                            "asset_id": asset.asset_id,
                            "project_id": asset.project_id,
                            "author_id": asset.author_id,
                            "revision": asset_rev,
                            "content": asset.content,
                        }
                    )
                    notes.append(
                        f"Shared publication {asset.asset_id} (rev {asset_rev}): {asset.content[:500]}"
                    )
            claim = work_run_claim_from_context(db, ctx, purpose="discovery_review")

        resolved_methods, resolved_snapshot_id = self._deps.active_methods_provider(
            ctx.writer.store,
            ctx.company_id,
            agent_id,
            ctx.work_item.duty_kind,
        )
        if resolved_snapshot_id is None and resolved_methods:
            resolved_snapshot_id = compute_active_snapshot_id(resolved_methods)

        proactive = build_core_proactive_tool_contribution(
            writer=ctx.writer,
            claim=claim,
            tx_lock=ctx.tx_lock,
            domain_plugins=self._deps.domain_plugins,
            active_snapshot_id=resolved_snapshot_id,
        )
        has_peer_discovery_input = (
            payload.get("trigger_kind") == "peer_response"
            or (
                isinstance(payload.get("coalesced_revisions"), list)
                and any(
                    isinstance(e, dict) and e.get("trigger_kind") == "peer_response"
                    for e in payload["coalesced_revisions"]
                )
            )
            or any(
                isinstance(r, dict)
                and (
                    r.get("kind") == "peer_outcome"
                    or (
                        r.get("kind") == "shared_asset"
                        and r.get("author_id") != agent_id
                    )
                )
                for r in receipts
            )
        )
        collab = build_core_collab_tool_contribution(
            writer=ctx.writer,
            claim=claim,
            purpose=claim.purpose,
            tx_lock=ctx.tx_lock,
            include_messaging=not has_peer_discovery_input,
            include_peer_request=not has_peer_discovery_input,
            include_peer_respond=False,
            include_handoffs=False,
            include_roster=True,
        )
        merged_specs, merged_handlers, merged_recoverable = merge_tool_contributions(
            (), {}, (proactive, collab)
        )
        sealed_tool_allowlist: frozenset[str] | None = None
        if (
            ctx.work_item.source_kind in ("offline_trial", "recursive_learning")
            and "sealed_tool_manifest" in payload
        ):
            raw_manifest = payload.get("sealed_tool_manifest")
            if not isinstance(raw_manifest, list):
                raise RuntimeRefused(
                    "sealed_tool_manifest must be a list of tool names"
                )
            parsed_manifest = frozenset(
                name.strip()
                for name in raw_manifest
                if isinstance(name, str) and name.strip()
            )
            if not parsed_manifest:
                raise RuntimeRefused(
                    "sealed_tool_manifest must contain at least one tool name"
                )
            sealed_tool_allowlist = parsed_manifest

        run_res = self._deps.shift_runner.run(
            ctx,
            mandate_summary=(
                "Review your standing responsibility conditions within your standing grant. "
                "Ground every work decision in verified DecisionEvidence via record_work_decision."
            ),
            instruction=(
                f"Conduct discovery review for work item {ctx.work_item.work_item_id} "
                f"(run_id={ctx.run.run_id}). "
                "If any improvement_procedure skill is listed in active_methods, read the latest-version "
                "improvement_procedure via skill_read first and follow it; otherwise inspect open_receipts "
                "and ALWAYS finish by calling record_work_decision (cite verified DecisionEvidence from "
                "open_receipts when available, or pass evidence=[] for an evidence-free baseline decision)."
            ),
            purpose="discovery_review",
            base_tools=merged_specs,
            base_handlers=merged_handlers,
            history_notes=tuple(notes),
            open_receipts=tuple(receipts),
            include_core_desk_tools=not has_peer_discovery_input,
            include_plugin_tools=True,
            extra_recoverable_exceptions=merged_recoverable,
            pre_resolved_active_methods=(resolved_methods, resolved_snapshot_id),
            allowed_tool_names=sealed_tool_allowlist,
        )
        if run_res.run_status == "waiting":
            return DutyOutcome(
                disposition="waiting",
                wait_until_ns=run_res.wait_until_ns,
                wait_event_key=run_res.wait_event_key,
                wait_reason="waiting_for_peer_response",
                result={"waiting": True},
            )
        with scoped_reader(ctx.writer.store) as db:
            dec = _find_committed_work_decision(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
            )
        disc_out_bytes = run_res.shift_result.output_text.strip().encode("utf-8")
        if dec is None:
            reconciled_trace = reconcile_shift_episode_outcome(
                ctx,
                outcome_status="blocked",
                reason="no_work_decision",
                causal_method_trace=run_res.causal_method_trace,
            )
            return DutyOutcome(
                disposition="blocked",
                result={
                    "reason": "no_work_decision",
                    "output_digest": sha256(disc_out_bytes).hexdigest(),
                    "output_bytes": len(disc_out_bytes),
                    "cost_minor_units": run_res.settlement.cost_minor_units,
                    "journal_entry_id": run_res.settlement.journal_entry_id,
                    "active_snapshot_id": run_res.active_snapshot_id,
                    "causal_method_trace": reconciled_trace,
                    "stable_prefix_sha256": run_res.stable_prefix_sha256,
                },
            )
        dec_id, act_kind, next_cid, dec_run_id = dec
        outcome_result: dict[str, Any] = {
            "decision_id": dec_id,
            "action_kind": act_kind,
            "next_condition_id": next_cid,
            "output_digest": sha256(disc_out_bytes).hexdigest(),
            "output_bytes": len(disc_out_bytes),
            "provider": run_res.shift_result.provider,
            "model": run_res.shift_result.model,
            "cost_minor_units": run_res.settlement.cost_minor_units,
            "journal_entry_id": run_res.settlement.journal_entry_id,
            "active_snapshot_id": run_res.active_snapshot_id,
            "causal_method_trace": run_res.causal_method_trace,
            "stable_prefix_sha256": run_res.stable_prefix_sha256,
        }
        if dec_run_id != ctx.run.run_id:
            outcome_result["recovered_committed_decision"] = True
            outcome_result["recovered_from_run_id"] = dec_run_id
        return DutyOutcome(disposition="completed", result=outcome_result)
