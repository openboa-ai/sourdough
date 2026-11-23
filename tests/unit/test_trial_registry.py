import json
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
    TrialCeiling,
    TrialGrade,
    TrialInputs,
    TrialRefused,
    TrialSlot,
    TrialSuite,
    TrialTerminalEvidence,
    blind_trial_packet,
    commit_blind_grade,
    commit_trial_observation,
    invalidate_trial_slot,
    reconcile_paired_grades,
    reserve_trial_launch,
    seal_trial_suite,
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
