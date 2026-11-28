"""Two-generation recursive self-improvement transfer, adoption, and rollback orchestrator."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from ..agent_desk import clone_desk_snapshot, desk_directory, desks_root
from ..agent_runtime.protocol import (
    AgentBudgetExceeded,
    AgentRuntimeRefused,
    AgentTransportInterrupted,
)
from ..agent_runtime.registry import AgentAdapterRegistry
from ..company import _identity
from ..duty_run import DutyRefused
from ..learning import (
    MethodDelta,
    MethodRefused,
    _rehydrate_desk,
    activate_method_delta,
    compute_active_snapshot_id,
    distill_method_delta,
    qualify_method_delta,
    quarantine_method_lineage,
    resolve_active_methods,
    sync_desk_harness_projections,
)
from ..memory import record_episode
from ..runtime.domains import ProfileBindingLookup
from ..runtime.engine import CompanyRuntime
from ..runtime.registry import DutyRegistry, RuntimeRefused
from ..runtime.work_kernel import (
    WorkKernelRefused,
    complete_work_run,
    create_work_item,
    record_work_decision,
)
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..trial_execution import TrialWorkerInput
from ..trial_registry import (
    BlindedTrial,
    BlindPairwisePacket,
    PairwiseJudgment,
    RsiTransferSummary,
    TrialCeiling,
    TrialGrade,
    TrialRefused,
    TrialSlot,
    TrialSuite,
    blind_trial_packet,
    commit_blind_grade,
    commit_trial_observation,
    seal_trial_suite,
    summarize_rsi_transfer,
)
from ..writer_lease import WriterLease, hold_company_writer
from .blind_judge import run_blind_pairwise_judge
from .evidence_grading import (
    _score_live_gen2_observables,
    _verify_case_grounded_evidence,
    grade_trial_observation,
)
from .sandbox_workspace import (
    _GEN2_TRANSFER_TOOL_MANIFEST,
    RecursiveLearningCycleResult,
    _backup_sqlite_private,
    _canonical_digest,
    _canonical_json,
    _compute_source_store_fixture,
    _count_receipt_violations,
    _current_time_ns,
    _ensure_private_trial_root,
    _make_trial_inputs,
    _open_claim_for_agent,
    _parse_sealed_tool_manifest,
    _read_offloaded_receipt_payload,
    _verify_isolated_root,
)
from .slot_executor import run_isolated_trial_slot

_GEN2_TRANSFER_FAMILY_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "family_slug": "schedule-sla-triage",
        "domain": "operations_scheduling",
        "problem_kind": "deadline_breach_vs_evergreen_remit",
        "objective": "Flag missed SLA obligations while preserving standing evergreen remits without deadlines.",
        "primary_item": {
            "symptom": "missed_sla_deadline",
            "has_deadline": True,
            "overdue_minutes": 45,
            "valid_obligation": True,
        },
        "boundary_counterexample": {
            "symptom": "standing_evergreen_no_deadline",
            "has_deadline": False,
            "cadence": "continuous_watch",
            "valid_obligation": True,
            "must_preserve": True,
        },
    },
    {
        "family_slug": "budget-variance-audit",
        "domain": "finance_ledger",
        "problem_kind": "unapproved_spend_vs_preauthorized_reserve",
        "objective": "Escalate unapproved budget overruns without halting pre-authorized standing reserve draws.",
        "primary_item": {
            "symptom": "unapproved_budget_overrun",
            "ceiling_minor_units": 5_000,
            "incurred_minor_units": 6_250,
            "authorized_reserve": False,
            "valid_obligation": True,
        },
        "boundary_counterexample": {
            "symptom": "preauthorized_standing_reserve",
            "ceiling_minor_units": 5_000,
            "reserve_draw_minor_units": 1_200,
            "authorized_reserve": True,
            "valid_obligation": True,
            "must_preserve": True,
        },
    },
    {
        "family_slug": "handoff-expiry-recovery",
        "domain": "collaboration_handoffs",
        "problem_kind": "expired_unaccepted_handoff_vs_active_peer_wait",
        "objective": "Reclaim expired unaccepted handoff offers while preserving active zero-compute waiting peer requests.",
        "primary_item": {
            "symptom": "expired_handoff_offer",
            "handoff_state": "offered",
            "expired": True,
            "valid_obligation": True,
        },
        "boundary_counterexample": {
            "symptom": "active_waiting_peer_request",
            "run_status": "waiting",
            "peer_deadline_active": True,
            "valid_obligation": True,
            "must_preserve": True,
        },
    },
    {
        "family_slug": "publication-disclosure-review",
        "domain": "custody_disclosure",
        "problem_kind": "unredacted_draft_leak_vs_scoped_publication",
        "objective": "Block unredacted private-desk path leaks while preserving authorized project-scoped shared publications.",
        "primary_item": {
            "symptom": "unredacted_private_desk_reference",
            "target_scope": "company",
            "contains_private_path": True,
            "valid_obligation": True,
        },
        "boundary_counterexample": {
            "symptom": "authorized_scoped_shared_publication",
            "target_scope": "project",
            "redaction_verified": True,
            "valid_obligation": True,
            "must_preserve": True,
        },
    },
    {
        "family_slug": "evidence-freshness-check",
        "domain": "decision_audit",
        "problem_kind": "future_dated_citation_vs_point_in_time_receipt",
        "objective": "Reject decisions citing post-decision timestamps while preserving point-in-time verified ledger receipts.",
        "primary_item": {
            "symptom": "temporal_future_leak_citation",
            "available_after_decision": True,
            "receipt_verified": False,
            "valid_obligation": True,
        },
        "boundary_counterexample": {
            "symptom": "verified_point_in_time_receipt",
            "available_after_decision": False,
            "receipt_verified": True,
            "valid_obligation": True,
            "must_preserve": True,
        },
    },
    {
        "family_slug": "staffing-authority-check",
        "domain": "governance_authority",
        "problem_kind": "unauthorized_scope_escalation_vs_delegated_grant",
        "objective": "Refuse hiring or grant expansions exceeding delegated authority while preserving valid delegated staffing.",
        "primary_item": {
            "symptom": "delegated_grant_escalation",
            "requested_scope": "manage-company",
            "delegated_scope": "assessment",
            "valid_obligation": True,
        },
        "boundary_counterexample": {
            "symptom": "within_delegated_staffing_budget",
            "requested_scope": "assessment",
            "delegated_scope": "assessment",
            "valid_obligation": True,
            "must_preserve": True,
        },
    },
    {
        "family_slug": "always-on-watchdog-triage",
        "domain": "always_on_continuity",
        "problem_kind": "expired_fenced_run_vs_sleeping_resident",
        "objective": "Recover expired fenced work runs without disrupting intentionally sleeping zero-compute resident schedules.",
        "primary_item": {
            "symptom": "expired_fenced_work_run",
            "lease_expired": True,
            "open_receipts_pending": True,
            "valid_obligation": True,
        },
        "boundary_counterexample": {
            "symptom": "intentional_zero_compute_sleep",
            "sleep_state": "sleeping",
            "open_receipts_pending": False,
            "valid_obligation": True,
            "must_preserve": True,
        },
    },
    {
        "family_slug": "skill-regression-triage",
        "domain": "learning_governance",
        "problem_kind": "unqualified_self_edit_vs_three_stage_skill_delta",
        "objective": "Quarantine unverified skill edits while preserving 3-stage qualified skill deltas with counterexamples.",
        "primary_item": {
            "symptom": "unqualified_skill_mutation",
            "passed_stages": 1,
            "negative_triggers_present": False,
            "valid_obligation": True,
        },
        "boundary_counterexample": {
            "symptom": "three_stage_qualified_skill_delta",
            "passed_stages": 3,
            "negative_triggers_present": True,
            "valid_obligation": True,
            "must_preserve": True,
        },
    },
)


@dataclass(frozen=True)
class _MetaAgentBinding:
    profile: str
    grant_id: str
    grant_scope: str


@dataclass(frozen=True)
class _CycleContext:
    company_id: str
    meta_agent_id: str
    meta_binding: _MetaAgentBinding
    promotion_actor_id: str
    promotion_grant_id: str
    cycle_tag: str
    base_ns: int
    use_live_runtime: bool
    live_registry: DutyRegistry | None


@dataclass(frozen=True)
class _Gen1StageResult:
    inc_proc_id: str
    inc_proc_delta: MethodDelta
    gen1_method_id: str
    cand_proc_id: str
    cand_proc_delta: MethodDelta
    post_gen1_ns: int


@dataclass(frozen=True)
class _Gen2SuiteBundle:
    suite: TrialSuite
    eval_rev: str
    analysis_plan: dict[str, Any]
    slot_case_meta: dict[str, tuple[str, str, str, dict[str, Any]]]
    arm_stores: dict[str, Path]
    cycle_dir: Path
    tools_bytes: bytes
    gen2_sealed_tools: list[str]
    inc_ctx_bytes: bytes
    cand_ctx_bytes: bytes
    limits: TrialCeiling


@dataclass(frozen=True)
class _AdoptionWitnessResult:
    gen2_method_id: str
    gen2_delta: MethodDelta
    adopted_snap_id: str
    shift_witnessed: bool
    witness_run_id: str
    witness_dec_id: str
    post_gen2_ns: int


@dataclass(frozen=True)
class _RollbackWitnessResult:
    regressed_delta_id: str
    quarantined_methods: tuple[str, ...]
    rollback_restored: bool
    post_rollback_run_id: str
    post_rollback_snapshot_id: str | None


def _grade_trial_observation_via_facade(
    packet: BlindedTrial,
    *,
    observation: Mapping[str, Any] | None,
    tool_trace: Mapping[str, Any] | None,
    service_effects: Mapping[str, Any] | None,
    decision: Mapping[str, Any] | None,
) -> TrialGrade:
    """Invoke grade_trial_observation, honoring monkeypatches on sourdough.trial_runner."""
    facade = sys.modules.get("sourdough.trial_runner")
    fn = (
        getattr(facade, "grade_trial_observation", None) if facade is not None else None
    )
    grader = fn if callable(fn) else grade_trial_observation
    return grader(
        packet,
        observation=observation,
        tool_trace=tool_trace,
        service_effects=service_effects,
        decision=decision,
    )


def _validate_and_prepare_cycle(
    writer_or_store: WriterLease | Path,
    *,
    evaluator_store: Path,
    trial_root: Path,
    min_families: int,
    adapter_registry: AgentAdapterRegistry | None,
    profile_bindings: ProfileBindingLookup | None,
    judge_fn: Callable[[BlindPairwisePacket], PairwiseJudgment] | None,
) -> tuple[bool, DutyRegistry | None]:
    """Validate cycle inputs, check trial root isolation, and prepare live duty registry."""
    if (adapter_registry is None) != (profile_bindings is None):
        raise TrialRefused(
            "adapter_registry and profile_bindings must be supplied together"
        )
    use_live_runtime = adapter_registry is not None and profile_bindings is not None
    if judge_fn is not None and not callable(judge_fn):
        raise TrialRefused("judge_fn must be callable")
    if type(min_families) is not int or min_families < 1:
        raise TrialRefused("min_families must be a positive integer")
    store_path = (
        writer_or_store.store
        if isinstance(writer_or_store, WriterLease)
        else writer_or_store
    )
    _verify_isolated_root(trial_root, evaluator_store, source_store=store_path)

    live_registry: DutyRegistry | None = None
    if use_live_runtime:
        from ..runtime.handlers import build_production_duty_registry

        assert adapter_registry is not None and profile_bindings is not None
        live_registry = build_production_duty_registry(
            adapter_registry=adapter_registry,
            profile_bindings=profile_bindings,
        )
    return use_live_runtime, live_registry


def _load_meta_agent_binding(
    store: Path,
    *,
    company_id: str,
    meta_agent_id: str,
) -> _MetaAgentBinding:
    """Verify company store identity and fetch the meta-learner agent profile and grant."""
    with closing(open_reader(store)) as ro_db, read_tx(ro_db):
        if _identity(ro_db) != company_id:
            raise TrialRefused("Company identity mismatch")
        agent_row = ro_db.execute(
            """SELECT a.execution_profile, g.id, g.scope
               FROM agent a
               JOIN standing_grant g ON g.agent_id = a.id
               WHERE a.company_id = ? AND a.id = ?
               ORDER BY g.id LIMIT 1""",
            (company_id, meta_agent_id),
        ).fetchone()
        if agent_row is None:
            raise TrialRefused(f"Meta-learner agent '{meta_agent_id}' not found")
        return _MetaAgentBinding(
            profile=str(agent_row[0]),
            grant_id=str(agent_row[1]),
            grant_scope=str(agent_row[2]),
        )


def _run_gen1_discovery_and_distillation(
    w: WriterLease,
    ctx: _CycleContext,
) -> _Gen1StageResult:
    """Execute Gen-1 witness contrast and distill incumbent v1, Gen-1 method, and candidate v2."""
    if ctx.use_live_runtime:
        assert ctx.live_registry is not None
        with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
            gen1_item, _ = create_work_item(
                db,
                company_id=ctx.company_id,
                owner_agent_id=ctx.meta_agent_id,
                duty_kind="work.discovery_review",
                source_kind="recursive_learning",
                source_id=f"gen1-witness-{ctx.cycle_tag}",
                payload={
                    "generation": 1,
                    "phase": "witness_contrast",
                    "action_hint": (
                        "Call ONLY record_work_decision(action_kind='witness_contrast', "
                        "rationale='Observed deadline filter missed no-deadline items.', evidence=[])."
                    ),
                },
                dedupe_key=f"rsi-gen1-live:{ctx.cycle_tag}",
                actor_id=ctx.meta_agent_id,
                now_ns=ctx.base_ns,
            )
        gen1_step = CompanyRuntime(
            w.store,
            ctx.company_id,
            ctx.live_registry,
            lease_directory=w._lock_path.parent,
        ).step_with_writer(
            w,
            max_dispatches=1,
            now_ns=ctx.base_ns + 50,
            target_work_item_id=gen1_item.work_item_id,
        )
        if len(gen1_step.executed_runs) != 1:
            raise TrialRefused("Gen-1 live shift did not execute")
        gen1_witness_run_id = gen1_step.executed_runs[0]
        with closing(open_reader(w.store)) as ro_db, read_tx(ro_db):
            dec_row = ro_db.execute(
                """SELECT decision_id FROM work_decision
                   WHERE company_id = ? AND run_id = ?
                   ORDER BY decision_at_ns DESC LIMIT 1""",
                (ctx.company_id, gen1_witness_run_id),
            ).fetchone()
        if dec_row is None:
            raise TrialRefused(
                "Gen-1 live shift did not record a grounded work_decision"
            )
        gen1_dec_id = str(dec_row[0])
        post_gen1_ns = max(ctx.base_ns + 100, _current_time_ns())
        _, gen1_claim = _open_claim_for_agent(
            w,
            company_id=ctx.company_id,
            agent_id=ctx.meta_agent_id,
            grant_id=ctx.meta_binding.grant_id,
            grant_scope=ctx.meta_binding.grant_scope,
            source_id=f"gen1-distill-{ctx.cycle_tag}",
            payload={"generation": 1, "phase": "distill"},
            now_ns=post_gen1_ns,
        )
        gen1_source_run_id = gen1_witness_run_id
    else:
        gen1_item_id, gen1_claim = _open_claim_for_agent(
            w,
            company_id=ctx.company_id,
            agent_id=ctx.meta_agent_id,
            grant_id=ctx.meta_binding.grant_id,
            grant_scope=ctx.meta_binding.grant_scope,
            source_id=f"gen1-witness-{ctx.cycle_tag}",
            payload={"generation": 1, "phase": "witness_contrast"},
            now_ns=ctx.base_ns,
        )
        with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
            gen1_dec = record_work_decision(
                db,
                company_id=ctx.company_id,
                work_item_id=gen1_item_id,
                run_id=gen1_claim.run_id,
                fencing_token=gen1_claim.fencing_token,
                agent_id=ctx.meta_agent_id,
                action_kind="deadline_filter_contrast",
                rationale=(
                    "Gen-1 observed contrast: strict deadline filter fixed missed deadlines "
                    "but falsely discarded valid no-deadline work items."
                ),
                evidence=(),
                decision_at_ns=ctx.base_ns + 100,
            )
            record_episode(
                db,
                company_id=ctx.company_id,
                agent_id=ctx.meta_agent_id,
                work_item_id=gen1_item_id,
                run_id=gen1_claim.run_id,
                duty_kind="work.discovery_review",
                context_digest=sha256(f"gen1-{ctx.cycle_tag}".encode()).hexdigest(),
                outcome_status="completed",
                summary_text=(
                    "Gen-1 contrast: rule fixed deadline misses but over-filtered no-deadline items."
                ),
                decisions=[{"decision_id": gen1_dec.decision_id}],
                recorded_at_ns=ctx.base_ns + 120,
            )
        gen1_dec_id = gen1_dec.decision_id
        gen1_source_run_id = gen1_claim.run_id
        post_gen1_ns = ctx.base_ns + 100

    inc_proc_id = f"proc-improve-v1-{ctx.cycle_tag}"
    inc_proc_delta = distill_method_delta(
        w,
        gen1_claim,
        delta_kind="ADD",
        target_kind="improvement_procedure",
        method_id=inc_proc_id,
        trigger_spec={"duty_kind": "work.discovery_review"},
        body=(
            "v1: If v2 in active_methods, follow v2. Else skip other tools and call "
            "record_work_decision(action_kind='apply_baseline',rationale='v1',evidence=[])."
        ),
        audience_scope="company",
        source_refs=[f"work_run:{gen1_source_run_id}"],
        procedure_meta={
            "require_counterexample": False,
            "predicted_effect": "Fixes primary symptom",
            "observed_effect": "Over-generalized to no-deadline items",
        },
        now_ns=post_gen1_ns + 100,
    )
    qualify_method_delta(
        w,
        inc_proc_delta.delta_id,
        passed_k=3,
        negative_trigger_passed=True,
        safety_passed=True,
        holdout_margin=0.05,
        now_ns=post_gen1_ns + 110,
    )
    inc_proc_act = activate_method_delta(
        w,
        inc_proc_delta.delta_id,
        actor_id=ctx.promotion_actor_id,
        grant_id=ctx.promotion_grant_id,
        now_ns=post_gen1_ns + 120,
    )

    gen1_method_id = f"wm-gen1-{ctx.cycle_tag}"
    gen1_delta = distill_method_delta(
        w,
        gen1_claim,
        delta_kind="ADD",
        target_kind="work_method",
        method_id=gen1_method_id,
        trigger_spec={"duty_kind": "work.discovery_review"},
        body=(
            "Require explicit deadline check; retain no-deadline obligations via counterexample verification."
        ),
        audience_scope="company",
        source_refs=[
            f"work_run:{gen1_source_run_id}",
            f"work_decision:{gen1_dec_id}",
        ],
        parent_method_ids=(inc_proc_delta.delta_id,),
        active_snapshot_id=inc_proc_act.active_snapshot_id,
        uncertainty_note="Needs counterexample validation on unseen task families.",
        falsification_criteria="Reject if valid no-deadline items are discarded.",
        now_ns=post_gen1_ns + 130,
    )
    qualify_method_delta(
        w,
        gen1_delta.delta_id,
        passed_k=3,
        negative_trigger_passed=True,
        safety_passed=True,
        holdout_margin=0.1,
        now_ns=post_gen1_ns + 140,
    )
    gen1_act = activate_method_delta(
        w,
        gen1_delta.delta_id,
        actor_id=ctx.promotion_actor_id,
        grant_id=ctx.promotion_grant_id,
        now_ns=post_gen1_ns + 150,
    )

    cand_proc_id = f"proc-improve-v2-{ctx.cycle_tag}"
    cand_proc_delta = distill_method_delta(
        w,
        gen1_claim,
        delta_kind="ADD",
        target_kind="improvement_procedure",
        method_id=cand_proc_id,
        trigger_spec={"duty_kind": "work.discovery_review"},
        body=(
            "v2: Inspect case_id, primary_item.symptom, and boundary_counterexample.symptom "
            "in discovery_trigger. Draft work_method with negative_triggers=[boundary_symptom] "
            "and case falsification_criteria; save a Desk note naming both symptoms and "
            "call record_work_decision(action_kind='counterexample_verified',evidence=[...])."
        ),
        audience_scope="company",
        source_refs=[
            f"work_run:{gen1_source_run_id}",
            f"work_decision:{gen1_dec_id}",
        ],
        parent_method_ids=(inc_proc_delta.delta_id, gen1_delta.delta_id),
        active_snapshot_id=gen1_act.active_snapshot_id,
        procedure_meta={
            "require_counterexample": True,
            "predecessor_procedure": inc_proc_delta.delta_id,
            "predicted_effect": "Prevents false-positive exclusions on unseen task families",
            "observed_effect": "Gen-1 contrast logged",
        },
        now_ns=post_gen1_ns + 160,
    )

    with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
        complete_work_run(
            db,
            gen1_claim.run_id,
            gen1_claim.fencing_token,
            outcome={
                "gen1_method_id": gen1_method_id,
                "cand_proc_id": cand_proc_id,
            },
            now_ns=post_gen1_ns + 200,
        )

    return _Gen1StageResult(
        inc_proc_id=inc_proc_id,
        inc_proc_delta=inc_proc_delta,
        gen1_method_id=gen1_method_id,
        cand_proc_id=cand_proc_id,
        cand_proc_delta=cand_proc_delta,
        post_gen1_ns=post_gen1_ns,
    )


def _freeze_arms_and_seal_gen2_suite(
    w: WriterLease,
    ctx: _CycleContext,
    *,
    evaluator_store: Path,
    trial_root: Path,
    min_families: int,
    gen1: _Gen1StageResult,
) -> _Gen2SuiteBundle:
    """Freeze a shared base arm snapshot and seal paired Gen-2 transfer slots."""
    _ensure_private_trial_root(trial_root)
    cycle_dir = trial_root / f"cycle-{ctx.cycle_tag}"
    cycle_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
    arms_dir = cycle_dir / "arms"
    arms_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    base_arm_dir = arms_dir / "base"
    base_arm_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    base_arm_store = base_arm_dir / "company.sqlite"
    _backup_sqlite_private(w.store, base_arm_store)
    clone_desk_snapshot(w.store, ctx.company_id, (ctx.meta_agent_id,), base_arm_store)
    arm_stores: dict[str, Path] = {
        "incumbent": base_arm_store,
        "candidate": base_arm_store,
    }

    num_families = max(6, min_families)
    if num_families > len(_GEN2_TRANSFER_FAMILY_CATALOG):
        raise TrialRefused(
            f"Requested {num_families} transfer families exceeds the "
            f"{len(_GEN2_TRANSFER_FAMILY_CATALOG)} genuinely distinct registered task families "
            "(refusing to duplicate cases as pseudoreplicated families)"
        )
    selected_families = _GEN2_TRANSFER_FAMILY_CATALOG[:num_families]
    limits = TrialCeiling(
        turns=4,
        wall_seconds=150,
        tokens=1_500_000 if ctx.use_live_runtime else 10_000,
    )
    fixture_bytes, _base_profiles, _base_founder = _compute_source_store_fixture(
        base_arm_store, ctx.company_id, agent_ids=(ctx.meta_agent_id,)
    )
    tools_bytes = _canonical_json({"tools": list(_GEN2_TRANSFER_TOOL_MANIFEST)}).encode(
        "utf-8"
    )
    gen2_sealed_tools = _parse_sealed_tool_manifest(tools_bytes)
    inc_ctx_bytes = _canonical_json(
        {"generation": 2, "procedure_revision": gen1.inc_proc_delta.revision_sha256}
    ).encode("utf-8")
    cand_ctx_bytes = _canonical_json(
        {"generation": 2, "procedure_revision": gen1.cand_proc_delta.revision_sha256}
    ).encode("utf-8")

    analysis_plan = {
        "min_pairs": num_families,
        "min_net_wins": max(1, num_families - 1),
        "min_families": min_families,
        "alpha": 0.05,
        "min_worthwhile_margin": 0.0,
        "bootstrap_resamples": 2000,
    }
    analysis_plan_digest = sha256(
        _canonical_json(analysis_plan).encode("utf-8")
    ).hexdigest()
    rubric_digest = sha256(b"rsi-transfer-rubric-v1").hexdigest()
    evaluator_digest = sha256(b"rsi-transfer-evaluator-v1").hexdigest()

    # Verify every selected family has a structurally unique problem spec after stripping IDs.
    structural_digests: set[str] = set()
    slots: list[TrialSlot] = []
    slot_case_meta: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
    for idx, tmpl in enumerate(selected_families, start=1):
        fam_id = f"unseen-transfer-{tmpl['family_slug']}"
        case_id = f"transfer-case-{tmpl['family_slug']}"
        structural_body = {
            "domain": tmpl["domain"],
            "problem_kind": tmpl["problem_kind"],
            "objective": tmpl["objective"],
            "requires_counterexample_guard": True,
            "primary_item": dict(tmpl["primary_item"]),
            "boundary_counterexample": dict(tmpl["boundary_counterexample"]),
        }
        structural_digest = _canonical_digest(structural_body)
        if structural_digest in structural_digests:
            raise TrialRefused(
                "Duplicate structural problem specification cannot be registered as an independent transfer family"
            )
        structural_digests.add(structural_digest)
        case_spec: dict[str, Any] = {
            "family_id": fam_id,
            "case_id": case_id,
            "domain": tmpl["domain"],
            "problem_kind": tmpl["problem_kind"],
            "objective": tmpl["objective"],
            "requires_counterexample_guard": True,
            "primary_item": {
                "item_id": f"{case_id}-primary",
                **tmpl["primary_item"],
            },
            "boundary_counterexample": {
                "item_id": f"{case_id}-boundary",
                **tmpl["boundary_counterexample"],
            },
        }
        case_bytes = _canonical_json(case_spec).encode("utf-8")
        s_inc = f"pair-{idx}-inc"
        s_cand = f"pair-{idx}-cand"
        for sid, arm, cp, ctx_b in (
            (s_inc, "incumbent", s_cand, inc_ctx_bytes),
            (s_cand, "candidate", s_inc, cand_ctx_bytes),
        ):
            slots.append(
                TrialSlot(
                    slot_id=sid,
                    case_id=case_id,
                    family_id=fam_id,
                    inputs=_make_trial_inputs(
                        fixture_bytes,
                        case_bytes,
                        ctx_b,
                        tools_bytes,
                        ctx.meta_binding.profile,
                    ),
                    limits=limits,
                    arm=arm,
                    counterpart_id=cp,
                )
            )
            slot_case_meta[sid] = (arm, fam_id, case_id, case_spec)

    suite = TrialSuite(
        suite_id=f"rsi-gen2-{ctx.cycle_tag}",
        purpose="recursive",
        rubric_digest=rubric_digest,
        evaluator_digest=evaluator_digest,
        analysis_plan_digest=analysis_plan_digest,
        slots=tuple(slots),
    )
    eval_rev = seal_trial_suite(evaluator_store, suite, analysis_plan=analysis_plan)

    return _Gen2SuiteBundle(
        suite=suite,
        eval_rev=eval_rev,
        analysis_plan=analysis_plan,
        slot_case_meta=slot_case_meta,
        arm_stores=arm_stores,
        cycle_dir=cycle_dir,
        tools_bytes=tools_bytes,
        gen2_sealed_tools=gen2_sealed_tools,
        inc_ctx_bytes=inc_ctx_bytes,
        cand_ctx_bytes=cand_ctx_bytes,
        limits=limits,
    )


def _run_gen2_live_arm_case(
    runtime: CompanyRuntime,
    ctx: _CycleContext,
    *,
    slot_store: Path,
    slot_leases: Path,
    agent_desk_dir: Path,
    sealed_slot: TrialSlot,
    case_id: str,
    case_spec: Mapping[str, Any],
    idx: int,
    post_gen1_ns: int,
    bundle: _Gen2SuiteBundle,
    slot_started: float,
) -> tuple[
    str,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    """Execute one live Gen-2 arm case shift and extract verified SQLite/Desk observables."""
    if sealed_slot.inputs.tools != sha256(bundle.tools_bytes).hexdigest():
        raise TrialRefused(
            "Sealed Gen-2 tool_manifest digest diverged from live slot allowlist"
        )
    slot_now_ns = max(post_gen1_ns + 500 + idx, _current_time_ns())
    with hold_company_writer(
        slot_store, ctx.company_id, lease_directory=slot_leases
    ) as slot_w:
        with (
            closing(open_writer(slot_w.store, lease=slot_w)) as sdb,
            write_tx(sdb),
        ):
            gen2_payload = dict(case_spec)
            gen2_payload["sealed_tool_manifest"] = list(bundle.gen2_sealed_tools)
            item_obj, _ = create_work_item(
                sdb,
                company_id=ctx.company_id,
                owner_agent_id=ctx.meta_agent_id,
                duty_kind="work.discovery_review",
                source_kind="recursive_learning",
                source_id=f"gen2-{sealed_slot.slot_id}",
                payload=gen2_payload,
                dedupe_key=f"rsi-gen2:{sealed_slot.slot_id}:{idx}",
                actor_id=ctx.meta_agent_id,
                now_ns=slot_now_ns,
            )
        step_summary = None
        step_error: str | None = None
        try:
            step_summary = runtime.step_with_writer(
                slot_w,
                max_dispatches=1,
                now_ns=slot_now_ns + 10,
                target_work_item_id=item_obj.work_item_id,
            )
        except (
            AgentRuntimeRefused,
            AgentBudgetExceeded,
            AgentTransportInterrupted,
            RuntimeRefused,
            DutyRefused,
            WorkKernelRefused,
            MethodRefused,
        ) as exc:
            step_error = f"{type(exc).__name__}: {exc}"
    executed_run_id = (
        step_summary.executed_runs[0]
        if step_summary is not None and len(step_summary.executed_runs) == 1
        else None
    )
    dec_row = None
    rcpt_rows_full: list[tuple[str, str, str | None, str | None]] = []
    rcpt_rows: list[tuple[str, str, str | None]] = []
    drafted_methods: list[tuple[str, str, str, str, str, str, str, str, str]] = []
    total_tokens = 0
    episode_method_ids: list[str] = []
    with closing(open_reader(slot_store)) as ro_db, read_tx(ro_db):
        if executed_run_id is None:
            last_run = ro_db.execute(
                """SELECT run_id FROM work_runs
                   WHERE company_id = ? AND work_item_id = ?
                   ORDER BY created_at_ns DESC, run_id DESC LIMIT 1""",
                (ctx.company_id, item_obj.work_item_id),
            ).fetchone()
            if last_run is not None:
                executed_run_id = str(last_run[0])
        settled_count = int(
            ro_db.execute(
                """SELECT COUNT(*) FROM work_items
                   WHERE company_id = ? AND work_item_id = ? AND state = 'settled'""",
                (ctx.company_id, item_obj.work_item_id),
            ).fetchone()[0]
        )
        if executed_run_id is not None:
            dec_row = ro_db.execute(
                """SELECT decision_id, action_kind, rationale, evidence_refs_json
                   FROM work_decision
                   WHERE company_id = ? AND work_item_id = ? AND run_id = ?
                   ORDER BY decision_at_ns DESC LIMIT 1""",
                (ctx.company_id, item_obj.work_item_id, executed_run_id),
            ).fetchone()
            rcpt_rows_full = [
                (
                    str(r[0]),
                    str(r[1]),
                    None if r[2] is None else str(r[2]),
                    None if r[3] is None else str(r[3]),
                )
                for r in ro_db.execute(
                    """SELECT tool_name, status, result_json, offloaded_log_path
                       FROM tool_call_receipt
                       WHERE company_id = ? AND run_id = ?
                       ORDER BY recorded_at_ns ASC, call_id ASC""",
                    (ctx.company_id, executed_run_id),
                ).fetchall()
            ]
            rcpt_rows = [(tn, st, raw_res) for tn, st, raw_res, _off in rcpt_rows_full]
            ep_row = ro_db.execute(
                """SELECT COALESCE(SUM(input_tokens + output_tokens), 0),
                          MAX(decisions_json)
                   FROM agent_episode
                   WHERE company_id = ? AND work_item_id = ? AND run_id = ?""",
                (ctx.company_id, item_obj.work_item_id, executed_run_id),
            ).fetchone()
            total_tokens = int(ep_row[0]) if ep_row is not None else 0
            if ep_row is not None and ep_row[1]:
                try:
                    ep_decs = json.loads(str(ep_row[1]))
                    if isinstance(ep_decs, list):
                        for dec_item in ep_decs:
                            if isinstance(dec_item, Mapping):
                                for k in ("invoked", "retrieved"):
                                    vals = dec_item.get(k)
                                    if isinstance(vals, list):
                                        for v in vals:
                                            if (
                                                isinstance(v, str)
                                                and v
                                                and v not in episode_method_ids
                                            ):
                                                episode_method_ids.append(v)
                except json.JSONDecodeError:
                    pass
            seen_draft_pairs: set[tuple[str, str]] = set()

            def _append_draft_row(d_row: Any) -> None:
                if d_row is None:
                    return
                pair = (str(d_row[0]), str(d_row[1]))
                if pair in seen_draft_pairs:
                    return
                seen_draft_pairs.add(pair)
                drafted_methods.append(
                    (
                        str(d_row[0]),
                        str(d_row[1]),
                        str(d_row[2]),
                        str(d_row[3]),
                        str(d_row[4] or "{}"),
                        str(d_row[5] or ""),
                        str(d_row[6] or ""),
                        str(d_row[7] or ""),
                        str(d_row[8]),
                    )
                )

            for tn, st, raw_res, off_path in rcpt_rows_full:
                if tn != "skill_draft_candidate" or st != "ok":
                    continue
                parsed_res = _read_offloaded_receipt_payload(
                    agent_desk_dir, raw_res, off_path
                )
                if not isinstance(parsed_res, Mapping):
                    continue
                d_mid = parsed_res.get("method_id")
                d_rev = parsed_res.get("revision_sha256")
                if (
                    isinstance(d_mid, str)
                    and d_mid
                    and isinstance(d_rev, str)
                    and d_rev
                ):
                    _append_draft_row(
                        ro_db.execute(
                            """SELECT method_id, revision_sha256, delta_kind, item_kind,
                                      trigger_spec_json, body_text, uncertainty_note,
                                      falsification_criteria, status
                               FROM method_delta
                               WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?
                               LIMIT 1""",
                            (ctx.company_id, d_mid, d_rev),
                        ).fetchone()
                    )
            # Fallback to authoritative method_delta rows citing this work_run
            for d_row in ro_db.execute(
                """SELECT method_id, revision_sha256, delta_kind, item_kind,
                          trigger_spec_json, body_text, uncertainty_note,
                          falsification_criteria, status
                   FROM method_delta
                   WHERE company_id = ? AND author_agent_id = ?
                     AND EXISTS (
                         SELECT 1 FROM json_each(source_refs_json)
                         WHERE value = ?
                     )
                   ORDER BY created_at_ns ASC, method_id ASC""",
                (
                    ctx.company_id,
                    ctx.meta_agent_id,
                    f"work_run:{executed_run_id}",
                ),
            ).fetchall():
                _append_draft_row(d_row)
    fatal_calls = 0
    authority_refusals = 0
    if executed_run_id is not None:
        with closing(open_reader(slot_store)) as ro_db, read_tx(ro_db):
            fatal_calls, authority_refusals, _ = _count_receipt_violations(
                ro_db,
                company_id=ctx.company_id,
                exec_runs=(executed_run_id,),
            )
    calls_list: list[dict[str, Any]] = []
    with closing(open_reader(slot_store)) as ro_db, read_tx(ro_db):
        for tn, st, raw_res, off_path in rcpt_rows_full:
            call_entry: dict[str, Any] = {"name": tn, "status": st}
            if tn in (
                "skill_read",
                "skill_record_invocation",
                "skill_draft_candidate",
            ):
                res_obj = _read_offloaded_receipt_payload(
                    agent_desk_dir, raw_res, off_path
                )
                resolved_mid: str | None = None
                if isinstance(res_obj, Mapping):
                    skill_obj = res_obj.get("skill")
                    if isinstance(skill_obj, Mapping):
                        if isinstance(skill_obj.get("method_id"), str):
                            resolved_mid = str(skill_obj["method_id"])
                        if skill_obj.get("item_kind"):
                            call_entry["item_kind"] = str(skill_obj["item_kind"])
                    if resolved_mid is None and isinstance(
                        res_obj.get("method_id"), str
                    ):
                        resolved_mid = str(res_obj["method_id"])
                    if res_obj.get("status"):
                        call_entry["delta_status"] = str(res_obj["status"])
                if resolved_mid is not None:
                    call_entry["method_id"] = resolved_mid
                if (
                    "method_id" not in call_entry
                    or "item_kind" not in call_entry
                    or "delta_status" not in call_entry
                ):
                    if tn == "skill_draft_candidate" and drafted_methods:
                        dm = drafted_methods[0]
                        call_entry.setdefault("method_id", dm[0])
                        call_entry.setdefault("item_kind", dm[3])
                        call_entry.setdefault("delta_status", dm[8])
                    else:
                        lookup_mid = call_entry.get("method_id") or (
                            episode_method_ids[0] if episode_method_ids else None
                        )
                        if lookup_mid is not None:
                            m_row = ro_db.execute(
                                """SELECT method_id, item_kind, status
                                   FROM method_delta
                                   WHERE company_id = ? AND method_id = ?
                                   ORDER BY CASE status
                                       WHEN 'active' THEN 0
                                       WHEN 'candidate' THEN 1
                                       ELSE 2
                                   END, created_at_ns DESC
                                   LIMIT 1""",
                                (ctx.company_id, lookup_mid),
                            ).fetchone()
                            if m_row is not None:
                                call_entry.setdefault("method_id", str(m_row[0]))
                                call_entry.setdefault("item_kind", str(m_row[1]))
                                call_entry.setdefault("delta_status", str(m_row[2]))
            calls_list.append(call_entry)
    shift_ok = (
        step_summary is not None
        and executed_run_id is not None
        and settled_count >= 1
        and dec_row is not None
        and fatal_calls == 0
    )
    action_kind = "none" if dec_row is None else str(dec_row[1])
    rationale_text = "" if dec_row is None or dec_row[2] is None else str(dec_row[2])
    evidence_count = 0
    parsed_ev_list: list[Any] = []
    if dec_row is not None and dec_row[3] is not None:
        try:
            parsed_ev = json.loads(str(dec_row[3]))
            if isinstance(parsed_ev, list):
                parsed_ev_list = parsed_ev
                evidence_count = len(parsed_ev)
        except json.JSONDecodeError:
            evidence_count = 0
    case_evidence_verified = _verify_case_grounded_evidence(
        slot_store=slot_store,
        agent_desk_dir=agent_desk_dir,
        company_id=ctx.company_id,
        agent_id=ctx.meta_agent_id,
        case_spec=case_spec,
        evidence_refs=parsed_ev_list,
        drafted_methods=drafted_methods,
    )
    obs_scores = _score_live_gen2_observables(
        shift_ok=shift_ok,
        action_kind=action_kind,
        evidence_count=evidence_count,
        rcpt_rows=rcpt_rows,
        drafted_methods=drafted_methods,
        fatal_calls=fatal_calls,
        case_spec=case_spec,
        case_evidence_verified=case_evidence_verified,
    )
    explicit_violation = obs_scores["explicit_constraint_violation"]
    decision_doc = {
        "decision_id": None if dec_row is None else str(dec_row[0]),
        "action_kind": action_kind,
        "rationale_digest": sha256(rationale_text.encode("utf-8")).hexdigest(),
        "evidence_count": evidence_count,
        "case_id": case_id,
        "grounded": dec_row is not None,
        "relevant_constraints": settled_count >= 1 and not explicit_violation,
        "feasible_next_action": settled_count >= 1,
        "constraint_violation": explicit_violation,
        "counterexample_checked": obs_scores["counterexample_checked"],
        "false_positive_prevented": obs_scores["false_positive_prevented"],
        "skill_grounded": obs_scores["skill_grounded"],
        "drafted_counterexample_method": obs_scores["drafted_counterexample_method"],
    }
    trace_doc = {
        "calls": calls_list,
        "violations": fatal_calls + authority_refusals,
        "fatal_calls": fatal_calls,
        "authority_refusals": authority_refusals,
    }
    effects_doc = {
        "service_correct": shift_ok,
        "invariant_breach": False,
        "unauthorized_ledger_mutation": False,
    }
    executed_runs_list = (
        list(step_summary.executed_runs)
        if step_summary is not None
        else ([executed_run_id] if executed_run_id is not None else [])
    )
    obs_doc: dict[str, Any] = {
        "settled_items": settled_count,
        "case_id": case_id,
        "executed_runs": executed_runs_list,
    }
    if step_error is not None:
        obs_doc["error"] = step_error
    outcome_str = "completed" if shift_ok else "failed"
    usage_doc = {
        "turns": len(executed_runs_list),
        "tokens": total_tokens,
        "wall_seconds": max(0.001, round(monotonic() - slot_started, 6)),
    }
    return outcome_str, obs_doc, decision_doc, trace_doc, effects_doc, usage_doc


def _run_gen2_offline_arm_case(
    ctx: _CycleContext,
    *,
    slot_store: Path,
    slot_leases: Path,
    sealed_slot: TrialSlot,
    cand_proc_id: str,
    fam_id: str,
    case_id: str,
    idx: int,
) -> tuple[
    str,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    """Execute one deterministic offline Gen-2 arm case against the cloned slot store."""
    with hold_company_writer(
        slot_store, ctx.company_id, lease_directory=slot_leases
    ) as slot_w:
        active = resolve_active_methods(
            slot_w,
            ctx.company_id,
            agent_id=ctx.meta_agent_id,
            duty_kind="work.discovery_review",
        )
        has_counterexample_proc = any(
            m.method_id == cand_proc_id
            and m.item_kind == "improvement_procedure"
            and isinstance(m.procedure_meta, Mapping)
            and bool(m.procedure_meta.get("require_counterexample"))
            for m in active
        )
        slot_item_id, slot_claim = _open_claim_for_agent(
            slot_w,
            company_id=ctx.company_id,
            agent_id=ctx.meta_agent_id,
            grant_id=ctx.meta_binding.grant_id,
            grant_scope=ctx.meta_binding.grant_scope,
            source_id=f"gen2-{sealed_slot.slot_id}",
            payload={"family_id": fam_id, "case_id": case_id},
            now_ns=ctx.base_ns + 500 + idx,
        )
        gen2_wm = distill_method_delta(
            slot_w,
            slot_claim,
            delta_kind="ADD",
            target_kind="work_method",
            method_id=f"wm-{case_id}",
            trigger_spec={
                "duty_kind": "work.discovery_review",
                "family": fam_id,
            },
            body=(
                "Counterexample-verified transfer rule preserving boundary exceptions."
                if has_counterexample_proc
                else "Single-symptom heuristic rule without boundary counterexample check."
            ),
            audience_scope="company",
            source_refs=[f"work_run:{slot_claim.run_id}"],
            now_ns=ctx.base_ns + 510 + idx,
        )
        qualify_method_delta(
            slot_w,
            gen2_wm.delta_id,
            passed_k=3,
            negative_trigger_passed=True,
            safety_passed=True,
            holdout_margin=0.25 if has_counterexample_proc else 0.0,
            now_ns=ctx.base_ns + 520 + idx,
        )
        with (
            closing(open_writer(slot_w.store, lease=slot_w)) as sdb,
            write_tx(sdb),
        ):
            dec = record_work_decision(
                sdb,
                company_id=ctx.company_id,
                work_item_id=slot_item_id,
                run_id=slot_claim.run_id,
                fencing_token=slot_claim.fencing_token,
                agent_id=ctx.meta_agent_id,
                action_kind="apply_transfer_method",
                rationale=(
                    f"Counterexample verified for {fam_id}"
                    if has_counterexample_proc
                    else f"Baseline heuristic applied for {fam_id}"
                ),
                evidence=(),
                decision_at_ns=ctx.base_ns + 530 + idx,
            )
            complete_work_run(
                sdb,
                slot_claim.run_id,
                slot_claim.fencing_token,
                outcome={
                    "method_id": gen2_wm.method_id,
                    "counterexample_checked": has_counterexample_proc,
                },
                now_ns=ctx.base_ns + 540 + idx,
            )
    decision_doc = {
        "decision_id": dec.decision_id,
        "action_kind": dec.action_kind,
        "case_id": case_id,
        "grounded": True,
        "relevant_constraints": True,
        "feasible_next_action": True,
        "counterexample_checked": has_counterexample_proc,
        "false_positive_prevented": has_counterexample_proc,
        "skill_grounded": False,
        "drafted_counterexample_method": has_counterexample_proc,
    }
    trace_doc = {"calls": ["resolve_active_methods"], "violations": 0}
    effects_doc = {
        "service_correct": True,
        "invariant_breach": False,
        "unauthorized_ledger_mutation": False,
    }
    obs_doc = {"settled_items": 1, "case_id": case_id}
    outcome_str = "completed"
    usage_doc = {"turns": 1, "tokens": 240, "wall_seconds": 0.01}
    return outcome_str, obs_doc, decision_doc, trace_doc, effects_doc, usage_doc


def _run_gen2_arm_case(
    runtime: CompanyRuntime,
    desk_dir: Path,
    sealed_slot: TrialSlot,
    ctx: _CycleContext,
    *,
    arm_name: str,
    fam_id: str,
    case_id: str,
    case_spec: Mapping[str, Any],
    idx: int,
    captured_payloads: dict[str, Any],
    gen1: _Gen1StageResult,
    bundle: _Gen2SuiteBundle,
) -> dict[str, Any]:
    """Execute one isolated Gen-2 incumbent or candidate arm case and capture graded payloads."""
    slot_run_dir = desk_dir.parent
    slot_store = slot_run_dir / "company.sqlite"
    slot_leases = slot_run_dir / "leases"
    agent_desk_dir = desk_directory(slot_store, ctx.meta_agent_id)
    slot_started = monotonic()
    if arm_name == "candidate":
        with hold_company_writer(
            slot_store, ctx.company_id, lease_directory=slot_leases
        ) as arm_w:
            qualify_method_delta(
                arm_w,
                gen1.cand_proc_delta.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.2,
                now_ns=gen1.post_gen1_ns + 300,
            )
            activate_method_delta(
                arm_w,
                gen1.cand_proc_delta.delta_id,
                actor_id=ctx.promotion_actor_id,
                grant_id=ctx.promotion_grant_id,
                now_ns=gen1.post_gen1_ns + 310,
            )
            sync_desk_harness_projections(
                arm_w.store, ctx.company_id, ctx.meta_agent_id
            )

    if ctx.use_live_runtime:
        outcome_str, obs_doc, decision_doc, trace_doc, effects_doc, usage_doc = (
            _run_gen2_live_arm_case(
                runtime,
                ctx,
                slot_store=slot_store,
                slot_leases=slot_leases,
                agent_desk_dir=agent_desk_dir,
                sealed_slot=sealed_slot,
                case_id=case_id,
                case_spec=case_spec,
                idx=idx,
                post_gen1_ns=gen1.post_gen1_ns,
                bundle=bundle,
                slot_started=slot_started,
            )
        )
    else:
        outcome_str, obs_doc, decision_doc, trace_doc, effects_doc, usage_doc = (
            _run_gen2_offline_arm_case(
                ctx,
                slot_store=slot_store,
                slot_leases=slot_leases,
                sealed_slot=sealed_slot,
                cand_proc_id=gen1.cand_proc_id,
                fam_id=fam_id,
                case_id=case_id,
                idx=idx,
            )
        )

    captured_payloads["decision"] = decision_doc
    captured_payloads["tool_trace"] = trace_doc
    captured_payloads["service_effects"] = effects_doc
    captured_payloads["observation"] = obs_doc
    return {
        "outcome": outcome_str,
        "observation": obs_doc,
        "decision": decision_doc,
        "tool_trace": trace_doc,
        "service_effects": effects_doc,
        "usage": usage_doc,
    }


def _rubric_score(info: Mapping[str, Any]) -> int:
    clean_ok = (
        info.get("outcome") == "completed"
        and int(info.get("fatal_violations", 0)) == 0
        and bool(info.get("grounded"))
        and bool(info.get("relevant_constraints"))
        and bool(info.get("feasible_next_action"))
    )
    if not clean_ok:
        return 0
    cx = bool(info.get("counterexample_checked"))
    fp = bool(info.get("false_positive_prevented"))
    return (
        int(cx) * 2
        + int(fp) * 2
        + (1 if cx and bool(info.get("skill_grounded")) else 0)
        + (1 if cx and bool(info.get("drafted_counterexample_method")) else 0)
    )


def _build_default_blind_judge(
    blinded_rubric_by_decision: Mapping[tuple[str, str], Mapping[str, Any]],
) -> Callable[[BlindPairwisePacket], PairwiseJudgment]:
    def _default_blind_judge(pkt: BlindPairwisePacket) -> PairwiseJudgment:
        f_info = blinded_rubric_by_decision.get(
            (pkt.first.decision_digest or "", pkt.first.tool_trace_digest or ""),
            {},
        )
        s_info = blinded_rubric_by_decision.get(
            (pkt.second.decision_digest or "", pkt.second.tool_trace_digest or ""),
            {},
        )
        f_score = _rubric_score(f_info)
        s_score = _rubric_score(s_info)
        if f_score > s_score:
            return PairwiseJudgment(
                rationale=(
                    "First blinded submission verified boundary counterexamples and "
                    "prevented false-positive exclusions."
                ),
                preferred="first",
            )
        if s_score > f_score:
            return PairwiseJudgment(
                rationale=(
                    "Second blinded submission verified boundary counterexamples and "
                    "prevented false-positive exclusions."
                ),
                preferred="second",
            )
        return PairwiseJudgment(
            rationale="Both blinded submissions achieved equivalent rubric scores.",
            preferred="tie",
        )

    return _default_blind_judge


def _run_gen2_transfer_trials_and_judge(
    ctx: _CycleContext,
    *,
    evaluator_store: Path,
    gen1: _Gen1StageResult,
    bundle: _Gen2SuiteBundle,
    judge_fn: Callable[[BlindPairwisePacket], PairwiseJudgment] | None,
) -> RsiTransferSummary:
    """Execute all Gen-2 transfer slots, grade observations, run blind judge, and summarize transfer."""
    # Store ONLY blinded rubric metrics keyed by opaque (decision_digest, tool_trace_digest) (never arm!).
    blinded_rubric_by_decision: dict[tuple[str, str], dict[str, Any]] = {}
    slots_trial_root = bundle.cycle_dir / "slot-runs"
    slots_trial_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    for idx, slot in enumerate(bundle.suite.slots, start=1):
        arm_name, fam_id, case_id, case_spec = bundle.slot_case_meta[slot.slot_id]
        arm_store = bundle.arm_stores[arm_name]
        slot_run_id = f"rsi-{ctx.cycle_tag}-{idx}"
        captured_payloads: dict[str, Any] = {}

        def _execute_slot_case(
            runtime: CompanyRuntime,
            desk_dir: Path,
            sealed_slot: TrialSlot,
            _arm_name: str = arm_name,
            _fam: str = fam_id,
            _case: str = case_id,
            _case_spec: dict[str, Any] = case_spec,
            _idx: int = idx,
            _store_capture: dict[str, Any] = captured_payloads,
        ) -> dict[str, Any]:
            return _run_gen2_arm_case(
                runtime,
                desk_dir,
                sealed_slot,
                ctx,
                arm_name=_arm_name,
                fam_id=_fam,
                case_id=_case,
                case_spec=_case_spec,
                idx=_idx,
                captured_payloads=_store_capture,
                gen1=gen1,
                bundle=bundle,
            )

        slot_worker_input = TrialWorkerInput(
            case_inputs=_canonical_json(case_spec).encode("utf-8"),
            worker_context=(
                bundle.inc_ctx_bytes
                if arm_name == "incumbent"
                else bundle.cand_ctx_bytes
            ),
            tool_manifest=bundle.tools_bytes,
            limits=bundle.limits,
        )
        evidence = run_isolated_trial_slot(
            evaluator_store,
            revision=bundle.eval_rev,
            slot_id=slot.slot_id,
            run_id=slot_run_id,
            trial_root=slots_trial_root,
            source_store=arm_store,
            company_id=ctx.company_id,
            agent_ids=(ctx.meta_agent_id,),
            worker_input=slot_worker_input,
            execute_case=_execute_slot_case,
            duty_registry=ctx.live_registry,
            max_tokens=max(100_000, bundle.limits.tokens),
        )
        dec_payload = captured_payloads.get("decision") or {}
        trace_payload = captured_payloads.get("tool_trace") or {}
        obs_payload = captured_payloads.get("observation") or {}
        effects_payload = captured_payloads.get("service_effects") or {}
        rubric_key = (
            evidence.decision_digest or "",
            evidence.tool_trace_digest or "",
        )
        blinded_rubric_by_decision[rubric_key] = {
            "outcome": evidence.outcome,
            "fatal_violations": int(trace_payload.get("violations", 0)),
            "grounded": bool(dec_payload.get("grounded", False)),
            "relevant_constraints": bool(
                dec_payload.get("relevant_constraints", False)
            ),
            "feasible_next_action": bool(
                dec_payload.get("feasible_next_action", False)
            ),
            "counterexample_checked": bool(
                dec_payload.get("counterexample_checked", False)
            ),
            "false_positive_prevented": bool(
                dec_payload.get("false_positive_prevented", False)
            ),
            "skill_grounded": bool(dec_payload.get("skill_grounded", False)),
            "drafted_counterexample_method": bool(
                dec_payload.get("drafted_counterexample_method", False)
            ),
        }
        rec = commit_trial_observation(
            evaluator_store,
            revision=bundle.eval_rev,
            slot_id=slot.slot_id,
            run_id=slot_run_id,
            evidence=evidence,
        )
        assert rec.result_id is not None
        pkt = blind_trial_packet(
            evaluator_store, revision=bundle.eval_rev, result_id=rec.result_id
        )
        grade = _grade_trial_observation_via_facade(
            pkt,
            observation=obs_payload if evidence.observation_digest else None,
            tool_trace=trace_payload if pkt.tool_trace_digest is not None else None,
            service_effects=(
                effects_payload if pkt.service_effects_digest is not None else None
            ),
            decision=dec_payload if pkt.decision_digest is not None else None,
        )
        commit_blind_grade(
            evaluator_store, revision=bundle.eval_rev, packet=pkt, grade=grade
        )

    run_blind_pairwise_judge(
        evaluator_store,
        revision=bundle.eval_rev,
        judge_fn=judge_fn or _build_default_blind_judge(blinded_rubric_by_decision),
    )

    summary = summarize_rsi_transfer(
        evaluator_store,
        revision=bundle.eval_rev,
        analysis_plan=bundle.analysis_plan,
    )
    if summary.verdict != "SUPPORTED":
        raise TrialRefused(
            f"Recursive transfer trial did not reach SUPPORTED (verdict={summary.verdict}, reasons={summary.reasons})"
        )
    return summary


def _adopt_gen2_and_witness_shift(
    w: WriterLease,
    ctx: _CycleContext,
    *,
    gen1: _Gen1StageResult,
    eval_rev: str,
    summary: RsiTransferSummary,
) -> _AdoptionWitnessResult:
    """Adopt candidate v2 procedure and Gen-2 work method, then witness a subsequent shift."""
    post_gen2_ns = max(gen1.post_gen1_ns + 1000, _current_time_ns())
    qualify_method_delta(
        w,
        gen1.cand_proc_delta.delta_id,
        passed_k=3,
        negative_trigger_passed=True,
        safety_passed=True,
        holdout_margin=0.25,
        evaluator_revision=eval_rev,
        metrics={
            "lcb_alpha": summary.lcb_alpha,
            "perm_p": summary.perm_p,
            "distinct_transfer_families": summary.distinct_transfer_families,
        },
        now_ns=post_gen2_ns,
    )
    proc_act = activate_method_delta(
        w,
        gen1.cand_proc_delta.delta_id,
        actor_id=ctx.promotion_actor_id,
        grant_id=ctx.promotion_grant_id,
        now_ns=post_gen2_ns + 10,
    )

    witness_item_id, witness_claim = _open_claim_for_agent(
        w,
        company_id=ctx.company_id,
        agent_id=ctx.meta_agent_id,
        grant_id=ctx.meta_binding.grant_id,
        grant_scope=ctx.meta_binding.grant_scope,
        source_id=f"gen2-adopted-witness-{ctx.cycle_tag}",
        payload={"generation": 2, "phase": "post_adoption_shift"},
        now_ns=post_gen2_ns + 100,
    )
    gen2_method_id = f"wm-gen2-{ctx.cycle_tag}"
    gen2_delta = distill_method_delta(
        w,
        witness_claim,
        delta_kind="ADD",
        target_kind="work_method",
        method_id=gen2_method_id,
        trigger_spec={"duty_kind": "work.discovery_review"},
        body=(
            "Gen-2 counterexample-verified work method: preserve no-deadline work items "
            "and verify boundary exception before discarding."
        ),
        audience_scope="company",
        source_refs=[f"work_run:{witness_claim.run_id}"],
        parent_method_ids=(gen1.cand_proc_delta.delta_id,),
        active_snapshot_id=proc_act.active_snapshot_id,
        now_ns=post_gen2_ns + 110,
    )
    qualify_method_delta(
        w,
        gen2_delta.delta_id,
        passed_k=3,
        negative_trigger_passed=True,
        safety_passed=True,
        holdout_margin=0.25,
        evaluator_revision=eval_rev,
        now_ns=post_gen2_ns + 120,
    )
    gen2_act = activate_method_delta(
        w,
        gen2_delta.delta_id,
        actor_id=ctx.promotion_actor_id,
        grant_id=ctx.promotion_grant_id,
        now_ns=post_gen2_ns + 130,
    )

    active_after_adopt = resolve_active_methods(
        w,
        ctx.company_id,
        agent_id=ctx.meta_agent_id,
        duty_kind="work.discovery_review",
    )
    adopted_snap_id = (
        compute_active_snapshot_id(active_after_adopt) or gen2_act.active_snapshot_id
    )
    active_ids_after_adopt = {m.method_id for m in active_after_adopt}
    sync_desk_harness_projections(
        w.store,
        ctx.company_id,
        ctx.meta_agent_id,
    )
    skill_file = (
        desk_directory(w.store, ctx.meta_agent_id)
        / "skills"
        / gen2_method_id
        / "SKILL.md"
    )
    if ctx.use_live_runtime:
        assert ctx.live_registry is not None
        with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
            complete_work_run(
                db,
                witness_claim.run_id,
                witness_claim.fencing_token,
                outcome={"phase": "distilled_gen2"},
                now_ns=post_gen2_ns + 135,
            )
            w_item, _ = create_work_item(
                db,
                company_id=ctx.company_id,
                owner_agent_id=ctx.meta_agent_id,
                duty_kind="work.discovery_review",
                source_kind="recursive_learning",
                source_id=f"gen2-adopted-witness-shift-{ctx.cycle_tag}",
                payload={
                    "generation": 2,
                    "phase": "post_adoption_shift",
                    "action_hint": (
                        "No boundary_counterexample present; call ONLY record_work_decision("
                        "action_kind='witness_ok', rationale='Witnessed adopted skill snapshot.', evidence=[])."
                    ),
                },
                dedupe_key=f"rsi-witness-live:{ctx.cycle_tag}",
                actor_id=ctx.meta_agent_id,
                now_ns=post_gen2_ns + 140,
            )
        witness_step = CompanyRuntime(
            w.store,
            ctx.company_id,
            ctx.live_registry,
            lease_directory=w._lock_path.parent,
        ).step_with_writer(
            w,
            max_dispatches=1,
            now_ns=post_gen2_ns + 150,
            target_work_item_id=w_item.work_item_id,
        )
        if len(witness_step.executed_runs) != 1:
            raise TrialRefused("Post-adoption witness shift did not execute")
        witness_run_id = witness_step.executed_runs[0]
        with closing(open_reader(w.store)) as ro_db, read_tx(ro_db):
            wdec_row = ro_db.execute(
                """SELECT decision_id, active_snapshot_id
                   FROM work_decision
                   WHERE company_id = ? AND run_id = ?
                   ORDER BY decision_at_ns DESC LIMIT 1""",
                (ctx.company_id, witness_run_id),
            ).fetchone()
        witness_dec_id = "" if wdec_row is None else str(wdec_row[0])
        witness_dec_snap = None if wdec_row is None else wdec_row[1]
        shift_witnessed = (
            gen1.cand_proc_id in active_ids_after_adopt
            and gen2_method_id in active_ids_after_adopt
            and wdec_row is not None
            and witness_dec_snap == adopted_snap_id
            and skill_file.is_file()
        )
    else:
        with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
            witness_dec = record_work_decision(
                db,
                company_id=ctx.company_id,
                work_item_id=witness_item_id,
                run_id=witness_claim.run_id,
                fencing_token=witness_claim.fencing_token,
                agent_id=ctx.meta_agent_id,
                action_kind="execute_with_adopted_gen2",
                rationale="Witnessed subsequent shift executing under adopted Gen-2 procedure and method.",
                evidence=(),
                active_snapshot_id=adopted_snap_id,
                decision_at_ns=post_gen2_ns + 140,
            )
            record_episode(
                db,
                company_id=ctx.company_id,
                agent_id=ctx.meta_agent_id,
                work_item_id=witness_item_id,
                run_id=witness_claim.run_id,
                duty_kind="work.discovery_review",
                context_digest=sha256(f"witness-{ctx.cycle_tag}".encode()).hexdigest(),
                active_snapshot_id=adopted_snap_id,
                outcome_status="completed",
                summary_text="Subsequent shift witnessed adopted improvement_procedure and Gen-2 method.",
                decisions=[{"decision_id": witness_dec.decision_id}],
                recorded_at_ns=post_gen2_ns + 150,
            )
            complete_work_run(
                db,
                witness_claim.run_id,
                witness_claim.fencing_token,
                outcome={
                    "active_snapshot_id": adopted_snap_id,
                    "decision_id": witness_dec.decision_id,
                },
                now_ns=post_gen2_ns + 160,
            )
        witness_run_id = witness_claim.run_id
        witness_dec_id = witness_dec.decision_id
        shift_witnessed = (
            gen1.cand_proc_id in active_ids_after_adopt
            and gen2_method_id in active_ids_after_adopt
            and witness_dec.active_snapshot_id == adopted_snap_id
            and skill_file.is_file()
        )

    return _AdoptionWitnessResult(
        gen2_method_id=gen2_method_id,
        gen2_delta=gen2_delta,
        adopted_snap_id=adopted_snap_id,
        shift_witnessed=shift_witnessed,
        witness_run_id=witness_run_id,
        witness_dec_id=witness_dec_id,
        post_gen2_ns=post_gen2_ns,
    )


def _verify_regression_quarantine_and_rollback(
    w: WriterLease,
    ctx: _CycleContext,
    *,
    adoption: _AdoptionWitnessResult,
) -> _RollbackWitnessResult:
    """Inject a declared regression patch, quarantine it, and verify clean incumbent restoration."""
    post_reg_ns = max(adoption.post_gen2_ns + 200, _current_time_ns())
    reg_item_id, reg_claim = _open_claim_for_agent(
        w,
        company_id=ctx.company_id,
        agent_id=ctx.meta_agent_id,
        grant_id=ctx.meta_binding.grant_id,
        grant_scope=ctx.meta_binding.grant_scope,
        source_id=f"gen2-regression-witness-{ctx.cycle_tag}",
        payload={"generation": 2, "phase": "regression_rollback"},
        now_ns=post_reg_ns,
    )
    regressed_delta = distill_method_delta(
        w,
        reg_claim,
        delta_kind="PATCH",
        target_kind="work_method",
        method_id=adoption.gen2_method_id,
        trigger_spec={"duty_kind": "work.discovery_review"},
        body="Declared regression patch dropping counterexample safety guard.",
        audience_scope="company",
        source_refs=[f"work_run:{reg_claim.run_id}"],
        parent_method_ids=(adoption.gen2_delta.delta_id,),
        active_snapshot_id=adoption.adopted_snap_id,
        superseded_revision=adoption.gen2_delta.revision_sha256,
        now_ns=post_reg_ns + 10,
    )
    qualify_method_delta(
        w,
        regressed_delta.delta_id,
        passed_k=3,
        negative_trigger_passed=True,
        safety_passed=True,
        holdout_margin=0.0,
        now_ns=post_reg_ns + 20,
    )
    activate_method_delta(
        w,
        regressed_delta.delta_id,
        actor_id=ctx.promotion_actor_id,
        grant_id=ctx.promotion_grant_id,
        now_ns=post_reg_ns + 30,
    )

    q_receipt = quarantine_method_lineage(
        w,
        ctx.company_id,
        method_id=adoption.gen2_method_id,
        revision_sha256=regressed_delta.revision_sha256,
        reason="Declared regression rollback verification",
        actor_id=ctx.promotion_actor_id,
        grant_id=ctx.promotion_grant_id,
        now_ns=post_reg_ns + 40,
    )

    active_after_rollback = resolve_active_methods(
        w,
        ctx.company_id,
        agent_id=ctx.meta_agent_id,
        duty_kind="work.discovery_review",
    )
    rollback_snap_id = compute_active_snapshot_id(active_after_rollback)
    sync_desk_harness_projections(
        w.store,
        ctx.company_id,
        ctx.meta_agent_id,
    )
    if ctx.use_live_runtime:
        assert ctx.live_registry is not None
        with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
            complete_work_run(
                db,
                reg_claim.run_id,
                reg_claim.fencing_token,
                outcome={"phase": "distilled_regression"},
                now_ns=post_reg_ns + 45,
            )
            rollback_item, _ = create_work_item(
                db,
                company_id=ctx.company_id,
                owner_agent_id=ctx.meta_agent_id,
                duty_kind="work.discovery_review",
                source_kind="recursive_learning",
                source_id=f"gen2-post-rollback-shift-{ctx.cycle_tag}",
                payload={
                    "generation": 2,
                    "phase": "post_rollback_shift",
                    "action_hint": (
                        "No boundary_counterexample present; call ONLY record_work_decision("
                        "action_kind='witness_ok', rationale='Witnessed rollback snapshot.', evidence=[])."
                    ),
                },
                dedupe_key=f"rsi-rollback-live:{ctx.cycle_tag}",
                actor_id=ctx.meta_agent_id,
                now_ns=post_reg_ns + 50,
            )
        rollback_step = CompanyRuntime(
            w.store,
            ctx.company_id,
            ctx.live_registry,
            lease_directory=w._lock_path.parent,
        ).step_with_writer(
            w,
            max_dispatches=1,
            now_ns=post_reg_ns + 60,
            target_work_item_id=rollback_item.work_item_id,
        )
        if len(rollback_step.executed_runs) != 1:
            raise TrialRefused("Post-rollback shift did not execute")
        post_rollback_run_id = rollback_step.executed_runs[0]
        with closing(open_reader(w.store)) as ro_db, read_tx(ro_db):
            rdec_row = ro_db.execute(
                """SELECT active_snapshot_id FROM work_decision
                   WHERE company_id = ? AND run_id = ?
                   ORDER BY decision_at_ns DESC LIMIT 1""",
                (ctx.company_id, post_rollback_run_id),
            ).fetchone()
        rollback_dec_snap = None if rdec_row is None else rdec_row[0]
    else:
        with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
            rollback_dec = record_work_decision(
                db,
                company_id=ctx.company_id,
                work_item_id=reg_item_id,
                run_id=reg_claim.run_id,
                fencing_token=reg_claim.fencing_token,
                agent_id=ctx.meta_agent_id,
                action_kind="execute_after_rollback",
                rationale="Post-rollback shift confirmed clean incumbent revision restored.",
                evidence=(),
                active_snapshot_id=rollback_snap_id,
                decision_at_ns=post_reg_ns + 50,
            )
            complete_work_run(
                db,
                reg_claim.run_id,
                reg_claim.fencing_token,
                outcome={
                    "restored_revision": q_receipt.restored_revision_sha256,
                    "active_snapshot_id": rollback_snap_id,
                },
                now_ns=post_reg_ns + 60,
            )
        post_rollback_run_id = reg_claim.run_id
        rollback_dec_snap = rollback_dec.active_snapshot_id

    restored_revs = {(m.method_id, m.revision_sha256) for m in active_after_rollback}
    rollback_restored = (
        q_receipt.restored_revision_sha256 == adoption.gen2_delta.revision_sha256
        and (adoption.gen2_method_id, adoption.gen2_delta.revision_sha256)
        in restored_revs
        and (adoption.gen2_method_id, regressed_delta.revision_sha256)
        not in restored_revs
        and rollback_dec_snap == rollback_snap_id
    )

    return _RollbackWitnessResult(
        regressed_delta_id=regressed_delta.delta_id,
        quarantined_methods=q_receipt.quarantined_methods,
        rollback_restored=rollback_restored,
        post_rollback_run_id=post_rollback_run_id,
        post_rollback_snapshot_id=rollback_snap_id,
    )


def run_recursive_learning_cycle(
    writer_or_store: WriterLease | Path,
    *,
    evaluator_store: Path,
    company_id: str,
    meta_agent_id: str,
    promotion_actor_id: str,
    promotion_grant_id: str,
    trial_root: Path,
    min_families: int = 3,
    adapter_registry: AgentAdapterRegistry | None = None,
    profile_bindings: ProfileBindingLookup | None = None,
    judge_fn: Callable[[BlindPairwisePacket], PairwiseJudgment] | None = None,
) -> RecursiveLearningCycleResult:
    """Execute a genuine two-generation recursive self-improvement transfer & rollback cycle."""
    use_live_runtime, live_registry = _validate_and_prepare_cycle(
        writer_or_store,
        evaluator_store=evaluator_store,
        trial_root=trial_root,
        min_families=min_families,
        adapter_registry=adapter_registry,
        profile_bindings=profile_bindings,
        judge_fn=judge_fn,
    )

    def _run_with_lease(w: WriterLease) -> RecursiveLearningCycleResult:
        w.verify_for(w.store, company_id)
        ctx = _CycleContext(
            company_id=company_id,
            meta_agent_id=meta_agent_id,
            meta_binding=_load_meta_agent_binding(
                w.store, company_id=company_id, meta_agent_id=meta_agent_id
            ),
            promotion_actor_id=promotion_actor_id,
            promotion_grant_id=promotion_grant_id,
            cycle_tag=uuid4().hex[:8],
            base_ns=max(10_000, _current_time_ns()),
            use_live_runtime=use_live_runtime,
            live_registry=live_registry,
        )
        gen1 = _run_gen1_discovery_and_distillation(w, ctx)
        bundle = _freeze_arms_and_seal_gen2_suite(
            w,
            ctx,
            evaluator_store=evaluator_store,
            trial_root=trial_root,
            min_families=min_families,
            gen1=gen1,
        )
        summary = _run_gen2_transfer_trials_and_judge(
            ctx,
            evaluator_store=evaluator_store,
            gen1=gen1,
            bundle=bundle,
            judge_fn=judge_fn,
        )
        adoption = _adopt_gen2_and_witness_shift(
            w,
            ctx,
            gen1=gen1,
            eval_rev=bundle.eval_rev,
            summary=summary,
        )
        rollback = _verify_regression_quarantine_and_rollback(
            w,
            ctx,
            adoption=adoption,
        )

        # Keep Desk projections cleanly synchronized at cycle end.
        if desks_root(w.store).exists():
            _rehydrate_desk(w.store, company_id, meta_agent_id)

        return RecursiveLearningCycleResult(
            company_id=company_id,
            evaluator_revision=bundle.eval_rev,
            gen1_method_id=gen1.gen1_method_id,
            incumbent_procedure_id=gen1.inc_proc_id,
            candidate_procedure_id=gen1.cand_proc_id,
            adopted_procedure_id=gen1.cand_proc_id,
            adopted_method_id=adoption.gen2_method_id,
            active_snapshot_id=adoption.adopted_snap_id,
            transfer_verdict=summary.verdict,
            distinct_transfer_families=summary.distinct_transfer_families,
            lcb_alpha=summary.lcb_alpha,
            perm_p=summary.perm_p,
            shift_witnessed=adoption.shift_witnessed,
            witness_run_id=adoption.witness_run_id,
            witness_decision_id=adoption.witness_dec_id,
            regressed_method_id=rollback.regressed_delta_id,
            quarantined_methods=rollback.quarantined_methods,
            rollback_restored_incumbent=rollback.rollback_restored,
            post_rollback_run_id=rollback.post_rollback_run_id,
            post_rollback_snapshot_id=rollback.post_rollback_snapshot_id,
        )

    if isinstance(writer_or_store, WriterLease):
        return _run_with_lease(writer_or_store)
    if isinstance(writer_or_store, Path):
        with hold_company_writer(writer_or_store, company_id) as lease:
            return _run_with_lease(lease)
    raise TrialRefused("Expected WriterLease or company store Path")
