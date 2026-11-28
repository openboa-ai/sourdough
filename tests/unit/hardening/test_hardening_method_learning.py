"""Method learning CAS, quarantine, deprecation recovery, skill slug collision, and audience ACL hardening tests."""

from __future__ import annotations

import json
import unittest
from contextlib import closing
from time import time_ns
from typing import Any

from sourdough.agent_desk import (
    save_draft,
    write_desk_harness_files,
)
from sourdough.agent_runtime import (
    PROVIDER_OPENAI,
    AdapterManifest,
    AgentRuntimeAdapter,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
)
from sourdough.agent_runtime.registry import AgentAdapterRegistry
from sourdough.company_assets import Selection, assign_project, publish_selection
from sourdough.duty_run import DutyRefused
from sourdough.method_learning import (
    MethodRefused,
    activate_method_delta,
    distill_method_delta,
    qualify_method_delta,
    quarantine_method_lineage,
    redact_for_shared_scope,
    sync_desk_harness_projections,
)
from sourdough.runtime.method_tools import build_core_skill_tool_contribution
from sourdough.runtime.work_kernel import (
    complete_work_run,
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


class TestHardeningMethodLearning(HardeningTestCase):
    def test_p1_01_skill_slug_case_collision_disambiguation(self) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            for mid, body in (("SkillCase", "UPPER BODY"), ("skillcase", "LOWER BODY")):
                d = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id=mid,
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body=body,
                    audience_scope="company",
                    source_refs=[f"work_run:{claim.run_id}"],
                    now_ns=2_000,
                )
                qualify_method_delta(
                    w,
                    d.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=2_100,
                )
                activate_method_delta(
                    w,
                    d.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=2_200,
                )

            desk = sync_desk_harness_projections(
                self.store, self.company_id, self.founder_id
            )
            assert desk is not None
            skill_dirs = sorted(p.name for p in (desk / "skills").iterdir())
            self.assertEqual(len(skill_dirs), 2)

            contrib = build_core_skill_tool_contribution(writer=w, claim=claim)
            listed = contrib.handlers["skill_list_active"]({})
            self.assertEqual(len(listed["skills"]), 2)
            self.assertEqual(len({s["skill_name"] for s in listed["skills"]}), 2)

            upper = contrib.handlers["skill_read"]({"method_id": "SkillCase"})
            lower = contrib.handlers["skill_read"]({"method_id": "skillcase"})
            self.assertEqual(upper["skill"]["body_text"], "UPPER BODY")
            self.assertIn("UPPER BODY", upper["skill_markdown"])
            self.assertNotIn("LOWER BODY", upper["skill_markdown"])
            self.assertEqual(lower["skill"]["body_text"], "LOWER BODY")
            self.assertIn("LOWER BODY", lower["skill_markdown"])
            self.assertNotIn("UPPER BODY", lower["skill_markdown"])

            with self.assertRaises(DutyRefused):
                write_desk_harness_files(
                    self.store,
                    self.founder_id,
                    agents_md="# Test\n",
                    skills={
                        "Alpha": {"SKILL.md": b"a"},
                        "alpha": {"SKILL.md": b"b"},
                    },
                )

    def test_p1_01_skill_list_active_duty_filter_preserves_disambiguated_slugs(
        self,
    ) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            for mid, duty_kind, body in (
                ("SkillCase", "work.assessment_review", "ASSESS BODY"),
                ("skillcase", "work.discovery_review", "DISC BODY"),
            ):
                d = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id=mid,
                    trigger_spec={"duty_kind": duty_kind},
                    body=body,
                    audience_scope="company",
                    source_refs=[f"work_run:{claim.run_id}"],
                    now_ns=2_000,
                )
                qualify_method_delta(
                    w,
                    d.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=2_100,
                )
                activate_method_delta(
                    w,
                    d.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=2_200,
                )

            contrib = build_core_skill_tool_contribution(writer=w, claim=claim)
            filtered = contrib.handlers["skill_list_active"](
                {"duty_kind": "work.assessment_review"}
            )
            self.assertEqual(len(filtered["skills"]), 1)
            skill_entry = filtered["skills"][0]
            self.assertEqual(skill_entry["method_id"], "SkillCase")
            self.assertIn("-", skill_entry["skill_name"])
            read_back = contrib.handlers["skill_read"](
                {"skill_path": skill_entry["skill_path"]}
            )
            self.assertEqual(read_back["skill"]["method_id"], "SkillCase")
            self.assertEqual(read_back["skill"]["body_text"], "ASSESS BODY")

    def test_grec3_quarantined_retired_excluded_and_offloaded_skill_receipts_recovered(
        self,
    ) -> None:
        for bad_status in ("disputed", "quarantined", "retired"):
            draft = (
                "m-grec3",
                "r-1",
                "ADD",
                "work_method",
                json.dumps({"negative_triggers": ["no_deadline_item"]}),
                "Body",
                "",
                "Fails if valid item discarded",
                bad_status,
            )
            obs = _score_live_gen2_observables(
                shift_ok=True,
                action_kind="hold",
                evidence_count=0,
                rcpt_rows=(),
                drafted_methods=(draft,),
            )
            self.assertFalse(obs["drafted_counterexample_method"], bad_status)

        # Live Gen-2 cycle where active skill body > 2KB triggers offload on skill_read
        big_body = (
            "Boundary check rule: never discard valid no-deadline obligations. "
            + ("X" * 2800)
        )
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=time_ns(),
                )
            d_big = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="big-grec3",
                trigger_spec={"duty_kind": "work.discovery_review"},
                body=big_body,
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=600,
            )
            qualify_method_delta(
                w,
                d_big.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=650,
            )
            activate_method_delta(
                w,
                d_big.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=700,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db,
                    claim.run_id,
                    claim.fencing_token,
                    outcome={"ok": True},
                    now_ns=750,
                )

        class _LiveOffloadAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                has_cand_proc = False
                if request.tools and any(t.name == "skill_read" for t in request.tools):
                    sr = broker.invoke("c1", "skill_read", {"method_id": "big-grec3"})
                    if sr.get("offloaded"):
                        self.assert_offloaded = True
                    broker.invoke(
                        "c2", "skill_record_invocation", {"method_id": "big-grec3"}
                    )
                    for m in request.context.active_methods:
                        if m.get("item_kind") == "improvement_procedure":
                            pr = broker.invoke(
                                f"pr-{m['method_id']}",
                                "skill_read",
                                {"method_id": m["method_id"]},
                            )
                            pmeta = (pr.get("skill") or {}).get("procedure_meta") or {}
                            if pmeta.get("require_counterexample") is True:
                                has_cand_proc = True
                    if has_cand_proc:
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
                            "c3",
                            "skill_draft_candidate",
                            {
                                "method_id": f"cx-draft-{request.run_id[:12]}",
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
                        note_res = broker.invoke(
                            "c3b",
                            "desk_save_draft",
                            {
                                "name": f"grec3-{request.run_id[:8]}",
                                "content": f"Case {c_id}: handled {p_sym}, preserved {b_sym}.",
                            },
                        )
                ev_refs = (
                    [
                        {
                            "kind": "private_draft",
                            "resource_id": note_res["name"],
                            "revision": note_res["revision"],
                            "available_at_ns": 1,
                            "observed_at_ns": 1,
                        }
                    ]
                    if has_cand_proc
                    else []
                )
                broker.invoke(
                    "c4",
                    "record_work_decision",
                    {
                        "action_kind": (
                            "counterexample_verified" if has_cand_proc else "hold"
                        ),
                        "rationale": "Checked big skill.",
                        "evidence": ev_refs,
                        "action_id": f"dec-{request.run_id}",
                    },
                )
                return ShiftResult(
                    output_text="done",
                    usage=TokenUsage(5, 5),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=4,
                    events=(),
                )

        adapter = _LiveOffloadAdapter()
        reg = AgentAdapterRegistry()
        reg.register(adapter)
        eval_dir = self.root / "evals_grec3"
        eval_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        eval_store = eval_dir / "evaluator.sqlite"
        import sourdough.trial_runner as tr_mod

        captured_traces: list[dict[str, Any]] = []
        orig_grade = tr_mod.grade_trial_observation

        def _capture_grade(pkt, *, observation, tool_trace, service_effects, decision):
            captured_traces.append(dict(tool_trace))
            return orig_grade(
                pkt,
                observation=observation,
                tool_trace=tool_trace,
                service_effects=service_effects,
                decision=decision,
            )

        tr_mod.grade_trial_observation = _capture_grade
        try:
            with hold_company_writer(self.store, self.company_id) as w:
                res = run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_store,
                    company_id=self.company_id,
                    meta_agent_id=self.founder_id,
                    promotion_actor_id=self.founder_id,
                    promotion_grant_id=self.grant_id,
                    trial_root=self.root / "trials",
                    min_families=3,
                    adapter_registry=reg,
                    profile_bindings={self.founder_id: self.profile},
                )
        finally:
            tr_mod.grade_trial_observation = orig_grade
        self.assertTrue(getattr(adapter, "assert_offloaded", False))
        self.assertGreaterEqual(len(captured_traces), 12)
        seen_skill_read = False
        seen_invoke = False
        seen_draft = False
        for tr in captured_traces:
            for c in tr["calls"]:
                if c["name"] == "skill_read" and c.get("method_id") == "big-grec3":
                    seen_skill_read = True
                    self.assertEqual(c["item_kind"], "work_method")
                    self.assertEqual(c["delta_status"], "active")
                elif c["name"] == "skill_record_invocation":
                    seen_invoke = True
                    self.assertEqual(c["item_kind"], "work_method")
                elif c["name"] == "skill_draft_candidate":
                    seen_draft = True
                    self.assertEqual(c["delta_status"], "candidate")
        self.assertTrue(seen_skill_read and seen_invoke and seen_draft)
        self.assertEqual(res.transfer_verdict, "SUPPORTED")

    def test_gp4_1_activate_method_delta_cas_on_superseded_revision_and_quarantine(
        self,
    ) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            d_add = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-cas",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="v1",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=1_000,
            )
            qualify_method_delta(
                w,
                d_add.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=1_100,
            )
            activate_method_delta(
                w,
                d_add.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=1_200,
            )

            d_patch = distill_method_delta(
                w,
                claim,
                delta_kind="PATCH",
                target_kind="work_method",
                method_id="m-cas",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="v2-patch",
                audience_scope="company",
                superseded_revision=d_add.revision_sha256,
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=1_300,
            )
            qualify_method_delta(
                w,
                d_patch.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=1_400,
            )

            d_dep = distill_method_delta(
                w,
                claim,
                delta_kind="DEPRECATE",
                target_kind="work_method",
                method_id="m-cas",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="deprecate v1",
                audience_scope="company",
                superseded_revision=d_add.revision_sha256,
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=1_500,
            )
            qualify_method_delta(
                w,
                d_dep.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=1_600,
            )
            activate_method_delta(
                w,
                d_dep.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=1_700,
            )

            # Stale PATCH must fail CAS and not resurrect DEPRECATEd method
            with self.assertRaises(MethodRefused):
                activate_method_delta(
                    w,
                    d_patch.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=1_800,
                )

            # Quarantined target method_id cannot be activated either
            d_q = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-q-block",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="bad",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=2_000,
            )
            qualify_method_delta(
                w,
                d_q.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_100,
            )
            activate_method_delta(
                w,
                d_q.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=2_200,
            )
            quarantine_method_lineage(
                w, self.company_id, method_id="m-q-block", reason="bad", now_ns=2_300
            )

            d_readd = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-q-block",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="retry",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=2_400,
            )
            qualify_method_delta(
                w,
                d_readd.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_500,
            )
            with self.assertRaises(MethodRefused):
                activate_method_delta(
                    w,
                    d_readd.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=2_600,
                )

    def test_t5_new_3_candidate_quarantine_blocks_readd_and_add_enforces_active_cas(
        self,
    ) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            # 1. Quarantine an unactivated candidate -> blocks subsequent ADD activation
            d_cand = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-q-cand",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Bad candidate",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=1_000,
            )
            quarantine_method_lineage(
                w,
                self.company_id,
                method_id="m-q-cand",
                revision_sha256=d_cand.revision_sha256,
                reason="Unsafe candidate",
                now_ns=1_100,
            )
            d_readd = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-q-cand",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Retry under same method_id",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=1_200,
            )
            qualify_method_delta(
                w,
                d_readd.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=1_300,
            )
            with self.assertRaises(MethodRefused):
                activate_method_delta(
                    w,
                    d_readd.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=1_400,
                )

            # 2. ADD over an already active revision in the same scope fails closed
            d_act1 = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-add-cas",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Active v1",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=2_000,
            )
            qualify_method_delta(
                w,
                d_act1.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_100,
            )
            activate_method_delta(
                w,
                d_act1.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=2_200,
            )
            d_act2_dup = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-add-cas",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Overwrite without PATCH",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=2_300,
            )
            qualify_method_delta(
                w,
                d_act2_dup.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_400,
            )
            with self.assertRaises(MethodRefused):
                activate_method_delta(
                    w,
                    d_act2_dup.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=2_500,
                )

    def test_pr191_r2_quarantining_adopted_deprecation_restores_clean_incumbent(
        self,
    ) -> None:
        """PR #191 R2 P2: quarantining an adopted DEPRECATE restores the clean prior incumbent."""
        from sourdough.method_learning import resolve_active_methods

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            inc = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-deprecate-rollback",
                trigger_spec={
                    "duty_kind": "work.assessment_review",
                    "negative_triggers": ["skip on closed items"],
                },
                body="Always verify evidence timestamps.",
                audience_scope="one_agent",
                audience_target_id=self.founder_id,
                source_refs=[f"work_run:{claim.run_id}"],
                falsification_criteria="Fail if timestamps are unordered",
                now_ns=2_000,
            )
            qualify_method_delta(
                w,
                inc.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_500,
            )
            activate_method_delta(
                w,
                inc.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=3_000,
            )
            dep = distill_method_delta(
                w,
                claim,
                delta_kind="DEPRECATE",
                target_kind="work_method",
                method_id="m-deprecate-rollback",
                trigger_spec={
                    "duty_kind": "work.assessment_review",
                    "negative_triggers": ["keep when audits require it"],
                },
                body="Deprecate timestamp check.",
                audience_scope="one_agent",
                audience_target_id=self.founder_id,
                source_refs=[f"work_run:{claim.run_id}"],
                superseded_revision=inc.revision_sha256,
                parent_method_ids=(inc.method_id,),
                falsification_criteria="Revert if audit misses spike",
                now_ns=3_500,
            )
            qualify_method_delta(
                w,
                dep.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=4_000,
            )
            activate_method_delta(
                w,
                dep.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=4_500,
            )
            self.assertEqual(
                resolve_active_methods(w, self.company_id, agent_id=self.founder_id),
                (),
            )
            q_receipt = quarantine_method_lineage(
                w,
                self.company_id,
                method_id="m-deprecate-rollback",
                revision_sha256=dep.revision_sha256,
                reason="Defective deprecation caused audit regressions",
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=5_000,
            )
            self.assertEqual(q_receipt.restored_method_id, inc.method_id)
            self.assertEqual(q_receipt.restored_revision_sha256, inc.revision_sha256)
            restored = resolve_active_methods(
                w, self.company_id, agent_id=self.founder_id
            )
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0].method_id, inc.method_id)
            self.assertEqual(restored[0].revision_sha256, inc.revision_sha256)

    def test_pr191_r7_quarantining_replacement_add_stops_at_clean_deprecation(
        self,
    ) -> None:
        """PR #191 R7 P2: ADD -> DEPRECATE -> ADD then quarantining final ADD must NOT resurrect first ADD."""
        from sourdough.method_learning import resolve_active_methods

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _item, run, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
            src = (f"work_run:{run.run_id}",)
            # 1. Original ADD
            add1 = distill_method_delta(
                w,
                claim,
                method_id="m-dep-stop",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="company",
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Original deprecated method v1.",
                falsification_criteria="Fails if v1 is resurrected after deprecation.",
                source_refs=src,
                now_ns=1_100,
            )
            qualify_method_delta(
                w,
                add1.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_150,
            )
            activate_method_delta(
                w,
                add1.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=1_200,
            )
            # 2. Clean intentional DEPRECATE
            dep = distill_method_delta(
                w,
                claim,
                method_id="m-dep-stop",
                delta_kind="DEPRECATE",
                item_kind="work_method",
                audience_scope="company",
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Intentionally retire v1.",
                falsification_criteria="N/A retirement.",
                superseded_revision=add1.revision_sha256,
                source_refs=src,
                now_ns=1_300,
            )
            qualify_method_delta(
                w,
                dep.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_350,
            )
            activate_method_delta(
                w,
                dep.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=1_400,
            )
            with closing(open_reader(self.store)) as ro, read_tx(ro):
                self.assertEqual(
                    resolve_active_methods(
                        ro, self.company_id, agent_id=self.founder_id
                    ),
                    (),
                )
            # 3. Replacement ADD
            add2 = distill_method_delta(
                w,
                claim,
                method_id="m-dep-stop",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="company",
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Replacement method v2.",
                falsification_criteria="Fails if v2 regresses.",
                source_refs=src,
                now_ns=1_500,
            )
            qualify_method_delta(
                w,
                add2.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_550,
            )
            activate_method_delta(
                w,
                add2.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=1_600,
            )
            # 4. Quarantine replacement ADD -> must stop at clean DEPRECATE, NOT resurrect v1!
            receipt = quarantine_method_lineage(
                w,
                self.company_id,
                method_id="m-dep-stop",
                revision_sha256=add2.revision_sha256,
                reason="v2 faulted",
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=1_700,
            )
            self.assertIsNone(receipt.restored_revision_sha256)
            with closing(open_reader(self.store)) as ro, read_tx(ro):
                self.assertEqual(
                    resolve_active_methods(
                        ro, self.company_id, agent_id=self.founder_id
                    ),
                    (),
                )

    def test_pr191_r7_quarantine_activation_check_is_scoped_to_audience(
        self,
    ) -> None:
        """PR #191 R7 P2: quarantining Alice's one_agent method must not block Bob's unrelated method_id."""
        with hold_company_writer(self.store, self.company_id) as w:
            alice_id, alice_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="alice-r7",
                profile_id=self.profile.profile_id(),
            )
            bob_id, bob_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="bob-r7",
                profile_id=self.profile.profile_id(),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, run_a, claim_a = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=alice_id,
                    grant_id=alice_g,
                    grant_scope="assessment",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
                _, run_b, claim_b = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=bob_id,
                    grant_id=bob_g,
                    grant_scope="assessment",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
            alice_v1 = distill_method_delta(
                w,
                claim_a,
                method_id="shared-slug",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="one_agent",
                audience_target_id=alice_id,
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Alice private heuristic v1.",
                falsification_criteria="Fails if Alice heuristic regresses.",
                source_refs=(f"work_run:{run_a.run_id}",),
                now_ns=1_100,
            )
            bob_v1 = distill_method_delta(
                w,
                claim_b,
                method_id="shared-slug",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="one_agent",
                audience_target_id=bob_id,
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Bob unrelated private heuristic v1.",
                falsification_criteria="Fails if Bob heuristic regresses.",
                source_refs=(f"work_run:{run_b.run_id}",),
                now_ns=1_150,
            )
            qualify_method_delta(
                w,
                bob_v1.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_200,
            )
            # Quarantine Alice's candidate revision only
            quarantine_method_lineage(
                w,
                self.company_id,
                method_id="shared-slug",
                revision_sha256=alice_v1.revision_sha256,
                reason="Alice method faulted",
                actor_id=alice_id,
                grant_id=alice_g,
                now_ns=1_250,
            )
            # Alice's own replacement candidate under the same audience is still blocked
            alice_v2 = distill_method_delta(
                w,
                claim_a,
                method_id="shared-slug",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="one_agent",
                audience_target_id=alice_id,
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Alice retry heuristic v2.",
                falsification_criteria="Fails if Alice retry regresses.",
                source_refs=(f"work_run:{run_a.run_id}",),
                now_ns=1_300,
            )
            qualify_method_delta(
                w,
                alice_v2.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_350,
            )
            with self.assertRaisesRegex(MethodRefused, "active quarantine"):
                activate_method_delta(
                    w,
                    alice_v2.delta_id,
                    actor_id=alice_id,
                    grant_id=alice_g,
                    now_ns=1_400,
                )
            # Bob's unrelated qualified revision with the same method_id activates cleanly!
            bob_act = activate_method_delta(
                w,
                bob_v1.delta_id,
                actor_id=bob_id,
                grant_id=bob_g,
                now_ns=1_450,
            )
            self.assertEqual(bob_act.method_id, "shared-slug")
            self.assertEqual(bob_act.audience_target_id, bob_id)

            # Bob cannot cite Alice's private one_agent method as parent_method_ids
            with self.assertRaisesRegex(
                MethodRefused, "another Agent's private method"
            ):
                distill_method_delta(
                    w,
                    claim_b,
                    method_id="bob-cross-agent-ref",
                    delta_kind="ADD",
                    item_kind="work_method",
                    audience_scope="one_agent",
                    audience_target_id=bob_id,
                    trigger_spec={"duty_kinds": ["work.discovery_review"]},
                    body="Attempted cross-agent private lineage.",
                    falsification_criteria="Fails if cross-agent private lineage succeeds.",
                    parent_method_ids=(f"shared-slug:{alice_v1.revision_sha256}",),
                    source_refs=(f"work_run:{run_b.run_id}",),
                    now_ns=1_490,
                )

            # A descendant by Alice citing Alice's quarantined node as parent is STILL refused
            derived = distill_method_delta(
                w,
                claim_a,
                method_id="derived-from-alice",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="one_agent",
                audience_target_id=alice_id,
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Derived from Alice quarantined revision.",
                falsification_criteria="Fails if tainted lineage activates.",
                parent_method_ids=(f"shared-slug:{alice_v1.revision_sha256}",),
                source_refs=(f"work_run:{run_a.run_id}",),
                now_ns=1_500,
            )
            with self.assertRaisesRegex(MethodRefused, "quarantined"):
                qualify_method_delta(
                    w,
                    derived.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    holdout_margin=0.1,
                    now_ns=1_550,
                )
            with self.assertRaisesRegex(MethodRefused, "quarantined"):
                activate_method_delta(
                    w,
                    derived.delta_id,
                    actor_id=alice_id,
                    grant_id=alice_g,
                    now_ns=1_600,
                )

    def test_pr191_r8_shared_method_distill_rejects_narrow_audience_publication(
        self,
    ) -> None:
        """PR #191 R8 P2: selectively shared publication cannot be distilled into broader project/company method."""
        with hold_company_writer(self.store, self.company_id) as w:
            alice_id, alice_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="alice-r8",
                profile_id=self.profile.profile_id(),
            )
            bob_id, _bob_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="bob-r8",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-r8-aud",
                (alice_id, bob_id),
                publishers=(alice_id,),
            )
            co_proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-r8-all",
                (self.founder_id, alice_id, bob_id),
                publishers=(alice_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, claim_a = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=alice_id,
                    grant_id=alice_g,
                    grant_scope="assessment",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
            draft = save_draft(
                w,
                claim_a,
                claim_a.purpose,
                name="restricted-note",
                content="Restricted memo content only for Alice.",
                expected_revision=None,
            )
            alice_only_asset = publish_selection(
                w,
                claim_a,
                claim_a.purpose,
                Selection(
                    "pub-r8-alice-only",
                    "restricted-note",
                    draft.revision,
                    "proj-r8-aud",
                    proj_rev,
                    (alice_id,),  # Bob is a member of proj-r8-aud but NOT in audience!
                    0,
                    len(draft.content),
                ),
            )
            proj_wide_asset = publish_selection(
                w,
                claim_a,
                claim_a.purpose,
                Selection(
                    "pub-r8-proj-wide",
                    "restricted-note",
                    draft.revision,
                    "proj-r8-aud",
                    proj_rev,
                    (alice_id, bob_id),  # Both project members, but NOT founder!
                    0,
                    len(draft.content),
                ),
            )
            company_wide_asset = publish_selection(
                w,
                claim_a,
                claim_a.purpose,
                Selection(
                    "pub-r8-co-wide",
                    "restricted-note",
                    draft.revision,
                    "proj-r8-all",
                    co_proj_rev,
                    (self.founder_id, alice_id, bob_id),
                    0,
                    len(draft.content),
                ),
            )

            # 1. Alice-only publication CANNOT be distilled or redacted into project or company scope
            with self.assertRaisesRegex(MethodRefused, "restricted to a subset"):
                distill_method_delta(
                    w,
                    claim_a,
                    method_id="m-leak-proj",
                    delta_kind="ADD",
                    item_kind="work_method",
                    audience_scope="project",
                    audience_target_id="proj-r8-aud",
                    trigger_spec={"duty_kinds": ["work.discovery_review"]},
                    body="Copied restricted memo content.",
                    falsification_criteria="Fails if leaked.",
                    source_refs=(f"shared_publication:{alice_only_asset.asset_id}",),
                    now_ns=1_200,
                )
            with self.assertRaisesRegex(MethodRefused, "restricted to a subset"):
                redact_for_shared_scope(
                    w,
                    claim_a,
                    raw_body="Copied restricted memo content.",
                    source_refs=(f"shared_publication:{alice_only_asset.asset_id}",),
                    target_scope="project",
                    now_ns=1_250,
                )
            with self.assertRaisesRegex(MethodRefused, "not readable by all active"):
                distill_method_delta(
                    w,
                    claim_a,
                    method_id="m-leak-co",
                    delta_kind="ADD",
                    item_kind="work_method",
                    audience_scope="company",
                    trigger_spec={"duty_kinds": ["work.discovery_review"]},
                    body="Copied restricted memo content.",
                    falsification_criteria="Fails if leaked.",
                    source_refs=(f"shared_publication:{proj_wide_asset.asset_id}",),
                    now_ns=1_300,
                )

            # 2. Full project-wide publication succeeds for project scope
            ok_proj = distill_method_delta(
                w,
                claim_a,
                method_id="m-ok-proj",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="project",
                audience_target_id="proj-r8-aud",
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Authorized project-wide lesson.",
                falsification_criteria="Fails if project rule breaks.",
                source_refs=(f"shared_publication:{proj_wide_asset.asset_id}",),
                now_ns=1_350,
            )
            self.assertTrue(ok_proj.redaction_passed)

            # 3. Full company-wide publication succeeds for company scope
            ok_co = distill_method_delta(
                w,
                claim_a,
                method_id="m-ok-co",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="company",
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Authorized company-wide lesson.",
                falsification_criteria="Fails if company rule breaks.",
                source_refs=(f"shared_publication:{company_wide_asset.asset_id}",),
                now_ns=1_400,
            )
            self.assertTrue(ok_co.redaction_passed)

    def test_pr191_r9_revisionless_quarantine_seeds_scoped_to_authorized_audience(
        self,
    ) -> None:
        """PR #191 R9 P2: revisionless quarantine must not sweep another Agent's unrelated method_id."""
        from sourdough.method_learning import resolve_active_methods

        with hold_company_writer(self.store, self.company_id) as w:
            alice_id, alice_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="alice-r9q",
                profile_id=self.profile.profile_id(),
            )
            bob_id, bob_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="bob-r9q",
                profile_id=self.profile.profile_id(),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, run_a, claim_a = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=alice_id,
                    grant_id=alice_g,
                    grant_scope="assessment",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
                _, run_b, claim_b = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=bob_id,
                    grant_id=bob_g,
                    grant_scope="assessment",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
            # Bob activates earlier, Alice activates later under the SAME method_id
            bob_delta = distill_method_delta(
                w,
                claim_b,
                method_id="colliding-slug-r9",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="one_agent",
                audience_target_id=bob_id,
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Bob active method must survive Alice quarantine.",
                falsification_criteria="Fails if Bob loses active method.",
                source_refs=(f"work_run:{run_b.run_id}",),
                now_ns=1_100,
            )
            qualify_method_delta(
                w,
                bob_delta.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_150,
            )
            activate_method_delta(
                w,
                bob_delta.delta_id,
                actor_id=bob_id,
                grant_id=bob_g,
                now_ns=1_200,
            )
            alice_delta = distill_method_delta(
                w,
                claim_a,
                method_id="colliding-slug-r9",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="one_agent",
                audience_target_id=alice_id,
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Alice active method to be quarantined.",
                falsification_criteria="Fails if Alice method stays active.",
                source_refs=(f"work_run:{run_a.run_id}",),
                now_ns=1_300,
            )
            qualify_method_delta(
                w,
                alice_delta.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_350,
            )
            activate_method_delta(
                w,
                alice_delta.delta_id,
                actor_id=alice_id,
                grant_id=alice_g,
                now_ns=1_400,
            )

            # Alice invokes revisionless quarantine using only Alice's grant
            receipt = quarantine_method_lineage(
                w,
                self.company_id,
                method_id="colliding-slug-r9",
                revision_sha256=None,
                reason="Alice revokes her own method",
                actor_id=alice_id,
                grant_id=alice_g,
                now_ns=1_500,
            )
            self.assertEqual(
                receipt.quarantined_revisions,
                (("colliding-slug-r9", alice_delta.revision_sha256),),
            )
            with closing(open_reader(self.store)) as ro, read_tx(ro):
                alice_active = resolve_active_methods(
                    ro, self.company_id, agent_id=alice_id
                )
                bob_active = resolve_active_methods(
                    ro, self.company_id, agent_id=bob_id
                )
            self.assertEqual(alice_active, ())
            self.assertEqual(len(bob_active), 1)
            self.assertEqual(bob_active[0].revision_sha256, bob_delta.revision_sha256)

    def test_pr191_r10_activate_method_delta_idempotent_retry_and_conflict(
        self,
    ) -> None:
        """PR #191 R10 P2: activate_method_delta retries with same request_id return committed receipt."""
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, run, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
            src = (f"work_run:{run.run_id}",)
            d1 = distill_method_delta(
                w,
                claim,
                method_id="m-idem-r10",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="company",
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Idempotent activation v1.",
                falsification_criteria="Fails if retry breaks.",
                source_refs=src,
                now_ns=1_100,
            )
            qualify_method_delta(
                w,
                d1.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_150,
            )
            d2 = distill_method_delta(
                w,
                claim,
                method_id="m-other-r10",
                delta_kind="ADD",
                item_kind="work_method",
                audience_scope="company",
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Other method.",
                falsification_criteria="Fails if conflict passes.",
                source_refs=src,
                now_ns=1_160,
            )
            qualify_method_delta(
                w,
                d2.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_170,
            )

            # 1. ADD activation + matching retry returns identical MethodActivation
            act1 = activate_method_delta(
                w,
                d1.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                request_id="req-act-r10-1",
                now_ns=1_200,
            )
            act1_retry = activate_method_delta(
                w,
                d1.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                request_id="req-act-r10-1",
                now_ns=1_250,
            )
            self.assertEqual(act1_retry, act1)

            # 2. Reusing request_id with changed delta/terms fails closed
            with self.assertRaisesRegex(MethodRefused, "conflict"):
                activate_method_delta(
                    w,
                    d2.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    request_id="req-act-r10-1",
                    now_ns=1_300,
                )

            # 3. DEPRECATE activation (delta transitions to 'retired') ALSO replays idempotently
            dep = distill_method_delta(
                w,
                claim,
                method_id="m-idem-r10",
                delta_kind="DEPRECATE",
                item_kind="work_method",
                audience_scope="company",
                trigger_spec={"duty_kinds": ["work.discovery_review"]},
                body="Retire v1.",
                falsification_criteria="N/A.",
                superseded_revision=d1.revision_sha256,
                source_refs=src,
                now_ns=1_350,
            )
            qualify_method_delta(
                w,
                dep.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=1_360,
            )
            dep_act = activate_method_delta(
                w,
                dep.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                request_id="req-dep-r10-1",
                now_ns=1_400,
            )
            dep_retry = activate_method_delta(
                w,
                dep.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                request_id="req-dep-r10-1",
                now_ns=1_450,
            )
            self.assertEqual(dep_retry, dep_act)

    def test_pr191_r15_method_resolution_revalidates_shared_publication_access_on_audience_expansion(
        self,
    ) -> None:
        """PR #191 R15 P2: resolve_active_methods revalidates cited shared_publication access when project/company audience expands."""
        from sourdough.method_learning import resolve_active_methods

        with hold_company_writer(self.store, self.company_id) as w:
            # Part 1: Project membership expansion after activation (direct + transitive child method).
            proj_rev_1 = assign_project(
                w,
                self.grant_id,
                "proj-r15-exp",
                (self.founder_id,),
                publishers=(self.founder_id,),
            )
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
            d1 = save_draft(
                w,
                f_claim,
                f_claim.purpose,
                name="alice-only-pub",
                content="ALICE_ONLY_PROJECT_SECRET_R15",
                expected_revision=None,
            )
            pub_proj = publish_selection(
                w,
                f_claim,
                f_claim.purpose,
                Selection(
                    "pub-r15-proj",
                    "alice-only-pub",
                    d1.revision,
                    "proj-r15-exp",
                    proj_rev_1,
                    (self.founder_id,),
                    0,
                    len(d1.content),
                ),
            )
            # Company-scoped method while founder is the sole active company agent
            d_co = save_draft(
                w,
                f_claim,
                f_claim.purpose,
                name="alice-only-co",
                content="ALICE_ONLY_COMPANY_SECRET_R15",
                expected_revision=None,
            )
            pub_co = publish_selection(
                w,
                f_claim,
                f_claim.purpose,
                Selection(
                    "pub-r15-co",
                    "alice-only-co",
                    d_co.revision,
                    "proj-r15-exp",
                    proj_rev_1,
                    (self.founder_id,),
                    0,
                    len(d_co.content),
                ),
            )

            # Distill, qualify, and activate a project method + a derived child project method + a company method.
            proj_delta = distill_method_delta(
                w,
                f_claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-r15-proj",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Derived project checklist from Alice-only publication.",
                audience_scope="project",
                audience_target_id="proj-r15-exp",
                source_refs=[f"shared_publication:{pub_proj.asset_id}"],
                now_ns=2_100,
            )
            proj_q = qualify_method_delta(
                w,
                proj_delta.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_200,
            )
            activate_method_delta(
                w,
                proj_delta.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                qualification_id=proj_q.qualification_id,
                now_ns=2_300,
            )

            # Derived child project method citing only work_run + parent_method_ids (transitive source!).
            child_delta = distill_method_delta(
                w,
                f_claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-r15-proj-child",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Child project rule derived transitively from parent.",
                audience_scope="project",
                audience_target_id="proj-r15-exp",
                source_refs=[f"work_run:{f_claim.run_id}"],
                parent_method_ids=[
                    f"{proj_delta.method_id}:{proj_delta.revision_sha256}"
                ],
                now_ns=2_400,
            )
            child_q = qualify_method_delta(
                w,
                child_delta.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_450,
            )
            activate_method_delta(
                w,
                child_delta.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                qualification_id=child_q.qualification_id,
                now_ns=2_500,
            )

            co_delta = distill_method_delta(
                w,
                f_claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-r15-co",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Company checklist from founder-only publication.",
                audience_scope="company",
                source_refs=[f"shared_publication:{pub_co.asset_id}"],
                now_ns=2_700,
            )
            co_q = qualify_method_delta(
                w,
                co_delta.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_800,
            )
            activate_method_delta(
                w,
                co_delta.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                qualification_id=co_q.qualification_id,
                now_ns=2_900,
            )

            # Founder (sole original member) resolves all 3 methods.
            alice_ids = {
                m.method_id
                for m in resolve_active_methods(
                    w, self.company_id, agent_id=self.founder_id
                )
            }
            self.assertEqual(alice_ids, {"m-r15-proj", "m-r15-proj-child", "m-r15-co"})

            # Expand company & project audience by hiring Bob and adding him to proj-r15-exp.
            bob_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="bob-new-member-r15",
                profile_id=self.profile.profile_id(),
            )
            assign_project(
                w,
                self.grant_id,
                "proj-r15-exp",
                (self.founder_id, bob_id),
                publishers=(self.founder_id,),
                expected_revision=proj_rev_1,
            )

            # Bob is NOT in pub-r15-proj or pub-r15-co audience -> MUST NOT receive any of the 3 methods!
            bob_methods = resolve_active_methods(w, self.company_id, agent_id=bob_id)
            self.assertEqual(bob_methods, ())
            # Alice still retains access to her own authorized methods!
            alice_after = {
                m.method_id
                for m in resolve_active_methods(
                    w, self.company_id, agent_id=self.founder_id
                )
            }
            self.assertEqual(
                alice_after, {"m-r15-proj", "m-r15-proj-child", "m-r15-co"}
            )

    def test_pr191_r18_method_patch_inherits_superseded_shared_publication_restrictions(
        self,
    ) -> None:
        """PR #191 R18 P2: PATCH linked only via superseded_revision must inherit predecessor shared_publication restrictions."""
        from sourdough.method_learning import resolve_active_methods

        with hold_company_writer(self.store, self.company_id) as w:
            proj_rev_1 = assign_project(
                w,
                self.grant_id,
                "proj-r18-patch",
                (self.founder_id,),
                publishers=(self.founder_id,),
            )
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
            d1 = save_draft(
                w,
                f_claim,
                f_claim.purpose,
                name="alice-r18-pub",
                content="RESTRICTED_ALICE_ONLY_R18",
                expected_revision=None,
            )
            pub = publish_selection(
                w,
                f_claim,
                f_claim.purpose,
                Selection(
                    "pub-r18-only",
                    "alice-r18-pub",
                    d1.revision,
                    "proj-r18-patch",
                    proj_rev_1,
                    (self.founder_id,),
                    0,
                    len(d1.content),
                ),
            )
            base_delta = distill_method_delta(
                w,
                f_claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-r18-patch-chain",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Initial project rule from Alice-only publication.",
                audience_scope="project",
                audience_target_id="proj-r18-patch",
                source_refs=[f"shared_publication:{pub.asset_id}"],
                now_ns=2_000,
            )
            base_q = qualify_method_delta(
                w,
                base_delta.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_100,
            )
            activate_method_delta(
                w,
                base_delta.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                qualification_id=base_q.qualification_id,
                now_ns=2_200,
            )

            # Activate a PATCH linked ONLY via superseded_revision, citing only work_run (no parent_method_ids!).
            patch_delta = distill_method_delta(
                w,
                f_claim,
                delta_kind="PATCH",
                target_kind="work_method",
                method_id="m-r18-patch-chain",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Patched project rule retaining restricted guidance.",
                audience_scope="project",
                audience_target_id="proj-r18-patch",
                source_refs=[f"work_run:{f_claim.run_id}"],
                superseded_revision=base_delta.revision_sha256,
                now_ns=2_300,
            )
            patch_q = qualify_method_delta(
                w,
                patch_delta.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_400,
            )
            activate_method_delta(
                w,
                patch_delta.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                qualification_id=patch_q.qualification_id,
                now_ns=2_500,
            )

            # Expand project membership by adding Bob.
            bob_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="bob-r18-patch",
                profile_id=self.profile.profile_id(),
            )
            assign_project(
                w,
                self.grant_id,
                "proj-r18-patch",
                (self.founder_id, bob_id),
                publishers=(self.founder_id,),
                expected_revision=proj_rev_1,
            )
            self.assertEqual(
                resolve_active_methods(w, self.company_id, agent_id=bob_id), ()
            )
            alice_active = resolve_active_methods(
                w, self.company_id, agent_id=self.founder_id
            )
            self.assertEqual(
                [m.revision_sha256 for m in alice_active], [patch_delta.revision_sha256]
            )

    def test_pr191_r23_security_one_agent_method_metadata_redacted_and_lineage_validated(
        self,
    ) -> None:
        """PR #191 R23 Security #20: reject forged parent_method_ids and opaque-map one_agent method metadata in Board evals/detail/audit."""
        from sourdough.ipc.projections import (
            read_agent_detail_projection,
            read_system_audit_projection,
            read_system_evals_projection,
        )
        from sourdough.method_learning import (
            MethodRefused,
            activate_method_delta,
            distill_method_delta,
            qualify_method_delta,
        )

        now_ns = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            worker, worker_grant = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="worker-sec20-r23",
                profile_id=self.profile.profile_id(),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, w_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=worker,
                    grant_id=worker_grant,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                )
            # 1. Forged parent_method_ids carrying canary text MUST be refused!
            with self.assertRaisesRegex(MethodRefused, "parent_method_ids"):
                distill_method_delta(
                    w,
                    w_claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="forged-parent-r23",
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body="Safe body.",
                    audience_scope="one_agent",
                    parent_method_ids=("PRIVATE_DESK_CONTENT_CANARY_7491",),
                    source_refs=[f"work_run:{w_claim.run_id}"],
                    now_ns=now_ns,
                )

            # 2. Legitimate one_agent parent + child with filename-like method_ids
            d_parent = distill_method_delta(
                w,
                w_claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="secret-merger-plan-r23",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Private parent body.",
                audience_scope="one_agent",
                source_refs=[f"work_run:{w_claim.run_id}"],
                now_ns=now_ns + 10,
            )
            qualify_method_delta(
                w,
                d_parent.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=now_ns + 20,
            )
            activate_method_delta(
                w,
                d_parent.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=now_ns + 30,
            )
            d_child = distill_method_delta(
                w,
                w_claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="secret-child-canary-r23",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Private child body.",
                audience_scope="one_agent",
                parent_method_ids=(d_parent.delta_id,),
                source_refs=[f"work_run:{w_claim.run_id}"],
                now_ns=now_ns + 40,
            )
            qualify_method_delta(
                w,
                d_child.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                holdout_margin=0.1,
                now_ns=now_ns + 50,
            )
            activate_method_delta(
                w,
                d_child.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=now_ns + 60,
            )

        evals_proj, _ = read_system_evals_projection(self.store, self.company_id)
        detail_proj, _ = read_agent_detail_projection(
            self.store, self.company_id, worker
        )
        audit_proj, _ = read_system_audit_projection(self.store, self.company_id)

        for label, blob in (
            ("system-evals-v1", json.dumps(evals_proj, sort_keys=True)),
            ("agent-detail-v1", json.dumps(detail_proj, sort_keys=True)),
            ("system-audit-v1", json.dumps(audit_proj, sort_keys=True)),
        ):
            for secret in (
                "PRIVATE_DESK_CONTENT_CANARY_7491",
                "secret-merger-plan-r23",
                "secret-child-canary-r23",
            ):
                self.assertNotIn(
                    secret, blob, f"{label} leaked one_agent identifier {secret}"
                )
        # Verify opaque mapping preserved count & parent linkage
        one_agent_deltas = [
            d for d in evals_proj["method_deltas"] if d["audience_scope"] == "one_agent"
        ]
        self.assertEqual(len(one_agent_deltas), 2)
        self.assertTrue(
            all(
                d["method_id"].startswith("one_agent_method:") for d in one_agent_deltas
            )
        )
        with closing(open_reader(self.store)) as ro_db, read_tx(ro_db):
            audit_rows = ro_db.execute(
                "SELECT entity_id, payload_json FROM audit_event WHERE domain = 'learning'"
            ).fetchall()
        audit_blob = json.dumps(audit_rows)
        self.assertNotIn("secret-merger-plan-r23", audit_blob)
        self.assertNotIn("secret-child-canary-r23", audit_blob)


if __name__ == "__main__":
    unittest.main()
