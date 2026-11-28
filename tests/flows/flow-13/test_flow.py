import json
import sqlite3
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.trial_execution import (
    CompanyRuntimeOfflineAdapter,
    TrialWorkerInput,
    dispatch_registered_trial,
)
from sourdough.trial_registry import (
    PairwiseJudgment,
    TrialCeiling,
    TrialGrade,
    TrialInputs,
    TrialRefused,
    TrialSlot,
    TrialSuite,
    blind_pairwise_packets,
    blind_trial_packet,
    commit_blind_grade,
    commit_position_swapped_pairwise_grade,
    commit_trial_observation,
    invalidate_trial_slot,
    seal_trial_suite,
    summarize_rsi_transfer,
    trial_judgment_summary,
)


def _sha(raw: bytes) -> str:
    return sha256(raw).hexdigest()


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
        with TemporaryDirectory(prefix="sd-f13-") as scratch:
            root = Path(scratch)
            # 1. Operating company store lives strictly outside offline evaluator store.
            operating_store = root / "operating" / "company.sqlite"
            operating_store.parent.mkdir(mode=0o700)
            found(
                operating_store,
                FoundingTerms("live-co", "charter-live", "paper", "profile"),
            )
            operating_store.chmod(0o600)
            operating_before = operating_store.read_bytes()

            eval_dir = root / "evaluator"
            eval_dir.mkdir(mode=0o700)
            offline_store = eval_dir / "offline.sqlite"
            trial_root = root / "isolated_trials"

            # 2. Construct sealed two-generation recursive transfer suite (2 paired cases).
            fixture_bytes = b"isolated-company-fixture-v1"
            tools_bytes = b'{"tools":["inspect_desk","record_decision"]}'
            limits = TrialCeiling(turns=4, wall_seconds=60, tokens=10_000)

            case1_bytes = b'{"case":"unseen-family-case-1","facts":"v1"}'
            case2_bytes = b'{"case":"unseen-family-case-2","facts":"v1"}'
            inc_ctx = b'{"generation":1,"procedure":"baseline-v1"}'
            cand_ctx = b'{"generation":2,"procedure":"revised-meta-v2"}'

            def make_input(case_b: bytes, ctx_b: bytes) -> TrialWorkerInput:
                return TrialWorkerInput(case_b, ctx_b, tools_bytes, limits)

            def make_trial_inputs(case_b: bytes, ctx_b: bytes) -> TrialInputs:
                return TrialInputs(
                    _sha(fixture_bytes),
                    _sha(case_b),
                    _sha(ctx_b),
                    _sha(tools_bytes),
                    "company-runtime-offline-v1",
                )

            slot_specs = [
                (
                    "p1-inc",
                    "case-1",
                    "transfer-a",
                    case1_bytes,
                    inc_ctx,
                    "incumbent",
                    "p1-cand",
                ),
                (
                    "p1-cand",
                    "case-1",
                    "transfer-a",
                    case1_bytes,
                    cand_ctx,
                    "candidate",
                    "p1-inc",
                ),
                (
                    "p2-inc",
                    "case-2",
                    "transfer-b",
                    case2_bytes,
                    inc_ctx,
                    "incumbent",
                    "p2-cand",
                ),
                (
                    "p2-cand",
                    "case-2",
                    "transfer-b",
                    case2_bytes,
                    cand_ctx,
                    "candidate",
                    "p2-inc",
                ),
            ]
            slots = tuple(
                TrialSlot(
                    slot_id=sid,
                    case_id=cid,
                    family_id=fid,
                    inputs=make_trial_inputs(cb, ctxb),
                    limits=limits,
                    arm=arm,
                    counterpart_id=cp,
                )
                for sid, cid, fid, cb, ctxb, arm, cp in slot_specs
            )
            plan_doc = {"min_pairs": 2, "min_families": 2}
            plan_digest = _sha(
                json.dumps(plan_doc, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            suite = TrialSuite(
                suite_id="flow-13-rsi-transfer",
                purpose="recursive",
                rubric_digest=_sha(b"rubric-v1"),
                evaluator_digest=_sha(b"evaluator-v1"),
                analysis_plan_digest=plan_digest,
                slots=slots,
            )

            # Operating company store cannot be repurposed as an offline evaluator store.
            with self.assertRaisesRegex(
                TrialRefused, "not an offline evaluator registry"
            ):
                seal_trial_suite(operating_store, suite, analysis_plan=plan_doc)
            self.assertEqual(operating_store.read_bytes(), operating_before)

            revision = seal_trial_suite(offline_store, suite, analysis_plan=plan_doc)
            self.assertEqual(offline_store.stat().st_mode & 0o077, 0)

            # 3. Run all 4 slots via CompanyRuntimeOfflineAdapter (fresh CompanyRuntime + Desk).
            def execute_case(runtime, desk_dir: Path, worker_input: TrialWorkerInput):
                # Prove isolated desk sits under trial_root (strictly outside operating & evaluator dirs),
                # and execute a real durable CompanyRuntime.step() bounded by limits.turns.
                self.assertTrue(desk_dir.resolve().is_relative_to(trial_root.resolve()))
                self.assertFalse(desk_dir.resolve().is_relative_to(eval_dir.resolve()))
                step_summary = runtime.step(max_dispatches=worker_input.limits.turns)
                note = desk_dir / "draft.md"
                note.write_text(
                    f"Analyzed {len(worker_input.case_inputs)} bytes", encoding="utf-8"
                )
                ctx = json.loads(worker_input.worker_context.decode("utf-8"))
                return {
                    "outcome": "completed",
                    "observation": {
                        "desk_file": note.name,
                        "executed_runs": list(step_summary.executed_runs),
                    },
                    "decision": {"action": "defer", "gen": ctx["generation"]},
                    "tool_trace": {"calls": ["inspect_desk"], "violations": 0},
                    "service_effects": {"ledger_mutated": False},
                    "usage": {"tokens": 420},
                }

            adapter = CompanyRuntimeOfflineAdapter(
                trial_root=trial_root,
                evaluator_store=offline_store,
                profile="company-runtime-offline-v1",
                execute_case=execute_case,
            )
            # Refuses launching if trial_root aliases evaluator directory!
            bad_adapter = CompanyRuntimeOfflineAdapter(
                trial_root=eval_dir,
                evaluator_store=offline_store,
                profile="company-runtime-offline-v1",
                execute_case=execute_case,
            )
            with self.assertRaisesRegex(TrialRefused, "trial isolation"):
                dispatch_registered_trial(
                    offline_store,
                    revision=revision,
                    slot_id="p1-inc",
                    run_id="bad-run",
                    company_fixture=fixture_bytes,
                    worker_input=make_input(case1_bytes, inc_ctx),
                    runtime=bad_adapter,
                )

            result_ids: dict[str, str] = {}
            for idx, (sid, _, _, cb, ctxb, _, _) in enumerate(slot_specs, start=1):
                run_id = f"trial-run-{idx}"
                res = dispatch_registered_trial(
                    offline_store,
                    revision=revision,
                    slot_id=sid,
                    run_id=run_id,
                    company_fixture=fixture_bytes,
                    worker_input=make_input(cb, ctxb),
                    runtime=adapter,
                )
                self.assertTrue(res.launch_permitted)
                run_store = adapter.run_stores[run_id]
                self.assertNotEqual(run_store.resolve(), operating_store.resolve())
                self.assertNotEqual(run_store.resolve(), offline_store.resolve())
                self.assertTrue(run_store.exists())
                record = commit_trial_observation(
                    offline_store,
                    revision=revision,
                    slot_id=sid,
                    run_id=run_id,
                    evidence=adapter.evidences[run_id],
                )
                assert record.result_id is not None
                result_ids[sid] = record.result_id

                # Blind per-case rubric grade.
                packet = blind_trial_packet(
                    offline_store, revision=revision, result_id=record.result_id
                )
                commit_blind_grade(
                    offline_store,
                    revision=revision,
                    packet=packet,
                    grade=TrialGrade(True, True, True, 0, True),
                )

            # 4. Position-swapped (AB, BA) blind pairwise judging across both pairs.
            cand_results = {result_ids["p1-cand"], result_ids["p2-cand"]}
            pairs = blind_pairwise_packets(offline_store, revision=revision)
            self.assertEqual(len(pairs), 2)
            for ab, ba in pairs:
                ab_pref = "first" if ab.first.result_id in cand_results else "second"
                ba_pref = "second" if ab_pref == "first" else "first"
                pw = commit_position_swapped_pairwise_grade(
                    offline_store,
                    revision=revision,
                    ab_packet=ab,
                    ab_judgment=PairwiseJudgment("Clearer grounding.", ab_pref),
                    ba_packet=ba,
                    ba_judgment=PairwiseJudgment("Confirmed after swap.", ba_pref),
                )
                self.assertTrue(pw.consistent)
                self.assertIn(pw.winner_result_id, cand_results)

            rsi = summarize_rsi_transfer(
                offline_store,
                revision=revision,
                min_pairs=2,
                min_net_wins=2,
                min_families=2,
            )
            self.assertEqual(rsi.verdict, "SUPPORTED")
            self.assertEqual(rsi.candidate_pairwise_wins, 2)
            self.assertEqual(rsi.candidate_authority_violations, 0)
            self.assertEqual(rsi.distinct_transfer_families, 2)

            summary = trial_judgment_summary(offline_store, revision=revision)
            self.assertEqual(
                (
                    summary.planned,
                    summary.completed,
                    summary.judgment_passed,
                    summary.invalidated,
                ),
                (4, 4, 4, 0),
            )

            # 5. Discovering leakage invalidates slot without erasing attempt and blocks SUPPORTED!
            invalidate_trial_slot(
                offline_store,
                revision=revision,
                slot_id="p2-cand",
                evidence_digest=_sha(b"leaked-label-evidence"),
            )
            post_leak = summarize_rsi_transfer(
                offline_store, revision=revision, min_pairs=2, min_net_wins=1
            )
            self.assertEqual(post_leak.verdict, "INCONCLUSIVE")
            self.assertEqual(post_leak.invalidated_slots, 1)

            # Verify operating company store remained 100% untouched byte-for-byte.
            self.assertEqual(operating_store.read_bytes(), operating_before)
            with closing(sqlite3.connect(offline_store)) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM trial_observation").fetchone(),
                    (4,),
                )
