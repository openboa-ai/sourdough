import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
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
    invalidate_trial_slot,
    reconcile_paired_grades,
    reserve_trial_launch,
    seal_trial_suite,
    summarize_rsi_transfer,
    trial_judgment_summary,
    trial_run_records,
)


class OfflineTrialRegistry(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "offline.sqlite"
        self.inputs = TrialInputs("a" * 64, "b" * 64, "c" * 64, "d" * 64, "profile")
        self.limits = TrialCeiling(2, 45, 5000)
        self.slot = TrialSlot(
            "slot-one", "document-review", "work-facts", self.inputs, self.limits
        )
        self.other = replace(self.slot, slot_id="slot-two", case_id="missing-document")
        self.plan = TrialSuite(
            "diagnostic",
            "judgment",
            "e" * 64,
            "f" * 64,
            "0" * 64,
            (self.slot, self.other),
        )

    def reserve(self, revision, **changes):
        request = {
            "revision": revision,
            "slot_id": self.slot.slot_id,
            "run_id": "first",
            "inputs": self.inputs,
            "limits": self.limits,
        }
        return reserve_trial_launch(self.store, **(request | changes))

    def test_exact_seal_only_and_refuse_changed_inputs_before_launch(self):
        revision = seal_trial_suite(self.store, self.plan)
        self.assertEqual(seal_trial_suite(self.store, self.plan), revision)
        with self.assertRaisesRegex(TrialRefused, "already sealed"):
            seal_trial_suite(
                self.store, replace(self.plan, slots=tuple(reversed(self.plan.slots)))
            )
        self.assertEqual(self.store.stat().st_mode & 0o077, 0)
        for change in (
            {"inputs": replace(self.inputs, worker_context="0" * 64)},
            {"inputs": replace(self.inputs, runtime_profile="different")},
            {"limits": replace(self.limits, turns=3)},
            {"slot_id": "not planned"},
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(TrialRefused, "not registered"),
            ):
                self.reserve(revision, **change)
        with self.assertRaisesRegex(TrialRefused, "registration revision changed"):
            self.reserve("1" * 64)
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM trial_reservation").fetchone(), (0,)
            )
        permitted = self.reserve(revision)
        self.assertTrue(permitted.launch_permitted)
        self.assertEqual(set(asdict(permitted)), {"run_id", "launch_permitted"})
        with self.assertRaisesRegex(TrialRefused, "different trial slot"):
            self.reserve(revision, slot_id=self.other.slot_id)

    def test_process_exit_after_reservation_leaves_one_attempt_and_an_uncovered_slot(
        self,
    ):
        revision = seal_trial_suite(self.store, self.plan)
        script = """
import json, os, sys
from pathlib import Path
from sourdough.trial_registry import TrialCeiling, TrialInputs, reserve_trial_launch
inputs, limits = json.load(sys.stdin)
receipt = reserve_trial_launch(
    Path(sys.argv[1]), revision=sys.argv[2], slot_id="slot-one", run_id="first",
    inputs=TrialInputs(**inputs), limits=TrialCeiling(**limits),
)
assert receipt.launch_permitted
os._exit(74)
"""
        separate = subprocess.run(
            [sys.executable, "-IB", "-c", script, str(self.store), revision],
            input=json.dumps([asdict(self.inputs), asdict(self.limits)]),
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(separate.returncode, 74, separate.stderr)
        self.assertFalse(self.reserve(revision).launch_permitted)
        with self.assertRaisesRegex(TrialRefused, "original run"):
            self.reserve(revision, run_id="different")
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            planned = json.loads(
                db.execute("SELECT document FROM trial_suite").fetchone()[0]
            )
            self.assertEqual(
                [slot["slot_id"] for slot in planned["slots"]], ["slot-one", "slot-two"]
            )
            self.assertEqual(
                db.execute("SELECT slot_id,run_id FROM trial_reservation").fetchall(),
                [("slot-one", "first")],
            )

    def test_process_exit_after_terminal_commit_replays_the_original_record(self):
        revision = seal_trial_suite(self.store, self.plan)
        self.reserve(revision)
        evidence = TrialTerminalEvidence(
            "completed", "1" * 64, decision_digest="2" * 64, usage_digest="3" * 64
        )
        script = """
import json, os, sys
from pathlib import Path
from sourdough.trial_registry import TrialTerminalEvidence, commit_trial_observation
commit_trial_observation(
    Path(sys.argv[1]), revision=sys.argv[2], slot_id="slot-one",
    run_id="first", evidence=TrialTerminalEvidence(**json.load(sys.stdin)),
)
os._exit(75)
"""
        separate = subprocess.run(
            [sys.executable, "-IB", "-c", script, str(self.store), revision],
            input=json.dumps(asdict(evidence)),
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(separate.returncode, 75, separate.stderr)
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            result_id, raw = db.execute(
                "SELECT result_id,evidence FROM trial_observation WHERE run_id='first'"
            ).fetchone()
        self.assertRegex(result_id, r"^[a-f0-9]{32}$")
        self.assertEqual(json.loads(raw), asdict(evidence))
        request = {
            "revision": revision,
            "slot_id": self.slot.slot_id,
            "run_id": "first",
        }
        original = commit_trial_observation(self.store, **request, evidence=evidence)
        self.assertEqual(original.result_id, result_id)
        self.assertFalse(self.reserve(revision).launch_permitted)
        with self.assertRaisesRegex(TrialRefused, "already committed"):
            commit_trial_observation(
                self.store, **request, evidence=replace(evidence, usage_digest=None)
            )
        records = trial_run_records(self.store, revision=revision)
        self.assertEqual(records[0], original)
        self.assertEqual(
            [record.state for record in records], ["completed", "uncovered"]
        )

    def test_failed_timeout_and_known_no_decision_are_not_missing_or_rerunnable(self):
        terminal_states = ("failed", "timed-out", "no-decision")
        terminals = tuple(
            replace(self.slot, slot_id=outcome) for outcome in terminal_states
        )
        revision = seal_trial_suite(
            self.store, replace(self.plan, slots=(self.slot, *terminals, self.other))
        )
        self.reserve(revision)
        self.assertEqual(
            [
                record.state
                for record in trial_run_records(self.store, revision=revision)
            ],
            ["incomplete", "uncovered", "uncovered", "uncovered", "uncovered"],
        )
        for terminal in terminals:
            run_id = f"run-{terminal.slot_id}"
            reservation = {"slot_id": terminal.slot_id, "run_id": run_id}
            evidence = TrialTerminalEvidence(terminal.slot_id, "1" * 64)
            with self.assertRaisesRegex(TrialRefused, "original reservation"):
                commit_trial_observation(
                    self.store, revision=revision, **reservation, evidence=evidence
                )
            self.reserve(revision, **reservation)
            with self.assertRaisesRegex(TrialRefused, "original reservation"):
                commit_trial_observation(
                    self.store,
                    revision=revision,
                    slot_id=terminal.slot_id,
                    run_id="first",
                    evidence=evidence,
                )
            commit_trial_observation(
                self.store, revision=revision, **reservation, evidence=evidence
            )
            self.assertFalse(self.reserve(revision, **reservation).launch_permitted)

        records = trial_run_records(self.store, revision=revision)
        self.assertEqual(
            [record.state for record in records],
            ["incomplete", *terminal_states, "uncovered"],
        )
        self.assertIsNone(records[0].evidence)
        self.assertTrue(
            all(record.evidence.usage_digest is None for record in records[1:4])
        )
        self.assertIsNone(records[-1].run_id)
        self.assertEqual(len({record.result_id for record in records[1:4]}), 3)
        with self.assertRaisesRegex(TrialRefused, "Invalid terminal trial evidence"):
            TrialTerminalEvidence("completed", "1" * 64)
        with self.assertRaisesRegex(TrialRefused, "Invalid terminal trial evidence"):
            TrialTerminalEvidence("no-decision", "1" * 64, decision_digest="2" * 64)

    def test_recursive_registration_accepts_only_matched_pairs(self):
        left = replace(self.slot, arm="incumbent", counterpart_id="right")
        right = replace(
            self.slot,
            slot_id="right",
            arm="candidate",
            counterpart_id=left.slot_id,
            inputs=replace(self.inputs, worker_context="9" * 64),
        )
        trial = replace(self.plan, purpose="recursive", slots=(left, right))
        seal_trial_suite(self.store, trial)
        for candidate in (
            replace(right, limits=replace(right.limits, tokens=100)),
            replace(right, inputs=replace(right.inputs, runtime_profile="other")),
            replace(right, family_id="changed"),
            replace(right, arm="incumbent"),
        ):
            with self.assertRaisesRegex(TrialRefused, "not matched"):
                replace(trial, slots=(left, candidate))
        with self.assertRaisesRegex(TrialRefused, "complete declared pairs"):
            replace(self.plan, purpose="recursive")

    def test_reconcile_arms_only_after_the_separate_blind_scores_commit(self):
        left = replace(self.slot, arm="incumbent", counterpart_id="right")
        right = replace(
            left,
            slot_id="right",
            arm="candidate",
            counterpart_id=left.slot_id,
            inputs=replace(self.inputs, worker_context="9" * 64),
        )
        revision = seal_trial_suite(
            self.store, replace(self.plan, purpose="recursive", slots=(left, right))
        )
        packets = []
        for slot in (left, right):
            run_id = f"private-{slot.arm}"
            self.reserve(
                revision, slot_id=slot.slot_id, run_id=run_id, inputs=slot.inputs
            )
            record = commit_trial_observation(
                self.store,
                revision=revision,
                slot_id=slot.slot_id,
                run_id=run_id,
                evidence=TrialTerminalEvidence(
                    "completed", "1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64
                ),
            )
            packets.append(
                blind_trial_packet(
                    self.store, revision=revision, result_id=record.result_id
                )
            )
        self.assertIsNone(reconcile_paired_grades(self.store, revision=revision))
        script = """
import json, sys
from dataclasses import asdict
from sourdough.trial_registry import TrialGrade
packet = json.load(sys.stdin)
assert set(packet) == {
    "result_id", "rubric_digest", "evaluator_digest", "outcome",
    "decision_digest", "tool_trace_digest", "service_effects_digest",
}
assert packet["rubric_digest"] == "e" * 64 and packet["evaluator_digest"] == "f" * 64
assert packet["decision_digest"] == "2" * 64 and packet["tool_trace_digest"] == "3" * 64
print(json.dumps(asdict(TrialGrade(True, True, True, 1, True))))
"""
        scorer = subprocess.run(
            [sys.executable, "-IB", "-c", script],
            input=json.dumps(asdict(packets[0])),
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
        )
        denied = TrialGrade(**json.loads(scorer.stdout))
        request = {"revision": revision, "packet": packets[0], "grade": denied}
        with self.assertRaisesRegex(TrialRefused, "original blinded packet"):
            commit_blind_grade(
                self.store,
                **(request | {"packet": replace(packets[0], rubric_digest="8" * 64)}),
            )
        self.assertEqual(commit_blind_grade(self.store, **request), denied)
        self.assertIsNone(reconcile_paired_grades(self.store, revision=revision))
        self.assertEqual(commit_blind_grade(self.store, **request), denied)
        with self.assertRaisesRegex(TrialRefused, "already committed"):
            commit_blind_grade(
                self.store,
                **(request | {"grade": replace(denied, authority_violations=0)}),
            )
        commit_blind_grade(
            self.store,
            revision=revision,
            packet=packets[1],
            grade=TrialGrade(True, True, True, 0, False),
        )
        grades = reconcile_paired_grades(self.store, revision=revision)
        self.assertEqual(
            [
                (item.arm, item.judgment_passed, item.grade.service_correct)
                for item in grades
            ],
            [("incumbent", False, True), ("candidate", True, False)],
        )
        self.assertEqual(
            [item.packet.result_id for item in grades], [p.result_id for p in packets]
        )
        baseline = trial_judgment_summary(self.store, revision=revision)
        self.assertEqual(
            (baseline.planned, baseline.scored, baseline.judgment_passed), (2, 2, 1)
        )
        self.assertEqual((baseline.planned_cases, baseline.cases_all_passed), (1, 0))
        self.assertEqual((baseline.service_correct, baseline.service_incorrect), (1, 1))
        observed_leak = b"worker input contains a sealed expected decision"
        invalidation = {
            "revision": revision,
            "slot_id": right.slot_id,
            "evidence_digest": sha256(observed_leak).hexdigest(),
        }
        self.assertEqual(
            invalidate_trial_slot(self.store, **invalidation),
            invalidation["evidence_digest"],
        )
        self.assertEqual(
            invalidate_trial_slot(self.store, **invalidation),
            invalidation["evidence_digest"],
        )
        with self.assertRaisesRegex(TrialRefused, "already committed"):
            invalidate_trial_slot(
                self.store, **(invalidation | {"evidence_digest": "7" * 64})
            )
        self.assertIsNone(reconcile_paired_grades(self.store, revision=revision))
        with self.assertRaisesRegex(TrialRefused, "invalidated"):
            blind_trial_packet(
                self.store, revision=revision, result_id=packets[1].result_id
            )
        records = trial_run_records(self.store, revision=revision)
        self.assertEqual(records[-1].result_id, packets[1].result_id)
        summary = trial_judgment_summary(self.store, revision=revision)
        self.assertEqual(
            summary,
            replace(baseline, invalidated=1, judgment_passed=0, service_incorrect=0),
        )
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM trial_grade").fetchone(), (2,)
            )
            self.assertEqual(
                db.execute("SELECT evidence_digest FROM trial_invalidation").fetchone(),
                (invalidation["evidence_digest"],),
            )

    def test_missing_observations_cannot_support_a_grade_claim(self):
        revision = seal_trial_suite(self.store, self.plan)
        self.reserve(revision)
        with self.assertRaisesRegex(TrialRefused, "opaque trial result"):
            blind_trial_packet(self.store, revision=revision, result_id="first")
        record = commit_trial_observation(
            self.store,
            revision=revision,
            slot_id=self.slot.slot_id,
            run_id="first",
            evidence=TrialTerminalEvidence("timed-out", "1" * 64),
        )
        packet = blind_trial_packet(
            self.store, revision=revision, result_id=record.result_id
        )
        unavailable = TrialGrade(None, None, None, None, None)
        for claim in (
            replace(unavailable, grounding=False),
            replace(unavailable, authority_violations=0),
            replace(unavailable, service_correct=False),
        ):
            with self.assertRaisesRegex(TrialRefused, "without its evidence"):
                commit_blind_grade(
                    self.store, revision=revision, packet=packet, grade=claim
                )
        self.assertEqual(
            commit_blind_grade(
                self.store, revision=revision, packet=packet, grade=unavailable
            ),
            unavailable,
        )
        self.assertEqual(
            [
                record.state
                for record in trial_run_records(self.store, revision=revision)
            ],
            ["timed-out", "uncovered"],
        )
        invalidate_trial_slot(
            self.store,
            revision=revision,
            slot_id=self.other.slot_id,
            evidence_digest="6" * 64,
        )
        with self.assertRaisesRegex(TrialRefused, "cannot launch"):
            self.reserve(revision, slot_id=self.other.slot_id, run_id="not-started")
        summary = trial_judgment_summary(self.store, revision=revision)
        self.assertEqual(
            (
                summary.planned,
                summary.timed_out,
                summary.uncovered,
                summary.invalidated,
            ),
            (2, 1, 1, 1),
        )
        self.assertEqual((summary.service_correct, summary.service_incorrect), (0, 0))
        self.assertEqual(summary.usage_evidence_available, 0)

    def test_operating_company_store_is_not_a_trial_store_even_with_local_write_access(
        self,
    ):
        operating = self.root / "company.sqlite"
        found(operating, FoundingTerms("operating", "charter", "manage", "runtime"))
        operating.chmod(0o600)
        before = operating.read_bytes()
        with self.assertRaisesRegex(TrialRefused, "not an offline evaluator registry"):
            seal_trial_suite(operating, self.plan)
        self.assertEqual(operating.read_bytes(), before)

    def test_offline_store_uses_wal_and_strict_tables_with_position_swapped_blind_judging(
        self,
    ):
        cand_inputs = replace(self.inputs, worker_context="9" * 64)
        pair1_inc = TrialSlot(
            "p1-inc",
            "case-1",
            "transfer-family",
            self.inputs,
            self.limits,
            arm="incumbent",
            counterpart_id="p1-cand",
        )
        pair1_cand = TrialSlot(
            "p1-cand",
            "case-1",
            "transfer-family",
            cand_inputs,
            self.limits,
            arm="candidate",
            counterpart_id="p1-inc",
        )
        pair2_inc = TrialSlot(
            "p2-inc",
            "case-2",
            "transfer-family",
            self.inputs,
            self.limits,
            arm="incumbent",
            counterpart_id="p2-cand",
        )
        pair2_cand = TrialSlot(
            "p2-cand",
            "case-2",
            "transfer-family",
            cand_inputs,
            self.limits,
            arm="candidate",
            counterpart_id="p2-inc",
        )
        suite = TrialSuite(
            "rsi-transfer-1",
            "recursive",
            "e" * 64,
            "f" * 64,
            "0" * 64,
            (pair1_inc, pair1_cand, pair2_inc, pair2_cand),
        )
        revision = seal_trial_suite(self.store, suite)
        with closing(sqlite3.connect(self.store)) as db:
            self.assertEqual(
                db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal"
            )
            strict_flags = db.execute(
                "SELECT name, strict FROM pragma_table_list WHERE schema='main' AND type='table' AND name LIKE 'trial_%'"
            ).fetchall()
            self.assertEqual(len(strict_flags), 6)
            self.assertTrue(all(flag == 1 for _, flag in strict_flags))

        # Execute & commit all 4 slots.
        slot_results: dict[str, str] = {}
        for idx, slot in enumerate(suite.slots, start=1):
            run_id = f"run-{idx}"
            reserve_trial_launch(
                self.store,
                revision=revision,
                slot_id=slot.slot_id,
                run_id=run_id,
                inputs=slot.inputs,
                limits=slot.limits,
            )
            rec = commit_trial_observation(
                self.store,
                revision=revision,
                slot_id=slot.slot_id,
                run_id=run_id,
                evidence=TrialTerminalEvidence(
                    "completed",
                    sha256(f"obs-{slot.slot_id}".encode()).hexdigest(),
                    decision_digest=sha256(f"dec-{slot.slot_id}".encode()).hexdigest(),
                    tool_trace_digest=sha256(f"tr-{slot.slot_id}".encode()).hexdigest(),
                    service_effects_digest=sha256(
                        f"fx-{slot.slot_id}".encode()
                    ).hexdigest(),
                    usage_digest=sha256(f"use-{slot.slot_id}".encode()).hexdigest(),
                ),
            )
            assert rec.result_id is not None
            slot_results[slot.slot_id] = rec.result_id
            pkt = blind_trial_packet(
                self.store, revision=revision, result_id=rec.result_id
            )
            commit_blind_grade(
                self.store,
                revision=revision,
                packet=pkt,
                grade=TrialGrade(True, True, True, 0, True),
            )

        # Pairwise packets strip arm/slot/run identifiers and provide exact AB + BA swaps.
        pairs = blind_pairwise_packets(self.store, revision=revision)
        self.assertEqual(len(pairs), 2)
        for ab, ba in pairs:
            raw_ab = json.dumps(asdict(ab))
            for forbidden in ("incumbent", "candidate", "p1-inc", "p1-cand", "run-1"):
                self.assertNotIn(forbidden, raw_ab)
            self.assertEqual(ab.first, ba.second)
            self.assertEqual(ab.second, ba.first)

        # Rationale is mandatory before score.
        with self.assertRaisesRegex(TrialRefused, "rationale"):
            PairwiseJudgment("   ", "first")

        # Pair 1: consistent candidate win across (A, B) and (B, A).
        cand_ids = {slot_results["p1-cand"], slot_results["p2-cand"]}
        ab0, ba0 = pairs[0]
        ab0_pref = "first" if ab0.first.result_id in cand_ids else "second"
        ba0_pref = "second" if ab0_pref == "first" else "first"
        res0 = commit_position_swapped_pairwise_grade(
            self.store,
            revision=revision,
            ab_packet=ab0,
            ab_judgment=PairwiseJudgment(
                "Candidate stayed grounded on evidence.", ab0_pref
            ),
            ba_packet=ba0,
            ba_judgment=PairwiseJudgment("Same winner after position swap.", ba0_pref),
        )
        self.assertTrue(res0.consistent)
        self.assertEqual(res0.outcome, "winner")
        self.assertIn(res0.winner_result_id, cand_ids)
        # Idempotent replay succeeds; conflicting judgment is refused.
        self.assertEqual(
            commit_position_swapped_pairwise_grade(
                self.store,
                revision=revision,
                ab_packet=ab0,
                ab_judgment=PairwiseJudgment(
                    "Candidate stayed grounded on evidence.", ab0_pref
                ),
                ba_packet=ba0,
                ba_judgment=PairwiseJudgment(
                    "Same winner after position swap.", ba0_pref
                ),
            ),
            res0,
        )
        with self.assertRaisesRegex(TrialRefused, "already committed"):
            commit_position_swapped_pairwise_grade(
                self.store,
                revision=revision,
                ab_packet=ab0,
                ab_judgment=PairwiseJudgment("Flipped", "tie"),
                ba_packet=ba0,
                ba_judgment=PairwiseJudgment("Flipped", "tie"),
            )

        # Pair 2: position-biased inconsistent judge ("first" in BOTH AB and BA) -> tie_noise!
        ab1, ba1 = pairs[1]
        res1 = commit_position_swapped_pairwise_grade(
            self.store,
            revision=revision,
            ab_packet=ab1,
            ab_judgment=PairwiseJudgment("Preferred first shown.", "first"),
            ba_packet=ba1,
            ba_judgment=PairwiseJudgment("Preferred first shown again.", "first"),
        )
        self.assertFalse(res1.consistent)
        self.assertEqual(res1.outcome, "tie_noise")
        self.assertIsNone(res1.winner_result_id)

        # Net wins = 1 (cand=1, inc=0, noise=1) -> SUPPORTED with min_net_wins=1,
        # INCONCLUSIVE with min_net_wins=2 (explicitly recording position-bias-noise).
        supported = summarize_rsi_transfer(
            self.store, revision=revision, min_pairs=2, min_net_wins=1
        )
        self.assertEqual(supported.verdict, "SUPPORTED")
        self.assertEqual(
            (
                supported.candidate_pairwise_wins,
                supported.incumbent_pairwise_wins,
                supported.pairwise_noise,
            ),
            (1, 0, 1),
        )
        inconclusive = summarize_rsi_transfer(
            self.store, revision=revision, min_pairs=2, min_net_wins=2
        )
        self.assertEqual(inconclusive.verdict, "INCONCLUSIVE")
        self.assertIn("position-bias-noise", inconclusive.reasons)

    def _build_two_pair_suite(
        self, *, same_family: bool = False
    ) -> tuple[TrialSuite, str]:
        cand_inputs = replace(self.inputs, worker_context="9" * 64)
        fam2 = "fam-a" if same_family else "fam-b"
        slots = (
            TrialSlot(
                "p1-inc",
                "case-1",
                "fam-a",
                self.inputs,
                self.limits,
                arm="incumbent",
                counterpart_id="p1-cand",
            ),
            TrialSlot(
                "p1-cand",
                "case-1",
                "fam-a",
                cand_inputs,
                self.limits,
                arm="candidate",
                counterpart_id="p1-inc",
            ),
            TrialSlot(
                "p2-inc",
                "case-2",
                fam2,
                self.inputs,
                self.limits,
                arm="incumbent",
                counterpart_id="p2-cand",
            ),
            TrialSlot(
                "p2-cand",
                "case-2",
                fam2,
                cand_inputs,
                self.limits,
                arm="candidate",
                counterpart_id="p2-inc",
            ),
        )
        suite = TrialSuite(
            "rsi-adversarial",
            "recursive",
            "e" * 64,
            "f" * 64,
            "0" * 64,
            slots,
        )
        store = self.root / f"adv-{os.urandom(4).hex()}.sqlite"
        rev = seal_trial_suite(store, suite)
        return suite, (store, rev)

    def _run_and_grade_suite(
        self,
        store: Path,
        suite: TrialSuite,
        revision: str,
        *,
        grade_for_slot=None,
        evidence_for_slot=None,
    ) -> dict[str, str]:
        results: dict[str, str] = {}
        for idx, slot in enumerate(suite.slots, start=1):
            run_id = f"run-{idx}"
            reserve_trial_launch(
                store,
                revision=revision,
                slot_id=slot.slot_id,
                run_id=run_id,
                inputs=slot.inputs,
                limits=slot.limits,
            )
            ev = (
                evidence_for_slot(slot)
                if evidence_for_slot is not None
                else TrialTerminalEvidence(
                    "completed",
                    sha256(f"obs-{slot.slot_id}".encode()).hexdigest(),
                    decision_digest=sha256(f"dec-{slot.slot_id}".encode()).hexdigest(),
                    tool_trace_digest=sha256(f"tr-{slot.slot_id}".encode()).hexdigest(),
                    service_effects_digest=sha256(
                        f"fx-{slot.slot_id}".encode()
                    ).hexdigest(),
                    usage_digest=sha256(f"use-{slot.slot_id}".encode()).hexdigest(),
                )
            )
            rec = commit_trial_observation(
                store,
                revision=revision,
                slot_id=slot.slot_id,
                run_id=run_id,
                evidence=ev,
            )
            assert rec.result_id is not None
            results[slot.slot_id] = rec.result_id
            pkt = blind_trial_packet(store, revision=revision, result_id=rec.result_id)
            grade = (
                grade_for_slot(slot)
                if grade_for_slot is not None
                else TrialGrade(True, True, True, 0, True)
            )
            commit_blind_grade(store, revision=revision, packet=pkt, grade=grade)
        return results

    def test_pairwise_ddl_and_tamper_detection_fail_closed(self):
        suite, (store, rev) = self._build_two_pair_suite()
        results = self._run_and_grade_suite(store, suite, rev)
        pairs = blind_pairwise_packets(store, revision=rev)
        cand_ids = {results["p1-cand"], results["p2-cand"]}
        for ab, ba in pairs:
            pref = "first" if ab.first.result_id in cand_ids else "second"
            swap = "second" if pref == "first" else "first"
            commit_position_swapped_pairwise_grade(
                store,
                revision=rev,
                ab_packet=ab,
                ab_judgment=PairwiseJudgment("Better.", pref),
                ba_packet=ba,
                ba_judgment=PairwiseJudgment("Still better.", swap),
            )

        # 1. SQLite CHECK refuses cross-pair winner_result_id and inconsistent winner.
        ab0, _ = pairs[0]
        ab1, _ = pairs[1]
        with closing(sqlite3.connect(store)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "UPDATE trial_pairwise_grade SET winner_result_id=? WHERE pair_id=?",
                    (ab1.first.result_id, ab0.pair_id),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "UPDATE trial_pairwise_grade SET consistent=0 WHERE pair_id=?",
                    (ab0.pair_id,),
                )
            # 2. Tampering outcome/winner to contradict stored payload_json is detected on read!
            other_winner = (
                ab0.second.result_id
                if ab0.first.result_id in cand_ids
                else ab0.first.result_id
            )
            db.execute(
                "UPDATE trial_pairwise_grade SET winner_result_id=? WHERE pair_id=?",
                (other_winner, ab0.pair_id),
            )
            db.commit()
        with self.assertRaisesRegex(TrialRefused, "contradicts stored judgments"):
            summarize_rsi_transfer(store, revision=rev, min_pairs=2, min_net_wins=1)

        # 3. Restore pair 0 and inject an orphaned cross-pair row -> refused fail-closed!
        orig_winner = (
            ab0.first.result_id
            if ab0.first.result_id in cand_ids
            else ab0.second.result_id
        )
        cross_a, cross_b = sorted((results["p1-inc"], results["p2-cand"]))
        with closing(sqlite3.connect(store)) as db:
            db.execute(
                "UPDATE trial_pairwise_grade SET winner_result_id=? WHERE pair_id=?",
                (orig_winner, ab0.pair_id),
            )
            db.execute(
                """INSERT INTO trial_pairwise_grade VALUES (
                       ?, ?, ?, ?, ?, 1, 'tie', NULL, ?
                   )""",
                (
                    sha256(b"orphan-pair").hexdigest(),
                    cross_a,
                    cross_b,
                    "1" * 64,
                    "2" * 64,
                    json.dumps(
                        {
                            "ab_judgment": {"rationale": "x", "preferred": "tie"},
                            "ba_judgment": {"rationale": "y", "preferred": "tie"},
                        }
                    ),
                ),
            )
            db.commit()
        with self.assertRaisesRegex(TrialRefused, "declared suite pair"):
            summarize_rsi_transfer(store, revision=rev, min_pairs=2, min_net_wins=1)

    def test_summarize_rsi_transfer_fail_closed_verdicts_and_invalidation_precedence(
        self,
    ):
        # Non-recursive suite & invalid params rejected.
        diag_rev = seal_trial_suite(self.store, self.plan)
        with self.assertRaisesRegex(TrialRefused, "recursive"):
            summarize_rsi_transfer(self.store, revision=diag_rev)
        for bad_kwargs in (
            {"min_pairs": 0},
            {"min_pairs": True},
            {"min_net_wins": 0},
            {"min_net_wins": False},
            {"min_families": 0},
        ):
            with self.assertRaisesRegex(TrialRefused, "Invalid RSI"):
                summarize_rsi_transfer(self.store, revision=diag_rev, **bad_kwargs)

        # Case A: Incomplete rubric (authority_violations=None) -> INCONCLUSIVE, never SUPPORTED.
        suite_a, (store_a, rev_a) = self._build_two_pair_suite()
        res_a = self._run_and_grade_suite(
            store_a,
            suite_a,
            rev_a,
            evidence_for_slot=lambda s: TrialTerminalEvidence(
                "completed",
                sha256(f"obs-{s.slot_id}".encode()).hexdigest(),
                decision_digest=sha256(f"dec-{s.slot_id}".encode()).hexdigest(),
                tool_trace_digest=None,
                service_effects_digest=sha256(f"fx-{s.slot_id}".encode()).hexdigest(),
            ),
            grade_for_slot=lambda _s: TrialGrade(True, True, True, None, True),
        )
        cand_a = {res_a["p1-cand"], res_a["p2-cand"]}
        for ab, ba in blind_pairwise_packets(store_a, revision=rev_a):
            pref = "first" if ab.first.result_id in cand_a else "second"
            swap = "second" if pref == "first" else "first"
            commit_position_swapped_pairwise_grade(
                store_a,
                revision=rev_a,
                ab_packet=ab,
                ab_judgment=PairwiseJudgment("Better.", pref),
                ba_packet=ba,
                ba_judgment=PairwiseJudgment("Better.", swap),
            )
        verdict_a = summarize_rsi_transfer(
            store_a, revision=rev_a, min_pairs=2, min_net_wins=1
        )
        self.assertEqual(verdict_a.verdict, "INCONCLUSIVE")
        self.assertIn("incomplete-rubric-coverage", verdict_a.reasons)

        # Case B: All timed-out runs (cand_passes == 0, tool_trace present with 0 violations)
        # -> INCONCLUSIVE ("zero-candidate-judgment-passes"), never SUPPORTED.
        suite_b, (store_b, rev_b) = self._build_two_pair_suite()
        res_b = self._run_and_grade_suite(
            store_b,
            suite_b,
            rev_b,
            evidence_for_slot=lambda s: TrialTerminalEvidence(
                "timed-out",
                sha256(f"obs-{s.slot_id}".encode()).hexdigest(),
                decision_digest=None,
                tool_trace_digest=sha256(f"tr-{s.slot_id}".encode()).hexdigest(),
                service_effects_digest=None,
            ),
            grade_for_slot=lambda _s: TrialGrade(None, None, None, 0, None),
        )
        cand_b = {res_b["p1-cand"], res_b["p2-cand"]}
        for ab, ba in blind_pairwise_packets(store_b, revision=rev_b):
            pref = "first" if ab.first.result_id in cand_b else "second"
            swap = "second" if pref == "first" else "first"
            commit_position_swapped_pairwise_grade(
                store_b,
                revision=rev_b,
                ab_packet=ab,
                ab_judgment=PairwiseJudgment("Timed out slower.", pref),
                ba_packet=ba,
                ba_judgment=PairwiseJudgment("Timed out slower.", swap),
            )
        verdict_b = summarize_rsi_transfer(
            store_b, revision=rev_b, min_pairs=2, min_net_wins=1
        )
        self.assertEqual(verdict_b.verdict, "INCONCLUSIVE")
        self.assertIn("zero-candidate-judgment-passes", verdict_b.reasons)

        # Case C: Candidate authority violation stays UNSUPPORTED even when another slot is invalidated!
        suite_c, (store_c, rev_c) = self._build_two_pair_suite()
        self._run_and_grade_suite(
            store_c,
            suite_c,
            rev_c,
            grade_for_slot=lambda s: (
                TrialGrade(True, True, True, 1, True)
                if s.slot_id == "p1-cand"
                else TrialGrade(True, True, True, 0, True)
            ),
        )
        invalidate_trial_slot(
            store_c,
            revision=rev_c,
            slot_id="p2-inc",
            evidence_digest=sha256(b"unrelated-leak").hexdigest(),
        )
        # Invalidated pair is excluded from blind_pairwise_packets!
        remaining_pairs = blind_pairwise_packets(store_c, revision=rev_c)
        self.assertEqual(len(remaining_pairs), 1)
        verdict_c = summarize_rsi_transfer(
            store_c, revision=rev_c, min_pairs=2, min_net_wins=1
        )
        self.assertEqual(verdict_c.verdict, "UNSUPPORTED")
        self.assertIn("candidate-authority-violation", verdict_c.reasons)
        self.assertIn("contaminated-or-invalidated-slots", verdict_c.reasons)

        # Case D: Incumbent outperforms candidate -> UNSUPPORTED; same family with min_families=2 -> INCONCLUSIVE.
        suite_d, (store_d, rev_d) = self._build_two_pair_suite(same_family=True)
        res_d = self._run_and_grade_suite(store_d, suite_d, rev_d)
        inc_d = {res_d["p1-inc"], res_d["p2-inc"]}
        for ab, ba in blind_pairwise_packets(store_d, revision=rev_d):
            pref = "first" if ab.first.result_id in inc_d else "second"
            swap = "second" if pref == "first" else "first"
            commit_position_swapped_pairwise_grade(
                store_d,
                revision=rev_d,
                ab_packet=ab,
                ab_judgment=PairwiseJudgment("Incumbent stronger.", pref),
                ba_packet=ba,
                ba_judgment=PairwiseJudgment("Incumbent stronger.", swap),
            )
        verdict_d_fam = summarize_rsi_transfer(
            store_d, revision=rev_d, min_pairs=2, min_net_wins=1, min_families=2
        )
        self.assertEqual(verdict_d_fam.verdict, "INCONCLUSIVE")
        self.assertIn("insufficient-transfer-families", verdict_d_fam.reasons)
        verdict_d_inc = summarize_rsi_transfer(
            store_d, revision=rev_d, min_pairs=2, min_net_wins=1, min_families=1
        )
        self.assertEqual(verdict_d_inc.verdict, "UNSUPPORTED")
        self.assertIn("incumbent-outperformed-candidate", verdict_d_inc.reasons)

        # Case E: Pairwise commit on an invalidated pair is refused.
        suite_e, (store_e, rev_e) = self._build_two_pair_suite()
        self._run_and_grade_suite(store_e, suite_e, rev_e)
        ab_e, ba_e = blind_pairwise_packets(store_e, revision=rev_e)[0]
        invalidate_trial_slot(
            store_e,
            revision=rev_e,
            slot_id="p1-inc",
            evidence_digest=sha256(b"p1-leak").hexdigest(),
        )
        invalidate_trial_slot(
            store_e,
            revision=rev_e,
            slot_id="p2-inc",
            evidence_digest=sha256(b"p2-leak").hexdigest(),
        )
        with self.assertRaisesRegex(TrialRefused, "original valid blinded pair"):
            commit_position_swapped_pairwise_grade(
                store_e,
                revision=rev_e,
                ab_packet=ab_e,
                ab_judgment=PairwiseJudgment("Tie.", "tie"),
                ba_packet=ba_e,
                ba_judgment=PairwiseJudgment("Tie.", "tie"),
            )

    def test_pairwise_all_inconsistent_and_tie_combinations(self):
        inconsistent_cases = (
            ("first", "first"),
            ("second", "second"),
            ("first", "tie"),
            ("tie", "first"),
            ("second", "tie"),
            ("tie", "second"),
        )
        for ab_pref, ba_pref in inconsistent_cases:
            with self.subTest(ab=ab_pref, ba=ba_pref):
                suite, (store, rev) = self._build_two_pair_suite()
                self._run_and_grade_suite(store, suite, rev)
                ab, ba = blind_pairwise_packets(store, revision=rev)[0]
                res = commit_position_swapped_pairwise_grade(
                    store,
                    revision=rev,
                    ab_packet=ab,
                    ab_judgment=PairwiseJudgment("AB rationale.", ab_pref),
                    ba_packet=ba,
                    ba_judgment=PairwiseJudgment("BA rationale.", ba_pref),
                )
                self.assertFalse(res.consistent)
                self.assertEqual(res.outcome, "tie_noise")
                self.assertIsNone(res.winner_result_id)

        suite, (store, rev) = self._build_two_pair_suite()
        self._run_and_grade_suite(store, suite, rev)
        ab, ba = blind_pairwise_packets(store, revision=rev)[0]
        tie_res = commit_position_swapped_pairwise_grade(
            store,
            revision=rev,
            ab_packet=ab,
            ab_judgment=PairwiseJudgment("Equally strong.", "tie"),
            ba_packet=ba,
            ba_judgment=PairwiseJudgment("Still tied.", "tie"),
        )
        self.assertTrue(tie_res.consistent)
        self.assertEqual(tie_res.outcome, "tie")
        self.assertIsNone(tie_res.winner_result_id)

    def test_offline_store_rejects_injected_triggers_views_and_symlinked_wal(self):
        rev = seal_trial_suite(self.store, self.plan)
        with closing(sqlite3.connect(self.store)) as db:
            db.execute("CREATE VIEW sneaky_view AS SELECT 1")
            db.commit()
        with self.assertRaisesRegex(TrialRefused, "not a sealed trial registry"):
            trial_run_records(self.store, revision=rev)
        with closing(sqlite3.connect(self.store)) as db:
            db.execute("DROP VIEW sneaky_view")
            db.execute(
                """CREATE TRIGGER sneaky_trig AFTER INSERT ON trial_reservation
                   BEGIN SELECT 1; END"""
            )
            db.commit()
        with self.assertRaisesRegex(TrialRefused, "not a sealed trial registry"):
            self.reserve(rev)
        with closing(sqlite3.connect(self.store)) as db:
            db.execute("DROP TRIGGER sneaky_trig")
            db.commit()

        # Replace -wal with a symlink -> rejected before opening SQLite.
        wal_path = Path(f"{self.store}-wal")
        if wal_path.exists():
            wal_path.unlink()
        decoy = self.root / "decoy.wal"
        decoy.write_bytes(b"")
        wal_path.symlink_to(decoy)
        with self.assertRaisesRegex(TrialRefused, "WAL sidecar"):
            trial_run_records(self.store, revision=rev)
