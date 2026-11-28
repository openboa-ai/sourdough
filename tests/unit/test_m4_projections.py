"""Unit tests for M4 Feature #18: Read-only Client projections, Desk privacy, and Core isolation."""

from __future__ import annotations

import json
import os
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.agent_desk import desks_root, ensure_private_desk, save_draft
from sourdough.company import FoundingTerms, FoundingRefused, found
from sourdough.duty_run import WorkRunClaim
from sourdough.ipc.projections import (
    PROJECTION_AGENT_DETAIL,
    PROJECTION_SYSTEM_EVALS,
    _count_private_drafts,
    projection_etag,
    read_agent_detail_projection,
    read_system_evals_projection,
)
from sourdough.memory import record_episode
from sourdough.method_learning import (
    activate_method_delta,
    collect_lineage_blast_radius,
    default_active_methods_provider,
    distill_method_delta,
    qualify_method_delta,
    quarantine_method_lineage,
)
from sourdough.runtime.work_kernel import (
    claim_work_run,
    complete_work_run,
    create_work_item,
    record_work_decision,
    register_responsibility,
)
from sourdough.storage.checkpoint import compute_logical_watermark
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
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
)
from sourdough.writer_lease import hold_company_writer


def _sha(raw: bytes | str) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return sha256(data).hexdigest()


class M4ProjectionsTests(unittest.TestCase):
    def test_count_private_drafts_deduplicates_stems_and_refuses_symlinks(self) -> None:
        with TemporaryDirectory(prefix="sd-m4-drafts-") as tmp:
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            leases.mkdir(mode=0o700)
            rec = found(
                store, FoundingTerms("proj-co", "charter-v1", "manage", "prof-v1")
            )
            with hold_company_writer(
                store, rec.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    ensure_private_desk(db, rec.company_id, rec.founder_id, "prof-v1")
                    item, _ = create_work_item(
                        db,
                        company_id=rec.company_id,
                        owner_agent_id=rec.founder_id,
                        duty_kind="work.discovery_review",
                        source_kind="manual",
                        source_id="s1",
                        actor_id=rec.founder_id,
                        now_ns=1_000,
                    )
                    run = claim_work_run(
                        db,
                        company_id=rec.company_id,
                        work_item_id=item.work_item_id,
                        lease_owner="test",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=1_000,
                    )
                claim = WorkRunClaim(
                    rec.company_id,
                    rec.founder_id,
                    item.work_item_id,
                    run.run_id,
                    run.fencing_token,
                    rec.grant_id,
                    "manage",
                    "work.discovery_review",
                    _clock=2_000,
                )
                save_draft(
                    w,
                    claim,
                    claim.purpose,
                    name="secret-alpha",
                    content="CANARY_SECRET_TEXT",
                    expected_revision=None,
                )

            drafts_dir = desks_root(store) / rec.founder_id / ".desk" / "drafts"
            # save_draft wrote secret-alpha.json + secret-alpha.md -> counted as 1 unique stem
            self.assertEqual(_count_private_drafts(store, rec.founder_id), 1)
            # Standalone .md draft counts as 2, dotfile ignored
            (drafts_dir / "notes-beta.md").write_text("standalone", encoding="utf-8")
            (drafts_dir / ".tmp-hidden.json").write_text("{}", encoding="utf-8")
            self.assertEqual(_count_private_drafts(store, rec.founder_id), 2)

            # Symlinked file inside drafts is ignored; symlinked drafts dir returns 0
            outside = root / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            os.symlink(outside, drafts_dir / "sym-leak.md")
            self.assertEqual(_count_private_drafts(store, rec.founder_id), 2)

            # Symlinked second agent drafts dir returns 0
            outside_dir = root / "outside-dir"
            outside_dir.mkdir()
            (outside_dir / "leak.json").write_text("{}", encoding="utf-8")
            other_desk_meta = desks_root(store) / "other-agent" / ".desk"
            other_desk_meta.mkdir(parents=True)
            os.symlink(outside_dir, other_desk_meta / "drafts")
            self.assertEqual(_count_private_drafts(store, "other-agent"), 0)

    def test_agent_detail_and_system_evals_projections_complete_and_read_only(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-m4-proj-") as tmp:
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            leases.mkdir(mode=0o700)
            eval_store = root / "evals" / "evaluator.sqlite"
            eval_store.parent.mkdir(mode=0o700)

            rec = found(
                store,
                FoundingTerms(
                    "eval-proj-co", "charter-v1", "manage-company", "prof-v1"
                ),
            )
            with hold_company_writer(
                store, rec.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    ensure_private_desk(db, rec.company_id, rec.founder_id, "prof-v1")
                    resp = register_responsibility(
                        db,
                        company_id=rec.company_id,
                        owner_agent_id=rec.founder_id,
                        duty_kind="work.discovery_review",
                        remit="Review work",
                        readable_scope=["*"],
                        now_ns=1_000,
                    )
                    item, _ = create_work_item(
                        db,
                        company_id=rec.company_id,
                        owner_agent_id=rec.founder_id,
                        duty_kind="work.discovery_review",
                        source_kind="manual",
                        source_id="s1",
                        actor_id=rec.founder_id,
                        now_ns=1_100,
                    )
                    run = claim_work_run(
                        db,
                        company_id=rec.company_id,
                        work_item_id=item.work_item_id,
                        lease_owner="test",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=1_100,
                    )
                claim = WorkRunClaim(
                    rec.company_id,
                    rec.founder_id,
                    item.work_item_id,
                    run.run_id,
                    run.fencing_token,
                    rec.grant_id,
                    "manage-company",
                    "work.discovery_review",
                    _clock=2_000,
                )
                save_draft(
                    w,
                    claim,
                    claim.purpose,
                    name="hidden-draft-name",
                    content="PRIVATE_CANARY_42",
                    expected_revision=None,
                )
                d0 = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-root",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Root method",
                    audience_scope="company",
                    source_refs=[f"work_run:{claim.run_id}"],
                    now_ns=1_200,
                )
                qualify_method_delta(
                    w,
                    d0.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=1_210,
                )
                a0 = activate_method_delta(
                    w,
                    d0.delta_id,
                    actor_id=rec.founder_id,
                    grant_id=rec.grant_id,
                    now_ns=1_220,
                )
                d1 = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-child",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Child method",
                    audience_scope="company",
                    source_refs=[f"work_run:{claim.run_id}"],
                    parent_method_ids=(d0.delta_id,),
                    active_snapshot_id=a0.active_snapshot_id,
                    now_ns=1_230,
                )
                qualify_method_delta(
                    w,
                    d1.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=1_240,
                )
                activate_method_delta(
                    w,
                    d1.delta_id,
                    actor_id=rec.founder_id,
                    grant_id=rec.grant_id,
                    now_ns=1_250,
                )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    record_work_decision(
                        db,
                        company_id=rec.company_id,
                        work_item_id=item.work_item_id,
                        run_id=claim.run_id,
                        fencing_token=claim.fencing_token,
                        agent_id=rec.founder_id,
                        action_kind="hold",
                        rationale="Grounded wait",
                        evidence=(),
                        active_snapshot_id=a0.active_snapshot_id,
                        decision_at_ns=1_300,
                        next_condition={
                            "responsibility_id": resp.responsibility_id,
                            "trigger_kind": "timer",
                            "due_at_ns": 99_000,
                        },
                    )
                    record_episode(
                        db,
                        company_id=rec.company_id,
                        agent_id=rec.founder_id,
                        work_item_id=item.work_item_id,
                        run_id=claim.run_id,
                        duty_kind="work.discovery_review",
                        context_digest=_sha(b"ctx"),
                        active_snapshot_id=a0.active_snapshot_id,
                        outcome_status="completed",
                        summary_text="Episode summary",
                        recorded_at_ns=1_310,
                    )
                    complete_work_run(
                        db,
                        claim.run_id,
                        claim.fencing_token,
                        outcome={"ok": True},
                        now_ns=1_320,
                    )
                quarantine_method_lineage(
                    w,
                    rec.company_id,
                    method_id="m-root",
                    reason="Quarantine blast radius test",
                    now_ns=1_400,
                )

            # Seal a recursive suite in evaluator_store and commit observations + grades
            limits = TrialCeiling(2, 10, 1000)
            suite = TrialSuite(
                suite_id="eval-proj-suite",
                purpose="recursive",
                rubric_digest=_sha(b"r"),
                evaluator_digest=_sha(b"e"),
                analysis_plan_digest=_sha(b"p"),
                slots=(
                    TrialSlot(
                        "s1",
                        "c1",
                        "f1",
                        TrialInputs(
                            _sha(b"f"), _sha(b"c"), _sha(b"i"), _sha(b"t"), "p"
                        ),
                        limits,
                        "incumbent",
                        "s2",
                    ),
                    TrialSlot(
                        "s2",
                        "c1",
                        "f1",
                        TrialInputs(
                            _sha(b"f"), _sha(b"c"), _sha(b"x"), _sha(b"t"), "p"
                        ),
                        limits,
                        "candidate",
                        "s1",
                    ),
                ),
            )
            rev = seal_trial_suite(eval_store, suite)
            cand_res_id = ""
            for idx, slot in enumerate(suite.slots, start=1):
                rid = f"r-{idx}"
                reserve_trial_launch(
                    eval_store,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=rid,
                    inputs=slot.inputs,
                    limits=slot.limits,
                )
                obs = commit_trial_observation(
                    eval_store,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=rid,
                    evidence=TrialTerminalEvidence(
                        "completed",
                        _sha(f"o-{slot.slot_id}"),
                        _sha(f"d-{slot.slot_id}"),
                        _sha(b"t"),
                        _sha(b"s"),
                    ),
                )
                if slot.arm == "candidate":
                    cand_res_id = obs.result_id or ""
                pkt = blind_trial_packet(
                    eval_store, revision=rev, result_id=obs.result_id
                )
                commit_blind_grade(
                    eval_store,
                    revision=rev,
                    packet=pkt,
                    grade=TrialGrade(True, True, True, 0, True),
                )
            for ab, ba in blind_pairwise_packets(eval_store, revision=rev):
                p1 = "first" if ab.first.result_id == cand_res_id else "second"
                p2 = "second" if p1 == "first" else "first"
                commit_position_swapped_pairwise_grade(
                    eval_store,
                    revision=rev,
                    ab_packet=ab,
                    ab_judgment=PairwiseJudgment("Better", p1),
                    ba_packet=ba,
                    ba_judgment=PairwiseJudgment("Confirmed", p2),
                )

            with closing(open_reader(store)) as ro_db, read_tx(ro_db):
                wm_before = compute_logical_watermark(ro_db, rec.company_id)
            eval_before = eval_store.read_bytes()

            detail1, seq1 = read_agent_detail_projection(
                store, rec.company_id, rec.founder_id, domain_plugins=()
            )
            detail2, seq2 = read_agent_detail_projection(
                store, rec.company_id, rec.founder_id, domain_plugins=()
            )
            assert detail1 is not None and detail2 is not None
            self.assertEqual(detail1["projection"], PROJECTION_AGENT_DETAIL)
            self.assertEqual(seq1, seq2)
            self.assertEqual(
                projection_etag(detail1, seq1), projection_etag(detail2, seq2)
            )
            self.assertEqual(detail1["desk_workspace"]["private_draft_count"], 1)
            self.assertEqual(detail1["active_methods"], [])
            self.assertEqual(len(detail1["armed_conditions"]), 1)
            self.assertEqual(len(detail1["tabs"]["work_decisions"]), 1)
            self.assertEqual(len(detail1["tabs"]["agent_episodes"]), 1)
            serialized_detail = json.dumps(detail1)
            self.assertNotIn("hidden-draft-name", serialized_detail)
            self.assertNotIn("PRIVATE_CANARY_42", serialized_detail)

            evals1, eseq1 = read_system_evals_projection(
                store, rec.company_id, evaluator_store=eval_store
            )
            evals2, eseq2 = read_system_evals_projection(
                store, rec.company_id, evaluator_store=eval_store
            )
            self.assertEqual(evals1["projection"], PROJECTION_SYSTEM_EVALS)
            self.assertEqual(eseq1, eseq2)
            self.assertEqual(
                projection_etag(evals1, eseq1), projection_etag(evals2, eseq2)
            )
            self.assertEqual(len(evals1["method_deltas"]), 2)
            self.assertEqual(len(evals1["method_qualifications"]), 2)
            self.assertEqual(len(evals1["method_activations"]), 2)
            root_act = next(
                a for a in evals1["method_activations"] if a["method_id"] == "m-root"
            )
            self.assertEqual(root_act["blast_radius_descendants"], [d1.delta_id])

            off = evals1["offline_evaluator"]
            self.assertTrue(off["connected"])
            self.assertEqual(len(off["suites"]), 1)
            self.assertEqual(off["suites"][0]["revision"], rev)
            self.assertEqual(off["suites"][0]["observation_count"], 2)
            self.assertEqual(off["suites"][0]["grade_count"], 2)
            self.assertEqual(off["suites"][0]["pairwise_grade_count"], 1)
            self.assertEqual(len(evals1["rsi_transfer_summaries"]), 1)
            self.assertEqual(
                evals1["rsi_transfer_summaries"][0]["suite_id"], "eval-proj-suite"
            )

            # Verify zero side effects on both stores
            with closing(open_reader(store)) as ro_db, read_tx(ro_db):
                self.assertEqual(
                    compute_logical_watermark(ro_db, rec.company_id), wm_before
                )
            self.assertEqual(eval_store.read_bytes(), eval_before)

            with self.assertRaises(FoundingRefused):
                read_system_evals_projection(store, "wrong-company")

    def test_system_evals_fallback_revision_isolation_and_snapshot_blast_radius(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-m4-proj-fb-") as tmp:
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            leases.mkdir(mode=0o700)
            rec = found(
                store,
                FoundingTerms("proj-fb-co", "charter-v1", "manage-company", "prof-v1"),
            )
            with hold_company_writer(
                store, rec.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    ensure_private_desk(db, rec.company_id, rec.founder_id, "prof-v1")
                    item, _ = create_work_item(
                        db,
                        company_id=rec.company_id,
                        owner_agent_id=rec.founder_id,
                        duty_kind="work.discovery_review",
                        source_kind="manual",
                        source_id="fb-1",
                        actor_id=rec.founder_id,
                        now_ns=1_000,
                    )
                    run = claim_work_run(
                        db,
                        company_id=rec.company_id,
                        work_item_id=item.work_item_id,
                        lease_owner="test",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=1_000,
                    )
                claim = WorkRunClaim(
                    rec.company_id,
                    rec.founder_id,
                    item.work_item_id,
                    run.run_id,
                    run.fencing_token,
                    rec.grant_id,
                    "manage-company",
                    "work.discovery_review",
                    _clock=2_000,
                )

                # 1. Activate unrelated m-alpha and m-beta (no prior revision)
                d_alpha = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-alpha",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Alpha method",
                    audience_scope="company",
                    source_refs=[f"work_run:{claim.run_id}"],
                    now_ns=1_100,
                )
                qualify_method_delta(
                    w,
                    d_alpha.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=1_110,
                )
                activate_method_delta(
                    w,
                    d_alpha.delta_id,
                    actor_id=rec.founder_id,
                    grant_id=rec.grant_id,
                    now_ns=1_120,
                )

                d_beta = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-beta",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Beta v1",
                    audience_scope="company",
                    source_refs=[f"work_run:{claim.run_id}"],
                    now_ns=1_130,
                )
                qualify_method_delta(
                    w,
                    d_beta.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=1_140,
                )
                activate_method_delta(
                    w,
                    d_beta.delta_id,
                    actor_id=rec.founder_id,
                    grant_id=rec.grant_id,
                    now_ns=1_150,
                )

                # Snapshot child distilled with default_active_methods_provider(...)[1]
                _, shift_snap = default_active_methods_provider(
                    store, rec.company_id, rec.founder_id, "work.discovery_review"
                )
                d_snap_child = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-snap-child",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Child from shift snapshot",
                    audience_scope="company",
                    active_snapshot_id=shift_snap,
                    source_refs=[f"work_run:{claim.run_id}"],
                    now_ns=1_160,
                )
                qualify_method_delta(
                    w,
                    d_snap_child.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=1_170,
                )
                activate_method_delta(
                    w,
                    d_snap_child.delta_id,
                    actor_id=rec.founder_id,
                    grant_id=rec.grant_id,
                    now_ns=1_180,
                )

                # Check blast_radius_descendants includes snapshot-linked child BEFORE quarantine
                evals_pre, _ = read_system_evals_projection(store, rec.company_id)
                alpha_act_pre = next(
                    a
                    for a in evals_pre["method_activations"]
                    if a["method_id"] == "m-alpha"
                )
                beta_act_pre = next(
                    a
                    for a in evals_pre["method_activations"]
                    if a["method_id"] == "m-beta"
                )
                self.assertIn(
                    d_snap_child.delta_id, alpha_act_pre["blast_radius_descendants"]
                )
                self.assertIn(
                    d_snap_child.delta_id, beta_act_pre["blast_radius_descendants"]
                )
                # Neither m-alpha nor m-beta has a fallback revision of its own!
                self.assertIsNone(alpha_act_pre["fallback_revision"])
                self.assertIsNone(beta_act_pre["fallback_revision"])

                # Quarantine m-beta -> m-beta and m-snap-child quarantine, fallback_revision for m-beta stays None
                quarantine_method_lineage(
                    w,
                    rec.company_id,
                    method_id="m-beta",
                    reason="Faulty beta",
                    now_ns=1_200,
                )
                evals_post, _ = read_system_evals_projection(store, rec.company_id)
                beta_act_post = next(
                    a
                    for a in evals_post["method_activations"]
                    if a["method_id"] == "m-beta"
                )
                self.assertTrue(beta_act_post["quarantined"])
                self.assertIsNone(beta_act_post["fallback_revision"])
                with closing(open_reader(store)) as ro_db, read_tx(ro_db):
                    self.assertEqual(
                        tuple(beta_act_post["blast_radius_descendants"]),
                        collect_lineage_blast_radius(
                            ro_db, rec.company_id, "m-beta", d_beta.revision_sha256
                        ),
                    )

                # Now patch m-alpha v1 -> v2, activate v2, and quarantine v2: fallback_revision MUST be v1!
                d_alpha_v2 = distill_method_delta(
                    w,
                    claim,
                    delta_kind="PATCH",
                    target_kind="work_method",
                    method_id="m-alpha",
                    superseded_revision=d_alpha.revision_sha256,
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Alpha v2",
                    audience_scope="company",
                    source_refs=[f"work_run:{claim.run_id}"],
                    now_ns=1_300,
                )
                qualify_method_delta(
                    w,
                    d_alpha_v2.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=1_310,
                )
                activate_method_delta(
                    w,
                    d_alpha_v2.delta_id,
                    actor_id=rec.founder_id,
                    grant_id=rec.grant_id,
                    now_ns=1_320,
                )
                quarantine_method_lineage(
                    w,
                    rec.company_id,
                    method_id="m-alpha",
                    revision_sha256=d_alpha_v2.revision_sha256,
                    reason="v2 regression",
                    now_ns=1_330,
                )
                evals_v2, _ = read_system_evals_projection(store, rec.company_id)
                alpha_v2_act = next(
                    a
                    for a in evals_v2["method_activations"]
                    if a["delta_id"] == d_alpha_v2.delta_id
                )
                self.assertEqual(
                    alpha_v2_act["fallback_revision"], d_alpha.revision_sha256
                )


if __name__ == "__main__":
    unittest.main()
