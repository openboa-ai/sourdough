"""Idle-window episodic dreaming duty handler and precondition."""

from __future__ import annotations

import json
from typing import Any

from ...duty_run import work_run_claim_from_context
from ...method_learning import audit_episodes_for_dreaming
from ...storage.db import write_uow
from ..registry import DutyContext, DutyOutcome
from ..work_kernel import WorkItem, assert_run_fencing
from .deps import DUTY_WORK_DREAMING, CoreDutyDeps


def dreaming_precondition(conn: Any, item: WorkItem) -> tuple[bool, str]:
    blocking = conn.execute(
        """SELECT 1 FROM work_items
           WHERE company_id = ? AND work_item_id != ? AND state = 'due'
             AND (
                 duty_kind NOT IN ('work.dreaming', 'work.discovery_review')
                 OR (owner_agent_id = ? AND duty_kind != 'work.dreaming')
             )
           LIMIT 1""",
        (item.company_id, item.work_item_id, item.owner_agent_id),
    ).fetchone()
    if blocking is not None:
        return False, "mandatory_work_pending"
    return True, "idle_window"


class DreamingDuties:
    """Audits recent Agent episodes during idle windows to draft candidate method deltas."""

    def __init__(self, deps: CoreDutyDeps) -> None:
        self._deps = deps

    def run_work_dreaming(self, ctx: DutyContext) -> DutyOutcome:
        agent_id = ctx.work_item.owner_agent_id
        try:
            payload = json.loads(ctx.work_item.payload_json)
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        def _int_opt(key: str, default: int | None, minimum: int = 0) -> int | None:
            val = payload.get(key)
            if val is None:
                return default
            if type(val) is int and val >= minimum:
                return val
            return default

        episode_limit = _int_opt("episode_limit", 20, minimum=1) or 20
        min_pattern_count = _int_opt("min_pattern_count", 2, minimum=2) or 2
        max_proposals = _int_opt("max_proposals", 3, minimum=1) or 3
        raw_cooldown_ns = _int_opt("cooldown_ns", None, minimum=0)
        max_runs_per_window = _int_opt("max_runs_per_window", None, minimum=1)
        raw_window_ns = _int_opt("window_ns", None, minimum=1)
        next_timer_delay_ns = _int_opt("next_timer_delay_ns", None, minimum=1)
        resp_id = payload.get("responsibility_id")
        eff_resp_id = (
            resp_id.strip() if isinstance(resp_id, str) and resp_id.strip() else None
        )

        next_cond: dict[str, Any] | None = None
        with write_uow(ctx.writer, tx_lock=ctx.tx_lock) as db:
            assert_run_fencing(
                db, ctx.run.run_id, ctx.run.fencing_token, now_ns=ctx.now_ns
            )
            resp_row = None
            if eff_resp_id is not None:
                resp_row = db.execute(
                    """SELECT status, readable_scope_json
                       FROM work_responsibilities
                       WHERE company_id = ? AND responsibility_id = ?""",
                    (ctx.company_id, eff_resp_id),
                ).fetchone()
                if resp_row is not None and str(resp_row[0]) == "active":
                    try:
                        scope_cfg = json.loads(str(resp_row[1]))
                    except json.JSONDecodeError:
                        scope_cfg = None
                    if isinstance(scope_cfg, dict):
                        if (
                            max_runs_per_window is None
                            and type(scope_cfg.get("max_wakes_per_window")) is int
                            and scope_cfg["max_wakes_per_window"] >= 1
                        ):
                            max_runs_per_window = scope_cfg["max_wakes_per_window"]
                        if (
                            raw_window_ns is None
                            and type(scope_cfg.get("wake_window_ns")) is int
                            and scope_cfg["wake_window_ns"] >= 1
                        ):
                            raw_window_ns = scope_cfg["wake_window_ns"]
                        if (
                            raw_cooldown_ns is None
                            and type(scope_cfg.get("cooldown_ns")) is int
                            and scope_cfg["cooldown_ns"] >= 0
                        ):
                            raw_cooldown_ns = scope_cfg["cooldown_ns"]
            window_ns = (
                raw_window_ns if raw_window_ns is not None else 3_600_000_000_000
            )
            cooldown_ns = raw_cooldown_ns if raw_cooldown_ns is not None else 0
            if (
                eff_resp_id is not None
                and resp_row is not None
                and str(resp_row[0]) == "active"
                and next_timer_delay_ns is not None
            ):
                next_cond = {
                    "responsibility_id": eff_resp_id,
                    "trigger_kind": "timer",
                    "due_at_ns": ctx.now_ns + next_timer_delay_ns,
                    "cooldown_ns": cooldown_ns,
                }

            if cooldown_ns > 0:
                last_row = db.execute(
                    """SELECT MAX(updated_at_ns)
                       FROM work_runs
                       WHERE company_id = ? AND agent_id = ?
                         AND duty_kind = ? AND status = 'completed'
                         AND run_id != ?
                         AND outcome_json IS NOT NULL
                         AND json_valid(outcome_json)
                         AND json_extract(outcome_json, '$.audited') = 1""",
                    (ctx.company_id, agent_id, DUTY_WORK_DREAMING, ctx.run.run_id),
                ).fetchone()
                if (
                    last_row is not None
                    and last_row[0] is not None
                    and ctx.now_ns < int(last_row[0]) + cooldown_ns
                ):
                    return DutyOutcome(
                        disposition="completed",
                        next_condition=next_cond,
                        result={
                            "audited": False,
                            "skipped_reason": "cooldown_active",
                            "episodes_audited": 0,
                            "patterns_detected": 0,
                            "proposals": [],
                        },
                    )

            if max_runs_per_window is not None:
                window_start = max(0, ctx.now_ns - window_ns)
                run_count = int(
                    db.execute(
                        """SELECT COUNT(*)
                           FROM work_runs
                           WHERE company_id = ? AND agent_id = ?
                             AND duty_kind = ? AND status = 'completed'
                             AND run_id != ? AND updated_at_ns >= ?
                             AND outcome_json IS NOT NULL
                             AND json_valid(outcome_json)
                             AND json_extract(outcome_json, '$.audited') = 1""",
                        (
                            ctx.company_id,
                            agent_id,
                            DUTY_WORK_DREAMING,
                            ctx.run.run_id,
                            window_start,
                        ),
                    ).fetchone()[0]
                )
                if run_count >= max_runs_per_window:
                    return DutyOutcome(
                        disposition="completed",
                        next_condition=next_cond,
                        result={
                            "audited": False,
                            "skipped_reason": "budget_cap_reached",
                            "episodes_audited": 0,
                            "patterns_detected": 0,
                            "proposals": [],
                        },
                    )

            claim = work_run_claim_from_context(db, ctx, purpose="dreaming")
            audit_res = audit_episodes_for_dreaming(
                db,
                claim,
                episode_limit=episode_limit,
                min_pattern_count=min_pattern_count,
                max_proposals=max_proposals,
                now_ns=ctx.now_ns,
            )

        return DutyOutcome(
            disposition="completed",
            next_condition=next_cond,
            result={
                "audited": True,
                "skipped_reason": audit_res.skipped_reason,
                "episodes_audited": audit_res.episodes_audited,
                "patterns_detected": len(audit_res.patterns_detected),
                "patterns": list(audit_res.patterns_detected),
                "proposals": [
                    {
                        "delta_id": d.delta_id,
                        "method_id": d.method_id,
                        "revision_sha256": d.revision_sha256,
                        "delta_kind": d.delta_kind,
                        "status": d.status,
                    }
                    for d in audit_res.drafted_deltas
                ],
            },
        )
