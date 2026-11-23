import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
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
