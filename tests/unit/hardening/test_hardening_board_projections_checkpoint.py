"""Board projections redaction, evals projection, checkpoint audit, and CLI run-company hardening tests."""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.agent_desk import (
    save_draft,
)
from sourdough.agent_runtime import (
    PROVIDER_OPENAI,
    AdapterManifest,
    AgentRuntimeAdapter,
    AgentRuntimeRefused,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
)
from sourdough.agent_runtime.registry import AgentAdapterRegistry
from sourdough.memory import record_episode
from sourdough.method_learning import (
    activate_method_delta,
    distill_method_delta,
    qualify_method_delta,
)
from sourdough.runtime import (
    create_work_item,
)
from sourdough.runtime.work_kernel import (
    DecisionEvidence,
    record_work_decision,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer

from ._fixtures import (
    HardeningTestCase,
    _hire_active_agent,
    _open_work_run_claim,
)


class TestHardeningBoardProjectionsCheckpoint(HardeningTestCase):
    def test_pr191_evals_projection_preserves_sealed_analysis_plan_criteria(
        self,
    ) -> None:
        """PR #191 P2: read_system_evals_projection must never weaken sealed analysis criteria."""
        from sourdough.ipc.projections import read_system_evals_projection
        from sourdough.trial_registry import (
            PairwiseJudgment,
            TrialCeiling,
            TrialGrade,
            TrialInputs,
            TrialSlot,
            TrialSuite,
            TrialTerminalEvidence,
            blind_pairwise_packets,
            blind_trial_packet,
            commit_blind_grade,
            commit_position_swapped_pairwise_grade,
            commit_trial_observation,
            reserve_trial_launch,
            seal_trial_suite,
            summarize_rsi_transfer,
        )

        def _populate_three_family_wins(
            eval_path: Path, *, pass_plan_at_seal: bool
        ) -> tuple[str, dict[str, object]]:
            plan: dict[str, object] = {
                "min_pairs": 3,
                "min_net_wins": 1,
                "min_families": 3,
                "alpha": 0.05,
                "min_worthwhile_margin": 0.0,
                "bootstrap_resamples": 1000,
            }
            plan_digest = sha256(
                json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            slots = []
            for idx in (1, 2, 3):
                for arm, cp in (("incumbent", f"s{idx}-c"), ("candidate", f"s{idx}-i")):
                    sid = f"s{idx}-i" if arm == "incumbent" else f"s{idx}-c"
                    slots.append(
                        TrialSlot(
                            slot_id=sid,
                            case_id=f"c{idx}",
                            family_id=f"f{idx}",
                            inputs=TrialInputs(
                                sha256(b"fix").hexdigest(),
                                sha256(f"case-{idx}".encode()).hexdigest(),
                                sha256(arm.encode()).hexdigest(),
                                sha256(b"tools").hexdigest(),
                                "offline-v1",
                            ),
                            limits=TrialCeiling(2, 10, 1000),
                            arm=arm,
                            counterpart_id=cp,
                        )
                    )
            suite = TrialSuite(
                suite_id="strict-3fam-suite",
                purpose="recursive",
                rubric_digest=sha256(b"rubric").hexdigest(),
                evaluator_digest=sha256(b"eval").hexdigest(),
                analysis_plan_digest=plan_digest,
                slots=tuple(slots),
            )
            rev = seal_trial_suite(
                eval_path,
                suite,
                analysis_plan=plan if pass_plan_at_seal else None,
            )
            cand_results: set[str] = set()
            for idx, slot in enumerate(suite.slots, start=1):
                rid = f"r-{idx}"
                reserve_trial_launch(
                    eval_path,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=rid,
                    inputs=slot.inputs,
                    limits=slot.limits,
                )
                rec = commit_trial_observation(
                    eval_path,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=rid,
                    evidence=TrialTerminalEvidence(
                        "completed",
                        sha256(f"o-{slot.slot_id}".encode()).hexdigest(),
                        sha256(f"d-{slot.slot_id}".encode()).hexdigest(),
                        sha256(b"t").hexdigest(),
                        sha256(b"s").hexdigest(),
                    ),
                )
                assert rec.result_id is not None
                if slot.arm == "candidate":
                    cand_results.add(rec.result_id)
                pkt = blind_trial_packet(
                    eval_path, revision=rev, result_id=rec.result_id
                )
                commit_blind_grade(
                    eval_path,
                    revision=rev,
                    packet=pkt,
                    grade=TrialGrade(True, True, True, 0, True),
                )
            for ab, ba in blind_pairwise_packets(eval_path, revision=rev):
                ab_pref = "first" if ab.first.result_id in cand_results else "second"
                ba_pref = "first" if ba.first.result_id in cand_results else "second"
                commit_position_swapped_pairwise_grade(
                    eval_path,
                    revision=rev,
                    ab_packet=ab,
                    ab_judgment=PairwiseJudgment("Candidate won", ab_pref),
                    ba_packet=ba,
                    ba_judgment=PairwiseJudgment("Candidate won", ba_pref),
                )
            return rev, plan

        # Case 1: Sealed with explicit plan (3 families, perm_p = 0.125 > alpha = 0.05 -> INCONCLUSIVE)
        eval_sealed = self.root / "eval_sealed.sqlite"
        rev1, plan1 = _populate_three_family_wins(eval_sealed, pass_plan_at_seal=True)
        direct = summarize_rsi_transfer(eval_sealed, revision=rev1, analysis_plan=plan1)
        self.assertEqual(direct.verdict, "INCONCLUSIVE")
        self.assertAlmostEqual(direct.perm_p, 0.125)
        proj1, _ = read_system_evals_projection(
            self.store, self.company_id, evaluator_store=eval_sealed
        )
        self.assertEqual(len(proj1["rsi_transfer_summaries"]), 1)
        self.assertEqual(proj1["rsi_transfer_summaries"][0]["verdict"], "INCONCLUSIVE")
        self.assertIn(
            "permutation-p-above-alpha", proj1["rsi_transfer_summaries"][0]["reasons"]
        )

        # Case 2: Sealed without embedded plan -> fail-closed UNAVAILABLE (never weaker SUPPORTED!)
        eval_opaque = self.root / "eval_opaque.sqlite"
        _rev2, plan2 = _populate_three_family_wins(eval_opaque, pass_plan_at_seal=False)
        proj2, _ = read_system_evals_projection(
            self.store, self.company_id, evaluator_store=eval_opaque
        )
        self.assertEqual(len(proj2["rsi_transfer_summaries"]), 1)
        self.assertEqual(proj2["rsi_transfer_summaries"][0]["verdict"], "UNAVAILABLE")
        self.assertIn(
            "sealed-analysis-plan-unavailable",
            proj2["rsi_transfer_summaries"][0]["reasons"],
        )
        # Passing the verified plan explicitly restores exact INCONCLUSIVE verdict
        proj2_with_plan, _ = read_system_evals_projection(
            self.store,
            self.company_id,
            evaluator_store=eval_opaque,
            analysis_plan=plan2,
        )
        self.assertEqual(
            proj2_with_plan["rsi_transfer_summaries"][0]["verdict"], "INCONCLUSIVE"
        )

    def test_pr191_r3_board_projection_redacts_private_draft_identifiers(self) -> None:
        """PR #191 R3 P2: Board agent-detail projection must never disclose private draft names."""
        from sourdough.ipc.projections import read_agent_detail_projection

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, run, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
            draft = save_draft(
                w,
                claim,
                claim.purpose,
                name="secret-acquisition-plan",
                content="Confidential private draft content",
                expected_revision=None,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=claim.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="Verified internal note",
                    evidence=(
                        DecisionEvidence(
                            "private_draft",
                            f"{self.founder_id}/secret-acquisition-plan",
                            draft.revision,
                            900,
                            800,
                        ),
                    ),
                    decision_at_ns=1_500,
                )
            detail, _ = read_agent_detail_projection(
                self.store, self.company_id, self.founder_id
            )
            serialized = json.dumps(detail)
            self.assertNotIn("secret-acquisition-plan", serialized)
            refs = detail["tabs"]["work_decisions"][0]["evidence_refs"]
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["kind"], "private_draft")
            self.assertTrue(refs[0]["resource_id"].startswith("private_draft:"))
            self.assertEqual(refs[0]["revision"], draft.revision)

    def test_pr191_r5_detail_projection_redacts_episode_summary_and_one_agent_method_body(
        self,
    ) -> None:
        """PR #191 R5 Security P2: Board agent-detail projection must redact episode summary & one_agent method bodies."""
        from sourdough.ipc.projections import read_agent_detail_projection

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, run, claim = _open_work_run_claim(
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
                claim,
                claim.purpose,
                name="secret-alpha",
                content="TOP_SECRET_ALPHA_DRAFT",
                expected_revision=None,
            )
            priv_delta = distill_method_delta(
                w,
                claim,
                method_id="priv-method-r5",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="one_agent",
                audience_target_id=self.founder_id,
                trigger_spec={
                    "duty_kinds": ["work.discovery_review"],
                    "negative_triggers": ["PRIVATE_TRIGGER_HINT_66"],
                },
                body="PRIVATE_ONE_AGENT_SECRET_77",
                uncertainty_note="PRIVATE_UNCERTAINTY_88",
                falsification_criteria="PRIVATE_FALSIFICATION_99",
                source_refs=("private_draft:secret-alpha", f"work_run:{run.run_id}"),
                now_ns=1_100,
            )
            qualify_method_delta(
                w,
                priv_delta.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_150,
            )
            activate_method_delta(
                w,
                priv_delta.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=1_200,
            )
            pub_delta = distill_method_delta(
                w,
                claim,
                method_id="shared-method-r5",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="company",
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="COMPANY_SHARED_METHOD_OK",
                falsification_criteria="Fails if shared rule breaks.",
                source_refs=(f"work_run:{run.run_id}",),
                now_ns=1_250,
            )
            qualify_method_delta(
                w,
                pub_delta.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_300,
            )
            activate_method_delta(
                w,
                pub_delta.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=1_350,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                record_episode(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    duty_kind="work.discovery_review",
                    context_digest=sha256(b"ctx-r5").hexdigest(),
                    outcome_status="completed",
                    summary_text="SECRET_EPISODE_DRAFT_TEXT_55",
                    recorded_at_ns=1_400,
                )

        detail, _wm = read_agent_detail_projection(
            self.store, self.company_id, self.founder_id
        )
        serialized = json.dumps(detail, sort_keys=True)
        for leaked in (
            "PRIVATE_ONE_AGENT_SECRET_77",
            "PRIVATE_UNCERTAINTY_88",
            "PRIVATE_FALSIFICATION_99",
            "PRIVATE_TRIGGER_HINT_66",
            "priv-method-r5",
            "secret-alpha",
            "TOP_SECRET_ALPHA_DRAFT",
            "SECRET_EPISODE_DRAFT_TEXT_55",
        ):
            self.assertNotIn(leaked, serialized)

        priv_list = [
            m for m in detail["active_methods"] if m["audience_scope"] == "one_agent"
        ]
        self.assertEqual(len(priv_list), 1)
        priv_proj = priv_list[0]
        self.assertTrue(priv_proj["method_id"].startswith("one_agent_method:"))
        self.assertTrue(priv_proj["method_id_redacted"])
        self.assertIsNone(priv_proj["body_text"])
        self.assertTrue(priv_proj["body_redacted"])
        self.assertEqual(
            priv_proj["body_digest"],
            sha256(b"PRIVATE_ONE_AGENT_SECRET_77").hexdigest(),
        )
        self.assertIsNone(priv_proj["uncertainty_note"])
        self.assertIsNone(priv_proj["falsification_criteria"])
        self.assertEqual(priv_proj["trigger_spec"], {"redacted": True})

        by_method = {m["method_id"]: m for m in detail["active_methods"]}
        pub_proj = by_method["shared-method-r5"]
        self.assertEqual(pub_proj["body_text"], "COMPANY_SHARED_METHOD_OK")

        ep = detail["tabs"]["agent_episodes"][0]
        self.assertEqual(ep["summary_text"], "completed:work.discovery_review")
        self.assertTrue(ep["summary_redacted"])
        self.assertEqual(
            ep["summary_digest"],
            sha256(b"SECRET_EPISODE_DRAFT_TEXT_55").hexdigest(),
        )

    def test_pr191_r9_board_detail_projection_redacts_work_decision_rationale(
        self,
    ) -> None:
        """PR #191 R9 Security P2: Board agent-detail projection must redact work_decision rationales."""
        from sourdough.ipc.projections import read_agent_detail_projection

        canary_rationale = "PRIVATE_DRAFT_CANARY_RATIONALE_9347"
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, run, _claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
                record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale=canary_rationale,
                    evidence=(),
                    decision_at_ns=1_500,
                )
        detail, _wm = read_agent_detail_projection(
            self.store, self.company_id, self.founder_id
        )
        serialized = json.dumps(detail, sort_keys=True)
        self.assertNotIn(canary_rationale, serialized)
        dec_proj = detail["tabs"]["work_decisions"][0]
        self.assertIsNone(dec_proj["rationale"])
        self.assertTrue(dec_proj["rationale_redacted"])
        self.assertEqual(
            dec_proj["rationale_digest"],
            sha256(canary_rationale.encode("utf-8")).hexdigest(),
        )

    def test_pr191_r9_cli_run_company_once_waits_past_11s_for_valid_shift(
        self,
    ) -> None:
        """PR #191 R9 P2: run-company --once must allow permitted shift duration (>120s) without premature timeout."""
        from unittest.mock import patch

        from sourdough.runtime.resident import ResidentHealth

        from sourdough import cli

        recorded_timeouts: list[float] = []
        recorded_preds: list[Any] = []

        class _FakeService:
            def __init__(self, *args, **kwargs) -> None:
                self.info = type(
                    "_Info",
                    (),
                    {
                        "__dataclass_fields__": {"socket_path": None},
                        "socket_path": "/tmp/fake.sock",
                    },
                )()
                self.socket_path = Path("/tmp/fake.sock")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def wait_for(self, predicate, *, timeout_seconds: float = 10.0):
                recorded_timeouts.append(timeout_seconds)
                recorded_preds.append(predicate)
                return ResidentHealth(
                    running=True,
                    ticks_completed=1,
                    runs_executed=1,
                    triggers_enqueued=0,
                    last_tick_at_ns=1_000,
                    last_error=None,
                )

        with (
            patch.object(cli, "ResidentCompanyService", _FakeService),
            patch.object(cli, "asdict", lambda _obj: {"socket_path": "/tmp/fake.sock"}),
            patch.object(
                sys,
                "argv",
                [
                    "sourdough",
                    "run-company",
                    str(self.store),
                    "--company-id",
                    self.company_id,
                    "--once",
                ],
            ),
        ):
            rc = cli.main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(recorded_timeouts), 1)
        self.assertGreaterEqual(recorded_timeouts[0], 960.0)
        pred = recorded_preds[0]
        self.assertFalse(pred(ResidentHealth(True, 0, 0, 0, None, None)))
        self.assertTrue(pred(ResidentHealth(True, 1, 1, 0, 1_000, None)))
        self.assertTrue(pred(ResidentHealth(False, 0, 0, 0, None, "stopped")))

    def test_pr191_r18_security_checkpoint_audit_omits_private_desk_paths_and_verifies_digest(
        self,
    ) -> None:
        """PR #191 R18 Security P2: company.checkpoint_cut audit_event must not persist private Desk paths in company.sqlite."""
        from sourdough.company_checkpoint import (
            checkpoint_company,
            inspect_company_checkpoint,
        )

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, f_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            save_draft(
                w,
                f_claim,
                f_claim.purpose,
                name="secret-acquisition-target",
                content="Private target details.",
                expected_revision=None,
            )

        archive_path = self.root / "r18-checkpoint.zip"
        manifest = checkpoint_company(self.store, self.company_id, archive_path)
        # Full path manifest remains inside the owner-restricted (0600) archive
        inspected = inspect_company_checkpoint(archive_path, self.company_id)
        self.assertEqual(inspected.desk_digest, manifest.desk_digest)
        self.assertTrue(
            any("secret-acquisition-target" in p for p in inspected.desk_files)
        )
        # Shared company.sqlite audit_event MUST store only digests + count, NEVER private Desk paths!
        with closing(open_reader(self.store)) as ro_db, read_tx(ro_db):
            cut_rows = ro_db.execute(
                """SELECT payload_json FROM audit_event
                   WHERE company_id = ? AND event_type = 'company.checkpoint_cut'""",
                (self.company_id,),
            ).fetchall()
        self.assertEqual(len(cut_rows), 1)
        cut_payload = json.loads(str(cut_rows[0][0]))
        self.assertNotIn("desk_files", cut_payload)
        self.assertIn("desk_files_digest", cut_payload)
        self.assertEqual(cut_payload["desk_file_count"], len(manifest.desk_files))
        self.assertNotIn("secret-acquisition-target", str(cut_rows[0][0]))

    def test_pr191_r22_run_company_once_returns_nonzero_on_failed_tick(self) -> None:
        """PR #191 R22 Code P2: run-company --once propagates scheduler tick failure as non-zero exit status."""
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock

        from sourdough import cli

        now_ns = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.assessment_review",
                    source_kind="manual",
                    source_id="r22-cli-fail",
                    now_ns=now_ns,
                )

        class _FailingAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                raise AgentRuntimeRefused("Synthetic adapter failure for --once test")

        areg = AgentAdapterRegistry()
        areg.register(_FailingAdapter())
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with (
            TemporaryDirectory(dir="/tmp", prefix="sd-r22-") as sock_tmp,
            mock.patch.object(cli, "build_default_adapter_registry", return_value=areg),
            mock.patch.object(
                sys,
                "argv",
                [
                    "sourdough",
                    "run-company",
                    str(self.store),
                    "--company-id",
                    self.company_id,
                    "--socket-dir",
                    sock_tmp,
                    "--once",
                ],
            ),
            redirect_stdout(stdout_buf),
            redirect_stderr(stderr_buf),
        ):
            rc = cli.main()
        self.assertNotEqual(rc, 0)
        self.assertIn("Company single-tick failed", stderr_buf.getvalue())

    def test_p0_agent_detail_redacts_one_agent_ids_in_run_outcome_trace_proposals_and_patterns(
        self,
    ) -> None:
        """PR-1 P0-A: read_agent_detail_projection opaque-maps one_agent IDs in run outcomes."""
        from sourdough.ipc.projections import read_agent_detail_projection
        from sourdough.method_learning import (
            distill_method_delta,
            opaque_one_agent_delta_id,
            opaque_one_agent_method_id,
        )
        from sourdough.runtime.work_kernel import complete_work_run

        secret_mid = "private-secret-alpha-method-canary"
        shared_mid = "company-shared-checklist-method"
        now_ns = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, run, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    now_ns=now_ns,
                )
                priv_delta = distill_method_delta(
                    db,
                    claim,
                    delta_kind="ADD",
                    item_kind="work_method",
                    method_id=secret_mid,
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body="Private one_agent procedure body.",
                    audience_scope="one_agent",
                    audience_target_id=self.founder_id,
                    source_refs=[f"work_run:{run.run_id}"],
                    now_ns=now_ns,
                )
                pub_delta = distill_method_delta(
                    db,
                    claim,
                    delta_kind="ADD",
                    item_kind="work_method",
                    method_id=shared_mid,
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body="Shared company procedure body.",
                    audience_scope="company",
                    source_refs=[f"work_run:{run.run_id}"],
                    now_ns=now_ns,
                )
                complete_work_run(
                    db,
                    run.run_id,
                    run.fencing_token,
                    outcome={
                        "causal_method_trace": {
                            "available": [secret_mid, shared_mid],
                            "retrieved": [secret_mid],
                            "invoked": [f"{secret_mid}:{priv_delta.revision_sha256}"],
                        },
                        "proposals": [
                            {
                                "delta_id": priv_delta.delta_id,
                                "method_id": secret_mid,
                                "revision_sha256": priv_delta.revision_sha256,
                                "delta_kind": "ADD",
                                "status": "draft",
                            },
                            {
                                "delta_id": pub_delta.delta_id,
                                "method_id": shared_mid,
                                "revision_sha256": pub_delta.revision_sha256,
                                "delta_kind": "ADD",
                                "status": "draft",
                            },
                        ],
                        "patterns": [
                            {
                                "kind": "stale_or_contradicted_skill",
                                "method_id": secret_mid,
                                "delta_kind": "PATCH",
                            },
                            {
                                "kind": "recurring_success_workflow",
                                "method_id": shared_mid,
                            },
                        ],
                    },
                    now_ns=now_ns + 10,
                )

        detail, _ = read_agent_detail_projection(
            self.store, self.company_id, self.founder_id
        )
        assert detail is not None
        serialized = json.dumps(detail)
        self.assertNotIn(secret_mid, serialized)
        self.assertIn(shared_mid, serialized)

        run_row = next(
            r for r in detail["tabs"]["work_activity"] if r["run_id"] == run.run_id
        )
        outcome = run_row["outcome"]
        expected_opaque_mid = opaque_one_agent_method_id(secret_mid)
        expected_opaque_did = opaque_one_agent_delta_id(
            secret_mid, priv_delta.revision_sha256
        )
        self.assertEqual(
            outcome["causal_method_trace"]["available"],
            [expected_opaque_mid, shared_mid],
        )
        self.assertEqual(
            outcome["causal_method_trace"]["retrieved"], [expected_opaque_mid]
        )
        self.assertEqual(
            outcome["causal_method_trace"]["invoked"], [expected_opaque_did]
        )
        self.assertEqual(outcome["proposals"][0]["method_id"], expected_opaque_mid)
        self.assertEqual(outcome["proposals"][0]["delta_id"], expected_opaque_did)
        self.assertTrue(outcome["proposals"][0]["method_id_redacted"])
        self.assertEqual(outcome["proposals"][1]["method_id"], shared_mid)
        self.assertEqual(outcome["patterns"][0]["method_id"], expected_opaque_mid)
        self.assertTrue(outcome["patterns"][0]["method_id_redacted"])
        self.assertEqual(outcome["patterns"][1]["method_id"], shared_mid)

    def test_p0_events_stream_redacts_one_agent_method_entity_ids(self) -> None:
        """PR-1 P0-A: /v1/events redacts one_agent method/delta entity_ids identically to system-audit."""
        from sourdough.ipc.client import ControlPlaneClient
        from sourdough.ipc.projections import read_system_audit_projection
        from sourdough.ipc.server import CompanyDaemon
        from sourdough.method_learning import (
            distill_method_delta,
            opaque_one_agent_delta_id,
        )

        secret_mid = "private-event-stream-secret-method"
        shared_mid = "public-event-stream-company-method"
        now_ns = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, run, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    now_ns=now_ns,
                )
                priv_delta = distill_method_delta(
                    db,
                    claim,
                    delta_kind="ADD",
                    item_kind="work_method",
                    method_id=secret_mid,
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body="One-agent event body.",
                    audience_scope="one_agent",
                    audience_target_id=self.founder_id,
                    source_refs=[f"work_run:{run.run_id}"],
                    now_ns=now_ns,
                )
                pub_delta = distill_method_delta(
                    db,
                    claim,
                    delta_kind="ADD",
                    item_kind="work_method",
                    method_id=shared_mid,
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body="Company event body.",
                    audience_scope="company",
                    source_refs=[f"work_run:{run.run_id}"],
                    now_ns=now_ns + 5,
                )

        audit_proj, _ = read_system_audit_projection(self.store, self.company_id)
        expected_opaque = opaque_one_agent_delta_id(
            secret_mid, priv_delta.revision_sha256
        )
        with TemporaryDirectory(dir="/tmp", prefix="sd-p0-ev-") as sock_tmp:
            with CompanyDaemon(
                self.store,
                self.company_id,
                socket_dir=Path(sock_tmp),
            ) as daemon:
                client = ControlPlaneClient(daemon.socket_path)
                events = client.events(after_seq=0, limit=100, wait_ms=0)

        events_json = json.dumps(events)
        self.assertNotIn(secret_mid, events_json)
        self.assertIn(expected_opaque, events_json)
        self.assertIn(pub_delta.delta_id, events_json)

        audit_ids = {
            e["event_seq"]: e["entity_id"]
            for e in audit_proj["events"]
            if e["entity_type"] == "method_delta"
        }
        stream_ids = {
            e["event_seq"]: e["entity_id"]
            for e in events
            if e["entity_type"] == "method_delta"
        }
        self.assertEqual(stream_ids, audit_ids)

    def test_p0_handoff_five_states_projected_in_agents_graph_and_agent_detail(
        self,
    ) -> None:
        """PR-1 P0-A: agents-graph and agent-detail project all 5 work_handoff states accurately."""
        from sourdough.company_assets import (
            Selection,
            assign_project,
            publish_selection,
        )
        from sourdough.ipc.projections import (
            read_agent_detail_projection,
            read_agents_graph_projection,
        )
        from sourdough.work_handoff import (
            HandoffOffer,
            accept_handoff,
            decline_handoff,
            expire_handoffs,
            offer_handoff,
            withdraw_handoff,
        )

        now_sec = time_ns() // 1_000_000_000
        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, peer_grant = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="agent-handoff-peer",
                profile_id=self.profile.profile_id(),
                scope="assessment",
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, sender_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    purpose="manage-company",
                )
                _, _, recip_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=peer_id,
                    grant_id=peer_grant,
                    grant_scope="assessment",
                    purpose="assessment",
                )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-handoff-states",
                (self.founder_id, peer_id),
                publishers=(self.founder_id,),
            )
            draft = save_draft(
                w,
                sender_claim,
                sender_claim.purpose,
                name="handoff-draft",
                content="Shared handoff package content",
                expected_revision=None,
            )
            pub = publish_selection(
                w,
                sender_claim,
                sender_claim.purpose,
                Selection(
                    request_id="pub-handoff-states",
                    draft_name=draft.name,
                    draft_revision=draft.revision,
                    project_id="proj-handoff-states",
                    project_revision=proj_rev,
                    audience=(self.founder_id, peer_id),
                    start=0,
                    end=len(draft.content),
                ),
            )
            offer_ids: dict[str, str] = {}
            for label, ttl in (
                ("offered", 3600),
                ("accepted", 3600),
                ("declined", 3600),
                ("withdrawn", 3600),
                ("expired", 10),
            ):
                offer_ids[label] = offer_handoff(
                    w,
                    sender_claim,
                    HandoffOffer(
                        request_id=f"req-{label}",
                        recipient_id=peer_id,
                        asset_id=pub.asset_id,
                        expires_at=now_sec + ttl,
                        expected_result=f"Expect {label} outcome",
                    ),
                    now=now_sec,
                )
            withdraw_handoff(
                w,
                sender_claim,
                offer_ids["withdrawn"],
                now=now_sec + 1,
            )
            decline_handoff(
                w,
                recip_claim,
                "assessment",
                offer_ids["declined"],
                now=now_sec + 2,
                reason="capacity",
            )
            accept_handoff(
                w,
                recip_claim,
                "assessment",
                offer_ids["accepted"],
                now=now_sec + 3,
            )
            expired = expire_handoffs(w, now=now_sec + 20)
            self.assertIn(offer_ids["expired"], expired)

        graph, _ = read_agents_graph_projection(self.store, self.company_id)
        graph_handoffs = {
            e["edge_id"].removeprefix("handoff-"): e
            for e in graph["edges"]
            if e["kind"] == "handoff"
        }
        for label, oid in offer_ids.items():
            self.assertIn(oid, graph_handoffs)
            self.assertEqual(graph_handoffs[oid]["state"], label)
            self.assertEqual(graph_handoffs[oid]["active"], label == "offered")

        detail, _ = read_agent_detail_projection(
            self.store, self.company_id, self.founder_id
        )
        assert detail is not None
        detail_handoffs = {h["offer_id"]: h for h in detail["tabs"]["handoffs"]}
        for label, oid in offer_ids.items():
            row = detail_handoffs[oid]
            self.assertEqual(row["state"], label)
            self.assertEqual(row["accepted"], label == "accepted")
            self.assertIsInstance(row["expires_at"], int)
            if label == "accepted":
                self.assertIsNotNone(row["successor_work_item_id"])
            else:
                self.assertIsNone(row["successor_work_item_id"])

    def test_p0_private_draft_counting_runs_outside_sqlite_read_tx(self) -> None:
        """PR-1 P0-A: filesystem private-draft counting runs only after SQLite read_tx closes."""
        from contextlib import contextmanager
        from unittest import mock

        from sourdough.ipc import projections as proj_mod

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    purpose="manage-company",
                )
            save_draft(
                w,
                claim,
                claim.purpose,
                name="private-note-1",
                content="Confidential draft #1",
                expected_revision=None,
            )
            save_draft(
                w,
                claim,
                claim.purpose,
                name="private-note-2",
                content="Confidential draft #2",
                expected_revision=None,
            )

        active_tx_depth = 0
        observed_depths: list[int] = []
        orig_read_tx = proj_mod.read_tx
        orig_count = proj_mod._count_private_drafts

        @contextmanager
        def _tracked_read_tx(conn: Any):
            nonlocal active_tx_depth
            active_tx_depth += 1
            try:
                with orig_read_tx(conn):
                    yield
            finally:
                active_tx_depth -= 1

        def _tracked_count(store_path: Path, aid: str) -> int:
            observed_depths.append(active_tx_depth)
            return orig_count(store_path, aid)

        with (
            mock.patch.object(proj_mod, "read_tx", _tracked_read_tx),
            mock.patch.object(proj_mod, "_count_private_drafts", _tracked_count),
        ):
            graph, _ = proj_mod.read_agents_graph_projection(
                self.store, self.company_id
            )
            detail, _ = proj_mod.read_agent_detail_projection(
                self.store, self.company_id, self.founder_id
            )
            org, _ = proj_mod.read_org_projection(self.store, self.company_id)

        self.assertGreaterEqual(len(observed_depths), 3)
        self.assertTrue(
            all(d == 0 for d in observed_depths),
            f"_count_private_drafts called inside active read_tx: {observed_depths}",
        )
        assert detail is not None
        self.assertEqual(detail["desk_workspace"]["private_draft_count"], 2)
        founder_node = next(
            n for n in graph["nodes"] if n["agent_id"] == self.founder_id
        )
        self.assertEqual(founder_node["private_draft_count"], 2)
        founder_org = next(r for r in org["roster"] if r["agent_id"] == self.founder_id)
        self.assertEqual(founder_org["private_draft_count"], 2)


if __name__ == "__main__":
    unittest.main()
