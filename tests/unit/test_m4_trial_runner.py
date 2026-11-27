"""Unit tests for M4 Feature #17: Small-N statistical gate, offline trial runner, blind judge, and 2-Gen RSI cycle."""

from __future__ import annotations

import json
import stat
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from unittest.mock import patch

from sourdough.agent_desk import desk_directory, ensure_private_desk
from sourdough.agent_runtime.protocol import (
    PROVIDER_OPENAI,
    AdapterManifest,
    AgentProfileBinding,
    ShiftResult,
    TokenUsage,
)
from sourdough.agent_runtime.registry import AgentAdapterRegistry
from sourdough.company import FoundingTerms, found
from sourdough.duty_run import WorkRunClaim
from sourdough.method_learning import (
    activate_method_delta,
    distill_method_delta,
    qualify_method_delta,
)
from sourdough.runtime.work_kernel import (
    claim_work_run,
    complete_work_run,
    create_work_item,
    register_responsibility,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.trial_execution import TrialWorkerInput
from sourdough.trial_registry import (
    PairwiseJudgment,
    TrialCeiling,
    TrialGrade,
    TrialInputs,
    TrialRefused,
    TrialSlot,
    TrialSuite,
    TrialTerminalEvidence,
    blind_pairwise_packets,
    blind_trial_packet,
    commit_blind_grade,
    commit_position_swapped_pairwise_grade,
    commit_trial_observation,
    read_sealed_slot,
    read_sealed_suite_meta,
    reserve_trial_launch,
    seal_trial_suite,
    summarize_rsi_transfer,
    trial_judgment_summary,
)
from sourdough.trial_runner import (
    grade_trial_observation,
    run_blind_pairwise_judge,
    run_isolated_trial_slot,
    run_offline_trial_slot,
    run_recursive_learning_cycle,
)
from sourdough.writer_lease import hold_company_writer


def _sha(raw: bytes | str) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return sha256(data).hexdigest()


def _cjson(val: object) -> str:
    return json.dumps(val, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _build_recursive_suite(
    families: tuple[str, ...],
    *,
    analysis_plan: dict[str, object] | None = None,
    limits: TrialCeiling = TrialCeiling(4, 60, 10_000),
) -> TrialSuite:
    slots: list[TrialSlot] = []
    for idx, fam in enumerate(families, start=1):
        s_inc = f"p{idx}-inc"
        s_cand = f"p{idx}-cand"
        cid = f"case-{idx}"
        for sid, arm, cp, ctx in (
            (s_inc, "incumbent", s_cand, b"incumbent-ctx"),
            (s_cand, "candidate", s_inc, b"candidate-ctx"),
        ):
            slots.append(
                TrialSlot(
                    slot_id=sid,
                    case_id=cid,
                    family_id=fam,
                    inputs=TrialInputs(
                        company_fixture=_sha(b"fixture-v1"),
                        case_inputs=_sha(f"case-{idx}-{fam}".encode()),
                        worker_context=_sha(ctx),
                        tools=_sha(b'["desk_read_draft"]'),
                        runtime_profile="offline-v1",
                    ),
                    limits=limits,
                    arm=arm,
                    counterpart_id=cp,
                )
            )
    plan_digest = (
        _sha(_cjson(analysis_plan))
        if analysis_plan is not None
        else _sha(b"default-plan")
    )
    return TrialSuite(
        suite_id="rsi-unit-suite",
        purpose="recursive",
        rubric_digest=_sha(b"rubric-v1"),
        evaluator_digest=_sha(b"eval-v1"),
        analysis_plan_digest=plan_digest,
        slots=tuple(slots),
    )


def _commit_complete_suite_observations(
    eval_store: Path,
    suite: TrialSuite,
    rev: str,
    *,
    pair_outcomes: dict[int, str],
) -> None:
    """Populate reservations, observations, rubric grades, and pairwise judgments."""
    cand_results: set[str] = set()
    inc_results: set[str] = set()
    for idx, slot in enumerate(suite.slots, start=1):
        rid = f"run-{idx}"
        reserve_trial_launch(
            eval_store,
            revision=rev,
            slot_id=slot.slot_id,
            run_id=rid,
            inputs=slot.inputs,
            limits=slot.limits,
        )
        ev = TrialTerminalEvidence(
            outcome="completed",
            observation_digest=_sha(f"obs-{slot.slot_id}"),
            decision_digest=_sha(f"dec-{slot.slot_id}"),
            tool_trace_digest=_sha(f"trace-{slot.slot_id}"),
            service_effects_digest=_sha(f"svc-{slot.slot_id}"),
            usage_digest=_sha(b'{"tokens":100}'),
        )
        rec = commit_trial_observation(
            eval_store,
            revision=rev,
            slot_id=slot.slot_id,
            run_id=rid,
            evidence=ev,
        )
        assert rec.result_id is not None
        if slot.arm == "candidate":
            cand_results.add(rec.result_id)
        else:
            inc_results.add(rec.result_id)
        pkt = blind_trial_packet(eval_store, revision=rev, result_id=rec.result_id)
        commit_blind_grade(
            eval_store,
            revision=rev,
            packet=pkt,
            grade=TrialGrade(True, True, True, 0, True),
        )

    pairs = blind_pairwise_packets(eval_store, revision=rev)
    for idx, (ab, ba) in enumerate(pairs, start=1):
        mode = pair_outcomes.get(idx, "cand")
        if mode == "cand":
            ab_pref = "first" if ab.first.result_id in cand_results else "second"
            ba_pref = "second" if ab_pref == "first" else "first"
        elif mode == "inc":
            ab_pref = "first" if ab.first.result_id in inc_results else "second"
            ba_pref = "second" if ab_pref == "first" else "first"
        elif mode == "tie":
            ab_pref, ba_pref = "tie", "tie"
        else:  # noise
            ab_pref, ba_pref = "first", "first"
        commit_position_swapped_pairwise_grade(
            eval_store,
            revision=rev,
            ab_packet=ab,
            ab_judgment=PairwiseJudgment("Blinded pairwise comparison", ab_pref),
            ba_packet=ba,
            ba_judgment=PairwiseJudgment("Swapped pairwise confirmation", ba_pref),
        )


class M4StatisticalGateTests(unittest.TestCase):
    def test_read_sealed_slot_and_meta_are_read_only(self) -> None:
        with TemporaryDirectory(prefix="sd-m4-meta-") as tmp:
            eval_store = Path(tmp) / "evaluator.sqlite"
            suite = _build_recursive_suite(("f1", "f2"))
            rev = seal_trial_suite(eval_store, suite)
            before = eval_store.read_bytes()

            slot = read_sealed_slot(eval_store, revision=rev, slot_id="p1-cand")
            self.assertEqual(slot.arm, "candidate")
            self.assertEqual(slot.family_id, "f1")
            meta = read_sealed_suite_meta(eval_store)
            self.assertEqual(meta["revision"], rev)
            self.assertEqual(meta["suite_id"], "rsi-unit-suite")
            self.assertEqual(meta["planned_slots"], 4)
            self.assertEqual(eval_store.read_bytes(), before)

            with self.assertRaises(TrialRefused):
                read_sealed_slot(eval_store, revision=rev, slot_id="nonexistent")

    def test_bootstrap_lcb_permutation_and_analysis_plan_digest_verification(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-m4-stat-") as tmp:
            root = Path(tmp)
            # 1. Six families, candidate wins all 6 -> SUPPORTED, lcb_alpha > 0, perm_p == 1/64
            plan = {
                "min_pairs": 6,
                "min_net_wins": 4,
                "min_families": 3,
                "alpha": 0.05,
                "min_worthwhile_margin": 0.2,
                "bootstrap_resamples": 1000,
            }
            s1 = _build_recursive_suite(
                ("f1", "f2", "f3", "f4", "f5", "f6"), analysis_plan=plan
            )
            e1 = root / "e1.sqlite"
            r1 = seal_trial_suite(e1, s1)
            _commit_complete_suite_observations(e1, s1, r1, pair_outcomes={})
            res1 = summarize_rsi_transfer(e1, revision=r1, analysis_plan=plan)
            self.assertEqual(res1.verdict, "SUPPORTED")
            self.assertGreater(res1.lcb_alpha, 0.2)
            self.assertAlmostEqual(res1.perm_p, 1.0 / 64.0)
            self.assertEqual(res1.mean_family_delta, 1.0)

            # Tampered analysis_plan rejected fail-closed
            with self.assertRaisesRegex(TrialRefused, "analysis_plan_digest"):
                summarize_rsi_transfer(
                    e1, revision=r1, analysis_plan=plan | {"min_net_wins": 1}
                )

            # 2. Mixed 6 families (4 cand, 2 inc) -> mean=0.33, bootstrap LCB crosses 0 -> INCONCLUSIVE
            e2 = root / "e2.sqlite"
            s2 = _build_recursive_suite(("f1", "f2", "f3", "f4", "f5", "f6"))
            r2 = seal_trial_suite(e2, s2)
            _commit_complete_suite_observations(
                e2, s2, r2, pair_outcomes={5: "inc", 6: "inc"}
            )
            res2 = summarize_rsi_transfer(
                e2, revision=r2, min_pairs=6, min_net_wins=1, min_families=3
            )
            self.assertEqual(res2.verdict, "INCONCLUSIVE")
            self.assertLessEqual(res2.lcb_alpha, 0.0)

            # 3. Four families with explicit analysis_plan: min p = 1/16 = 0.0625 > 0.05 -> INCONCLUSIVE
            plan4 = {
                "min_pairs": 4,
                "min_net_wins": 4,
                "min_families": 3,
                "alpha": 0.05,
                "min_worthwhile_margin": 0.0,
                "bootstrap_resamples": 1000,
            }
            e3 = root / "e3.sqlite"
            s3 = _build_recursive_suite(("f1", "f2", "f3", "f4"), analysis_plan=plan4)
            r3 = seal_trial_suite(e3, s3)
            _commit_complete_suite_observations(e3, s3, r3, pair_outcomes={})
            res3 = summarize_rsi_transfer(e3, revision=r3, analysis_plan=plan4)
            self.assertEqual(res3.verdict, "INCONCLUSIVE")
            self.assertIn("permutation-p-above-alpha", res3.reasons)
            self.assertAlmostEqual(res3.perm_p, 0.0625)

            # 4. Futility / incumbent stronger across 6 families -> UNSUPPORTED
            e4 = root / "e4.sqlite"
            s4 = _build_recursive_suite(("f1", "f2", "f3", "f4", "f5", "f6"))
            r4 = seal_trial_suite(e4, s4)
            _commit_complete_suite_observations(
                e4,
                s4,
                r4,
                pair_outcomes={i: "inc" for i in range(1, 7)},
            )
            res4 = summarize_rsi_transfer(
                e4, revision=r4, min_pairs=6, min_net_wins=2, min_families=3
            )
            self.assertEqual(res4.verdict, "UNSUPPORTED")
            self.assertLess(res4.ucb_alpha, 0.0)

            # 5. Pseudoreplication: 6 pairs across only 2 families -> INCONCLUSIVE
            e5 = root / "e5.sqlite"
            s5 = _build_recursive_suite(("f1", "f1", "f1", "f2", "f2", "f2"))
            r5 = seal_trial_suite(e5, s5)
            _commit_complete_suite_observations(e5, s5, r5, pair_outcomes={})
            res5 = summarize_rsi_transfer(
                e5, revision=r5, min_pairs=6, min_net_wins=2, min_families=3
            )
            self.assertEqual(res5.verdict, "INCONCLUSIVE")
            self.assertIn("insufficient-transfer-families", res5.reasons)


class M4TrialRunnerTests(unittest.TestCase):
    def test_run_isolated_trial_slot_ceilings_and_replay_protection(self) -> None:
        with TemporaryDirectory(prefix="sd-m4-slot-") as tmp:
            root = Path(tmp)
            eval_dir = root / "evals"
            eval_dir.mkdir(mode=0o700)
            eval_store = eval_dir / "evaluator.sqlite"
            trial_root = root / "trials"

            suite = _build_recursive_suite(
                ("f1",), limits=TrialCeiling(turns=2, wall_seconds=5, tokens=500)
            )
            rev = seal_trial_suite(eval_store, suite)

            # Overlap with evaluator dir refused
            with self.assertRaisesRegex(TrialRefused, "isolated"):
                run_isolated_trial_slot(
                    eval_store,
                    revision=rev,
                    slot_id="p1-inc",
                    run_id="bad-overlap",
                    trial_root=eval_dir / "inside",
                )

            # Sealed-slot-only mode runs isolated CompanyRuntime and produces valid evidence
            ev = run_offline_trial_slot(
                eval_store,
                revision=rev,
                slot_id="p1-inc",
                run_id="slot-run-1",
                trial_root=trial_root,
            )
            self.assertEqual(ev.outcome, "completed")
            self.assertIsNotNone(ev.decision_digest)
            rec = commit_trial_observation(
                eval_store,
                revision=rev,
                slot_id="p1-inc",
                run_id="slot-run-1",
                evidence=ev,
            )
            pkt = blind_trial_packet(eval_store, revision=rev, result_id=rec.result_id)
            grade = grade_trial_observation(pkt)
            self.assertTrue(grade.judgment_passed(pkt.outcome))
            commit_blind_grade(eval_store, revision=rev, packet=pkt, grade=grade)

            # Token ceiling exceeded in execute_case -> outcome='failed', decision_digest=None
            ev_over = run_isolated_trial_slot(
                eval_store,
                revision=rev,
                slot_id="p1-cand",
                run_id="slot-run-2",
                trial_root=trial_root,
                execute_case=lambda _rt, _d, _s: {
                    "outcome": "completed",
                    "decision": {"ok": True},
                    "usage": {"turns": 1, "tokens": 999},
                },
            )
            self.assertEqual(ev_over.outcome, "failed")
            self.assertIsNone(ev_over.decision_digest)

            # Replay with different run_id on already reserved slot refused
            with self.assertRaises(TrialRefused):
                run_isolated_trial_slot(
                    eval_store,
                    revision=rev,
                    slot_id="p1-inc",
                    run_id="slot-run-replay",
                    trial_root=trial_root,
                )

            summary = trial_judgment_summary(eval_store, revision=rev)
            self.assertEqual((summary.planned, summary.completed), (2, 1))

    def test_explicit_worker_input_dispatch_and_grade_digest_checks(self) -> None:
        with TemporaryDirectory(prefix="sd-m4-grade-") as tmp:
            root = Path(tmp)
            eval_store = root / "evals" / "evaluator.sqlite"
            eval_store.parent.mkdir(mode=0o700)
            fixture = b"fixture-bytes"
            case_b = b"case-bytes"
            ctx_b = b"ctx-bytes"
            tools_b = b"tools-bytes"
            limits = TrialCeiling(2, 30, 1000)
            inputs = TrialInputs(
                _sha(fixture), _sha(case_b), _sha(ctx_b), _sha(tools_b), "offline-v1"
            )
            slot = TrialSlot("s1", "c1", "f1", inputs, limits)
            suite = TrialSuite(
                "s-grade",
                "judgment",
                _sha(b"r"),
                _sha(b"e"),
                _sha(b"p"),
                (slot,),
            )
            rev = seal_trial_suite(eval_store, suite)

            decision_doc = {
                "action_kind": "hold",
                "grounded": True,
                "relevant_constraints": True,
                "feasible_next_action": True,
            }
            trace_doc = {
                "calls": [{"name": "desk_read_draft", "status": "ok"}],
                "violations": 0,
            }
            effects_doc = {"service_correct": True, "invariant_breach": False}
            obs_doc = {"settled": True}

            ev = run_isolated_trial_slot(
                eval_store,
                revision=rev,
                slot_id="s1",
                run_id="explicit-1",
                trial_root=root / "trials",
                company_fixture=fixture,
                worker_input=TrialWorkerInput(case_b, ctx_b, tools_b, limits),
                execute_case=lambda _rt, _desk, _wi: {
                    "outcome": "completed",
                    "observation": obs_doc,
                    "decision": decision_doc,
                    "tool_trace": trace_doc,
                    "service_effects": effects_doc,
                    "usage": {"turns": 1, "tokens": 50},
                },
            )
            rec = commit_trial_observation(
                eval_store, revision=rev, slot_id="s1", run_id="explicit-1", evidence=ev
            )
            pkt = blind_trial_packet(eval_store, revision=rev, result_id=rec.result_id)
            grade = grade_trial_observation(
                pkt,
                decision=decision_doc,
                tool_trace=trace_doc,
                service_effects=effects_doc,
            )
            self.assertTrue(grade.judgment_passed(pkt.outcome))
            self.assertTrue(grade.service_correct)

            with self.assertRaisesRegex(TrialRefused, "digest"):
                grade_trial_observation(pkt, decision={"tampered": True})

    def test_blind_pairwise_judge_adapter_enforces_zero_tools_and_swap_noise(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-m4-judge-") as tmp:
            root = Path(tmp)
            eval_store = root / "evaluator.sqlite"
            suite = _build_recursive_suite(("f1", "f2"))
            rev = seal_trial_suite(eval_store, suite)
            evidence_by_result_id: dict[str, dict[str, object]] = {}
            for idx, slot in enumerate(suite.slots, start=1):
                rid = f"jrun-{idx}"
                reserve_trial_launch(
                    eval_store,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=rid,
                    inputs=slot.inputs,
                    limits=slot.limits,
                )
                decision_doc = {
                    "action_kind": "counterexample_verified"
                    if slot.arm == "candidate"
                    else "hold",
                    "grounded": True,
                    "arm": slot.arm,
                    "slot_id": slot.slot_id,
                    "run_id": rid,
                    "desk_path": f"/Users/secret/desks/{slot.slot_id}/draft.md",
                }
                trace_doc = {
                    "calls": [{"name": "skill_read", "status": "ok"}],
                    "violations": 0,
                }
                effects_doc = {"service_correct": True, "invariant_breach": False}
                rec = commit_trial_observation(
                    eval_store,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=rid,
                    evidence=TrialTerminalEvidence(
                        "completed",
                        _sha(f"o-{slot.slot_id}"),
                        _sha(_cjson(decision_doc)),
                        _sha(_cjson(trace_doc)),
                        _sha(_cjson(effects_doc)),
                    ),
                )
                assert rec.result_id is not None
                evidence_by_result_id[rec.result_id] = {
                    "decision": decision_doc,
                    "tool_trace": trace_doc,
                    "service_effects": effects_doc,
                }

            seen_tools: list[tuple[str, ...]] = []
            seen_notes: list[str] = []

            class _PositionBiasedAdapter:
                def run_shift(self, profile_arg, req, broker):
                    del profile_arg, broker
                    seen_tools.append(tuple(t.name for t in req.tools))
                    seen_notes.extend(req.context.history_notes)
                    return ShiftResult(
                        provider=PROVIDER_OPENAI,
                        model="gpt-6-luna",
                        run_id=req.run_id,
                        turn_id=req.turn_id,
                        output_text=json.dumps(
                            {"rationale": "First shown", "preferred": "first"}
                        ),
                        usage=TokenUsage(input_tokens=10, output_tokens=5),
                        tool_calls_executed=0,
                        events=(),
                    )

            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-6-luna",
                revision="v1",
                credential_handle_ref="envref:KEY",
            )
            # Fail closed when rubric or verified evidence is omitted or tampered
            with self.assertRaisesRegex(TrialRefused, "rubric_document"):
                run_blind_pairwise_judge(
                    eval_store,
                    revision=rev,
                    judge_adapter=_PositionBiasedAdapter(),
                    judge_profile=profile,
                )
            with self.assertRaisesRegex(TrialRefused, "verified evidence"):
                run_blind_pairwise_judge(
                    eval_store,
                    revision=rev,
                    judge_adapter=_PositionBiasedAdapter(),
                    judge_profile=profile,
                    rubric_document="rubric-v1",
                )
            tampered_ev = {
                rid: {**payload, "decision": {"tampered": True}}
                for rid, payload in evidence_by_result_id.items()
            }
            with self.assertRaisesRegex(TrialRefused, "digest"):
                run_blind_pairwise_judge(
                    eval_store,
                    revision=rev,
                    judge_adapter=_PositionBiasedAdapter(),
                    judge_profile=profile,
                    rubric_document="rubric-v1",
                    evidence_by_result_id=tampered_ev,
                )

            results = run_blind_pairwise_judge(
                eval_store,
                revision=rev,
                judge_adapter=_PositionBiasedAdapter(),
                judge_profile=profile,
                rubric_document="rubric-v1",
                evidence_by_result_id=evidence_by_result_id,
            )
            self.assertEqual(len(results), 2)
            self.assertTrue(all(t == () for t in seen_tools))
            self.assertTrue(
                all(r.outcome == "tie_noise" and not r.consistent for r in results)
            )
            # Verify actual rubric & redacted evidence were supplied without arm/run/path leaks
            self.assertEqual(len(seen_notes), 4)
            for note in seen_notes:
                parsed = json.loads(note)
                self.assertEqual(parsed["rubric"], "rubric-v1")
                for side in ("first", "second"):
                    ev = parsed[side]["evidence"]
                    self.assertIn("decision", ev)
                    self.assertIn("tool_trace", ev)
                    self.assertIn("service_effects", ev)
                    self.assertNotIn("arm", ev["decision"])
                    self.assertNotIn("slot_id", ev["decision"])
                    self.assertNotIn("run_id", ev["decision"])
                    self.assertNotIn("desk_path", ev["decision"])
                self.assertNotIn("/Users/secret", note)
                self.assertNotIn("p1-cand", note)
                self.assertNotIn("p1-inc", note)

    def test_run_recursive_learning_cycle_end_to_end(self) -> None:
        with TemporaryDirectory(prefix="sd-m4-rsi-") as tmp:
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            leases.mkdir(mode=0o700)
            eval_store = root / "evals" / "evaluator.sqlite"
            eval_store.parent.mkdir(mode=0o700)
            trial_root = root / "rsi-trials"

            rec = found(
                store,
                FoundingTerms("rsi-co", "charter-v1", "manage-company", "prof-v1"),
            )
            with hold_company_writer(
                store, rec.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    ensure_private_desk(db, rec.company_id, rec.founder_id, "prof-v1")
                    register_responsibility(
                        db,
                        company_id=rec.company_id,
                        owner_agent_id=rec.founder_id,
                        duty_kind="work.discovery_review",
                        remit="Monitor and improve work methods",
                        readable_scope=["*"],
                        now_ns=1_000,
                    )
                cycle = run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_store,
                    company_id=rec.company_id,
                    meta_agent_id=rec.founder_id,
                    promotion_actor_id=rec.founder_id,
                    promotion_grant_id=rec.grant_id,
                    trial_root=trial_root,
                    min_families=3,
                )

            self.assertEqual(cycle.transfer_verdict, "SUPPORTED")
            self.assertGreaterEqual(cycle.distinct_transfer_families, 6)
            self.assertGreater(cycle.lcb_alpha, 0.0)
            self.assertLessEqual(cycle.perm_p, 0.05)
            self.assertTrue(cycle.shift_witnessed)
            self.assertTrue(cycle.rollback_restored_incumbent)
            self.assertIn(cycle.adopted_method_id, cycle.quarantined_methods)

            # Verify durable records in operating company.sqlite & evaluator.sqlite
            with closing(open_reader(store)) as ro_db, read_tx(ro_db):
                statuses = {
                    (str(r[0]), str(r[1]))
                    for r in ro_db.execute(
                        "SELECT method_id, status FROM method_delta WHERE company_id = ?",
                        (rec.company_id,),
                    ).fetchall()
                }
                self.assertIn((cycle.adopted_procedure_id, "active"), statuses)
                self.assertIn((cycle.adopted_method_id, "active"), statuses)
                self.assertIn((cycle.adopted_method_id, "quarantined"), statuses)

            meta = read_sealed_suite_meta(eval_store)
            self.assertEqual(meta["revision"], cycle.evaluator_revision)
            self.assertEqual(meta["purpose"], "recursive")
            self.assertGreaterEqual(meta["pairwise_grade_count"], 6)

            # Reuse same trial_root across a second cycle -> trial_root MUST be 0o700 and succeed!
            self.assertEqual(stat.S_IMODE(trial_root.stat().st_mode), 0o700)
            eval_store_2 = root / "evals" / "evaluator2.sqlite"
            with hold_company_writer(
                store, rec.company_id, lease_directory=leases
            ) as w2:
                cycle2 = run_recursive_learning_cycle(
                    w2,
                    evaluator_store=eval_store_2,
                    company_id=rec.company_id,
                    meta_agent_id=rec.founder_id,
                    promotion_actor_id=rec.founder_id,
                    promotion_grant_id=rec.grant_id,
                    trial_root=trial_root,
                    min_families=3,
                )
            self.assertEqual(cycle2.transfer_verdict, "SUPPORTED")

    def test_trial_runner_usage_validation_operating_subdir_isolation_and_live_adapter_cycle(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-m4-live-rsi-") as tmp:
            root = Path(tmp)
            co_dir = root / "co"
            co_dir.mkdir(mode=0o700)
            store = co_dir / "company.sqlite"
            leases = co_dir / "leases"
            leases.mkdir(mode=0o700)
            eval_dir = root / "evals"
            eval_dir.mkdir(mode=0o700)
            eval_store = eval_dir / "evaluator.sqlite"

            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-6-luna",
                revision="v1",
                credential_handle_ref="envref:KEY",
            )
            rec = found(
                store,
                FoundingTerms(
                    "live-rsi-co", "charter-v1", "manage-company", profile.profile_id()
                ),
            )

            # 1. Operating company subdir containment rejected
            suite = _build_recursive_suite(("f1",), limits=TrialCeiling(2, 10, 500))
            rev = seal_trial_suite(eval_store, suite)
            for bad_sub in (co_dir / "desks" / "sub", co_dir / "leases"):
                with self.assertRaisesRegex(TrialRefused, "isolated"):
                    run_isolated_trial_slot(
                        eval_store,
                        revision=rev,
                        slot_id="p1-inc",
                        run_id="bad-op-dir",
                        trial_root=bad_sub,
                        source_store=store,
                        company_id=rec.company_id,
                    )

            # 2. Invalid usage counters rejected fail-closed with TrialRefused
            trials = root / "slot-trials"
            for idx, bad_usage in enumerate(
                (
                    {"turns": True, "tokens": 10},
                    {"turns": 1, "tokens": -5},
                    {"turns": 1, "tokens": 10, "wall_seconds": float("nan")},
                    {"turns": 1, "tokens": 10, "wall_seconds": "bad"},
                ),
                start=1,
            ):
                with self.subTest(bad_usage=bad_usage):
                    with self.assertRaises(TrialRefused):
                        run_isolated_trial_slot(
                            eval_store,
                            revision=rev,
                            slot_id="p1-inc",
                            run_id=f"bad-usage-{idx}",
                            trial_root=trials,
                            execute_case=lambda _rt, _d, _s, u=bad_usage: {
                                "outcome": "completed",
                                "usage": u,
                            },
                        )

            # 3. Partial adapter_registry / profile_bindings fails closed
            adapters = AgentAdapterRegistry()
            with self.assertRaises(TrialRefused):
                run_recursive_learning_cycle(
                    store,
                    evaluator_store=eval_store,
                    company_id=rec.company_id,
                    meta_agent_id=rec.founder_id,
                    promotion_actor_id=rec.founder_id,
                    promotion_grant_id=rec.grant_id,
                    trial_root=root / "rsi-partial",
                    adapter_registry=adapters,
                    profile_bindings=None,
                )

            # 3b. Non-callable judge_fn fails closed
            with self.assertRaisesRegex(TrialRefused, "callable"):
                run_recursive_learning_cycle(
                    store,
                    evaluator_store=eval_store,
                    company_id=rec.company_id,
                    meta_agent_id=rec.founder_id,
                    promotion_actor_id=rec.founder_id,
                    promotion_grant_id=rec.grant_id,
                    trial_root=root / "rsi-bad-judge",
                    judge_fn="not-callable",  # type: ignore[arg-type]
                )

            # 4. Full live-adapter 2-Gen RSI cycle executes real shifts via ShiftRunner
            shift_run_ids: list[str] = []
            skill_reads: list[str] = []
            skill_invocations: list[str] = []
            drafted_work_methods: list[str] = []

            class _RsiTestAdapter:
                manifest = AdapterManifest(
                    PROVIDER_OPENAI, "test-v1", "https_json", True, True, True
                )

                def run_shift(self, prof, req, broker):
                    del prof
                    shift_run_ids.append(req.run_id)
                    counterexample_proc_id: str | None = None
                    for entry in req.context.active_methods:
                        if entry.get("item_kind") != "improvement_procedure":
                            continue
                        mid = str(entry["method_id"])
                        read_res = broker.invoke(
                            f"read-{req.run_id}-{mid}",
                            "skill_read",
                            {"method_id": mid},
                        )
                        skill_reads.append(mid)
                        broker.invoke(
                            f"inv-{req.run_id}-{mid}",
                            "skill_record_invocation",
                            {
                                "method_id": mid,
                                "note": f"Evaluated improvement procedure {mid}",
                            },
                        )
                        skill_invocations.append(mid)
                        proc_meta = (read_res.get("skill") or {}).get(
                            "procedure_meta"
                        ) or {}
                        if proc_meta.get("require_counterexample") is True:
                            counterexample_proc_id = mid

                    if counterexample_proc_id is not None:
                        draft_id = f"wm-obs-{req.run_id[:20]}"
                        broker.invoke(
                            f"draft-{req.run_id}",
                            "skill_draft_candidate",
                            {
                                "method_id": draft_id,
                                "delta_kind": "ADD",
                                "item_kind": "work_method",
                                "trigger_spec": {
                                    "duty_kind": "work.discovery_review",
                                    "negative_triggers": [
                                        "valid_no_deadline_obligation"
                                    ],
                                },
                                "body": (
                                    "Counterexample-verified rule retaining valid no-deadline "
                                    "boundary exceptions and preventing false-positive exclusions."
                                ),
                                "falsification_criteria": (
                                    "Reject if valid no-deadline boundary items are discarded."
                                ),
                                "audience_scope": "company",
                                "source_refs": [f"work_run:{req.run_id}"],
                            },
                        )
                        drafted_work_methods.append(draft_id)
                        broker.invoke(
                            f"dec-{req.run_id}",
                            "record_work_decision",
                            {
                                "action_kind": "counterexample_verified",
                                "rationale": (
                                    f"Verified boundary counterexample via {counterexample_proc_id}; "
                                    f"preserved no-deadline exception and prevented false-positive "
                                    f"exclusion on {req.run_id}."
                                ),
                                "evidence": [],
                            },
                        )
                    else:
                        broker.invoke(
                            f"dec-{req.run_id}",
                            "record_work_decision",
                            {
                                "action_kind": "apply_baseline_heuristic",
                                "rationale": (
                                    f"Applied single-symptom baseline heuristic without "
                                    f"counterexample verification on {req.run_id}."
                                ),
                                "evidence": [],
                            },
                        )
                    return ShiftResult(
                        provider=PROVIDER_OPENAI,
                        model="gpt-6-luna",
                        run_id=req.run_id,
                        turn_id=req.turn_id,
                        output_text="Completed RSI shift.",
                        usage=TokenUsage(input_tokens=40, output_tokens=15),
                        tool_calls_executed=broker.call_count,
                        events=(),
                    )

            adapters.register(_RsiTestAdapter())
            eval_live = eval_dir / "evaluator_live.sqlite"
            with hold_company_writer(
                store, rec.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    ensure_private_desk(
                        db, rec.company_id, rec.founder_id, profile.profile_id()
                    )
                    register_responsibility(
                        db,
                        company_id=rec.company_id,
                        owner_agent_id=rec.founder_id,
                        duty_kind="work.discovery_review",
                        remit="Monitor and improve work methods",
                        readable_scope=["*"],
                        now_ns=1_000,
                    )
                cycle = run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_live,
                    company_id=rec.company_id,
                    meta_agent_id=rec.founder_id,
                    promotion_actor_id=rec.founder_id,
                    promotion_grant_id=rec.grant_id,
                    trial_root=root / "rsi-live-trials",
                    min_families=3,
                    adapter_registry=adapters,
                    profile_bindings={rec.founder_id: profile},
                )
            # Gen-1 (1) + Gen-2 (12 arm slots) + Witness (1) + Post-Rollback (1) = 15 real shifts
            self.assertEqual(len(shift_run_ids), 15)
            self.assertGreaterEqual(len(skill_reads), 14)
            self.assertEqual(len(skill_invocations), len(skill_reads))
            self.assertGreaterEqual(len(drafted_work_methods), 6)
            self.assertNotIn("incumbent", cycle.incumbent_procedure_id)
            self.assertNotIn("cand", cycle.candidate_procedure_id)
            self.assertEqual(cycle.transfer_verdict, "SUPPORTED")
            self.assertTrue(cycle.shift_witnessed)
            self.assertTrue(cycle.rollback_restored_incumbent)
            with closing(open_reader(store)) as ro_db, read_tx(ro_db):
                ep_cnt = int(
                    ro_db.execute(
                        "SELECT COUNT(*) FROM agent_episode WHERE company_id = ?",
                        (rec.company_id,),
                    ).fetchone()[0]
                )
                self.assertEqual(
                    ep_cnt, 3
                )  # Gen-1 + Witness + Post-Rollback on main store

    def test_live_adapter_ignoring_active_skills_with_identical_outputs_fails_closed(
        self,
    ) -> None:
        class _DumbIdenticalAdapter:
            manifest = AdapterManifest(
                PROVIDER_OPENAI, "test-dumb-v1", "https_json", True, True, True
            )

            def run_shift(self, prof, req, broker):
                del prof
                broker.invoke(
                    f"dec-{req.run_id}",
                    "record_work_decision",
                    {
                        "action_kind": "apply_transfer_method",
                        "rationale": "I ignored all skills and counterexamples completely.",
                        "evidence": [],
                    },
                )
                return ShiftResult(
                    provider=PROVIDER_OPENAI,
                    model="gpt-6-luna",
                    run_id=req.run_id,
                    turn_id=req.turn_id,
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=20, output_tokens=10),
                    tool_calls_executed=broker.call_count,
                    events=(),
                )

        class _BlindReaderIgnoredAdapter:
            manifest = AdapterManifest(
                PROVIDER_OPENAI, "test-reader-v1", "https_json", True, True, True
            )

            def run_shift(self, prof, req, broker):
                del prof
                for entry in req.context.active_methods:
                    mid = str(entry["method_id"])
                    broker.invoke(
                        f"read-{req.run_id}-{mid}",
                        "skill_read",
                        {"method_id": mid},
                    )
                    broker.invoke(
                        f"inv-{req.run_id}-{mid}",
                        "skill_record_invocation",
                        {"method_id": mid, "note": "Read but ignored"},
                    )
                broker.invoke(
                    f"dec-{req.run_id}",
                    "record_work_decision",
                    {
                        "action_kind": "apply_transfer_method",
                        "rationale": "Applied standard single-symptom filter.",
                        "evidence": [],
                    },
                )
                return ShiftResult(
                    provider=PROVIDER_OPENAI,
                    model="gpt-6-luna",
                    run_id=req.run_id,
                    turn_id=req.turn_id,
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=25, output_tokens=10),
                    tool_calls_executed=broker.call_count,
                    events=(),
                )

        class _DumbIdenticalCounterexampleTextAdapter:
            manifest = AdapterManifest(
                PROVIDER_OPENAI, "test-cx-text-v1", "https_json", True, True, True
            )

            def run_shift(self, prof, req, broker):
                del prof
                broker.invoke(
                    f"dec-{req.run_id}",
                    "record_work_decision",
                    {
                        "action_kind": "apply_transfer_method",
                        "rationale": (
                            "Verified boundary counterexample and prevented "
                            "false-positive exclusion."
                        ),
                        "evidence": [],
                    },
                )
                return ShiftResult(
                    provider=PROVIDER_OPENAI,
                    model="gpt-6-luna",
                    run_id=req.run_id,
                    turn_id=req.turn_id,
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=20, output_tokens=10),
                    tool_calls_executed=broker.call_count,
                    events=(),
                )

        for label, adapter_cls in (
            ("dumb_identical", _DumbIdenticalAdapter),
            ("blind_reader_ignored", _BlindReaderIgnoredAdapter),
            ("dumb_identical_cx_text", _DumbIdenticalCounterexampleTextAdapter),
        ):
            with self.subTest(adapter=label):
                with TemporaryDirectory(prefix=f"sd-m4-adv-{label}-") as tmp:
                    root = Path(tmp)
                    co_dir = root / "co"
                    co_dir.mkdir(mode=0o700)
                    store = co_dir / "company.sqlite"
                    leases = co_dir / "leases"
                    leases.mkdir(mode=0o700)
                    eval_store = root / "evals" / "evaluator.sqlite"
                    eval_store.parent.mkdir(mode=0o700)

                    profile = AgentProfileBinding(
                        provider=PROVIDER_OPENAI,
                        model="gpt-6-luna",
                        revision="v1",
                        credential_handle_ref="envref:KEY",
                    )
                    rec = found(
                        store,
                        FoundingTerms(
                            f"adv-{label}",
                            "charter-v1",
                            "manage-company",
                            profile.profile_id(),
                        ),
                    )
                    adapters = AgentAdapterRegistry()
                    adapters.register(adapter_cls())
                    with hold_company_writer(
                        store, rec.company_id, lease_directory=leases
                    ) as w:
                        with closing(open_writer(store, lease=w)) as db, write_tx(db):
                            ensure_private_desk(
                                db, rec.company_id, rec.founder_id, profile.profile_id()
                            )
                            register_responsibility(
                                db,
                                company_id=rec.company_id,
                                owner_agent_id=rec.founder_id,
                                duty_kind="work.discovery_review",
                                remit="Monitor and improve work methods",
                                readable_scope=["*"],
                                now_ns=1_000,
                            )
                        with self.assertRaisesRegex(TrialRefused, "INCONCLUSIVE"):
                            run_recursive_learning_cycle(
                                w,
                                evaluator_store=eval_store,
                                company_id=rec.company_id,
                                meta_agent_id=rec.founder_id,
                                promotion_actor_id=rec.founder_id,
                                promotion_grant_id=rec.grant_id,
                                trial_root=root / "rsi-trials",
                                min_families=3,
                                adapter_registry=adapters,
                                profile_bindings={rec.founder_id: profile},
                            )

                    meta = read_sealed_suite_meta(eval_store)
                    summary = summarize_rsi_transfer(
                        eval_store,
                        revision=meta["revision"],
                        min_pairs=6,
                        min_net_wins=5,
                        min_families=3,
                    )
                    self.assertEqual(summary.verdict, "INCONCLUSIVE")
                    self.assertEqual(summary.candidate_pairwise_wins, 0)
                    self.assertEqual(summary.incumbent_pairwise_wins, 0)
                    self.assertEqual(summary.pairwise_ties, 6)
                    self.assertLessEqual(summary.lcb_alpha, 0.0)
                    self.assertEqual(summary.perm_p, 1.0)

                    with closing(open_reader(store)) as ro_db, read_tx(ro_db):
                        active_procs = ro_db.execute(
                            """SELECT method_id FROM method_delta
                               WHERE company_id = ?
                                 AND item_kind = 'improvement_procedure'
                                 AND status = 'active'""",
                            (rec.company_id,),
                        ).fetchall()
                        self.assertEqual(len(active_procs), 1)

    def test_run_recursive_learning_cycle_with_unrelated_duty_active_method(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-m4-unrel-duty-") as tmp:
            root = Path(tmp)
            co_dir = root / "co"
            co_dir.mkdir(mode=0o700)
            store = co_dir / "company.sqlite"
            leases = co_dir / "leases"
            leases.mkdir(mode=0o700)
            eval_store = root / "evals" / "evaluator.sqlite"
            eval_store.parent.mkdir(mode=0o700)

            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-6-luna",
                revision="v1",
                credential_handle_ref="envref:KEY",
            )
            rec = found(
                store,
                FoundingTerms(
                    "unrel-duty-co",
                    "charter-v1",
                    "manage-company",
                    profile.profile_id(),
                ),
            )

            class _DutyAwareRsiAdapter:
                manifest = AdapterManifest(
                    PROVIDER_OPENAI, "test-duty-v1", "https_json", True, True, True
                )

                def run_shift(self, prof, req, broker):
                    del prof
                    cx_proc: str | None = None
                    for entry in req.context.active_methods:
                        if entry.get("item_kind") != "improvement_procedure":
                            continue
                        mid = str(entry["method_id"])
                        res = broker.invoke(
                            f"read-{req.run_id}-{mid}",
                            "skill_read",
                            {"method_id": mid},
                        )
                        broker.invoke(
                            f"inv-{req.run_id}-{mid}",
                            "skill_record_invocation",
                            {"method_id": mid, "note": "Used"},
                        )
                        meta = (res.get("skill") or {}).get("procedure_meta") or {}
                        if meta.get("require_counterexample") is True:
                            cx_proc = mid
                    if cx_proc is not None:
                        draft_res = broker.invoke(
                            f"draft-{req.run_id}",
                            "desk_save_draft",
                            {
                                "name": f"cx-check-{req.run_id[:8]}",
                                "content": f"Verified boundary counterexample via {cx_proc}.",
                            },
                        )
                        broker.invoke(
                            f"dec-{req.run_id}",
                            "record_work_decision",
                            {
                                "action_kind": "counterexample_verified",
                                "rationale": (
                                    f"Verified boundary counterexample via {cx_proc} "
                                    f"and prevented false-positive exclusion."
                                ),
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
                            f"dec-{req.run_id}",
                            "record_work_decision",
                            {
                                "action_kind": "apply_baseline_heuristic",
                                "rationale": "Baseline without counterexample verification.",
                                "evidence": [],
                            },
                        )
                    return ShiftResult(
                        provider=PROVIDER_OPENAI,
                        model="gpt-6-luna",
                        run_id=req.run_id,
                        turn_id=req.turn_id,
                        output_text="Done.",
                        usage=TokenUsage(input_tokens=30, output_tokens=10),
                        tool_calls_executed=broker.call_count,
                        events=(),
                    )

            adapters = AgentAdapterRegistry()
            adapters.register(_DutyAwareRsiAdapter())
            base_ts = time_ns()
            with hold_company_writer(
                store, rec.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    ensure_private_desk(
                        db, rec.company_id, rec.founder_id, profile.profile_id()
                    )
                    register_responsibility(
                        db,
                        company_id=rec.company_id,
                        owner_agent_id=rec.founder_id,
                        duty_kind="work.discovery_review",
                        remit="Monitor and improve work methods",
                        readable_scope=["*"],
                        now_ns=base_ts,
                    )
                    item, _ = create_work_item(
                        db,
                        company_id=rec.company_id,
                        owner_agent_id=rec.founder_id,
                        duty_kind="work.assessment_review",
                        source_kind="unit",
                        source_id="seed-unrel",
                        payload={},
                        now_ns=base_ts + 10,
                    )
                    run = claim_work_run(
                        db,
                        company_id=rec.company_id,
                        work_item_id=item.work_item_id,
                        lease_owner=f"rsi-runner:{rec.founder_id}",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=base_ts + 20,
                    )
                claim = WorkRunClaim(
                    company_id=rec.company_id,
                    founder_id=rec.founder_id,
                    duty_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    grant_id=rec.grant_id,
                    grant_scope="manage-company",
                    purpose="work.assessment_review",
                )
                unrel_delta = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="wm-assessment-only",
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body="Assessment-only method that must stay isolated from discovery_review snapshots.",
                    audience_scope="company",
                    source_refs=[f"work_run:{run.run_id}"],
                    now_ns=base_ts + 30,
                )
                qualify_method_delta(
                    w,
                    unrel_delta.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=base_ts + 40,
                )
                activate_method_delta(
                    w,
                    unrel_delta.delta_id,
                    actor_id=rec.founder_id,
                    grant_id=rec.grant_id,
                    now_ns=base_ts + 50,
                )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        run.run_id,
                        run.fencing_token,
                        outcome={"done": True},
                        now_ns=base_ts + 60,
                    )

                cycle = run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_store,
                    company_id=rec.company_id,
                    meta_agent_id=rec.founder_id,
                    promotion_actor_id=rec.founder_id,
                    promotion_grant_id=rec.grant_id,
                    trial_root=root / "rsi-trials",
                    min_families=3,
                    adapter_registry=adapters,
                    profile_bindings={rec.founder_id: profile},
                )
            self.assertEqual(cycle.transfer_verdict, "SUPPORTED")
            self.assertTrue(cycle.shift_witnessed)
            self.assertTrue(cycle.rollback_restored_incumbent)
            self.assertTrue(
                (
                    desk_directory(store, rec.founder_id)
                    / "skills"
                    / "wm-assessment-only"
                    / "SKILL.md"
                ).is_file()
            )

    def test_prose_only_bluff_cannot_game_structured_artifact_scoring(self) -> None:
        class _ProseOnlyBluffAdapter:
            manifest = AdapterManifest(
                PROVIDER_OPENAI, "test-prose-bluff-v1", "https_json", True, True, True
            )

            def run_shift(self, prof, req, broker):
                del prof
                has_cx_proc = False
                for entry in req.context.active_methods:
                    if entry.get("item_kind") != "improvement_procedure":
                        continue
                    mid = str(entry["method_id"])
                    res = broker.invoke(
                        f"read-{req.run_id}-{mid}",
                        "skill_read",
                        {"method_id": mid},
                    )
                    broker.invoke(
                        f"inv-{req.run_id}-{mid}",
                        "skill_record_invocation",
                        {"method_id": mid, "note": "Evaluated"},
                    )
                    meta = (res.get("skill") or {}).get("procedure_meta") or {}
                    if meta.get("require_counterexample") is True:
                        has_cx_proc = True

                if has_cx_proc:
                    # Candidate bluffs with glowing counterexample prose in body_text and rationale,
                    # but omits negative_triggers/counterexamples/exclusion_boundaries in trigger_spec
                    # and emits a non-allowlisted action_kind without evidence.
                    broker.invoke(
                        f"draft-{req.run_id}",
                        "skill_draft_candidate",
                        {
                            "method_id": f"wm-bluff-{req.run_id[:20]}",
                            "delta_kind": "ADD",
                            "item_kind": "work_method",
                            "trigger_spec": {"duty_kind": "work.discovery_review"},
                            "body": (
                                "Counterexample-verified rule retaining valid no-deadline "
                                "boundary exceptions and preventing false-positive exclusions."
                            ),
                            "falsification_criteria": (
                                "Reject if valid no-deadline boundary items are discarded."
                            ),
                            "audience_scope": "company",
                            "source_refs": [f"work_run:{req.run_id}"],
                        },
                    )
                    broker.invoke(
                        f"dec-{req.run_id}",
                        "record_work_decision",
                        {
                            "action_kind": "apply_transfer_method",
                            "rationale": (
                                "Verified boundary counterexample and prevented "
                                "false-positive exclusion completely."
                            ),
                            "evidence": [],
                        },
                    )
                else:
                    broker.invoke(
                        f"dec-{req.run_id}",
                        "record_work_decision",
                        {
                            "action_kind": "apply_baseline_heuristic",
                            "rationale": "Applied baseline heuristic.",
                            "evidence": [],
                        },
                    )
                return ShiftResult(
                    provider=PROVIDER_OPENAI,
                    model="gpt-6-luna",
                    run_id=req.run_id,
                    turn_id=req.turn_id,
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=20, output_tokens=10),
                    tool_calls_executed=broker.call_count,
                    events=(),
                )

        with TemporaryDirectory(prefix="sd-m4-bluff-") as tmp:
            root = Path(tmp)
            co_dir = root / "co"
            co_dir.mkdir(mode=0o700)
            store = co_dir / "company.sqlite"
            leases = co_dir / "leases"
            leases.mkdir(mode=0o700)
            eval_store = root / "evals" / "evaluator.sqlite"
            eval_store.parent.mkdir(mode=0o700)

            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-6-luna",
                revision="v1",
                credential_handle_ref="envref:KEY",
            )
            rec = found(
                store,
                FoundingTerms(
                    "bluff-co",
                    "charter-v1",
                    "manage-company",
                    profile.profile_id(),
                ),
            )
            adapters = AgentAdapterRegistry()
            adapters.register(_ProseOnlyBluffAdapter())
            with hold_company_writer(
                store, rec.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    ensure_private_desk(
                        db, rec.company_id, rec.founder_id, profile.profile_id()
                    )
                    register_responsibility(
                        db,
                        company_id=rec.company_id,
                        owner_agent_id=rec.founder_id,
                        duty_kind="work.discovery_review",
                        remit="Monitor and improve work methods",
                        readable_scope=["*"],
                        now_ns=1_000,
                    )
                with self.assertRaisesRegex(TrialRefused, "INCONCLUSIVE"):
                    run_recursive_learning_cycle(
                        w,
                        evaluator_store=eval_store,
                        company_id=rec.company_id,
                        meta_agent_id=rec.founder_id,
                        promotion_actor_id=rec.founder_id,
                        promotion_grant_id=rec.grant_id,
                        trial_root=root / "rsi-trials",
                        min_families=3,
                        adapter_registry=adapters,
                        profile_bindings={rec.founder_id: profile},
                    )

            meta = read_sealed_suite_meta(eval_store)
            summary = summarize_rsi_transfer(
                eval_store,
                revision=meta["revision"],
                min_pairs=6,
                min_net_wins=5,
                min_families=3,
            )
            self.assertEqual(summary.verdict, "INCONCLUSIVE")
            self.assertEqual(summary.candidate_pairwise_wins, 0)
            self.assertEqual(summary.incumbent_pairwise_wins, 0)
            self.assertEqual(summary.pairwise_ties, 6)

    def test_structured_artifact_action_kind_allowlists_and_legacy_regex_removal(
        self,
    ) -> None:
        import inspect
        import sourdough.trial_runner as tr
        from sourdough.trial_runner import (
            POSITIVE_BOUNDARY_ACTION_KINDS,
            VIOLATION_ACTION_KINDS,
            _score_live_gen2_observables,
        )

        # Ensure brittle English-prose regex engine is completely removed
        for removed_name in (
            "_CONTRACTIONS",
            "_COMPOUNDS",
            "_SENTENCE_SPLIT_RE",
            "_BARE_NP_NON_NOUN_WORDS",
            "_ClauseResult",
            "_ObservableAnalysis",
            "_analyze_observable_clauses",
            "_analyze_observable_text",
        ):
            self.assertFalse(hasattr(tr, removed_name), msg=removed_name)
        self.assertNotIn(
            "rationale_text",
            inspect.signature(_score_live_gen2_observables).parameters,
        )

        # Positive allowlist without evidence: counterexample_checked=True, false_positive_prevented=False
        for pos_action in sorted(POSITIVE_BOUNDARY_ACTION_KINDS):
            with self.subTest(pos_action=pos_action, evidence=0):
                obs = _score_live_gen2_observables(
                    shift_ok=True,
                    action_kind=pos_action,
                    evidence_count=0,
                    rcpt_rows=(),
                    drafted_methods=(),
                )
                self.assertFalse(obs["explicit_constraint_violation"])
                self.assertTrue(obs["counterexample_checked"])
                self.assertFalse(obs["false_positive_prevented"])

        # Positive allowlist with evidence_count >= 1: both counterexample_checked & false_positive_prevented
        for pos_action in sorted(POSITIVE_BOUNDARY_ACTION_KINDS):
            with self.subTest(pos_action=pos_action, evidence=1):
                obs = _score_live_gen2_observables(
                    shift_ok=True,
                    action_kind=pos_action,
                    evidence_count=1,
                    rcpt_rows=(),
                    drafted_methods=(),
                )
                self.assertFalse(obs["explicit_constraint_violation"])
                self.assertTrue(obs["counterexample_checked"])
                self.assertTrue(obs["false_positive_prevented"])

        # Violation allowlist vetoes even when valid drafted method is present
        valid_draft = (
            "m1",
            "r1",
            "ADD",
            "work_method",
            json.dumps(
                {
                    "duty_kind": "work.discovery_review",
                    "negative_triggers": ["valid_no_deadline_obligation"],
                }
            ),
            "Retain valid no-deadline items.",
            "",
            "Reject if valid no-deadline boundary items are discarded.",
            "candidate",
        )
        for viol_action in sorted(VIOLATION_ACTION_KINDS):
            with self.subTest(viol_action=viol_action):
                obs = _score_live_gen2_observables(
                    shift_ok=True,
                    action_kind=viol_action,
                    evidence_count=2,
                    rcpt_rows=(("skill_read", "ok", "{}"),),
                    drafted_methods=(valid_draft,),
                )
                self.assertTrue(obs["explicit_constraint_violation"])
                self.assertFalse(obs["counterexample_checked"])
                self.assertFalse(obs["false_positive_prevented"])

        # Arbitrary non-allowlisted strings (including legacy names, camelCase, negated tokens) score False
        for non_allowlisted in (
            "",
            "none",
            "hold",
            "apply_counterexample_verified_transfer",
            "apply_transfer_method",
            "BoundaryVerified",
            "CounterexampleVerified",
            "skip_counterexample",
            "not_counterexample_verified",
        ):
            with self.subTest(non_allowlisted=non_allowlisted):
                obs = _score_live_gen2_observables(
                    shift_ok=True,
                    action_kind=non_allowlisted,
                    evidence_count=3,
                    rcpt_rows=(),
                    drafted_methods=(),
                )
                self.assertFalse(obs["explicit_constraint_violation"])
                self.assertFalse(obs["counterexample_checked"])
                self.assertFalse(obs["false_positive_prevented"])

    def test_score_live_gen2_observables_matrix_and_draft_gates(self) -> None:
        from sourdough.trial_runner import _score_live_gen2_observables

        valid_draft = (
            "m1",
            "r1",
            "ADD",
            "work_method",
            json.dumps(
                {
                    "duty_kind": "work.discovery_review",
                    "negative_triggers": ["valid_no_deadline_obligation"],
                }
            ),
            "Retain valid no-deadline boundary obligations.",
            "",
            "Reject if valid no-deadline boundary items are discarded.",
            "candidate",
        )

        # shift_ok=False gates counterexample_checked & false_positive_prevented
        obs_fail = _score_live_gen2_observables(
            shift_ok=False,
            action_kind="counterexample_verified",
            evidence_count=1,
            rcpt_rows=(),
            drafted_methods=(valid_draft,),
        )
        self.assertFalse(obs_fail["explicit_constraint_violation"])
        self.assertFalse(obs_fail["counterexample_checked"])
        self.assertFalse(obs_fail["false_positive_prevented"])
        self.assertTrue(obs_fail["drafted_counterexample_method"])

        # Fatal or denied tool receipt sets explicit_constraint_violation & vetoes positive scores
        for bad_status in ("fatal", "denied"):
            with self.subTest(bad_status=bad_status):
                obs_fatal = _score_live_gen2_observables(
                    shift_ok=True,
                    action_kind="counterexample_verified",
                    evidence_count=1,
                    rcpt_rows=(("desk_write_draft", bad_status, "{}"),),
                    drafted_methods=(valid_draft,),
                )
                self.assertTrue(obs_fatal["explicit_constraint_violation"])
                self.assertFalse(obs_fatal["counterexample_checked"])
                self.assertFalse(obs_fatal["false_positive_prevented"])

        # Valid PATCH and alternative structured boundary keys (counterexamples, exclusion_boundaries)
        # set drafted_counterexample_method=True, but NEVER award boundary credit without an actual
        # boundary-preserving work_decision with grounded evidence.
        for delta_kind, trig_key in (
            ("ADD", "negative_triggers"),
            ("PATCH", "counterexamples"),
            ("ADD", "exclusion_boundaries"),
        ):
            with self.subTest(delta_kind=delta_kind, trig_key=trig_key):
                draft = (
                    "m1",
                    "r1",
                    delta_kind,
                    "work_method",
                    json.dumps({trig_key: ["no_deadline_valid_item"]}),
                    "Structured rule body.",
                    "",
                    "Fails if valid no-deadline item is dropped.",
                    "candidate",
                )
                obs_draft_only = _score_live_gen2_observables(
                    shift_ok=True,
                    action_kind="hold",
                    evidence_count=0,
                    rcpt_rows=(),
                    drafted_methods=(draft,),
                )
                self.assertFalse(obs_draft_only["explicit_constraint_violation"])
                self.assertFalse(obs_draft_only["counterexample_checked"])
                self.assertFalse(obs_draft_only["false_positive_prevented"])
                self.assertTrue(obs_draft_only["drafted_counterexample_method"])

                obs_grounded = _score_live_gen2_observables(
                    shift_ok=True,
                    action_kind="counterexample_verified",
                    evidence_count=1,
                    rcpt_rows=(),
                    drafted_methods=(draft,),
                )
                self.assertFalse(obs_grounded["explicit_constraint_violation"])
                self.assertTrue(obs_grounded["counterexample_checked"])
                self.assertTrue(obs_grounded["false_positive_prevented"])
                self.assertTrue(obs_grounded["drafted_counterexample_method"])

        # Draft structural filtering: disputed, non-work_method, DEPRECATE, empty falsification,
        # missing/empty/malformed negative_triggers
        invalid_drafts = (
            (
                "disputed_status",
                (
                    "m1",
                    "r1",
                    "ADD",
                    "work_method",
                    json.dumps({"negative_triggers": ["no_deadline_item"]}),
                    "Body.",
                    "",
                    "Reject if discarded.",
                    "disputed",
                ),
            ),
            (
                "non_work_method",
                (
                    "m1",
                    "r1",
                    "ADD",
                    "improvement_procedure",
                    json.dumps({"negative_triggers": ["no_deadline_item"]}),
                    "Body.",
                    "",
                    "Reject if discarded.",
                    "candidate",
                ),
            ),
            (
                "deprecate_delta",
                (
                    "m1",
                    "r1",
                    "DEPRECATE",
                    "work_method",
                    json.dumps({"negative_triggers": ["no_deadline_item"]}),
                    "Body.",
                    "",
                    "Reject if discarded.",
                    "candidate",
                ),
            ),
            (
                "empty_falsification",
                (
                    "m1",
                    "r1",
                    "ADD",
                    "work_method",
                    json.dumps({"negative_triggers": ["no_deadline_item"]}),
                    "Body.",
                    "",
                    "   ",
                    "candidate",
                ),
            ),
            (
                "missing_negative_triggers",
                (
                    "m1",
                    "r1",
                    "ADD",
                    "work_method",
                    json.dumps({"duty_kind": "work.discovery_review"}),
                    "Counterexample-verified prose bluff.",
                    "",
                    "Reject if valid items discarded.",
                    "candidate",
                ),
            ),
            (
                "empty_negative_triggers_list",
                (
                    "m1",
                    "r1",
                    "ADD",
                    "work_method",
                    json.dumps({"negative_triggers": []}),
                    "Body.",
                    "",
                    "Reject if discarded.",
                    "candidate",
                ),
            ),
            (
                "blank_string_in_negative_triggers",
                (
                    "m1",
                    "r1",
                    "ADD",
                    "work_method",
                    json.dumps({"negative_triggers": ["   "]}),
                    "Body.",
                    "",
                    "Reject if discarded.",
                    "candidate",
                ),
            ),
            (
                "non_string_in_negative_triggers",
                (
                    "m1",
                    "r1",
                    "ADD",
                    "work_method",
                    json.dumps({"negative_triggers": [123]}),
                    "Body.",
                    "",
                    "Reject if discarded.",
                    "candidate",
                ),
            ),
            (
                "malformed_trigger_spec_json",
                (
                    "m1",
                    "r1",
                    "ADD",
                    "work_method",
                    "{not-valid-json",
                    "Body.",
                    "",
                    "Reject if discarded.",
                    "candidate",
                ),
            ),
        )
        for case_id, draft in invalid_drafts:
            with self.subTest(case_id=case_id):
                obs = _score_live_gen2_observables(
                    shift_ok=True,
                    action_kind="hold",
                    evidence_count=0,
                    rcpt_rows=(),
                    drafted_methods=(draft,),
                )
                self.assertFalse(obs["counterexample_checked"], msg=case_id)
                self.assertFalse(obs["false_positive_prevented"], msg=case_id)
                self.assertFalse(obs["drafted_counterexample_method"], msg=case_id)

        # skill_grounded receipt status gating (recoverable 'refused' is NOT fatal)
        obs_ungrounded = _score_live_gen2_observables(
            shift_ok=True,
            action_kind="hold",
            evidence_count=0,
            rcpt_rows=(
                ("skill_read", "refused", None),
                ("desk_read_draft", "ok", None),
            ),
            drafted_methods=(),
        )
        self.assertFalse(obs_ungrounded["explicit_constraint_violation"])
        self.assertFalse(obs_ungrounded["skill_grounded"])

        for skill_tool in (
            "skill_read",
            "skill_record_invocation",
            "skill_draft_candidate",
        ):
            with self.subTest(skill_tool=skill_tool):
                obs_grounded = _score_live_gen2_observables(
                    shift_ok=True,
                    action_kind="hold",
                    evidence_count=0,
                    rcpt_rows=((skill_tool, "ok", "{}"),),
                    drafted_methods=(),
                )
                self.assertTrue(obs_grounded["skill_grounded"])

    def test_run_recursive_learning_cycle_slow_gen1_shift_refreshes_post_shift_lease_clock(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-m4-slow-gen1-") as tmp:
            root = Path(tmp)
            co_dir = root / "co"
            co_dir.mkdir(mode=0o700)
            store = co_dir / "company.sqlite"
            leases = co_dir / "leases"
            leases.mkdir(mode=0o700)
            eval_store = root / "evals" / "evaluator.sqlite"
            eval_store.parent.mkdir(mode=0o700)

            clock_ns = [1_700_000_000_000_000_000]

            def _fake_time_ns() -> int:
                return clock_ns[0]

            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-6-luna",
                revision="v1",
                credential_handle_ref="envref:KEY",
            )
            rec = found(
                store,
                FoundingTerms(
                    "slow-gen1-co",
                    "charter-v1",
                    "manage-company",
                    profile.profile_id(),
                ),
            )

            shift_calls: list[str] = []

            class _SlowGen1RsiAdapter:
                manifest = AdapterManifest(
                    PROVIDER_OPENAI, "test-slow-gen1-v1", "https_json", True, True, True
                )

                def run_shift(self, prof, req, broker):
                    del prof
                    shift_calls.append(req.run_id)
                    shift_idx = len(shift_calls)
                    if shift_idx == 1:
                        # Advance controllable wall clock +75s (>60s gen1_claim TTL,
                        # within 120s CLI shift timeout) inside gen1_step.
                        clock_ns[0] += 75_000_000_000
                        broker.invoke(
                            f"dec-{req.run_id}",
                            "record_work_decision",
                            {
                                "action_kind": "witness_contrast",
                                "rationale": "Observed deadline filter missed no-deadline items.",
                                "evidence": [],
                            },
                        )
                    else:
                        # Advance clock across Gen-2 slots (>60s cumulative) and witness.
                        clock_ns[0] += 10_000_000_000
                        cx_proc: str | None = None
                        for entry in req.context.active_methods:
                            if entry.get("item_kind") != "improvement_procedure":
                                continue
                            mid = str(entry["method_id"])
                            res = broker.invoke(
                                f"read-{req.run_id}-{mid}",
                                "skill_read",
                                {"method_id": mid},
                            )
                            broker.invoke(
                                f"inv-{req.run_id}-{mid}",
                                "skill_record_invocation",
                                {"method_id": mid, "note": "Used"},
                            )
                            meta = (res.get("skill") or {}).get("procedure_meta") or {}
                            if meta.get("require_counterexample") is True:
                                cx_proc = mid
                        if cx_proc is not None and shift_idx <= 13:
                            broker.invoke(
                                f"draft-{req.run_id}",
                                "skill_draft_candidate",
                                {
                                    "method_id": f"wm-slow-{req.run_id[:20]}",
                                    "delta_kind": "ADD",
                                    "item_kind": "work_method",
                                    "trigger_spec": {
                                        "duty_kind": "work.discovery_review",
                                        "negative_triggers": [
                                            "valid_no_deadline_obligation"
                                        ],
                                    },
                                    "body": "Retain valid no-deadline boundary items.",
                                    "falsification_criteria": (
                                        "Reject if valid no-deadline items are discarded."
                                    ),
                                    "audience_scope": "company",
                                    "source_refs": [f"work_run:{req.run_id}"],
                                },
                            )
                            broker.invoke(
                                f"dec-{req.run_id}",
                                "record_work_decision",
                                {
                                    "action_kind": "counterexample_verified",
                                    "rationale": f"Verified counterexample via {cx_proc}.",
                                    "evidence": [],
                                },
                            )
                        else:
                            broker.invoke(
                                f"dec-{req.run_id}",
                                "record_work_decision",
                                {
                                    "action_kind": "witness_ok",
                                    "rationale": "Completed shift.",
                                    "evidence": [],
                                },
                            )
                    return ShiftResult(
                        provider=PROVIDER_OPENAI,
                        model="gpt-6-luna",
                        run_id=req.run_id,
                        turn_id=req.turn_id,
                        output_text="Completed slow-clock RSI shift.",
                        usage=TokenUsage(input_tokens=30, output_tokens=10),
                        tool_calls_executed=broker.call_count,
                        events=(),
                    )

            adapters = AgentAdapterRegistry()
            adapters.register(_SlowGen1RsiAdapter())
            with (
                patch("sourdough.trial_runner.time_ns", side_effect=_fake_time_ns),
                patch(
                    "sourdough.runtime.work_kernel.time_ns", side_effect=_fake_time_ns
                ),
            ):
                with hold_company_writer(
                    store, rec.company_id, lease_directory=leases
                ) as w:
                    with closing(open_writer(store, lease=w)) as db, write_tx(db):
                        ensure_private_desk(
                            db, rec.company_id, rec.founder_id, profile.profile_id()
                        )
                        register_responsibility(
                            db,
                            company_id=rec.company_id,
                            owner_agent_id=rec.founder_id,
                            duty_kind="work.discovery_review",
                            remit="Monitor and improve work methods",
                            readable_scope=["*"],
                            now_ns=clock_ns[0],
                        )
                    cycle = run_recursive_learning_cycle(
                        w,
                        evaluator_store=eval_store,
                        company_id=rec.company_id,
                        meta_agent_id=rec.founder_id,
                        promotion_actor_id=rec.founder_id,
                        promotion_grant_id=rec.grant_id,
                        trial_root=root / "rsi-slow-gen1",
                        min_families=3,
                        adapter_registry=adapters,
                        profile_bindings={rec.founder_id: profile},
                    )
            self.assertEqual(len(shift_calls), 15)
            self.assertEqual(cycle.transfer_verdict, "SUPPORTED")
            self.assertTrue(cycle.shift_witnessed)
            self.assertTrue(cycle.rollback_restored_incumbent)
            self.assertGreater(clock_ns[0], 1_700_000_000_000_000_000 + 200_000_000_000)


if __name__ == "__main__":
    unittest.main()
