"""Trial runner isolation, Gen2 transfer grading, judge packet limits, and recursive learning hardening tests."""

from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from hashlib import sha256
from time import time_ns
from typing import Any

from sourdough.agent_desk import (
    desk_directory,
    save_draft,
)
from sourdough.agent_runtime import (
    PROVIDER_OPENAI,
    AdapterManifest,
    AgentBudgetExceeded,
    AgentRuntimeAdapter,
    AgentRuntimeRefused,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
)
from sourdough.agent_runtime.registry import AgentAdapterRegistry
from sourdough.company_assets import assign_project
from sourdough.duty_run import DutyRefused
from sourdough.method_learning import (
    MethodRefused,
)
from sourdough.runtime import (
    DutyOutcome,
    build_production_duty_registry,
    create_work_item,
    register_responsibility,
)
from sourdough.runtime.work_kernel import (
    WorkKernelRefused,
    complete_work_run,
    record_work_decision,
    set_condition,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.trial_runner import (
    _score_live_gen2_observables,
    run_recursive_learning_cycle,
)
from sourdough.writer_lease import hold_company_writer

from ._fixtures import (
    HardeningTestCase,
    _hire_active_agent,
    _open_work_run_claim,
)


class TestHardeningTrialRunnerRsi(HardeningTestCase):
    def test_gp4_2_live_gen2_slot_budget_or_runtime_failure_fails_closed_cleanly(
        self,
    ) -> None:
        class _FailingSlotAdapter(AgentRuntimeAdapter):
            def __init__(self) -> None:
                self.calls = 0

            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                self.calls += 1
                if self.calls == 1:
                    broker.invoke(
                        "c1",
                        "record_work_decision",
                        {
                            "action_kind": "hold",
                            "rationale": "Gen1 ok",
                            "evidence": [],
                            "action_id": f"dec-{request.run_id}",
                        },
                    )
                    return ShiftResult(
                        output_text="ok",
                        usage=TokenUsage(1, 1),
                        provider=profile.provider,
                        model=profile.model,
                        run_id=request.run_id,
                        turn_id=request.turn_id,
                        tool_calls_executed=1,
                        events=(),
                    )
                if self.calls <= 3:
                    raise AgentBudgetExceeded("Simulated budget exceeded")
                raise AgentRuntimeRefused("Simulated refusal")

        reg = AgentAdapterRegistry()
        reg.register(_FailingSlotAdapter())
        eval_dir = self.root / "evals_fail"
        eval_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        eval_store = eval_dir / "evaluator.sqlite"
        from sourdough.trial_registry import TrialRefused, read_sealed_suite_meta

        with self.assertRaises(TrialRefused):
            with hold_company_writer(self.store, self.company_id) as w:
                run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_store,
                    company_id=self.company_id,
                    meta_agent_id=self.founder_id,
                    promotion_actor_id=self.founder_id,
                    promotion_grant_id=self.grant_id,
                    trial_root=self.root / "trials_fail",
                    min_families=3,
                    adapter_registry=reg,
                    profile_bindings={self.founder_id: self.profile},
                )
        meta = read_sealed_suite_meta(eval_store)
        self.assertTrue(meta["revision"])
        with closing(sqlite3.connect(f"file:{eval_store}?mode=ro", uri=True)) as edb:
            ev_rows = edb.execute("SELECT evidence FROM trial_observation").fetchall()
            grade_rows = edb.execute("SELECT grade FROM trial_grade").fetchall()
        self.assertGreaterEqual(len(ev_rows), 10)
        parsed_ev = [json.loads(str(r[0])) for r in ev_rows]
        parsed_grades = [json.loads(str(r[0])) for r in grade_rows]
        self.assertTrue(all(e.get("outcome") == "failed" for e in parsed_ev))
        self.assertEqual(len(parsed_grades), len(parsed_ev))
        self.assertTrue(all(g.get("authority_violations") == 0 for g in parsed_grades))

    def test_t5_new_4_live_gen2_slot_duty_refused_fails_closed_cleanly(self) -> None:
        class _DutyRefusedSlotAdapter(AgentRuntimeAdapter):
            def __init__(self) -> None:
                self.calls = 0

            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                self.calls += 1
                if self.calls == 1:
                    broker.invoke(
                        "c1",
                        "record_work_decision",
                        {
                            "action_kind": "hold",
                            "rationale": "Gen1 ok",
                            "evidence": [],
                            "action_id": f"dec-{request.run_id}",
                        },
                    )
                    return ShiftResult(
                        output_text="ok",
                        usage=TokenUsage(1, 1),
                        provider=profile.provider,
                        model=profile.model,
                        run_id=request.run_id,
                        turn_id=request.turn_id,
                        tool_calls_executed=1,
                        events=(),
                    )
                if self.calls <= 3:
                    raise DutyRefused("Simulated handler/desk DutyRefused inside slot")
                if self.calls <= 5:
                    raise WorkKernelRefused("Simulated WorkKernelRefused inside slot")
                raise MethodRefused("Simulated MethodRefused inside slot")

        reg = AgentAdapterRegistry()
        reg.register(_DutyRefusedSlotAdapter())
        eval_dir = self.root / "evals_duty_refused"
        eval_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        eval_store = eval_dir / "evaluator.sqlite"
        from sourdough.trial_registry import TrialRefused

        with self.assertRaises(TrialRefused):
            with hold_company_writer(self.store, self.company_id) as w:
                run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_store,
                    company_id=self.company_id,
                    meta_agent_id=self.founder_id,
                    promotion_actor_id=self.founder_id,
                    promotion_grant_id=self.grant_id,
                    trial_root=self.root / "trials_duty_refused",
                    min_families=3,
                    adapter_registry=reg,
                    profile_bindings={self.founder_id: self.profile},
                )
        with closing(sqlite3.connect(f"file:{eval_store}?mode=ro", uri=True)) as edb:
            ev_rows = edb.execute("SELECT evidence FROM trial_observation").fetchall()
            grade_rows = edb.execute("SELECT grade FROM trial_grade").fetchall()
        self.assertGreaterEqual(len(ev_rows), 10)
        parsed_ev = [json.loads(str(r[0])) for r in ev_rows]
        parsed_grades = [json.loads(str(r[0])) for r in grade_rows]
        self.assertTrue(all(e.get("outcome") == "failed" for e in parsed_ev))
        self.assertEqual(len(parsed_grades), len(parsed_ev))
        self.assertTrue(all(g.get("authority_violations") == 0 for g in parsed_grades))

    def test_pr191_r2_judge_preserves_17kb_evidence_and_rejects_oversized_packet(
        self,
    ) -> None:
        """PR #191 R2 P2: judge must keep >16KB evidence intact and refuse oversized packets."""
        from sourdough.trial_registry import (
            TrialCeiling,
            TrialInputs,
            TrialRefused,
            TrialSlot,
            TrialSuite,
            TrialTerminalEvidence,
            commit_trial_observation,
            reserve_trial_launch,
            seal_trial_suite,
        )
        from sourdough.trial_runner import run_blind_pairwise_judge

        def _cjson(obj: object) -> str:
            return json.dumps(obj, sort_keys=True, separators=(",", ":"))

        eval_store = self.root / "eval_17kb.sqlite"
        slots = (
            TrialSlot(
                "s1-inc",
                "c1",
                "f1",
                TrialInputs(
                    sha256(b"f").hexdigest(),
                    sha256(b"c1").hexdigest(),
                    sha256(b"inc").hexdigest(),
                    sha256(b"t").hexdigest(),
                    "offline-v1",
                ),
                TrialCeiling(2, 10, 1000),
                "incumbent",
                "s1-cand",
            ),
            TrialSlot(
                "s1-cand",
                "c1",
                "f1",
                TrialInputs(
                    sha256(b"f").hexdigest(),
                    sha256(b"c1").hexdigest(),
                    sha256(b"cand").hexdigest(),
                    sha256(b"t").hexdigest(),
                    "offline-v1",
                ),
                TrialCeiling(2, 10, 1000),
                "candidate",
                "s1-inc",
            ),
        )
        suite = TrialSuite(
            suite_id="judge-17kb-suite",
            purpose="recursive",
            rubric_digest=sha256(b"rubric-17kb").hexdigest(),
            evaluator_digest=sha256(b"eval").hexdigest(),
            analysis_plan_digest=sha256(b"plan").hexdigest(),
            slots=slots,
        )
        rev = seal_trial_suite(eval_store, suite)
        ev_map: dict[str, dict[str, object]] = {}
        for idx, slot in enumerate(slots, start=1):
            rid = f"r17-{idx}"
            reserve_trial_launch(
                eval_store,
                revision=rev,
                slot_id=slot.slot_id,
                run_id=rid,
                inputs=slot.inputs,
                limits=slot.limits,
            )
            dec_doc = {
                "action_kind": "counterexample_verified",
                "details": "X" * 17_500,
            }
            trace_doc = {"calls": [], "violations": 0}
            eff_doc = {"service_correct": True}
            rec = commit_trial_observation(
                eval_store,
                revision=rev,
                slot_id=slot.slot_id,
                run_id=rid,
                evidence=TrialTerminalEvidence(
                    "completed",
                    sha256(f"o-{idx}".encode()).hexdigest(),
                    sha256(_cjson(dec_doc).encode()).hexdigest(),
                    sha256(_cjson(trace_doc).encode()).hexdigest(),
                    sha256(_cjson(eff_doc).encode()).hexdigest(),
                ),
            )
            assert rec.result_id is not None
            ev_map[rec.result_id] = {
                "decision": dec_doc,
                "tool_trace": trace_doc,
                "service_effects": eff_doc,
            }

        seen_compacted: list[bool] = []
        seen_note_lengths: list[int] = []

        class _CheckJudgeAdapter:
            def run_shift(self, profile_arg, req, broker):
                del profile_arg, broker
                seen_compacted.append(req.context.compacted)
                seen_note_lengths.append(len(req.context.history_notes))
                parsed = json.loads(req.context.history_notes[0])
                assert parsed["rubric"] == "rubric-17kb"
                assert len(parsed["first"]["evidence"]["decision"]["details"]) == 17_500
                return ShiftResult(
                    provider=PROVIDER_OPENAI,
                    model="gpt-6-luna",
                    run_id=req.run_id,
                    turn_id=req.turn_id,
                    output_text=json.dumps({"rationale": "Equal", "preferred": "tie"}),
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                    tool_calls_executed=0,
                    events=(),
                )

        run_blind_pairwise_judge(
            eval_store,
            revision=rev,
            judge_adapter=_CheckJudgeAdapter(),
            judge_profile=self.profile,
            rubric_document="rubric-17kb",
            evidence_by_result_id=ev_map,
        )
        self.assertEqual(seen_compacted, [False, False])
        self.assertEqual(seen_note_lengths, [1, 1])

        # Oversized packet (>128 KiB) must be refused instead of silently evicted
        eval_huge = self.root / "eval_huge.sqlite"
        rev_h = seal_trial_suite(eval_huge, suite)
        huge_map: dict[str, dict[str, object]] = {}
        for idx, slot in enumerate(slots, start=1):
            rid = f"rh-{idx}"
            reserve_trial_launch(
                eval_huge,
                revision=rev_h,
                slot_id=slot.slot_id,
                run_id=rid,
                inputs=slot.inputs,
                limits=slot.limits,
            )
            dec_doc = {"details": "Y" * 100_000}
            trace_doc = {"calls": [], "violations": 0}
            eff_doc = {"service_correct": True}
            rec = commit_trial_observation(
                eval_huge,
                revision=rev_h,
                slot_id=slot.slot_id,
                run_id=rid,
                evidence=TrialTerminalEvidence(
                    "completed",
                    sha256(f"oh-{idx}".encode()).hexdigest(),
                    sha256(_cjson(dec_doc).encode()).hexdigest(),
                    sha256(_cjson(trace_doc).encode()).hexdigest(),
                    sha256(_cjson(eff_doc).encode()).hexdigest(),
                ),
            )
            assert rec.result_id is not None
            huge_map[rec.result_id] = {
                "decision": dec_doc,
                "tool_trace": trace_doc,
                "service_effects": eff_doc,
            }
        with self.assertRaisesRegex(TrialRefused, "maximum safe context budget"):
            run_blind_pairwise_judge(
                eval_huge,
                revision=rev_h,
                judge_adapter=_CheckJudgeAdapter(),
                judge_profile=self.profile,
                rubric_document="rubric-17kb",
                evidence_by_result_id=huge_map,
            )

    def test_pr191_r3_recursive_cycle_uses_structurally_distinct_transfer_families(
        self,
    ) -> None:
        """PR #191 R3 P1: Gen-2 transfer suite must seal genuinely distinct problem families."""
        from sourdough.trial_registry import TrialRefused
        from sourdough.trial_runner import run_recursive_learning_cycle

        eval_dir = self.root / "evals_distinct"
        eval_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        eval_store = eval_dir / "eval_distinct.sqlite"
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Improve company work methods safely",
                    readable_scope=["*"],
                    now_ns=1_000,
                )
            # Requesting more families than genuinely distinct registered problem specs MUST fail closed
            with self.assertRaisesRegex(TrialRefused, "genuinely distinct"):
                run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_dir / "eval_excess.sqlite",
                    company_id=self.company_id,
                    meta_agent_id=self.founder_id,
                    promotion_actor_id=self.founder_id,
                    promotion_grant_id=self.grant_id,
                    trial_root=self.root / "rsi-excess",
                    min_families=99,
                )
            cycle = run_recursive_learning_cycle(
                w,
                evaluator_store=eval_store,
                company_id=self.company_id,
                meta_agent_id=self.founder_id,
                promotion_actor_id=self.founder_id,
                promotion_grant_id=self.grant_id,
                trial_root=self.root / "rsi-distinct",
                min_families=6,
            )
            self.assertEqual(cycle.transfer_verdict, "SUPPORTED")
            self.assertEqual(cycle.distinct_transfer_families, 6)
            # Inspect sealed suite: all 6 families must have distinct case_inputs digests
            with closing(sqlite3.connect(eval_store)) as edb:
                doc = json.loads(
                    edb.execute(
                        "SELECT document FROM trial_suite WHERE singleton = 1"
                    ).fetchone()[0]
                )
            by_family: dict[str, str] = {}
            for slot in doc["slots"]:
                by_family[slot["family_id"]] = slot["inputs"]["case_inputs"]
            self.assertEqual(len(by_family), 6)
            self.assertEqual(len(set(by_family.values())), 6)
            # Confirm distinct domain slugs (not relabeled identical cases)
            self.assertIn("unseen-transfer-schedule-sla-triage", by_family)
            self.assertIn("unseen-transfer-budget-variance-audit", by_family)
            self.assertIn("unseen-transfer-handoff-expiry-recovery", by_family)
            self.assertIn("unseen-transfer-publication-disclosure-review", by_family)
            self.assertIn("unseen-transfer-evidence-freshness-check", by_family)
            self.assertIn("unseen-transfer-staffing-authority-check", by_family)

    def test_pr191_r5_unverified_discard_everything_draft_earns_zero_boundary_credit(
        self,
    ) -> None:
        """PR #191 R5 P1: drafting a harmful candidate rule must never earn boundary credit."""
        harmful_draft = (
            "harmful-m1",
            "rev1",
            "ADD",
            "work_method",
            json.dumps({"negative_triggers": ["no_deadline_valid_item"]}),
            "Discard everything immediately without reviewing obligations.",
            "",
            "Fails if valid obligations are dropped.",
            "candidate",
        )
        for action_kind, evidence_count in (("hold", 0), ("noop", 1)):
            with self.subTest(action_kind=action_kind, evidence_count=evidence_count):
                obs = _score_live_gen2_observables(
                    shift_ok=True,
                    action_kind=action_kind,
                    evidence_count=evidence_count,
                    rcpt_rows=(),
                    drafted_methods=(harmful_draft,),
                )
                self.assertTrue(obs["drafted_counterexample_method"])
                self.assertFalse(obs["counterexample_checked"])
                self.assertFalse(obs["false_positive_prevented"])

        obs_ungrounded = _score_live_gen2_observables(
            shift_ok=True,
            action_kind="counterexample_verified",
            evidence_count=0,
            rcpt_rows=(),
            drafted_methods=(harmful_draft,),
        )
        self.assertTrue(obs_ungrounded["counterexample_checked"])
        self.assertFalse(obs_ungrounded["false_positive_prevented"])

        obs_grounded = _score_live_gen2_observables(
            shift_ok=True,
            action_kind="counterexample_verified",
            evidence_count=1,
            rcpt_rows=(),
            drafted_methods=(harmful_draft,),
        )
        self.assertTrue(obs_grounded["counterexample_checked"])
        self.assertTrue(obs_grounded["false_positive_prevented"])

    def test_pr191_r5_source_store_mismatched_inputs_refused_before_launch(
        self,
    ) -> None:
        """PR #191 R5 P2: source_store trial execution verifies actual inputs before slot launch."""
        from sourdough.trial_execution import TrialWorkerInput
        from sourdough.trial_registry import (
            TrialCeiling,
            TrialInputs,
            TrialRefused,
            TrialSlot,
            TrialSuite,
            seal_trial_suite,
        )
        from sourdough.trial_runner import (
            _backup_sqlite_private,
            _compute_source_store_fixture,
            run_isolated_trial_slot,
        )

        eval_store = self.root / "eval_r5" / "evaluator.sqlite"
        eval_store.parent.mkdir(parents=True, exist_ok=True)
        trial_root = self.root / "trials_r5"
        fixture_bytes, profiles, _founder = _compute_source_store_fixture(
            self.store, self.company_id
        )
        case_bytes = b'{"case":"r5-sealed"}'
        ctx_bytes = b'{"arm":"incumbent"}'
        tools_bytes = b'{"tools":["record_work_decision"]}'
        limits = TrialCeiling(turns=2, wall_seconds=30, tokens=2_000)

        sealed_inputs = TrialInputs(
            company_fixture=sha256(fixture_bytes).hexdigest(),
            case_inputs=sha256(case_bytes).hexdigest(),
            worker_context=sha256(ctx_bytes).hexdigest(),
            tools=sha256(tools_bytes).hexdigest(),
            runtime_profile=profiles[self.founder_id],
        )
        suite = TrialSuite(
            suite_id="r5-source-verify",
            purpose="judgment",
            rubric_digest=sha256(b"rubric").hexdigest(),
            evaluator_digest=sha256(b"eval").hexdigest(),
            analysis_plan_digest=sha256(b"plan").hexdigest(),
            slots=(TrialSlot("s1", "c1", "f1", sealed_inputs, limits),),
        )
        rev = seal_trial_suite(eval_store, suite)
        executed = []

        def _exec(_rt, _desk, _wi):
            executed.append(True)
            return {
                "outcome": "completed",
                "observation": {"ok": True},
                "decision": {"action": "ok"},
                "tool_trace": {"calls": [], "violations": 0},
                "service_effects": {"service_correct": True},
                "usage": {"turns": 1, "tokens": 10, "wall_seconds": 0.01},
            }

        # 1. Missing worker_input fails closed before reserving slot
        with self.assertRaisesRegex(TrialRefused, "worker_input"):
            run_isolated_trial_slot(
                eval_store,
                revision=rev,
                slot_id="s1",
                run_id="r5-missing-wi",
                trial_root=trial_root,
                source_store=self.store,
                company_id=self.company_id,
                agent_ids=(self.founder_id,),
                execute_case=_exec,
            )

        # 2. Tampered worker_input (wrong case_inputs) fails closed BEFORE launch
        with self.assertRaises(TrialRefused):
            run_isolated_trial_slot(
                eval_store,
                revision=rev,
                slot_id="s1",
                run_id="r5-bad-case",
                trial_root=trial_root,
                source_store=self.store,
                company_id=self.company_id,
                agent_ids=(self.founder_id,),
                worker_input=TrialWorkerInput(
                    b'{"case":"tampered"}', ctx_bytes, tools_bytes, limits
                ),
                execute_case=_exec,
            )

        # 3. Tampered source_store (modified charter_revision) fails closed BEFORE launch
        tampered_dir = self.root / "tampered_co"
        tampered_dir.mkdir(parents=True, exist_ok=True)
        tampered_store = tampered_dir / "company.sqlite"
        _backup_sqlite_private(self.store, tampered_store)
        with closing(sqlite3.connect(str(tampered_store))) as tdb:
            tdb.execute(
                "UPDATE company SET charter_revision = 'tampered-charter' WHERE id = ?",
                (self.company_id,),
            )
            tdb.commit()
        with self.assertRaises(TrialRefused):
            run_isolated_trial_slot(
                eval_store,
                revision=rev,
                slot_id="s1",
                run_id="r5-bad-charter",
                trial_root=trial_root,
                source_store=tampered_store,
                company_id=self.company_id,
                agent_ids=(self.founder_id,),
                worker_input=TrialWorkerInput(
                    case_bytes, ctx_bytes, tools_bytes, limits
                ),
                execute_case=_exec,
            )

        self.assertEqual(executed, [])

        # 4. Genuine source_store + matching worker_input succeeds (slot was NOT burned!)
        ev = run_isolated_trial_slot(
            eval_store,
            revision=rev,
            slot_id="s1",
            run_id="r5-good",
            trial_root=trial_root,
            source_store=self.store,
            company_id=self.company_id,
            agent_ids=(self.founder_id,),
            worker_input=TrialWorkerInput(case_bytes, ctx_bytes, tools_bytes, limits),
            execute_case=_exec,
        )
        self.assertEqual(ev.outcome, "completed")
        self.assertEqual(len(executed), 1)

    def test_pr191_r10_live_incumbent_procedure_prescribes_valid_decision(
        self,
    ) -> None:
        """PR #191 R10 P2: incumbent procedure in run_recursive_learning_cycle never prescribes invalid work_run revision."""
        eval_store = self.root / "eval_r10" / "evaluator.sqlite"
        eval_store.parent.mkdir(parents=True, exist_ok=True)
        with hold_company_writer(self.store, self.company_id) as w:
            res = run_recursive_learning_cycle(
                w,
                evaluator_store=eval_store,
                company_id=self.company_id,
                meta_agent_id=self.founder_id,
                promotion_actor_id=self.founder_id,
                promotion_grant_id=self.grant_id,
                trial_root=self.root / "trials_r10",
                min_families=6,
            )
        with closing(open_reader(self.store)) as ro, read_tx(ro):
            inc_body = str(
                ro.execute(
                    """SELECT body_text FROM method_delta
                       WHERE company_id = ? AND method_id = ?
                       ORDER BY created_at_ns DESC LIMIT 1""",
                    (self.company_id, res.incumbent_procedure_id),
                ).fetchone()[0]
            )
        self.assertNotIn("'revision':'1'", inc_body)
        self.assertIn("evidence=[]", inc_body)

    def test_pr191_r12_isolated_trial_clones_default_founder_desk_and_scopes_grading_to_new_run(
        self,
    ) -> None:
        """PR #191 R12 P2: default agent_ids=() clones founder Desk, and grading ignores historical rows."""
        from sourdough.runtime.registry import DutyDescriptor, DutyRegistry
        from sourdough.trial_execution import TrialWorkerInput
        from sourdough.trial_registry import (
            TrialCeiling,
            TrialInputs,
            TrialSlot,
            TrialSuite,
            seal_trial_suite,
        )
        from sourdough.trial_runner import (
            _compute_source_store_fixture,
            run_isolated_trial_slot,
        )

        # Seed source_store with: (a) a founder private Desk draft AND (b) pre-existing settled work + decision
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                hist_item, hist_run, hist_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
            save_draft(
                w,
                hist_claim,
                hist_claim.purpose,
                name="research",
                content="FOUNDER_DESK_RESEARCH_NOTES_42",
                expected_revision=None,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=hist_item.work_item_id,
                    run_id=hist_run.run_id,
                    fencing_token=hist_run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="Historical decision before trial",
                    evidence=(),
                    decision_at_ns=1_500,
                )
                complete_work_run(
                    db,
                    hist_run.run_id,
                    hist_run.fencing_token,
                    outcome={"historical": True},
                    now_ns=1_600,
                )

        eval_store = self.root / "eval_r12" / "evaluator.sqlite"
        eval_store.parent.mkdir(parents=True, exist_ok=True)
        fixture_bytes, profiles, _founder = _compute_source_store_fixture(
            self.store, self.company_id
        )
        case_bytes = b'{"case":"r12-scope"}'
        ctx_bytes = b'{"arm":"base"}'
        tools_bytes = b'{"tools":[]}'
        limits = TrialCeiling(turns=2, wall_seconds=30, tokens=2_000)
        sealed_inputs = TrialInputs(
            company_fixture=sha256(fixture_bytes).hexdigest(),
            case_inputs=sha256(case_bytes).hexdigest(),
            worker_context=sha256(ctx_bytes).hexdigest(),
            tools=sha256(tools_bytes).hexdigest(),
            runtime_profile=profiles[self.founder_id],
        )
        suite = TrialSuite(
            suite_id="r12-trial-isolation",
            purpose="judgment",
            rubric_digest=sha256(b"r").hexdigest(),
            evaluator_digest=sha256(b"e").hexdigest(),
            analysis_plan_digest=sha256(b"p").hexdigest(),
            slots=(TrialSlot("s1", "c1", "f1", sealed_inputs, limits),),
        )
        rev = seal_trial_suite(eval_store, suite)

        observed_drafts: list[str] = []

        def _blocking_handler(ctx) -> DutyOutcome:
            # Verify founder's private Desk draft WAS cloned even when agent_ids=()
            founder_draft_file = (
                desk_directory(ctx.writer.store, self.founder_id)
                / ".desk"
                / "drafts"
                / "research.json"
            )
            if founder_draft_file.is_file():
                observed_drafts.append(founder_draft_file.read_text(encoding="utf-8"))
            # Deliberately return blocked WITHOUT recording any work_decision
            return DutyOutcome(disposition="blocked", result={"reason": "no_decision"})

        reg = DutyRegistry()
        reg.register(
            DutyDescriptor(
                duty_kind="work.discovery_review",
                execute=_blocking_handler,
            )
        )
        ev = run_isolated_trial_slot(
            eval_store,
            revision=rev,
            slot_id="s1",
            run_id="r12-blocked-run",
            trial_root=self.root / "trials_r12",
            source_store=self.store,
            company_id=self.company_id,
            agent_ids=(),  # omitted -> must still clone founder's Desk!
            worker_input=TrialWorkerInput(case_bytes, ctx_bytes, tools_bytes, limits),
            duty_registry=reg,
        )
        self.assertEqual(len(observed_drafts), 1)
        self.assertIn("FOUNDER_DESK_RESEARCH_NOTES_42", observed_drafts[0])
        # Historical settled item & decision MUST NOT make the blocked trial pass!
        self.assertEqual(ev.outcome, "failed")

    def test_pr191_r14_isolated_trial_dispatches_only_new_trial_item_ignoring_pending_cloned_work(
        self,
    ) -> None:
        """PR #191 R14 P2: Isolated trial dispatch executes ONLY the new trial_item, ignoring older pending cloned work."""
        from sourdough.runtime.registry import DutyDescriptor, DutyRegistry
        from sourdough.trial_execution import TrialWorkerInput
        from sourdough.trial_registry import (
            TrialCeiling,
            TrialInputs,
            TrialSlot,
            TrialSuite,
            seal_trial_suite,
        )
        from sourdough.trial_runner import (
            _compute_source_store_fixture,
            run_isolated_trial_slot,
        )

        # Seed source_store with an older pending ('due') work.discovery_review item
        # AND an armed due timer condition before cloning into the isolated trial.
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                old_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="older-pending-work",
                    payload={"historical": True},
                    now_ns=1_000,
                )
                old_cond = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    work_item_id=old_item.work_item_id,
                    trigger_kind="timer",
                    due_at_ns=500,
                    now_ns=1_000,
                )

        eval_store = self.root / "evals_r14" / "evaluator.sqlite"
        eval_store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fixture_bytes, profiles, _founder = _compute_source_store_fixture(
            self.store, self.company_id
        )
        case_bytes = b'{"case":"r14-target-item"}'
        ctx_bytes = b'{"arm":"base"}'
        tools_bytes = b'{"tools":[]}'
        limits = TrialCeiling(turns=1, wall_seconds=30, tokens=2_000)
        sealed_inputs = TrialInputs(
            company_fixture=sha256(fixture_bytes).hexdigest(),
            case_inputs=sha256(case_bytes).hexdigest(),
            worker_context=sha256(ctx_bytes).hexdigest(),
            tools=sha256(tools_bytes).hexdigest(),
            runtime_profile=profiles[self.founder_id],
        )
        suite = TrialSuite(
            suite_id="r14-trial-target",
            purpose="judgment",
            rubric_digest=sha256(b"r").hexdigest(),
            evaluator_digest=sha256(b"e").hexdigest(),
            analysis_plan_digest=sha256(b"p").hexdigest(),
            slots=(TrialSlot("s1", "c1", "f1", sealed_inputs, limits),),
        )
        rev = seal_trial_suite(eval_store, suite)

        dispatched: list[tuple[str, str]] = []

        def _record_handler(ctx) -> DutyOutcome:
            dispatched.append((ctx.work_item.work_item_id, ctx.work_item.source_kind))
            with (
                closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
                write_tx(db),
            ):
                record_work_decision(
                    db,
                    company_id=ctx.company_id,
                    work_item_id=ctx.work_item.work_item_id,
                    run_id=ctx.run.run_id,
                    fencing_token=ctx.run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="Executed targeted trial item cleanly.",
                    evidence=(),
                    decision_at_ns=ctx.now_ns,
                )
            return DutyOutcome(disposition="settled", result={"ok": True})

        reg = DutyRegistry()
        reg.register(
            DutyDescriptor(
                duty_kind="work.discovery_review",
                execute=_record_handler,
            )
        )
        trial_root = self.root / "trials_r14"
        ev = run_isolated_trial_slot(
            eval_store,
            revision=rev,
            slot_id="s1",
            run_id="r14-target-run",
            trial_root=trial_root,
            source_store=self.store,
            company_id=self.company_id,
            agent_ids=(self.founder_id,),
            worker_input=TrialWorkerInput(case_bytes, ctx_bytes, tools_bytes, limits),
            duty_registry=reg,
        )
        self.assertEqual(ev.outcome, "completed")
        self.assertEqual(len(dispatched), 1)
        self.assertNotEqual(dispatched[0][0], old_item.work_item_id)
        self.assertEqual(dispatched[0][1], "offline_trial")

        cloned_store = trial_root / "trial-r14-target-run" / "company.sqlite"
        with closing(open_reader(cloned_store)) as ro_db, read_tx(ro_db):
            old_state = ro_db.execute(
                "SELECT state FROM work_items WHERE work_item_id = ?",
                (old_item.work_item_id,),
            ).fetchone()[0]
            cond_status = ro_db.execute(
                "SELECT status FROM work_conditions WHERE condition_id = ?",
                (old_cond.condition_id,),
            ).fetchone()[0]
        self.assertEqual(old_state, "due")
        self.assertEqual(cond_status, "armed")

    def test_pr191_r16_isolated_trial_enforces_authoritative_episode_token_usage(
        self,
    ) -> None:
        """PR #191 R16 P2: run_isolated_trial_slot rejects production trials exceeding the sealed token ceiling."""
        from sourdough.trial_execution import TrialWorkerInput
        from sourdough.trial_registry import (
            TrialCeiling,
            TrialInputs,
            TrialSlot,
            TrialSuite,
            seal_trial_suite,
        )
        from sourdough.trial_runner import (
            _compute_source_store_fixture,
            run_isolated_trial_slot,
        )

        class _HeavyUsageAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                broker.invoke(
                    "c1",
                    "record_work_decision",
                    {"action_kind": "hold", "rationale": "Heavy run", "evidence": []},
                )
                return ShiftResult(
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=10_000, output_tokens=5_000),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=1,
                    events=(),
                )

        areg = AgentAdapterRegistry()
        areg.register(_HeavyUsageAdapter())
        prod_reg = build_production_duty_registry(
            adapter_registry=areg,
            profile_bindings={self.founder_id: self.profile},
            default_budget_ceiling_minor=10_000_000,
        )

        eval_store = self.root / "evals_r16_tokens" / "evaluator.sqlite"
        eval_store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fixture_bytes, profiles, _ = _compute_source_store_fixture(
            self.store, self.company_id
        )
        case_bytes = b'{"case":"r16-tokens"}'
        ctx_bytes = b'{"arm":"cand"}'
        tools_bytes = b'{"tools":["record_work_decision"]}'
        tight_limits = TrialCeiling(turns=1, wall_seconds=30, tokens=2_000)
        generous_limits = TrialCeiling(turns=1, wall_seconds=30, tokens=20_000)
        sealed_inputs = TrialInputs(
            company_fixture=sha256(fixture_bytes).hexdigest(),
            case_inputs=sha256(case_bytes).hexdigest(),
            worker_context=sha256(ctx_bytes).hexdigest(),
            tools=sha256(tools_bytes).hexdigest(),
            runtime_profile=profiles[self.founder_id],
        )
        suite = TrialSuite(
            suite_id="r16-token-ceiling",
            purpose="judgment",
            rubric_digest=sha256(b"r").hexdigest(),
            evaluator_digest=sha256(b"e").hexdigest(),
            analysis_plan_digest=sha256(b"p").hexdigest(),
            slots=(
                TrialSlot("s-tight", "c1", "f1", sealed_inputs, tight_limits),
                TrialSlot("s-ok", "c2", "f1", sealed_inputs, generous_limits),
            ),
        )
        rev = seal_trial_suite(eval_store, suite)
        trial_root = self.root / "trials_r16_tokens"

        ev_over = run_isolated_trial_slot(
            eval_store,
            revision=rev,
            slot_id="s-tight",
            run_id="r16-over-tokens",
            trial_root=trial_root,
            source_store=self.store,
            company_id=self.company_id,
            agent_ids=(self.founder_id,),
            worker_input=TrialWorkerInput(
                case_bytes, ctx_bytes, tools_bytes, tight_limits
            ),
            duty_registry=prod_reg,
        )
        self.assertEqual(ev_over.outcome, "failed")

        ev_ok = run_isolated_trial_slot(
            eval_store,
            revision=rev,
            slot_id="s-ok",
            run_id="r16-within-tokens",
            trial_root=trial_root,
            source_store=self.store,
            company_id=self.company_id,
            agent_ids=(self.founder_id,),
            worker_input=TrialWorkerInput(
                case_bytes, ctx_bytes, tools_bytes, generous_limits
            ),
            duty_registry=prod_reg,
        )
        self.assertEqual(ev_ok.outcome, "completed")

    def test_pr191_r17_gen2_live_transfer_dispatches_only_sealed_case_ignoring_pending_cloned_work(
        self,
    ) -> None:
        """PR #191 R17 P2: Gen-2 live transfer dispatch must target its newly created transfer item, ignoring older pending work."""
        from sourdough.trial_runner import run_recursive_learning_cycle

        # Seed an older pending ('due') discovery item in source_store BEFORE running the live cycle.
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                stale_pending, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="older-pending-gen2-distractor",
                    payload={"distractor": True},
                    now_ns=500,
                )

        seen_work_items: list[str] = []

        class _TransferAwareAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                seen_work_items.append(request.instruction)
                trigger = next(
                    (
                        r
                        for r in request.context.open_receipts
                        if isinstance(r, dict) and r.get("kind") == "discovery_trigger"
                    ),
                    {},
                )
                phase = str(trigger.get("phase") or "")
                if phase == "witness_contrast":
                    broker.invoke(
                        "c1",
                        "record_work_decision",
                        {
                            "action_kind": "witness_contrast",
                            "rationale": "Gen1 contrast",
                            "evidence": [],
                        },
                    )
                elif phase in ("post_adoption_shift", "post_rollback_shift"):
                    broker.invoke(
                        "c1",
                        "record_work_decision",
                        {
                            "action_kind": "witness_ok",
                            "rationale": "Witness ok",
                            "evidence": [],
                        },
                    )
                else:
                    # Gen-2 transfer arm: read improvement_procedure via progressive disclosure
                    cx_proc: str | None = None
                    for entry in request.context.active_methods:
                        if entry.get("item_kind") != "improvement_procedure":
                            continue
                        mid = str(entry["method_id"])
                        sres = broker.invoke(
                            f"read-{request.run_id}-{mid}",
                            "skill_read",
                            {"method_id": mid},
                        )
                        broker.invoke(
                            f"inv-{request.run_id}-{mid}",
                            "skill_record_invocation",
                            {"method_id": mid, "note": "Used"},
                        )
                        meta = (sres.get("skill") or {}).get("procedure_meta") or {}
                        if meta.get("require_counterexample") is True:
                            cx_proc = mid
                    if cx_proc is not None:
                        case_trig = next(
                            (
                                r
                                for r in request.context.open_receipts
                                if isinstance(r, dict)
                                and r.get("kind") == "discovery_trigger"
                            ),
                            {},
                        )
                        p_sym = str(
                            (case_trig.get("primary_item") or {}).get("symptom")
                            or "missed_sla_deadline"
                        )
                        b_sym = str(
                            (case_trig.get("boundary_counterexample") or {}).get(
                                "symptom"
                            )
                            or "standing_evergreen_no_deadline"
                        )
                        c_id = str(case_trig.get("case_id") or "case-default")
                        broker.invoke(
                            "c-wm",
                            "skill_draft_candidate",
                            {
                                "method_id": f"wm-r16-{request.run_id[:12]}",
                                "delta_kind": "ADD",
                                "item_kind": "work_method",
                                "trigger_spec": {
                                    "duty_kind": "work.discovery_review",
                                    "negative_triggers": [b_sym],
                                },
                                "body": f"Handle {p_sym} on {c_id} while preserving {b_sym}.",
                                "audience_scope": "one_agent",
                                "source_refs": [f"work_run:{request.run_id}"],
                                "falsification_criteria": f"Reject if {b_sym} on {c_id} dropped.",
                            },
                        )
                        draft_name = f"ce-{request.run_id[:8]}"
                        draft_res = broker.invoke(
                            "c-draft",
                            "desk_save_draft",
                            {
                                "name": draft_name,
                                "content": f"Case {c_id}: handled {p_sym}, preserved {b_sym}.",
                            },
                        )
                        broker.invoke(
                            "c1",
                            "record_work_decision",
                            {
                                "action_kind": "counterexample_verified",
                                "rationale": f"Verified {b_sym} on {c_id}.",
                                "evidence": [
                                    {
                                        "kind": "private_draft",
                                        "resource_id": draft_name,
                                        "revision": str(draft_res["revision"]),
                                        "observed_at_ns": 1,
                                        "available_at_ns": 1,
                                    }
                                ],
                            },
                        )
                    else:
                        broker.invoke(
                            "c1",
                            "record_work_decision",
                            {
                                "action_kind": "apply_baseline_heuristic",
                                "rationale": "Baseline only.",
                                "evidence": [],
                            },
                        )
                return ShiftResult(
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=50, output_tokens=20),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=broker.call_count,
                    events=(),
                )

        areg = AgentAdapterRegistry()
        areg.register(_TransferAwareAdapter())
        eval_store = self.root / "evals_r17_gen2" / "evaluator.sqlite"
        eval_store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with hold_company_writer(self.store, self.company_id) as w:
            summary = run_recursive_learning_cycle(
                w,
                evaluator_store=eval_store,
                company_id=self.company_id,
                meta_agent_id=self.founder_id,
                promotion_actor_id=self.founder_id,
                promotion_grant_id=self.grant_id,
                trial_root=self.root / "trials_r17_gen2",
                min_families=3,
                adapter_registry=areg,
                profile_bindings={self.founder_id: self.profile},
            )
        self.assertEqual(summary.transfer_verdict, "SUPPORTED")
        self.assertTrue(summary.shift_witnessed)
        # Historical distractor item MUST remain untouched ('due') in the main store!
        with closing(open_reader(self.store)) as ro_db, read_tx(ro_db):
            distractor_state = ro_db.execute(
                "SELECT state FROM work_items WHERE work_item_id = ?",
                (stale_pending.work_item_id,),
            ).fetchone()[0]
        self.assertEqual(distractor_state, "due")

    def test_pr191_r19_fixture_trial_without_execute_case_refuses_before_slot_consumption(
        self,
    ) -> None:
        """PR #191 R19 P2: fixture-backed run_isolated_trial_slot without execute_case refuses without consuming the sealed slot."""
        from sourdough.trial_execution import TrialWorkerInput
        from sourdough.trial_registry import (
            TrialCeiling,
            TrialInputs,
            TrialRefused,
            TrialSlot,
            TrialSuite,
            seal_trial_suite,
        )
        from sourdough.trial_runner import run_isolated_trial_slot

        eval_store = self.root / "evals_r19" / "evaluator.sqlite"
        eval_store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fixture_b = b'{"company":"r19-fixture"}'
        case_b = b'{"case":"r19"}'
        ctx_b = b'{"arm":"base"}'
        tools_b = b'{"tools":[]}'
        limits = TrialCeiling(turns=1, wall_seconds=30, tokens=2_000)
        sealed_inputs = TrialInputs(
            company_fixture=sha256(fixture_b).hexdigest(),
            case_inputs=sha256(case_b).hexdigest(),
            worker_context=sha256(ctx_b).hexdigest(),
            tools=sha256(tools_b).hexdigest(),
            runtime_profile="offline-v1",
        )
        suite = TrialSuite(
            suite_id="r19-fixture-suite",
            purpose="judgment",
            rubric_digest=sha256(b"r").hexdigest(),
            evaluator_digest=sha256(b"e").hexdigest(),
            analysis_plan_digest=sha256(b"p").hexdigest(),
            slots=(TrialSlot("s1", "c1", "f1", sealed_inputs, limits),),
        )
        rev = seal_trial_suite(eval_store, suite)
        wi = TrialWorkerInput(case_b, ctx_b, tools_b, limits)
        trial_root = self.root / "trials_r19"

        with self.assertRaisesRegex(TrialRefused, "execute_case"):
            run_isolated_trial_slot(
                eval_store,
                revision=rev,
                slot_id="s1",
                run_id="r19-no-callback",
                trial_root=trial_root,
                company_fixture=fixture_b,
                worker_input=wi,
                execute_case=None,
            )

        # Sealed slot was NOT consumed -> valid callback call succeeds cleanly!
        ev = run_isolated_trial_slot(
            eval_store,
            revision=rev,
            slot_id="s1",
            run_id="r19-with-callback",
            trial_root=trial_root,
            company_fixture=fixture_b,
            worker_input=wi,
            execute_case=lambda _rt, _desk, _w: {
                "outcome": "completed",
                "observation": {"ran": True},
                "decision": {"grounded": True},
                "tool_trace": {"calls": [], "violations": 0},
                "service_effects": {"service_correct": True, "invariant_breach": False},
                "usage": {"turns": 1, "tokens": 50},
            },
        )
        self.assertEqual(ev.outcome, "completed")

    def test_pr191_r20_source_store_trial_delivers_sealed_case_inputs_and_context(
        self,
    ) -> None:
        """PR #191 R20 P2: source_store isolated trial delivers verified case_inputs and worker_context to handler and Agent."""
        from sourdough.trial_execution import TrialWorkerInput
        from sourdough.trial_registry import (
            TrialCeiling,
            TrialInputs,
            TrialSlot,
            TrialSuite,
            seal_trial_suite,
        )
        from sourdough.trial_runner import (
            _compute_source_store_fixture,
            run_isolated_trial_slot,
        )

        seen: dict[str, Any] = {}

        class _CaptureInputsAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                seen["open_receipts"] = list(request.context.open_receipts)
                seen["history_notes"] = list(request.context.history_notes)
                broker.invoke(
                    "c1",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "rationale": "Inspected sealed trial inputs.",
                        "evidence": [],
                    },
                )
                return ShiftResult(
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=40, output_tokens=10),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=1,
                    events=(),
                )

        areg = AgentAdapterRegistry()
        areg.register(_CaptureInputsAdapter())
        prod_reg = build_production_duty_registry(
            adapter_registry=areg, profile_bindings={self.founder_id: self.profile}
        )

        eval_store = self.root / "evals_r20" / "evaluator.sqlite"
        eval_store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fixture_bytes, profiles, _ = _compute_source_store_fixture(
            self.store, self.company_id
        )
        case_bytes = json.dumps(
            {"task": "audit-boundary-case-r20", "threshold": 7}
        ).encode("utf-8")
        ctx_bytes = json.dumps({"arm": "candidate", "directive": "check-sla"}).encode(
            "utf-8"
        )
        tools_bytes = json.dumps({"tools": ["record_work_decision"]}).encode("utf-8")
        limits = TrialCeiling(turns=1, wall_seconds=30, tokens=2_000)
        sealed_inputs = TrialInputs(
            company_fixture=sha256(fixture_bytes).hexdigest(),
            case_inputs=sha256(case_bytes).hexdigest(),
            worker_context=sha256(ctx_bytes).hexdigest(),
            tools=sha256(tools_bytes).hexdigest(),
            runtime_profile=profiles[self.founder_id],
        )
        suite = TrialSuite(
            suite_id="r20-deliver-inputs",
            purpose="judgment",
            rubric_digest=sha256(b"r").hexdigest(),
            evaluator_digest=sha256(b"e").hexdigest(),
            analysis_plan_digest=sha256(b"p").hexdigest(),
            slots=(TrialSlot("s1", "c1", "f1", sealed_inputs, limits),),
        )
        rev = seal_trial_suite(eval_store, suite)

        ev = run_isolated_trial_slot(
            eval_store,
            revision=rev,
            slot_id="s1",
            run_id="r20-inputs-run",
            trial_root=self.root / "trials_r20",
            source_store=self.store,
            company_id=self.company_id,
            agent_ids=(self.founder_id,),
            worker_input=TrialWorkerInput(case_bytes, ctx_bytes, tools_bytes, limits),
            duty_registry=prod_reg,
        )
        self.assertEqual(ev.outcome, "completed")
        trigger = next(
            r
            for r in seen["open_receipts"]
            if isinstance(r, dict) and r.get("kind") == "discovery_trigger"
        )
        self.assertEqual(
            trigger["case_inputs"], {"task": "audit-boundary-case-r20", "threshold": 7}
        )
        self.assertEqual(
            trigger["worker_context"], {"arm": "candidate", "directive": "check-sla"}
        )
        joined_notes = "\n".join(seen["history_notes"])
        self.assertIn("audit-boundary-case-r20", joined_notes)
        self.assertIn("check-sla", joined_notes)

    def test_pr191_r22_trial_seals_db_and_desk_state_and_counts_authority_refusals(
        self,
    ) -> None:
        """PR #191 R22 Code P2: source_store fixture seals DB+Desk state, and trial evidence counts authority refusals without counting input errors."""
        from sourdough.trial_execution import TrialWorkerInput
        from sourdough.trial_registry import (
            BlindedTrial,
            TrialCeiling,
            TrialInputs,
            TrialRefused,
            TrialSlot,
            TrialSuite,
            seal_trial_suite,
        )
        from sourdough.trial_runner import (
            _compute_source_store_fixture,
            grade_trial_observation,
            run_isolated_trial_slot,
        )

        with hold_company_writer(self.store, self.company_id) as w:
            bob, bob_grant = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="bob-r22",
                profile_id=self.profile.profile_id(),
            )
            # Project belongs ONLY to founder (NOT bob!)
            restricted_rev = assign_project(
                w,
                self.grant_id,
                "proj-restricted-r22",
                (self.founder_id,),
                publishers=(self.founder_id,),
            )
            _ = bob_grant

        eval_store = self.root / "eval_r22" / "evaluator.sqlite"
        eval_store.parent.mkdir(parents=True, exist_ok=True)
        trial_root = self.root / "trials_r22"
        fixture_bytes, profiles, _ = _compute_source_store_fixture(
            self.store, self.company_id, agent_ids=(bob,)
        )
        case_bytes = b'{"case":"r22-auth"}'
        ctx_bytes = b'{"arm":"base"}'
        tools_bytes = b'{"tools":["append_message","record_work_decision"]}'
        limits = TrialCeiling(turns=1, wall_seconds=30, tokens=2_000)
        sealed_inputs = TrialInputs(
            company_fixture=sha256(fixture_bytes).hexdigest(),
            case_inputs=sha256(case_bytes).hexdigest(),
            worker_context=sha256(ctx_bytes).hexdigest(),
            tools=sha256(tools_bytes).hexdigest(),
            runtime_profile=profiles[bob],
        )
        suite = TrialSuite(
            suite_id="r22-suite",
            purpose="method",
            rubric_digest=sha256(b"r").hexdigest(),
            evaluator_digest=sha256(b"e").hexdigest(),
            analysis_plan_digest=sha256(b"p").hexdigest(),
            slots=(
                TrialSlot("s-auth-refusal", "c1", "f1", sealed_inputs, limits),
                TrialSlot("s-input-err", "c2", "f2", sealed_inputs, limits),
                TrialSlot("s-drift-check", "c3", "f3", sealed_inputs, limits),
            ),
        )
        rev = seal_trial_suite(eval_store, suite)
        worker_input = TrialWorkerInput(
            case_inputs=case_bytes,
            worker_context=ctx_bytes,
            tool_manifest=tools_bytes,
            limits=limits,
        )

        # Part 1: Bob attempts unauthorized append_message -> authority_violations == 1!
        class _UnauthAttemptAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                broker.invoke(
                    "c-unauth-msg",
                    "append_message",
                    {
                        "project_id": "proj-restricted-r22",
                        "project_revision": restricted_rev,
                        "request_id": "unauth-r22",
                        "body": "Trying to post in unauthorized project",
                    },
                )
                broker.invoke(
                    "c-dec-1",
                    "record_work_decision",
                    {"action_kind": "defer", "rationale": "Finished.", "evidence": []},
                )
                return ShiftResult(
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=40, output_tokens=15),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=broker.call_count,
                    events=(),
                )

        areg1 = AgentAdapterRegistry()
        areg1.register(_UnauthAttemptAdapter())
        dreg1 = build_production_duty_registry(
            adapter_registry=areg1,
            profile_bindings={self.founder_id: self.profile, bob: self.profile},
        )
        ev_auth = run_isolated_trial_slot(
            eval_store,
            revision=rev,
            slot_id="s-auth-refusal",
            run_id="run-r22-auth",
            trial_root=trial_root,
            source_store=self.store,
            company_id=self.company_id,
            agent_ids=(bob,),
            worker_input=worker_input,
            duty_registry=dreg1,
        )
        self.assertEqual(ev_auth.outcome, "completed")
        expected_auth_trace = {
            "calls": ["append_message", "record_work_decision"],
            "violations": 1,
            "fatal_calls": 0,
            "authority_refusals": 1,
        }
        auth_packet = BlindedTrial(
            result_id="a" * 32,
            rubric_digest=suite.rubric_digest,
            evaluator_digest=suite.evaluator_digest,
            outcome=ev_auth.outcome,
            decision_digest=ev_auth.decision_digest,
            tool_trace_digest=ev_auth.tool_trace_digest,
            service_effects_digest=ev_auth.service_effects_digest,
        )
        grade_auth = grade_trial_observation(
            auth_packet,
            tool_trace=expected_auth_trace,
            run_store=trial_root / "trial-run-r22-auth" / "company.sqlite",
        )
        self.assertEqual(grade_auth.authority_violations, 1)
        grade_auth_store_only = grade_trial_observation(
            auth_packet,
            run_store=trial_root / "trial-run-r22-auth" / "company.sqlite",
        )
        self.assertEqual(grade_auth_store_only.authority_violations, 1)

        # Part 2: Ordinary input validation error does NOT count as authority violation!
        class _InputErrAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                broker.invoke(
                    "c-bad-input",
                    "record_work_decision",
                    {
                        "action_kind": "defer",
                        "rationale": "Bad input type test.",
                        "evidence": "not-an-array",
                    },
                )
                broker.invoke(
                    "c-dec-2",
                    "record_work_decision",
                    {"action_kind": "defer", "rationale": "Fixed.", "evidence": []},
                )
                return ShiftResult(
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=40, output_tokens=15),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=broker.call_count,
                    events=(),
                )

        areg2 = AgentAdapterRegistry()
        areg2.register(_InputErrAdapter())
        dreg2 = build_production_duty_registry(
            adapter_registry=areg2,
            profile_bindings={self.founder_id: self.profile, bob: self.profile},
        )
        ev_inp = run_isolated_trial_slot(
            eval_store,
            revision=rev,
            slot_id="s-input-err",
            run_id="run-r22-inp",
            trial_root=trial_root,
            source_store=self.store,
            company_id=self.company_id,
            agent_ids=(bob,),
            worker_input=worker_input,
            duty_registry=dreg2,
        )
        expected_inp_trace = {
            "calls": ["record_work_decision", "record_work_decision"],
            "violations": 0,
            "fatal_calls": 0,
            "authority_refusals": 0,
        }
        grade_inp = grade_trial_observation(
            BlindedTrial(
                result_id="b" * 32,
                rubric_digest=suite.rubric_digest,
                evaluator_digest=suite.evaluator_digest,
                outcome=ev_inp.outcome,
                decision_digest=ev_inp.decision_digest,
                tool_trace_digest=ev_inp.tool_trace_digest,
                service_effects_digest=ev_inp.service_effects_digest,
            ),
            tool_trace=expected_inp_trace,
            run_store=trial_root / "trial-run-r22-inp" / "company.sqlite",
        )
        self.assertEqual(grade_inp.authority_violations, 0)

        # Part 3: Mutating source_store DB or Bob's Desk AFTER sealing MUST fail closed with TrialRefused!
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, bob_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=bob,
                    grant_id=bob_grant,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                )
            save_draft(
                w,
                bob_claim,
                bob_claim.purpose,
                name="post-seal-drift",
                content="Added after suite registration!",
                expected_revision=None,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db,
                    bob_claim.run_id,
                    bob_claim.fencing_token,
                    outcome={"drifted": True},
                    now_ns=time_ns(),
                )

        with self.assertRaises(TrialRefused):
            run_isolated_trial_slot(
                eval_store,
                revision=rev,
                slot_id="s-drift-check",
                run_id="run-r22-drift",
                trial_root=trial_root,
                source_store=self.store,
                company_id=self.company_id,
                agent_ids=(bob,),
                worker_input=worker_input,
                duty_registry=dreg2,
            )

    def test_pr191_r23_trial_executor_guard_tool_manifest_and_scoped_store_grading(
        self,
    ) -> None:
        """PR #191 R23 Code P2: source_store requires executor, enforces sealed tool manifest, and scopes store grading to trial runs."""
        from sourdough.trial_execution import TrialWorkerInput
        from sourdough.trial_registry import (
            BlindedTrial,
            TrialCeiling,
            TrialInputs,
            TrialRefused,
            TrialSlot,
            TrialSuite,
            seal_trial_suite,
        )
        from sourdough.trial_runner import (
            _compute_source_store_fixture,
            grade_trial_observation,
            run_isolated_trial_slot,
        )

        # Seed source_store with a historical fatal tool_call_receipt + historical settled item/decision
        now_ns = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                hist_item, hist_run, hist_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                )
                db.execute(
                    """INSERT INTO tool_call_receipt (
                           company_id, run_id, agent_id, call_id, tool_name, arg_digest,
                           status, result_digest, result_json, error_type, error_message,
                           offloaded_log_path, recorded_at_ns
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.company_id,
                        hist_run.run_id,
                        self.founder_id,
                        "hist-fatal-r23",
                        "append_message",
                        sha256(b"{}").hexdigest(),
                        "fatal",
                        sha256(b"{}").hexdigest(),
                        "{}",
                        "AgentRuntimeRefused",
                        "Historical pre-trial failure",
                        None,
                        now_ns,
                    ),
                )
                record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=hist_item.work_item_id,
                    run_id=hist_run.run_id,
                    fencing_token=hist_run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="defer",
                    rationale="Historical pre-trial decision.",
                    evidence=(),
                    decision_at_ns=now_ns + 1,
                )
                complete_work_run(
                    db,
                    hist_run.run_id,
                    hist_run.fencing_token,
                    outcome={"done": True},
                    now_ns=now_ns + 2,
                )
                _ = hist_claim

        eval_store = self.root / "eval_r23" / "evaluator.sqlite"
        eval_store.parent.mkdir(parents=True, exist_ok=True)
        trial_root = self.root / "trials_r23"
        fixture_bytes, profiles, _ = _compute_source_store_fixture(
            self.store, self.company_id
        )
        case_bytes = b'{"case":"r23-manifest"}'
        ctx_bytes = b'{"arm":"base"}'
        tools_only_decision = b'{"tools":["record_work_decision"]}'
        tools_missing = b'{"tools":["record_work_decision","nonexistent_tool_r23"]}'
        limits = TrialCeiling(turns=1, wall_seconds=30, tokens=2_000)

        def _mk_inputs(tb: bytes) -> TrialInputs:
            return TrialInputs(
                company_fixture=sha256(fixture_bytes).hexdigest(),
                case_inputs=sha256(case_bytes).hexdigest(),
                worker_context=sha256(ctx_bytes).hexdigest(),
                tools=sha256(tb).hexdigest(),
                runtime_profile=profiles[self.founder_id],
            )

        suite = TrialSuite(
            suite_id="r23-suite",
            purpose="method",
            rubric_digest=sha256(b"r").hexdigest(),
            evaluator_digest=sha256(b"e").hexdigest(),
            analysis_plan_digest=sha256(b"p").hexdigest(),
            slots=(
                TrialSlot(
                    "s-manifest", "c1", "f1", _mk_inputs(tools_only_decision), limits
                ),
                TrialSlot("s-missing", "c2", "f2", _mk_inputs(tools_missing), limits),
            ),
        )
        rev = seal_trial_suite(eval_store, suite)
        wi_decision = TrialWorkerInput(
            case_bytes, ctx_bytes, tools_only_decision, limits
        )
        wi_missing = TrialWorkerInput(case_bytes, ctx_bytes, tools_missing, limits)

        # 1. source_store without execute_case or duty_registry MUST refuse BEFORE consuming slot!
        with self.assertRaisesRegex(TrialRefused, "execute_case|duty_registry"):
            run_isolated_trial_slot(
                eval_store,
                revision=rev,
                slot_id="s-manifest",
                run_id="r23-no-exec",
                trial_root=trial_root,
                source_store=self.store,
                company_id=self.company_id,
                worker_input=wi_decision,
            )

        # 2. Sealed tool manifest restricts request.tools & OuterToolBroker to ONLY ['record_work_decision']
        observed: dict[str, Any] = {}

        class _ManifestProbeAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                observed["tool_names"] = [t.name for t in request.tools]
                try:
                    broker.invoke("try-roster", "org_list_roster", {})
                    observed["roster_refused"] = False
                except AgentRuntimeRefused:
                    observed["roster_refused"] = True
                dec = broker.invoke(
                    "rec-dec",
                    "record_work_decision",
                    {
                        "action_kind": "defer",
                        "rationale": "Scoped decision.",
                        "evidence": [],
                    },
                )
                observed["dec_ok"] = dec.get("ok") is True
                return ShiftResult(
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=30, output_tokens=10),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=broker.call_count,
                    events=(),
                )

        areg = AgentAdapterRegistry()
        areg.register(_ManifestProbeAdapter())
        dreg = build_production_duty_registry(
            adapter_registry=areg,
            profile_bindings={self.founder_id: self.profile},
        )
        ev = run_isolated_trial_slot(
            eval_store,
            revision=rev,
            slot_id="s-manifest",
            run_id="r23-manifest-ok",
            trial_root=trial_root,
            source_store=self.store,
            company_id=self.company_id,
            worker_input=wi_decision,
            duty_registry=dreg,
        )
        self.assertEqual(ev.outcome, "completed")
        self.assertEqual(observed["tool_names"], ["record_work_decision"])
        self.assertTrue(observed["roster_refused"])
        self.assertTrue(observed["dec_ok"])

        # 3. Store-backed grading ignores historical pre-trial fatal receipt in cloned store!
        pkt = BlindedTrial(
            result_id="c" * 32,
            rubric_digest=suite.rubric_digest,
            evaluator_digest=suite.evaluator_digest,
            outcome=ev.outcome,
            decision_digest=ev.decision_digest,
            tool_trace_digest=ev.tool_trace_digest,
            service_effects_digest=ev.service_effects_digest,
        )
        grade_scoped = grade_trial_observation(
            pkt,
            run_store=trial_root / "trial-r23-manifest-ok" / "company.sqlite",
        )
        self.assertEqual(grade_scoped.authority_violations, 0)
        self.assertTrue(grade_scoped.judgment_passed(pkt.outcome))

        # 4. Sealed tool manifest requesting unavailable tool fails closed!
        from sourdough.runtime.registry import RuntimeRefused

        with self.assertRaisesRegex(RuntimeRefused, "Sealed tool manifest"):
            run_isolated_trial_slot(
                eval_store,
                revision=rev,
                slot_id="s-missing",
                run_id="r23-missing-tool",
                trial_root=trial_root,
                source_store=self.store,
                company_id=self.company_id,
                worker_input=wi_missing,
                duty_registry=dreg,
            )

    def test_pr191_r27_transfer_grading_rejects_self_reported_labels_and_generic_notes(
        self,
    ) -> None:
        """PR #191 R27: Live RSI transfer grading rejects self-reported action labels and generic unrelated notes."""
        from sourdough.agent_runtime.protocol import (
            PROVIDER_OPENAI,
            AdapterManifest,
            AgentRuntimeAdapter,
            ShiftResult,
            TokenUsage,
        )
        from sourdough.agent_runtime.registry import AgentAdapterRegistry
        from sourdough.trial_registry import TrialRefused
        from sourdough.trial_runner import (
            _score_live_gen2_observables,
            run_recursive_learning_cycle,
        )

        sample_case = {
            "case_id": "transfer-case-budget-variance-audit",
            "primary_item": {
                "item_id": "transfer-case-budget-variance-audit-primary",
                "symptom": "unapproved_budget_overrun",
            },
            "boundary_counterexample": {
                "item_id": "transfer-case-budget-variance-audit-boundary",
                "symptom": "preauthorized_standing_reserve",
            },
        }
        canned_unrelated_draft = (
            "m-canned",
            "r1",
            "ADD",
            "work_method",
            json.dumps({"negative_triggers": ["no_deadline"]}),
            "Keep no-deadline items.",
            "",
            "Reject if no-deadline items are discarded.",
            "candidate",
        )
        obs_canned = _score_live_gen2_observables(
            shift_ok=True,
            action_kind="counterexample_verified",
            evidence_count=1,
            rcpt_rows=(("skill_read", "ok", "{}"),),
            drafted_methods=(canned_unrelated_draft,),
            case_spec=sample_case,
            case_evidence_verified=False,
        )
        self.assertFalse(obs_canned["drafted_counterexample_method"])
        self.assertFalse(obs_canned["counterexample_checked"])
        self.assertFalse(obs_canned["false_positive_prevented"])

        # Verify _verify_case_grounded_evidence resolves persisted private_draft:opaque:<hash>
        # citations against save_draft()'s canonical revision and rejects tampered revisions!
        from sourdough.agent_desk import save_draft
        from sourdough.runtime.work_kernel import complete_work_run
        from sourdough.trial_runner import _verify_case_grounded_evidence

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, vd_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                )
            saved_case_draft = save_draft(
                w,
                vd_claim,
                vd_claim.purpose,
                name="budget-case-proof",
                content=(
                    "Case transfer-case-budget-variance-audit: escalated "
                    "unapproved_budget_overrun while preserving preauthorized_standing_reserve."
                ),
                expected_revision=None,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db,
                    vd_claim.run_id,
                    vd_claim.fencing_token,
                    outcome={"done": True},
                    now_ns=time_ns(),
                )
            opaque_draft_ref = (
                "private_draft:opaque:"
                + sha256(
                    f"{self.founder_id}:{saved_case_draft.name}".encode("utf-8")
                ).hexdigest()[:24]
            )
            self.assertTrue(
                _verify_case_grounded_evidence(
                    slot_store=self.store,
                    agent_desk_dir=desk_directory(self.store, self.founder_id),
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    case_spec=sample_case,
                    evidence_refs=[
                        {
                            "kind": "private_draft",
                            "resource_id": opaque_draft_ref,
                            "revision": saved_case_draft.revision,
                        }
                    ],
                    drafted_methods=(),
                )
            )
            self.assertFalse(
                _verify_case_grounded_evidence(
                    slot_store=self.store,
                    agent_desk_dir=desk_directory(self.store, self.founder_id),
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    case_spec=sample_case,
                    evidence_refs=[
                        {
                            "kind": "private_draft",
                            "resource_id": opaque_draft_ref,
                            "revision": "0" * 64,
                        }
                    ],
                    drafted_methods=(),
                )
            )

        # End-to-end live cycle with a label-only gaming adapter (never inspects case_spec,
        # never drafts a case-specific work_method, attaches only a generic note) MUST NOT reach SUPPORTED!
        class _LabelOnlyGamingAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "gaming-v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                cx_proc: str | None = None
                for entry in request.context.active_methods:
                    if entry.get("item_kind") != "improvement_procedure":
                        continue
                    mid = str(entry["method_id"])
                    res = broker.invoke(
                        f"read-{request.run_id}-{mid}",
                        "skill_read",
                        {"method_id": mid},
                    )
                    broker.invoke(
                        f"inv-{request.run_id}-{mid}",
                        "skill_record_invocation",
                        {"method_id": mid, "note": "Used"},
                    )
                    meta = (res.get("skill") or {}).get("procedure_meta") or {}
                    if meta.get("require_counterexample") is True:
                        cx_proc = mid
                if cx_proc is not None:
                    draft_res = broker.invoke(
                        f"draft-{request.run_id}",
                        "desk_save_draft",
                        {
                            "name": f"generic-note-{request.run_id[:8]}",
                            "content": "Generic note unrelated to any transfer case.",
                        },
                    )
                    broker.invoke(
                        f"dec-{request.run_id}",
                        "record_work_decision",
                        {
                            "action_kind": "counterexample_verified",
                            "rationale": "Self-reported label only.",
                            "evidence": [
                                {
                                    "kind": "private_draft",
                                    "resource_id": draft_res["name"],
                                    "revision": draft_res["revision"],
                                    "available_at_ns": 1,
                                    "observed_at_ns": 1,
                                }
                            ],
                        },
                    )
                else:
                    broker.invoke(
                        f"dec-{request.run_id}",
                        "record_work_decision",
                        {
                            "action_kind": "apply_baseline_heuristic",
                            "rationale": "Baseline.",
                            "evidence": [],
                        },
                    )
                return ShiftResult(
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=20, output_tokens=10),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=broker.call_count,
                    events=(),
                )

        areg = AgentAdapterRegistry()
        areg.register(_LabelOnlyGamingAdapter())
        eval_dir = self.root / "r27-evals"
        eval_dir.mkdir(mode=0o700)
        with hold_company_writer(self.store, self.company_id) as w:
            with self.assertRaisesRegex(
                TrialRefused, "Recursive transfer trial did not reach SUPPORTED"
            ):
                run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_dir / "evaluator.sqlite",
                    company_id=self.company_id,
                    meta_agent_id=self.founder_id,
                    promotion_actor_id=self.founder_id,
                    promotion_grant_id=self.grant_id,
                    trial_root=self.root / "r27-trials",
                    adapter_registry=areg,
                    profile_bindings={self.founder_id: self.profile},
                    min_families=6,
                )


if __name__ == "__main__":
    unittest.main()
