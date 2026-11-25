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

from sourdough.company import FoundingTerms, found
from sourdough.trial_execution import (
    CompanyRuntimeOfflineAdapter,
    TrialWorkerInput,
    dispatch_registered_trial,
)
from sourdough.trial_registry import (
    TrialCeiling,
    TrialInputs,
    TrialRefused,
    TrialSlot,
    TrialSuite,
    seal_trial_suite,
    trial_run_records,
)


def _ok_case(_rt, _desk, _inp):
    return {"outcome": "completed", "decision": {"ok": 1}}


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

    def test_unsafe_run_id_and_missing_profile_refused_without_consuming_slot(self):
        for bad_run_id in ("../escape", "a/b", "a..b", ".hidden", ""):
            with (
                self.subTest(run_id=bad_run_id),
                self.assertRaisesRegex(TrialRefused, "safe non-traversal"),
            ):
                self.dispatch(run_id=bad_run_id)
        with self.assertRaisesRegex(
            TrialRefused, "no qualified offline trial interface"
        ):
            self.dispatch(
                runtime=SimpleNamespace(
                    can_isolate_and_enforce=lambda **_: True,
                    run_trial=lambda *_: None,
                )
            )
        self.runtime.run_trial.assert_not_called()
        self.assertEqual(
            trial_run_records(self.store, revision=self.revision)[0].state, "uncovered"
        )

    def test_adapter_refuses_evaluator_overlap_symlinks_and_bad_config(self):
        eval_dir = self.store.parent

        for bad_cfg in (
            {"max_turns": True},
            {"max_tokens": 0},
            {"max_wall_seconds": -5},
            {"profile": "   "},
        ):
            with self.assertRaisesRegex(
                TrialRefused, "Invalid CompanyRuntimeOfflineAdapter"
            ):
                CompanyRuntimeOfflineAdapter(
                    trial_root=eval_dir.parent / "other-trials",
                    evaluator_store=self.store,
                    profile=bad_cfg.get("profile", "p"),
                    execute_case=_ok_case,
                    max_turns=bad_cfg.get("max_turns", 4),
                    max_wall_seconds=bad_cfg.get("max_wall_seconds", 60),
                    max_tokens=bad_cfg.get("max_tokens", 10_000),
                )

        # Subdirectory of evaluator store dir, same dir, parent dir, dangling symlink,
        # and an existing operating company dir must ALL fail isolation.
        separate_root = Path(self.enterContext(TemporaryDirectory()))
        dangling = separate_root / "dangling-link"
        dangling.symlink_to(separate_root / "does-not-exist")
        operating_dir = separate_root / "operating-co"
        operating_dir.mkdir(mode=0o700)
        found(
            operating_dir / "company.sqlite",
            FoundingTerms("op", "charter", "manage", "prof"),
        )
        for unsafe_root in (
            eval_dir,
            eval_dir / "nested-trials",
            eval_dir.parent,
            dangling,
            operating_dir,
        ):
            with self.subTest(trial_root=unsafe_root):
                adapter = CompanyRuntimeOfflineAdapter(
                    trial_root=unsafe_root,
                    evaluator_store=self.store,
                    profile="pinned-profile",
                    execute_case=_ok_case,
                )
                self.assertFalse(
                    adapter.can_isolate_and_enforce(
                        company_fixture_digest=self.slot.inputs.company_fixture,
                        worker_input=self.payload,
                    )
                )
                with self.assertRaisesRegex(TrialRefused, "not isolated"):
                    adapter.run_trial("safe-run", self.payload)

        # Non-hex64 digest is rejected in can_isolate_and_enforce.
        clean_adapter = CompanyRuntimeOfflineAdapter(
            trial_root=separate_root / "clean-trials",
            evaluator_store=self.store,
            profile="pinned-profile",
            execute_case=_ok_case,
        )
        self.assertFalse(
            clean_adapter.can_isolate_and_enforce(
                company_fixture_digest="not-hex" + "0" * 57,
                worker_input=self.payload,
            )
        )

    def test_adapter_enforces_ceilings_and_fresh_run_directories(self):
        trials_root = Path(self.enterContext(TemporaryDirectory())) / "trials"

        # 1. TimeoutError -> outcome="timed-out", decision_digest=None.
        def raise_timeout(_rt, _desk, _inp):
            raise TimeoutError("wall clock expired")

        timeout_adapter = CompanyRuntimeOfflineAdapter(
            trial_root=trials_root,
            evaluator_store=self.store,
            profile="pinned-profile",
            execute_case=raise_timeout,
        )
        timeout_adapter.run_trial("run-timeout", self.payload)
        self.assertEqual(timeout_adapter.evidences["run-timeout"].outcome, "timed-out")
        self.assertIsNone(timeout_adapter.evidences["run-timeout"].decision_digest)

        # 2. Duplicate run_id directory refused fail-closed.
        with self.assertRaisesRegex(TrialRefused, "fresh trial directory"):
            timeout_adapter.run_trial("run-timeout", self.payload)

        # 3. Reported wall_seconds > ceiling -> timed-out; turns/tokens > ceiling -> failed.
        wall_adapter = CompanyRuntimeOfflineAdapter(
            trial_root=trials_root,
            evaluator_store=self.store,
            profile="pinned-profile",
            execute_case=lambda _r, _d, _i: {
                "outcome": "completed",
                "decision": {"ok": 1},
                "usage": {"wall_seconds": 31},
            },
        )
        wall_adapter.run_trial("run-wall-over", self.payload)
        self.assertEqual(wall_adapter.evidences["run-wall-over"].outcome, "timed-out")
        self.assertIsNone(wall_adapter.evidences["run-wall-over"].decision_digest)

        token_adapter = CompanyRuntimeOfflineAdapter(
            trial_root=trials_root,
            evaluator_store=self.store,
            profile="pinned-profile",
            execute_case=lambda _r, _d, _i: {
                "outcome": "completed",
                "decision": {"ok": 1},
                "usage": {"turns": 1, "tokens": 1001},
            },
        )
        token_adapter.run_trial("run-tokens-over", self.payload)
        self.assertEqual(token_adapter.evidences["run-tokens-over"].outcome, "failed")
        self.assertIsNone(token_adapter.evidences["run-tokens-over"].decision_digest)

        # 4. NaN usage, non-dict result, and over-cap limits rejected fail-closed.
        nan_adapter = CompanyRuntimeOfflineAdapter(
            trial_root=trials_root,
            evaluator_store=self.store,
            profile="pinned-profile",
            execute_case=lambda _r, _d, _i: {
                "outcome": "completed",
                "decision": {"ok": 1},
                "usage": {"wall_seconds": float("nan")},
            },
        )
        with self.assertRaisesRegex(TrialRefused, "finite non-negative"):
            nan_adapter.run_trial("run-nan", self.payload)

        bad_type_adapter = CompanyRuntimeOfflineAdapter(
            trial_root=trials_root,
            evaluator_store=self.store,
            profile="pinned-profile",
            execute_case=lambda _r, _d, _i: ["not-a-dict"],  # type: ignore[return-value]
        )
        with self.assertRaisesRegex(TrialRefused, "JSON-compatible dict"):
            bad_type_adapter.run_trial("run-badtype", self.payload)

        capped_adapter = CompanyRuntimeOfflineAdapter(
            trial_root=trials_root,
            evaluator_store=self.store,
            profile="pinned-profile",
            execute_case=_ok_case,
            max_turns=1,
        )
        with self.assertRaisesRegex(TrialRefused, "exceed adapter enforcement caps"):
            capped_adapter.run_trial(
                "run-overcap",
                replace(self.payload, limits=TrialCeiling(2, 30, 1000)),
            )
