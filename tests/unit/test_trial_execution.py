import json
import subprocess
import sys
import unittest
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from sourdough.trial_execution import TrialWorkerInput, dispatch_registered_trial
from sourdough.trial_registry import (
    TrialCeiling,
    TrialInputs,
    TrialRefused,
    TrialSlot,
    TrialSuite,
    seal_trial_suite,
    trial_run_records,
)


class RegisteredTrialExecution(unittest.TestCase):
    def setUp(self):
        self.store = Path(self.enterContext(TemporaryDirectory())) / "trial.sqlite"
        self.fixture = b"company fixture"
        self.payload = TrialWorkerInput(
            b"current case",
            b"permitted work context",
            b'["read"]',
            TrialCeiling(1, 30, 1000),
        )
        self.runtime = Mock(profile="pinned-profile")
        self.runtime.can_isolate_and_enforce.return_value = True
        inputs = TrialInputs(
            *(
                sha256(material).hexdigest()
                for material in (
                    self.fixture,
                    self.payload.case_inputs,
                    self.payload.worker_context,
                    self.payload.tool_manifest,
                )
            ),
            self.runtime.profile,
        )
        self.slot = TrialSlot(
            "private-slot", "private-case", "family", inputs, self.payload.limits
        )
        self.revision = seal_trial_suite(
            self.store,
            TrialSuite(
                "private-suite", "judgment", "a" * 64, "b" * 64, "c" * 64, (self.slot,)
            ),
        )

    def dispatch(self, **changes):
        terms = {
            "revision": self.revision,
            "slot_id": self.slot.slot_id,
            "run_id": "original-id",
            "company_fixture": self.fixture,
            "worker_input": self.payload,
            "runtime": self.runtime,
        }
        return dispatch_registered_trial(self.store, **(terms | changes))

    def test_changed_material_profile_or_budget_refused_without_reserving(self):
        for change in (
            {"company_fixture": b"changed"},
            {"worker_input": replace(self.payload, case_inputs=b"new case")},
            {"worker_input": replace(self.payload, worker_context=b"new context")},
            {"worker_input": replace(self.payload, tool_manifest=b'["write"]')},
            {"worker_input": replace(self.payload, limits=TrialCeiling(2, 30, 1000))},
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(TrialRefused, "not registered"),
            ):
                self.dispatch(**change)
        self.runtime.profile = "other-profile"
        with self.assertRaisesRegex(TrialRefused, "not registered"):
            self.dispatch()
        self.runtime.run_trial.assert_not_called()
        self.assertEqual(
            trial_run_records(self.store, revision=self.revision)[0].state, "uncovered"
        )

    def test_preflight_refusal_cannot_start_or_consume_a_trial(self):
        with self.assertRaisesRegex(
            TrialRefused, "no qualified offline trial interface"
        ):
            self.dispatch(runtime=SimpleNamespace(profile=self.runtime.profile))
        self.runtime.can_isolate_and_enforce.return_value = False
        with self.assertRaisesRegex(TrialRefused, "isolation and all sealed limits"):
            self.dispatch()
        self.runtime.can_isolate_and_enforce.assert_called_once_with(
            company_fixture_digest=self.slot.inputs.company_fixture,
            worker_input=self.payload,
        )
        self.runtime.run_trial.assert_not_called()
        self.assertEqual(
            trial_run_records(self.store, revision=self.revision)[0].state, "uncovered"
        )

    def test_a_child_receives_only_allowed_material_after_one_durable_reservation(self):
        def worker(run_id, payload):
            record = trial_run_records(self.store, revision=self.revision)[0]
            self.assertEqual((record.run_id, record.state), (run_id, "incomplete"))
            material = asdict(payload)
            for name in ("case_inputs", "worker_context", "tool_manifest"):
                material[name] = material[name].decode()
            child = subprocess.run(
                [
                    sys.executable,
                    "-IB",
                    "-c",
                    "import json,sys; print(json.dumps(json.load(sys.stdin)))",
                ],
                input=json.dumps(material),
                text=True,
                capture_output=True,
                check=True,
                timeout=5,
            )
            self.assertEqual(
                json.loads(child.stdout),
                {
                    "case_inputs": "current case",
                    "worker_context": "permitted work context",
                    "tool_manifest": '["read"]',
                    "limits": {"turns": 1, "wall_seconds": 30, "tokens": 1000},
                },
            )
            raise RuntimeError("result not independently observed")

        self.runtime.run_trial.side_effect = worker
        with self.assertRaisesRegex(RuntimeError, "not independently observed"):
            self.dispatch()
        self.assertFalse(self.dispatch().launch_permitted)
        self.runtime.run_trial.assert_called_once()
        self.assertEqual(
            trial_run_records(self.store, revision=self.revision)[0].state, "incomplete"
        )

    def test_binding_change_during_preflight_cannot_start(self):
        def changed_binding(**_terms):
            self.runtime.profile = "drifted-profile"
            return True

        self.runtime.can_isolate_and_enforce.side_effect = changed_binding
        with self.assertRaisesRegex(TrialRefused, "binding changed"):
            self.dispatch()
        self.runtime.run_trial.assert_not_called()
        self.assertEqual(
            trial_run_records(self.store, revision=self.revision)[0].state, "uncovered"
        )
