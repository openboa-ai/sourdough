"""Collaborative Core duties: peer responses, assessment reviews, and handoff follow-ups."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from ...company_assets import project_membership, shared_asset_for
from ...duty_run import DutyRefused, work_run_claim_from_context
from ...storage.db import write_uow
from ..collab_tools import build_core_collab_tool_contribution
from ..registry import DutyContext, DutyOutcome, RuntimeRefused
from ..shift_runner import reconcile_shift_episode_outcome
from ..work_kernel import (
    _PEER_REQUEST_COLUMNS,
    PeerRequest,
    record_peer_request_read,
    resolve_peer_request,
)
from .deps import CoreDutyDeps, hydrate_resolved_child_peer_outcome


class CollaborationDuties:
    """Executes multi-agent peer response, assessment review, and handoff follow-up duties."""

    def __init__(self, deps: CoreDutyDeps) -> None:
        self._deps = deps

    def run_work_peer_response(self, ctx: DutyContext) -> DutyOutcome:
        agent_id = ctx.work_item.owner_agent_id
        accessible_evidence: list[dict[str, str]] = []
        evidence_notes: list[str] = []
        child_receipts: list[dict[str, Any]] = []
        with write_uow(ctx.writer, tx_lock=ctx.tx_lock) as db:
            row = db.execute(
                f"""SELECT {_PEER_REQUEST_COLUMNS}
                    FROM work_peer_requests
                    WHERE company_id = ? AND response_work_item_id = ?""",
                (ctx.company_id, ctx.work_item.work_item_id),
            ).fetchone()
            if row is None:
                raise RuntimeRefused(
                    f"Peer response item '{ctx.work_item.work_item_id}' has no linked peer request"
                )
            peer = PeerRequest(*row)
            if peer.state != "open":
                return DutyOutcome(
                    disposition="completed",
                    result={"request_id": peer.request_id, "outcome": peer.state},
                )
            if ctx.now_ns >= peer.deadline_ns:
                resolve_peer_request(
                    db,
                    company_id=ctx.company_id,
                    request_id=peer.request_id,
                    actor_id="work_scheduler",
                    resolution_state="expired",
                    response_payload={"reason": "deadline_elapsed"},
                    caller_response_run_id=ctx.run.run_id,
                    now_ns=ctx.now_ns,
                )
                return DutyOutcome(
                    disposition="completed",
                    result={"request_id": peer.request_id, "outcome": "expired"},
                )
            record_peer_request_read(
                db,
                company_id=ctx.company_id,
                request_id=peer.request_id,
                actor_id=agent_id,
                response_run_id=ctx.run.run_id,
                response_fencing_token=ctx.run.fencing_token,
                now_ns=ctx.now_ns,
            )
            hydrate_resolved_child_peer_outcome(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
                agent_id=agent_id,
                receipts=child_receipts,
                notes=evidence_notes,
            )
            raw_refs = json.loads(peer.evidence_refs_json)
            if isinstance(raw_refs, list):
                for ref in raw_refs:
                    if not isinstance(ref, str):
                        continue
                    if ref.startswith("shared_publication:"):
                        asset_id = ref.removeprefix("shared_publication:").split(
                            "@", 1
                        )[0]
                        try:
                            asset = shared_asset_for(db, agent_id, asset_id)
                        except DutyRefused:
                            continue
                        accessible_evidence.append(
                            {
                                "asset_id": asset.asset_id,
                                "project_id": asset.project_id,
                                "author_id": asset.author_id,
                                "content": asset.content,
                            }
                        )
                        evidence_notes.append(
                            f"Shared evidence {asset.asset_id} (project {asset.project_id}): {asset.content[:500]}"
                        )
            claim = work_run_claim_from_context(db, ctx, purpose="peer_response")

        peer_receipt: dict[str, Any] = {
            "kind": "peer_request",
            "request_id": peer.request_id,
            "request_key": peer.request_key,
            "project_id": peer.project_id,
            "requester_agent_id": peer.requester_agent_id,
            "question": peer.question,
            "expected_decision": peer.expected_decision,
            "deadline_ns": peer.deadline_ns,
            "evidence": accessible_evidence,
        }
        collab = build_core_collab_tool_contribution(
            writer=ctx.writer,
            claim=claim,
            purpose=claim.purpose,
            tx_lock=ctx.tx_lock,
            include_messaging=peer.project_id is not None,
            include_peer_request=True,
            include_peer_respond=True,
            include_handoffs=False,
            include_roster=True,
            scoped_project_id=peer.project_id,
        )
        run_res = self._deps.shift_runner.run(
            ctx,
            mandate_summary=(
                "Respond to a scoped peer decision request within your standing grant. "
                "Do not take over the requester's work."
            ),
            instruction=(
                f"Review peer request ({peer.request_id}) from {peer.requester_agent_id}: "
                f"{peer.question} (expected decision: {peer.expected_decision}). "
                "Use respond_peer_request to answer or decline."
            ),
            purpose="peer_response",
            base_tools=collab.specs,
            base_handlers=collab.handlers,
            history_notes=tuple(evidence_notes),
            open_receipts=(peer_receipt, *child_receipts),
            include_core_desk_tools=False,
            include_plugin_tools=False,
            extra_recoverable_exceptions=collab.recoverable_exceptions,
        )
        if run_res.run_status == "waiting":
            return DutyOutcome(
                disposition="waiting",
                wait_until_ns=run_res.wait_until_ns,
                wait_event_key=run_res.wait_event_key,
                wait_reason="waiting_for_peer_response",
                result={"request_id": peer.request_id, "waiting": True},
            )
        blocked_no_peer = False
        with write_uow(ctx.writer, tx_lock=ctx.tx_lock) as db:
            post_row = db.execute(
                """SELECT state, deadline_ns
                   FROM work_peer_requests
                   WHERE company_id = ? AND request_id = ?""",
                (ctx.company_id, peer.request_id),
            ).fetchone()
            post_state = str(post_row[0]) if post_row is not None else "open"
            post_deadline = (
                int(post_row[1]) if post_row is not None else peer.deadline_ns
            )
            if post_state == "open":
                if ctx.now_ns >= post_deadline:
                    resolve_peer_request(
                        db,
                        company_id=ctx.company_id,
                        request_id=peer.request_id,
                        actor_id="work_scheduler",
                        resolution_state="expired",
                        response_payload={"reason": "deadline_elapsed"},
                        caller_response_run_id=ctx.run.run_id,
                        now_ns=ctx.now_ns,
                    )
                    return DutyOutcome(
                        disposition="completed",
                        result={"request_id": peer.request_id, "outcome": "expired"},
                    )
                blocked_no_peer = True
        if blocked_no_peer:
            reconciled_trace = reconcile_shift_episode_outcome(
                ctx,
                outcome_status="blocked",
                reason="no_peer_disposition",
                causal_method_trace=run_res.causal_method_trace,
            )
            return DutyOutcome(
                disposition="blocked",
                result={
                    "request_id": peer.request_id,
                    "reason": "no_peer_disposition",
                    "causal_method_trace": reconciled_trace,
                },
            )
        peer_out_bytes = run_res.shift_result.output_text.strip().encode("utf-8")
        return DutyOutcome(
            disposition="completed",
            result={
                "request_id": peer.request_id,
                "outcome": post_state,
                "output_digest": sha256(peer_out_bytes).hexdigest(),
                "output_bytes": len(peer_out_bytes),
                "cost_minor_units": run_res.settlement.cost_minor_units,
                "journal_entry_id": run_res.settlement.journal_entry_id,
                "active_snapshot_id": run_res.active_snapshot_id,
                "causal_method_trace": run_res.causal_method_trace,
                "stable_prefix_sha256": run_res.stable_prefix_sha256,
            },
        )

    def run_collaborative_review(
        self,
        ctx: DutyContext,
        *,
        purpose: str,
        mandate_summary: str,
        default_instruction: str,
    ) -> DutyOutcome:
        agent_id = ctx.work_item.owner_agent_id
        receipts: list[dict[str, Any]] = []
        notes: list[str] = []
        with write_uow(ctx.writer, tx_lock=ctx.tx_lock) as db:
            hydrate_resolved_child_peer_outcome(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
                agent_id=agent_id,
                receipts=receipts,
                notes=notes,
            )
            handoff_row = db.execute(
                """SELECT offer_id, sender_id, asset_id, expected_result
                   FROM work_handoff
                   WHERE company_id = ? AND successor_work_item_id = ?
                     AND recipient_id = ? AND state = 'accepted'""",
                (ctx.company_id, ctx.work_item.work_item_id, agent_id),
            ).fetchone()
            if handoff_row is not None:
                offer_id, sender_id, asset_id, expected_result = handoff_row
                handoff_receipt: dict[str, Any] = {
                    "kind": "accepted_handoff",
                    "offer_id": str(offer_id),
                    "sender_id": str(sender_id),
                    "asset_id": str(asset_id),
                    "expected_result": str(expected_result),
                }
                try:
                    asset = shared_asset_for(db, agent_id, str(asset_id))
                    handoff_receipt["shared_content"] = asset.content
                    handoff_receipt["project_id"] = asset.project_id
                    notes.append(
                        f"Accepted handoff {offer_id} from {sender_id} "
                        f"(asset {asset_id}, expected: {expected_result}): {asset.content[:500]}"
                    )
                except DutyRefused:
                    notes.append(
                        f"Accepted handoff {offer_id} from {sender_id} "
                        f"(expected: {expected_result})"
                    )
                receipts.append(handoff_receipt)
            brief_row = db.execute(
                """SELECT project_id, deliverable
                   FROM project_review_brief
                   WHERE company_id = ? AND work_item_id = ? AND agent_id = ?""",
                (ctx.company_id, ctx.work_item.work_item_id, agent_id),
            ).fetchone()
            if brief_row is not None:
                proj_id, deliverable = str(brief_row[0]), str(brief_row[1])
                try:
                    _, members, _ = project_membership(db, proj_id)
                except DutyRefused:
                    members = ()
                if agent_id in members:
                    receipts.append(
                        {
                            "kind": "project_review_brief",
                            "project_id": proj_id,
                            "deliverable": deliverable,
                        }
                    )
                    notes.append(
                        f"Assigned company deliverable for project {proj_id}: {deliverable}"
                    )
            claim = work_run_claim_from_context(db, ctx, purpose=purpose)

        has_peer_prompt = (
            purpose == "handoff_followup"
            or handoff_row is not None
            or any(
                isinstance(r, dict) and r.get("kind") == "peer_outcome"
                for r in receipts
            )
        )
        collab = build_core_collab_tool_contribution(
            writer=ctx.writer,
            claim=claim,
            purpose=claim.purpose,
            tx_lock=ctx.tx_lock,
            include_messaging=True,
            include_peer_request=True,
            include_peer_respond=False,
            include_handoffs=True,
            include_roster=True,
        )
        run_res = self._deps.shift_runner.run(
            ctx,
            mandate_summary=mandate_summary,
            instruction=default_instruction,
            purpose=purpose,
            base_tools=collab.specs,
            base_handlers=collab.handlers,
            history_notes=tuple(notes),
            open_receipts=tuple(receipts),
            include_core_desk_tools=not has_peer_prompt,
            include_plugin_tools=True,
            extra_recoverable_exceptions=collab.recoverable_exceptions,
        )
        if run_res.run_status == "waiting":
            return DutyOutcome(
                disposition="waiting",
                wait_until_ns=run_res.wait_until_ns,
                wait_event_key=run_res.wait_event_key,
                wait_reason="waiting_for_peer_response",
                result={"waiting": True},
            )
        collab_out_bytes = run_res.shift_result.output_text.strip().encode("utf-8")
        return DutyOutcome(
            disposition="completed",
            result={
                "output_digest": sha256(collab_out_bytes).hexdigest(),
                "output_bytes": len(collab_out_bytes),
                "provider": run_res.shift_result.provider,
                "model": run_res.shift_result.model,
                "cost_minor_units": run_res.settlement.cost_minor_units,
                "journal_entry_id": run_res.settlement.journal_entry_id,
                "active_snapshot_id": run_res.active_snapshot_id,
                "causal_method_trace": run_res.causal_method_trace,
                "stable_prefix_sha256": run_res.stable_prefix_sha256,
            },
        )

    def run_work_assessment_review(self, ctx: DutyContext) -> DutyOutcome:
        return self.run_collaborative_review(
            ctx,
            purpose="assessment_review",
            mandate_summary="Execute company assessment review within your standing grant.",
            default_instruction=(
                f"Execute assessment review work item {ctx.work_item.work_item_id} "
                "using your Desk, shared project assets, and collaboration tools."
            ),
        )

    def run_work_handoff_followup(self, ctx: DutyContext) -> DutyOutcome:
        return self.run_collaborative_review(
            ctx,
            purpose="handoff_followup",
            mandate_summary="Execute accepted handoff follow-up within your standing grant.",
            default_instruction=(
                f"Execute handoff follow-up work item {ctx.work_item.work_item_id} "
                "using the accepted handoff receipt, shared evidence, and collaboration tools."
            ),
        )
