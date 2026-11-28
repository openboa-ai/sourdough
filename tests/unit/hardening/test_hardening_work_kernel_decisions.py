"""Work kernel condition budgets, coalescing, decision evidence verification, and continuation hardening tests."""

from __future__ import annotations

import json
import unittest
from contextlib import closing
from hashlib import sha256
from time import time_ns
from typing import Any

from sourdough.agent_desk import (
    save_draft,
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
from sourdough.duty_run import WorkRunClaim
from sourdough.method_learning import (
    distill_method_delta,
    redact_for_shared_scope,
)
from sourdough.runtime import (
    CompanyRuntime,
    build_production_duty_registry,
    create_work_item,
    register_responsibility,
)
from sourdough.runtime.work_kernel import (
    DecisionEvidence,
    WorkKernelRefused,
    claim_work_run,
    complete_work_run,
    evaluate_conditions,
    open_peer_request,
    record_work_decision,
    set_condition,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer

from ._fixtures import (
    HardeningTestCase,
    _hire_active_agent,
    _open_work_run_claim,
)


class TestHardeningWorkKernelDecisions(HardeningTestCase):
    def test_gp3_1_discretionary_next_condition_never_escalates_priority_or_bypasses_budget(
        self,
    ) -> None:
        exec_order: list[str] = []

        class _OrderAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                exec_order.append(request.run_id)
                broker.invoke(
                    "c1",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "rationale": "Discovery done",
                        "evidence": [],
                        "action_id": f"act-{request.run_id}",
                    },
                )
                return ShiftResult(
                    output_text="done",
                    usage=TokenUsage(1, 1),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=1,
                    events=(),
                )

        reg = AgentAdapterRegistry()
        reg.register(_OrderAdapter())
        duty_reg = build_production_duty_registry(
            adapter_registry=reg, profile_bindings={self.founder_id: self.profile}
        )
        rt = CompanyRuntime(
            self.store, self.company_id, duty_reg, clock=lambda: 1_000_000
        )

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                dream_resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.dreaming",
                    remit="Idle dreaming",
                    readable_scope={
                        "max_wakes_per_window": 1,
                        "wake_window_ns": 100_000,
                        "cooldown_ns": 1_000,
                    },
                    now_ns=1_000,
                )
                disc_resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Discovery review",
                    readable_scope=["shared"],
                    now_ns=1_000,
                )
                # First dreaming item with next_timer_delay_ns=100 re-arms via DutyOutcome.next_condition
                dream_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.dreaming",
                    source_kind="manual",
                    source_id="dream-1",
                    payload={
                        "responsibility_id": dream_resp.responsibility_id,
                        "next_timer_delay_ns": 100,
                    },
                    now_ns=1_000,
                )
                # Pre-exhaust dream_resp budget
                db.execute(
                    """INSERT INTO work_discovery_state (
                           company_id, responsibility_id, consecutive_noops, cooldown_until_ns,
                           window_started_ns, window_wakes, updated_at_ns
                       ) VALUES (?, ?, 3, 999999, 1000, 99, 1000)""",
                    (self.company_id, dream_resp.responsibility_id),
                )
        rt.step(max_dispatches=1, now_ns=2_000)
        with closing(open_reader(self.store)) as db, read_tx(db):
            cond_row = db.execute(
                """SELECT work_item_id, status FROM work_conditions
                   WHERE company_id = ? AND responsibility_id = ?""",
                (self.company_id, dream_resp.responsibility_id),
            ).fetchone()
        self.assertIsNotNone(cond_row)
        self.assertIsNone(cond_row[0])  # NEVER linked to work_item_id!

        # Because budget & cooldown are exhausted, evaluate_conditions must NOT admit dreaming
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                admitted_blocked = evaluate_conditions(
                    db, self.company_id, now_ns=5_000
                )
                self.assertEqual(admitted_blocked, ())
                # Now reset budget & cooldown, trigger both dreaming and Tier 3 discovery_review!
                db.execute(
                    "DELETE FROM work_discovery_state WHERE company_id = ? AND responsibility_id = ?",
                    (self.company_id, dream_resp.responsibility_id),
                )
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=disc_resp.responsibility_id,
                    trigger_kind="timer",
                    due_at_ns=4_000,
                    now_ns=3_000,
                )
                admitted_ok = evaluate_conditions(db, self.company_id, now_ns=10_000)
                self.assertEqual(len(admitted_ok), 2)
                self.assertEqual(
                    [it.duty_kind for it in admitted_ok],
                    ["work.discovery_review", "work.dreaming"],
                )
                self.assertTrue(
                    all(it.parent_work_item_id is None for it in admitted_ok)
                )

    def test_gp3_2_shared_asset_revision_enforces_project_and_audience_acl(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            b_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="b-acl",
                profile_id=self.profile.profile_id(),
            )
            c_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="c-outsider",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-sec",
                (self.founder_id, b_id),
                publishers=(self.founder_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, ca = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            d = save_draft(
                w,
                ca,
                ca.purpose,
                name="sec-note",
                content="Confidential",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                ca,
                ca.purpose,
                Selection(
                    "pub-sec",
                    "sec-note",
                    d.revision,
                    "proj-sec",
                    proj_rev,
                    (self.founder_id, b_id),
                    0,
                    12,
                ),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                resp_c = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=c_id,
                    duty_kind="work.discovery_review",
                    remit="Outsider",
                    readable_scope=["shared"],
                    now_ns=1_100,
                )
                with self.assertRaises(WorkKernelRefused):
                    set_condition(
                        db,
                        company_id=self.company_id,
                        owner_agent_id=c_id,
                        responsibility_id=resp_c.responsibility_id,
                        trigger_kind="shared_asset_revision",
                        source_identity=asset.asset_id,
                        now_ns=1_200,
                    )
                resp_b = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=b_id,
                    duty_kind="work.discovery_review",
                    remit="Member B",
                    readable_scope=["shared"],
                    now_ns=1_200,
                )
                cond_b = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=b_id,
                    responsibility_id=resp_b.responsibility_id,
                    trigger_kind="shared_asset_revision",
                    source_identity=asset.asset_id,
                    now_ns=1_300,
                )
            # Remove B from proj-sec before evaluate_conditions
            assign_project(
                w,
                self.grant_id,
                "proj-sec",
                (self.founder_id,),
                publishers=(self.founder_id,),
                expected_revision=proj_rev,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                admitted = evaluate_conditions(
                    db,
                    self.company_id,
                    now_ns=2_000,
                    observed_assets={
                        asset.asset_id: sha256(b"Confidential").hexdigest()
                    },
                    triggering_actor_id=self.founder_id,
                )
                c_status = db.execute(
                    "SELECT status FROM work_conditions WHERE condition_id = ?",
                    (cond_b.condition_id,),
                ).fetchone()[0]
            self.assertEqual(admitted, ())
            self.assertEqual(c_status, "cancelled")

    def test_gp3_3_record_work_decision_rejects_nonexistent_core_evidence(self) -> None:
        item, run, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                for kind in (
                    "shared_publication",
                    "peer_request",
                    "work_run",
                    "work_item",
                    "tool_call_receipt",
                ):
                    with self.subTest(kind=kind):
                        with self.assertRaises(WorkKernelRefused):
                            record_work_decision(
                                db,
                                company_id=self.company_id,
                                work_item_id=item.work_item_id,
                                run_id=claim.run_id,
                                fencing_token=claim.fencing_token,
                                agent_id=self.founder_id,
                                action_kind="hold",
                                rationale="Bad ref",
                                evidence=(
                                    DecisionEvidence(
                                        kind=kind,
                                        resource_id=f"nonexistent-{kind}-999",
                                        revision="rev-999",
                                        observed_at_ns=1_500,
                                        available_at_ns=1_000,
                                    ),
                                ),
                                decision_at_ns=2_000,
                                action_id=f"act-bad-{kind}",
                            )

    def test_gp3_6_coalesced_single_shot_timer_transitions_to_triggered(self) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Coalesce check",
                    readable_scope=["shared"],
                    now_ns=1_000,
                )
                create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="open-disc",
                    payload={"responsibility_id": resp.responsibility_id},
                    now_ns=1_100,
                )
                cond = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="timer",
                    due_at_ns=2_000,
                    now_ns=1_200,
                )
                admitted = evaluate_conditions(db, self.company_id, now_ns=3_000)
                row = db.execute(
                    "SELECT status, fire_count, updated_at_ns FROM work_conditions WHERE condition_id = ?",
                    (cond.condition_id,),
                ).fetchone()
            self.assertEqual(admitted, ())
            self.assertEqual(row, ("triggered", 1, 3_000))

    def test_t5_new_2_coalesced_shared_asset_revision_max_fires_1_transitions_triggered(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, peer_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="pub-peer-t5new2",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-t5new2",
                (self.founder_id, peer_id),
                publishers=(peer_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, cp = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=peer_id,
                    grant_id=peer_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            d = save_draft(
                w,
                cp,
                cp.purpose,
                name="asset-t5new2",
                content="Revision one",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                cp,
                cp.purpose,
                Selection(
                    "pub-t5new2-1",
                    "asset-t5new2",
                    d.revision,
                    "proj-t5new2",
                    proj_rev,
                    (self.founder_id, peer_id),
                    0,
                    len(d.content),
                ),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Watch asset once",
                    readable_scope=["shared"],
                    now_ns=1_100,
                )
                open_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="open-disc-t5new2",
                    payload={"responsibility_id": resp.responsibility_id},
                    now_ns=1_200,
                )
                cond = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="shared_asset_revision",
                    source_identity=asset.asset_id,
                    max_fires=1,
                    now_ns=1_300,
                )
                admitted1 = evaluate_conditions(db, self.company_id, now_ns=2_000)
                row1 = db.execute(
                    "SELECT status, fire_count FROM work_conditions WHERE condition_id = ?",
                    (cond.condition_id,),
                ).fetchone()
                self.assertEqual(admitted1, ())
                self.assertEqual(row1, ("triggered", 1))
                # Complete the open item and publish a second revision; must NOT fire again!
                db.execute(
                    "UPDATE work_items SET state = 'settled' WHERE work_item_id = ?",
                    (open_item.work_item_id,),
                )
                admitted2 = evaluate_conditions(
                    db,
                    self.company_id,
                    now_ns=3_000,
                    observed_assets={
                        asset.asset_id: sha256(b"Revision two").hexdigest()
                    },
                    triggering_actor_id=peer_id,
                )
                row2 = db.execute(
                    "SELECT status, fire_count FROM work_conditions WHERE condition_id = ?",
                    (cond.condition_id,),
                ).fetchone()
            self.assertEqual(admitted2, ())
            self.assertEqual(row2, ("triggered", 1))

    def test_pr191_record_work_decision_validates_private_draft_existence_and_revision(
        self,
    ) -> None:
        """PR #191 P2: private_draft citations must resolve to the agent's real draft & revision."""
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
                name="real-note",
                content="Verified private working note",
                expected_revision=None,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                # 1. Nonexistent draft refused
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        fencing_token=claim.fencing_token,
                        agent_id=self.founder_id,
                        action_kind="hold",
                        rationale="citing nonexistent draft",
                        evidence=(
                            DecisionEvidence(
                                "private_draft",
                                "nonexistent",
                                "fake-revision",
                                900,
                                800,
                            ),
                        ),
                        decision_at_ns=1_500,
                    )
                # 2. Stale/wrong revision on existing draft refused
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        fencing_token=claim.fencing_token,
                        agent_id=self.founder_id,
                        action_kind="hold",
                        rationale="citing stale revision",
                        evidence=(
                            DecisionEvidence(
                                "private_draft",
                                f"{self.founder_id}/real-note",
                                "stale-revision",
                                900,
                                800,
                            ),
                        ),
                        decision_at_ns=1_500,
                    )
                # 3. Valid existing draft & exact revision accepted
                dec = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=claim.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="citing verified draft",
                    evidence=(
                        DecisionEvidence(
                            "private_draft",
                            f"{self.founder_id}/real-note",
                            draft.revision,
                            900,
                            800,
                        ),
                    ),
                    decision_at_ns=1_500,
                )
                self.assertEqual(dec.action_kind, "hold")

    def test_pr191_r3_tool_call_receipt_evidence_authenticates_digest_run_and_time(
        self,
    ) -> None:
        """PR #191 Security P2: tool_call_receipt citations must authenticate digest, run, and time."""
        from sourdough.runtime.work_kernel import complete_work_run

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _item0, run0, _claim0 = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=500,
                )
                complete_work_run(
                    db,
                    run_id=run0.run_id,
                    fencing_token=run0.fencing_token,
                    outcome="completed",
                    now_ns=900,
                )
                item1, run1, claim1 = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
                db.execute(
                    """INSERT INTO tool_call_receipt
                       (company_id, run_id, agent_id, call_id, tool_name, arg_digest,
                        status, result_digest, result_json, recorded_at_ns)
                       VALUES (?, ?, ?, 'call-shared', 'desk_read_draft', 'a1',
                               'ok', 'result-real-0', '{"ok":true}', 800),
                              (?, ?, ?, 'call-shared', 'desk_read_draft', 'a2',
                               'ok', 'result-real-1', '{"ok":true}', 1_100)""",
                    (
                        self.company_id,
                        run0.run_id,
                        self.founder_id,
                        self.company_id,
                        run1.run_id,
                        self.founder_id,
                    ),
                )
                # 1. Attacker-chosen forged revision MUST be refused
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item1.work_item_id,
                        run_id=run1.run_id,
                        fencing_token=claim1.fencing_token,
                        agent_id=self.founder_id,
                        action_kind="hold",
                        rationale="forged receipt digest",
                        evidence=(
                            DecisionEvidence(
                                "tool_call_receipt",
                                f"{run1.run_id}/call-shared",
                                "attacker-chosen-not-result-real",
                                1_500,
                                1_400,
                            ),
                        ),
                        decision_at_ns=2_000,
                    )
                # 2. Claiming availability BEFORE receipt recorded_at_ns MUST be refused
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item1.work_item_id,
                        run_id=run1.run_id,
                        fencing_token=claim1.fencing_token,
                        agent_id=self.founder_id,
                        action_kind="hold",
                        rationale="premature availability",
                        evidence=(
                            DecisionEvidence(
                                "tool_call_receipt",
                                f"{run1.run_id}/call-shared",
                                "result-real-1",
                                1_500,
                                1_050,
                            ),
                        ),
                        decision_at_ns=2_000,
                    )
                # 3. Unambiguous qualified receipt with exact result_digest & valid time accepted
                dec = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item1.work_item_id,
                    run_id=run1.run_id,
                    fencing_token=claim1.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="authenticated receipt citation",
                    evidence=(
                        DecisionEvidence(
                            "tool_call_receipt",
                            f"{run1.run_id}/call-shared",
                            "result-real-1",
                            1_500,
                            1_200,
                        ),
                    ),
                    decision_at_ns=2_000,
                )
                self.assertEqual(dec.action_kind, "hold")

    def test_pr191_r5_peer_response_exempt_from_discretionary_cooldown_backoff(
        self,
    ) -> None:
        """PR #191 R5 P2: peer_response conditions must fire immediately despite discovery backoff."""
        from sourdough.runtime.work_kernel import (
            record_peer_request_read,
            resolve_peer_request,
        )

        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, _peer_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="peer-r5",
                profile_id=self.profile.profile_id(),
            )
            assign_project(
                w,
                self.grant_id,
                "proj-peer-r5",
                (self.founder_id, peer_id),
                publishers=(self.founder_id, peer_id),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=peer_id,
                    duty_kind="work.peer_response",
                    remit="Answer compliance peer requests",
                    readable_scope={
                        "project_id": "proj-peer-r5",
                        "allowed_requesters": [self.founder_id],
                    },
                    now_ns=1_000,
                )
                disc_resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Discovery with strict cooldown",
                    readable_scope={
                        "max_wakes_per_window": 1,
                        "wake_window_ns": 1_000_000,
                        "cooldown_ns": 500_000,
                    },
                    now_ns=1_000,
                )
                # Put discovery responsibility deep in cooldown + exhausted wake budget
                db.execute(
                    """INSERT INTO work_discovery_state (
                           company_id, responsibility_id, consecutive_noops, cooldown_until_ns,
                           window_started_ns, window_wakes, updated_at_ns
                       ) VALUES (?, ?, 4, 999_999, 1_000, 99, 1_000)""",
                    (self.company_id, disc_resp.responsibility_id),
                )
                # Open & immediately answer a peer request from founder -> peer-r5
                parent_item, parent_run, _ = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_500,
                )
                req, _ = open_peer_request(
                    db,
                    company_id=self.company_id,
                    requester_work_item_id=parent_item.work_item_id,
                    requester_run_id=parent_run.run_id,
                    fencing_token=parent_run.fencing_token,
                    recipient_agent_id=peer_id,
                    question="Confirm compliance gate",
                    expected_decision="approve_or_decline",
                    evidence_refs=[],
                    deadline_ns=900_000,
                    request_id="req-r5-exempt",
                    project_id="proj-peer-r5",
                    now_ns=2_000,
                )
                peer_run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=req.response_work_item_id,
                    lease_owner="test-peer-r5",
                    lease_ttl_ns=100_000,
                    now_ns=2_200,
                )
                record_peer_request_read(
                    db,
                    company_id=self.company_id,
                    request_id=req.request_id,
                    actor_id=peer_id,
                    response_run_id=peer_run.run_id,
                    response_fencing_token=peer_run.fencing_token,
                    now_ns=2_300,
                )
                resolve_peer_request(
                    db,
                    company_id=self.company_id,
                    request_id=req.request_id,
                    actor_id=peer_id,
                    resolution_state="answered",
                    response_payload={"answer": "approved"},
                    response_run_id=peer_run.run_id,
                    response_fencing_token=peer_run.fencing_token,
                    now_ns=2_500,
                )
                # Arm BOTH a discretionary timer AND a peer_response condition on disc_resp
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=disc_resp.responsibility_id,
                    trigger_kind="timer",
                    due_at_ns=2_600,
                    now_ns=2_500,
                )
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=disc_resp.responsibility_id,
                    trigger_kind="peer_response",
                    source_identity=f"peer_response:{req.request_id}",
                    now_ns=2_500,
                )
                # Evaluate while STILL inside cooldown (3_000 < 999_999) and budget exhausted
                admitted = evaluate_conditions(db, self.company_id, now_ns=3_000)

        self.assertEqual(len(admitted), 1)
        payload = json.loads(admitted[0].payload_json)
        self.assertEqual(payload["trigger_kind"], "peer_response")
        self.assertEqual(payload["source_identity"], f"peer_response:{req.request_id}")

    def test_pr191_r6_unrecognized_decision_evidence_kind_fails_closed(
        self,
    ) -> None:
        """PR #191 R6 P2: record_work_decision rejects unrecognized evidence kinds without a validating plugin."""
        from sourdough.runtime.domains import BaseDomainPlugin

        class _NoOpInheritedPlugin(BaseDomainPlugin):
            domain_name = "noop_ev"

        class _NoneReturningPlugin(BaseDomainPlugin):
            domain_name = "none_ev"

            def validate_decision_evidence(self, _conn, _cid, _aid, _ts, _ev) -> None:
                return None

        class _ScopedMetricPlugin(BaseDomainPlugin):
            domain_name = "scoped_metric"

            def validate_decision_evidence(
                self, _conn, _cid, _aid, _ts, evidence
            ) -> bool:
                if evidence.kind == "custom_metric":
                    if evidence.revision != "verified-v1":
                        raise WorkKernelRefused("Invalid custom_metric revision")
                    return True
                return False

        fabricated = DecisionEvidence(
            kind="invented_metric",
            resource_id="nonexistent",
            revision="fake",
            observed_at_ns=1_500,
            available_at_ns=1_000,
        )
        valid_custom = DecisionEvidence(
            kind="custom_metric",
            resource_id="metric-1",
            revision="verified-v1",
            observed_at_ns=1_500,
            available_at_ns=1_000,
        )

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
                for label, plugins in (
                    ("core_only", ()),
                    ("base_noop_plugin", (_NoOpInheritedPlugin(),)),
                    ("none_returning_plugin", (_NoneReturningPlugin(),)),
                    ("unrelated_scoped_plugin", (_ScopedMetricPlugin(),)),
                ):
                    with self.subTest(label=label):
                        with self.assertRaisesRegex(
                            WorkKernelRefused, "Unsupported or unvalidated"
                        ):
                            record_work_decision(
                                db,
                                company_id=self.company_id,
                                work_item_id=item.work_item_id,
                                run_id=run.run_id,
                                fencing_token=run.fencing_token,
                                agent_id=self.founder_id,
                                action_kind="counterexample_verified",
                                rationale="Attempted fabricated evidence",
                                evidence=(fabricated,),
                                decision_at_ns=2_000,
                                domain_plugins=plugins,
                            )
                # Explicitly recognized & validated plugin evidence succeeds
                dec = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="counterexample_verified",
                    rationale="Authoritatively validated custom evidence",
                    evidence=(valid_custom,),
                    decision_at_ns=2_000,
                    domain_plugins=(_ScopedMetricPlugin(),),
                )
                self.assertTrue(dec.decision_id)

    def test_pr191_r8_record_work_decision_replay_normalizes_discretionary_next_condition_item_id(
        self,
    ) -> None:
        """PR #191 R8 P2: replaying record_work_decision with discovery resp + explicit work_item_id succeeds."""
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                disc_resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Periodic discovery",
                    readable_scope=["shared"],
                    now_ns=1_000,
                )
                item, run, _claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_500,
                )
                nxt = {
                    "responsibility_id": disc_resp.responsibility_id,
                    "work_item_id": item.work_item_id,
                    "trigger_kind": "timer",
                    "due_at_ns": 50_000,
                }
                d1 = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="Wait for next window",
                    evidence=(),
                    decision_at_ns=2_000,
                    action_id="act-r8-replay",
                    next_condition=nxt,
                )
                # Identical replay with explicit work_item_id + discretionary responsibility must succeed!
                d2 = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="Wait for next window",
                    evidence=(),
                    decision_at_ns=2_000,
                    action_id="act-r8-replay",
                    next_condition=nxt,
                )
                self.assertEqual(d1.decision_id, d2.decision_id)
                self.assertEqual(d1.next_condition_id, d2.next_condition_id)
                cond_item = db.execute(
                    "SELECT work_item_id FROM work_conditions WHERE condition_id = ?",
                    (d1.next_condition_id,),
                ).fetchone()
                self.assertEqual(cond_item, (None,))

    def test_pr191_r9_core_evidence_authenticates_work_run_work_item_and_peer_request_revisions(
        self,
    ) -> None:
        """PR #191 R9 Security P2: work_run, work_item, and peer_request evidence must verify canonical revision & time."""
        from sourdough.runtime.work_kernel import (
            peer_request_evidence_revision,
            work_item_evidence_revision,
            work_run_evidence_revision,
        )

        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, _peer_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="peer-r9",
                profile_id=self.profile.profile_id(),
            )
            assign_project(
                w,
                self.grant_id,
                "proj-r9",
                (self.founder_id, peer_id),
                publishers=(self.founder_id, peer_id),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=peer_id,
                    duty_kind="work.peer_response",
                    remit="Answer R9 requests",
                    readable_scope={
                        "project_id": "proj-r9",
                        "allowed_requesters": [self.founder_id],
                    },
                    now_ns=1_000,
                )
                item, run, _claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=2_000,
                )
                other_item, other_run, _ = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=peer_id,
                    grant_id=_peer_g,
                    grant_scope="assessment",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=2_100,
                )
                # Create a valid peer_request from peer -> founder so founder can cite it
                register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.peer_response",
                    remit="Founder peer responder",
                    readable_scope={
                        "project_id": "proj-r9",
                        "allowed_requesters": [peer_id],
                    },
                    now_ns=1_000,
                )
                req, _ = open_peer_request(
                    db,
                    company_id=self.company_id,
                    requester_work_item_id=other_item.work_item_id,
                    requester_run_id=other_run.run_id,
                    fencing_token=other_run.fencing_token,
                    recipient_agent_id=self.founder_id,
                    question="Confirm R9 revision auth",
                    expected_decision="approve",
                    evidence_refs=[],
                    deadline_ns=900_000,
                    request_id="req-r9-auth",
                    project_id="proj-r9",
                    now_ns=2_200,
                )

                run_row = db.execute(
                    """SELECT company_id, work_item_id, agent_id, duty_kind, fencing_token, created_at_ns
                       FROM work_runs WHERE run_id = ?""",
                    (run.run_id,),
                ).fetchone()
                valid_run_rev = work_run_evidence_revision(
                    run.run_id,
                    company_id=str(run_row[0]),
                    work_item_id=str(run_row[1]),
                    agent_id=str(run_row[2]),
                    duty_kind=str(run_row[3]),
                    fencing_token=int(run_row[4]),
                    created_at_ns=int(run_row[5]),
                )
                item_row = db.execute(
                    """SELECT company_id, owner_agent_id, duty_kind, source_kind, source_id, payload_json, created_at_ns
                       FROM work_items WHERE work_item_id = ?""",
                    (item.work_item_id,),
                ).fetchone()
                valid_item_rev = work_item_evidence_revision(
                    item.work_item_id,
                    company_id=str(item_row[0]),
                    owner_agent_id=str(item_row[1]),
                    duty_kind=str(item_row[2]),
                    source_kind=str(item_row[3]),
                    source_id=str(item_row[4]),
                    payload_json=str(item_row[5]),
                    created_at_ns=int(item_row[6]),
                )
                req_row = db.execute(
                    """SELECT company_id, requester_agent_id, recipient_agent_id,
                              state, question, expected_decision, response_json, created_at_ns
                       FROM work_peer_requests WHERE request_id = ?""",
                    (req.request_id,),
                ).fetchone()
                valid_req_rev = peer_request_evidence_revision(
                    req.request_id,
                    company_id=str(req_row[0]),
                    requester_agent_id=str(req_row[1]),
                    recipient_agent_id=str(req_row[2]),
                    state=str(req_row[3]),
                    question=str(req_row[4]),
                    expected_decision=str(req_row[5]),
                    response_json=None if req_row[6] is None else str(req_row[6]),
                    created_at_ns=int(req_row[7]),
                )

                # 1. Attacker-chosen revisions MUST fail closed for all three kinds
                for kind, rid in (
                    ("work_run", run.run_id),
                    ("work_item", item.work_item_id),
                    ("peer_request", req.request_id),
                ):
                    with self.subTest(bad_rev_kind=kind):
                        with self.assertRaisesRegex(WorkKernelRefused, "revision"):
                            record_work_decision(
                                db,
                                company_id=self.company_id,
                                work_item_id=item.work_item_id,
                                run_id=run.run_id,
                                fencing_token=run.fencing_token,
                                agent_id=self.founder_id,
                                action_kind="counterexample_verified",
                                rationale="Forged revision",
                                evidence=(
                                    DecisionEvidence(
                                        kind=kind,
                                        resource_id=rid,
                                        revision="attacker-chosen-not-a-revision",
                                        observed_at_ns=3_000,
                                        available_at_ns=2_500,
                                    ),
                                ),
                                decision_at_ns=3_500,
                            )

                # 2. Pre-creation available_at_ns MUST fail closed even with valid revision
                with self.assertRaisesRegex(WorkKernelRefused, "precede"):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        fencing_token=run.fencing_token,
                        agent_id=self.founder_id,
                        action_kind="counterexample_verified",
                        rationale="Backdated timestamp",
                        evidence=(
                            DecisionEvidence(
                                kind="work_run",
                                resource_id=run.run_id,
                                revision=valid_run_rev,
                                observed_at_ns=1_000,
                                available_at_ns=500,
                            ),
                        ),
                        decision_at_ns=3_500,
                    )

                # 3. Canonical authenticated revisions + valid timestamps succeed for all three!
                dec = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="counterexample_verified",
                    rationale="Authenticated core citations",
                    evidence=(
                        DecisionEvidence(
                            kind="work_run",
                            resource_id=run.run_id,
                            revision=valid_run_rev,
                            observed_at_ns=2_500,
                            available_at_ns=2_000,
                        ),
                        DecisionEvidence(
                            kind="work_item",
                            resource_id=item.work_item_id,
                            revision=valid_item_rev,
                            observed_at_ns=2_500,
                            available_at_ns=2_000,
                        ),
                        DecisionEvidence(
                            kind="peer_request",
                            resource_id=req.request_id,
                            revision=valid_req_rev,
                            observed_at_ns=2_500,
                            available_at_ns=2_200,
                        ),
                    ),
                    decision_at_ns=3_500,
                )
                self.assertTrue(dec.decision_id)

    def test_pr191_r9_shared_publication_evidence_rejects_post_decision_commit(
        self,
    ) -> None:
        """PR #191 R9 P2: shared_publication committed after decision_at_ns must fail closed."""
        with hold_company_writer(self.store, self.company_id) as w:
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-r9-pub",
                (self.founder_id,),
                publishers=(self.founder_id,),
            )
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
                name="pub-time-check",
                content="Point-in-time published guidance.",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                claim,
                claim.purpose,
                Selection(
                    "pub-r9-time",
                    "pub-time-check",
                    draft.revision,
                    "proj-r9-pub",
                    proj_rev,
                    (self.founder_id,),
                    0,
                    len(draft.content),
                ),
            )
            pub_digest = sha256(asset.content.encode("utf-8")).hexdigest()
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                pub_ts = int(
                    db.execute(
                        """SELECT MIN(recorded_at_ns) FROM audit_event
                           WHERE company_id = ? AND entity_id = ?
                             AND event_type = 'company.asset_published'""",
                        (self.company_id, asset.asset_id),
                    ).fetchone()[0]
                )
                # Backdated timestamps preceding publication commit MUST fail closed
                with self.assertRaisesRegex(WorkKernelRefused, "precede publication"):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        fencing_token=run.fencing_token,
                        agent_id=self.founder_id,
                        action_kind="hold",
                        rationale="Backdated future publication",
                        evidence=(
                            DecisionEvidence(
                                kind="shared_publication",
                                resource_id=asset.asset_id,
                                revision=pub_digest,
                                observed_at_ns=pub_ts - 10,
                                available_at_ns=pub_ts - 20,
                            ),
                        ),
                        decision_at_ns=pub_ts - 5,
                    )
                # Post-publication timestamps succeed
                dec = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="Point-in-time verified publication",
                    evidence=(
                        DecisionEvidence(
                            kind="shared_publication",
                            resource_id=asset.asset_id,
                            revision=pub_digest,
                            observed_at_ns=pub_ts + 10,
                            available_at_ns=pub_ts,
                        ),
                    ),
                    decision_at_ns=pub_ts + 20,
                )
                self.assertTrue(dec.decision_id)

    def test_pr191_r12_timestamp_free_decision_retry_reuses_committed_timestamp(
        self,
    ) -> None:
        """PR #191 R12 P2: omitting decision_at_ns on identical action_id retries returns committed decision."""
        from sourdough.runtime.proactive_tools import (
            build_core_proactive_tool_contribution,
        )

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
                # 1. Direct record_work_decision with omitted decision_at_ns & advancing clock
                d1 = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="Wait for next cycle",
                    evidence=(),
                    decision_at_ns=None,
                    now_ns=2_000,
                    action_id="act-r12-no-ts",
                )
                d2 = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="Wait for next cycle",
                    evidence=(),
                    decision_at_ns=None,
                    now_ns=9_999,
                    action_id="act-r12-no-ts",
                )
                self.assertEqual(d1.decision_id, d2.decision_id)
                self.assertEqual(d2.decision_at_ns, 2_000)

                # Explicit different decision_at_ns still raises WorkKernelRefused!
                with self.assertRaisesRegex(WorkKernelRefused, "conflicts"):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        fencing_token=run.fencing_token,
                        agent_id=self.founder_id,
                        action_kind="hold",
                        rationale="Wait for next cycle",
                        evidence=(),
                        decision_at_ns=9_999,
                        action_id="act-r12-no-ts",
                    )

            # 2. Proactive tool wrapper with advancing clock & omitted decision_at_ns
            from dataclasses import replace as dc_replace

            now_box = [3_000]
            clocked_claim = dc_replace(claim, _clock=lambda: now_box[0])
            contrib = build_core_proactive_tool_contribution(
                writer=w,
                claim=clocked_claim,
            )
            handler = contrib.handlers["record_work_decision"]
            r1 = handler(
                {
                    "action_kind": "hold",
                    "rationale": "Tool retry check",
                    "action_id": "act-r12-tool-retry",
                    "evidence": [],
                }
            )
            now_box[0] = 8_000
            r2 = handler(
                {
                    "action_kind": "hold",
                    "rationale": "Tool retry check",
                    "action_id": "act-r12-tool-retry",
                    "evidence": [],
                }
            )
            self.assertEqual(r1["decision_id"], r2["decision_id"])
            self.assertEqual(r2["decision_at_ns"], 3_000)

    def test_pr191_r13_discovery_review_instruction_has_no_invalid_work_run_revision(
        self,
    ) -> None:
        """PR #191 R13 P2: work.discovery_review prompt must never prescribe work_run revision '1'."""
        seen_instructions: list[str] = []

        class _CaptureInstructionAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                seen_instructions.append(request.instruction)
                broker.invoke(
                    "c1",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "rationale": "Baseline review",
                        "evidence": [],
                    },
                )
                return ShiftResult(
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=1,
                    events=(),
                )

        reg = AgentAdapterRegistry()
        reg.register(_CaptureInstructionAdapter())
        duty_reg = build_production_duty_registry(
            adapter_registry=reg, profile_bindings={self.founder_id: self.profile}
        )
        rt = CompanyRuntime(self.store, self.company_id, duty_reg, clock=lambda: 25_000)
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="r13-prompt",
                    now_ns=24_000,
                )
        rt.step(max_dispatches=1, now_ns=25_000)
        self.assertEqual(len(seen_instructions), 1)
        self.assertNotIn("'revision':'1'", seen_instructions[0])
        self.assertIn("evidence=[]", seen_instructions[0])

    def test_pr191_r13_item_linked_discovery_timers_bounded_coalesced_and_budgeted(
        self,
    ) -> None:
        """PR #191 R13 Security P2: item-linked discovery timers are capped at 1 per item, coalesced, and budgeted."""
        from sourdough.runtime.proactive_tools import (
            build_core_proactive_tool_contribution,
        )
        from sourdough.runtime.work_kernel import DEFAULT_DISCOVERY_WAKE_BUDGET

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                # Part 1: Calling set_condition without owner fields on an unscoped discovery item
                # permits at most 1 armed continuation; a 2nd attempt fails closed!
                unscoped_item, _, unscoped_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
            contrib = build_core_proactive_tool_contribution(
                writer=w, claim=unscoped_claim
            )
            set_cond_tool = contrib.handlers["set_condition"]
            first = set_cond_tool({"trigger_kind": "timer", "due_at_ns": 2_000})
            self.assertTrue(first["ok"])
            self.assertEqual(first["work_item_id"], unscoped_item.work_item_id)
            self.assertIsNone(first["responsibility_id"])

            # 2nd, 3rd, 4th calls on the same discovery item MUST be refused!
            with self.assertRaisesRegex(WorkKernelRefused, "at most one armed"):
                set_cond_tool({"trigger_kind": "timer", "due_at_ns": 2_000})

            # Part 2: When work item payload carries a responsibility_id, omitting owner fields
            # auto-binds to that standing responsibility (under its budget & hysteresis).
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                disc_resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Bounded discovery",
                    readable_scope={
                        "max_wakes_per_window": 2,
                        "wake_window_ns": 100_000,
                    },
                    now_ns=1_000,
                )
                complete_work_run(
                    db,
                    unscoped_claim.run_id,
                    unscoped_claim.fencing_token,
                    outcome={"done": True},
                    now_ns=1_500,
                )
                scoped_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="condition",
                    source_id="cond-resp",
                    payload={"responsibility_id": disc_resp.responsibility_id},
                    now_ns=1_600,
                )
                scoped_run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=scoped_item.work_item_id,
                    lease_owner="test-r13",
                    lease_ttl_ns=100_000,
                    now_ns=1_700,
                )
                scoped_claim = WorkRunClaim(
                    company_id=self.company_id,
                    founder_id=self.founder_id,
                    duty_id=scoped_item.work_item_id,
                    run_id=scoped_run.run_id,
                    fencing_token=scoped_run.fencing_token,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    purpose="discovery_review",
                    _clock=1_800,
                )
            scoped_contrib = build_core_proactive_tool_contribution(
                writer=w, claim=scoped_claim
            )
            bound = scoped_contrib.handlers["set_condition"](
                {"trigger_kind": "timer", "due_at_ns": 5_000}
            )
            self.assertEqual(bound["responsibility_id"], disc_resp.responsibility_id)
            self.assertIsNone(bound["work_item_id"])

            # Part 3: Unscoped discovery continuations in evaluate_conditions are bounded by
            # aggregate DEFAULT_DISCOVERY_WAKE_BUDGET and coalesce when an open item exists.
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                admitted1 = evaluate_conditions(db, self.company_id, now_ns=3_000)
                self.assertEqual(len(admitted1), 1)
                # Pre-fill DEFAULT_DISCOVERY_WAKE_BUDGET unscoped condition items in current window
                for idx in range(DEFAULT_DISCOVERY_WAKE_BUDGET):
                    wi, _ = create_work_item(
                        db,
                        company_id=self.company_id,
                        owner_agent_id=self.founder_id,
                        duty_kind="work.discovery_review",
                        source_kind="condition",
                        source_id=f"hist-unscoped-{idx}",
                        payload={"trigger_kind": "timer"},
                        now_ns=3_100 + idx,
                    )
                    db.execute(
                        "UPDATE work_items SET state = 'settled' WHERE work_item_id = ?",
                        (wi.work_item_id,),
                    )
                # Arm one more unscoped continuation -> aggregate budget MUST block admission!
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    work_item_id=admitted1[0].work_item_id,
                    trigger_kind="timer",
                    due_at_ns=4_000,
                    now_ns=3_500,
                )
                # Mark admitted1 settled so coalescing doesn't mask budget check
                db.execute(
                    "UPDATE work_items SET state = 'settled' WHERE work_item_id = ?",
                    (admitted1[0].work_item_id,),
                )
                admitted_over = evaluate_conditions(db, self.company_id, now_ns=4_500)
                self.assertEqual(admitted_over, ())

    def test_pr191_r14_coalescing_different_watched_assets_retains_original_trigger_and_hydrates_both(
        self,
    ) -> None:
        """PR #191 R14 P2: Coalescing different watched publications retains original trigger and hydrates all sources."""
        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, peer_grant = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="pub-peer-r14",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-r14-coalesce",
                (self.founder_id, peer_id),
                publishers=(peer_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, peer_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=peer_id,
                    grant_id=peer_grant,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            da = save_draft(
                w,
                peer_claim,
                peer_claim.purpose,
                name="alpha-r14",
                content="Alpha watched publication content",
                expected_revision=None,
            )
            pub_a = publish_selection(
                w,
                peer_claim,
                peer_claim.purpose,
                Selection(
                    "pub-r14-alpha",
                    "alpha-r14",
                    da.revision,
                    "proj-r14-coalesce",
                    proj_rev,
                    (self.founder_id, peer_id),
                    0,
                    len(da.content),
                ),
            )
            db_draft = save_draft(
                w,
                peer_claim,
                peer_claim.purpose,
                name="beta-r14",
                content="Beta watched publication content",
                expected_revision=None,
            )
            pub_b = publish_selection(
                w,
                peer_claim,
                peer_claim.purpose,
                Selection(
                    "pub-r14-beta",
                    "beta-r14",
                    db_draft.revision,
                    "proj-r14-coalesce",
                    proj_rev,
                    (self.founder_id, peer_id),
                    0,
                    len(db_draft.content),
                ),
            )

            # Part A: Responsibility-backed coalescing of two distinct watched publications
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Watch multiple project publications",
                    readable_scope=["shared"],
                    now_ns=2_000,
                )
                ca = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="shared_asset_revision",
                    source_identity=pub_a.asset_id,
                    max_fires=1,
                    now_ns=2_100,
                )
                cb = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="shared_asset_revision",
                    source_identity=pub_b.asset_id,
                    max_fires=1,
                    now_ns=2_200,
                )
                admitted = evaluate_conditions(db, self.company_id, now_ns=3_000)
                self.assertEqual(len(admitted), 1)
                row_ca = db.execute(
                    "SELECT status, fire_count FROM work_conditions WHERE condition_id = ?",
                    (ca.condition_id,),
                ).fetchone()
                row_cb = db.execute(
                    "SELECT status, fire_count FROM work_conditions WHERE condition_id = ?",
                    (cb.condition_id,),
                ).fetchone()
                self.assertEqual(row_ca, ("triggered", 1))
                self.assertEqual(row_cb, ("triggered", 1))
                payload = json.loads(
                    db.execute(
                        "SELECT payload_json FROM work_items WHERE work_item_id = ?",
                        (admitted[0].work_item_id,),
                    ).fetchone()[0]
                )
                coalesced_sids = [
                    e["source_identity"] for e in payload.get("coalesced_revisions", [])
                ]
                self.assertIn(pub_a.asset_id, coalesced_sids)
                self.assertIn(pub_b.asset_id, coalesced_sids)

        # Verify _run_work_discovery_review hydrates BOTH watched publications!
        seen_receipts: list[dict[str, Any]] = []
        seen_notes: list[str] = []

        class _CaptureHydratedAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                seen_receipts.extend(request.context.open_receipts)
                seen_notes.extend(request.context.history_notes)
                broker.invoke(
                    "c1",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "rationale": "Reviewed both coalesced publications.",
                        "evidence": [],
                    },
                )
                return ShiftResult(
                    output_text="Reviewed.",
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=1,
                    events=(),
                )

        areg = AgentAdapterRegistry()
        areg.register(_CaptureHydratedAdapter())
        dreg = build_production_duty_registry(
            adapter_registry=areg, profile_bindings={self.founder_id: self.profile}
        )
        rt = CompanyRuntime(self.store, self.company_id, dreg, clock=lambda: 4_000)
        rt.step(
            max_dispatches=1,
            now_ns=4_000,
            target_work_item_id=admitted[0].work_item_id,
        )
        hydrated_asset_ids = [
            r["asset_id"]
            for r in seen_receipts
            if isinstance(r, dict) and r.get("kind") == "shared_asset"
        ]
        self.assertIn(pub_a.asset_id, hydrated_asset_ids)
        self.assertIn(pub_b.asset_id, hydrated_asset_ids)
        joined_notes = "\n".join(seen_notes)
        self.assertIn("Alpha watched publication content", joined_notes)
        self.assertIn("Beta watched publication content", joined_notes)

        # Part B: Unscoped coalescing branch also preserves original trigger!
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                parent_a, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="unscoped-parent-a",
                    now_ns=5_000,
                )
                parent_b, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="unscoped-parent-b",
                    now_ns=5_001,
                )
                db.execute(
                    "UPDATE work_items SET state = 'settled' WHERE work_item_id IN (?, ?)",
                    (parent_a.work_item_id, parent_b.work_item_id),
                )
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    work_item_id=parent_a.work_item_id,
                    trigger_kind="shared_asset_revision",
                    source_identity=pub_a.asset_id,
                    max_fires=1,
                    now_ns=5_100,
                )
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    work_item_id=parent_b.work_item_id,
                    trigger_kind="shared_asset_revision",
                    source_identity=pub_b.asset_id,
                    max_fires=1,
                    now_ns=5_200,
                )
                admitted_unscoped = evaluate_conditions(
                    db, self.company_id, now_ns=6_000
                )
                self.assertEqual(len(admitted_unscoped), 1)
                unscoped_payload = json.loads(
                    db.execute(
                        "SELECT payload_json FROM work_items WHERE work_item_id = ?",
                        (admitted_unscoped[0].work_item_id,),
                    ).fetchone()[0]
                )
                unscoped_sids = [
                    e["source_identity"]
                    for e in unscoped_payload.get("coalesced_revisions", [])
                ]
                self.assertIn(pub_a.asset_id, unscoped_sids)
                self.assertIn(pub_b.asset_id, unscoped_sids)

    def test_pr191_r14_security_condition_budget_bypass_vectors_closed(self) -> None:
        """PR #191 R14 Security: close result_dep spoofing, explicit work_item_id escape, and duplicate continuation amplification."""
        from sourdough.runtime.proactive_tools import (
            build_core_proactive_tool_contribution,
        )
        from sourdough.runtime.work_kernel import (
            arm_result_continuation,
            open_peer_request,
            record_peer_request_read,
            resolve_peer_request,
        )
        from sourdough.storage.kernel import append_audit_event

        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, peer_grant = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="sec-peer-r14",
                profile_id=self.profile.profile_id(),
            )
            assign_project(
                w,
                self.grant_id,
                "proj-sec-r14",
                (self.founder_id, peer_id),
                publishers=(self.founder_id, peer_id),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=peer_id,
                    duty_kind="work.peer_response",
                    remit="Answer peer requests",
                    readable_scope={
                        "project_id": "proj-sec-r14",
                        "allowed_requesters": [self.founder_id],
                    },
                    now_ns=900,
                )
                disc_resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Bounded security check",
                    readable_scope={
                        "max_wakes_per_window": 1,
                        "wake_window_ns": 60_000,
                    },
                    now_ns=1_000,
                )
                other_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="other-settled-item",
                    now_ns=1_050,
                )
                db.execute(
                    "UPDATE work_items SET state = 'settled' WHERE work_item_id = ?",
                    (other_item.work_item_id,),
                )
                scoped_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="condition",
                    source_id="sec-cond-origin",
                    payload={"responsibility_id": disc_resp.responsibility_id},
                    now_ns=1_100,
                )
                scoped_run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=scoped_item.work_item_id,
                    lease_owner="test-sec-r14",
                    lease_ttl_ns=100_000,
                    now_ns=1_200,
                )
                scoped_claim = WorkRunClaim(
                    company_id=self.company_id,
                    founder_id=self.founder_id,
                    duty_id=scoped_item.work_item_id,
                    run_id=scoped_run.run_id,
                    fencing_token=scoped_run.fencing_token,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    purpose="discovery_review",
                    _clock=1_300,
                )

            contrib = build_core_proactive_tool_contribution(
                writer=w, claim=scoped_claim
            )
            set_cond_tool = contrib.handlers["set_condition"]

            # 1. Spoofing reserved source_identity='result_dep:...' via set_condition MUST be refused.
            with self.assertRaisesRegex(WorkKernelRefused, "result_dep:"):
                set_cond_tool(
                    {
                        "trigger_kind": "timer",
                        "due_at_ns": 0,
                        "source_identity": "result_dep:spoof",
                    }
                )

            # 2. Linking an unrelated historical work_item_id via set_condition MUST be refused.
            with self.assertRaisesRegex(WorkKernelRefused, "current work item"):
                set_cond_tool(
                    {
                        "trigger_kind": "timer",
                        "due_at_ns": 0,
                        "work_item_id": other_item.work_item_id,
                    }
                )

            # 3. Explicitly passing work_item_id=claim.duty_id on a responsibility-backed item
            # MUST still auto-bind to the active standing responsibility (cannot escape its budget!).
            bound = set_cond_tool(
                {
                    "trigger_kind": "timer",
                    "due_at_ns": 5_000,
                    "work_item_id": scoped_item.work_item_id,
                }
            )
            self.assertEqual(bound["responsibility_id"], disc_resp.responsibility_id)
            self.assertIsNone(bound["work_item_id"])

            # 4. Duplicate arm_result_continuation on the same dependency_asset_id admits at most 1 item,
            # and duplicate peer_response conditions on the same resolved request admit at most 1 item.
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                append_audit_event(
                    db,
                    company_id=self.company_id,
                    domain="assets",
                    entity_type="shared_publication",
                    entity_id="dep-dedupe-r14",
                    event_type="asset.published",
                    actor_id=peer_id,
                    payload={"asset_id": "dep-dedupe-r14"},
                    recorded_at_ns=1_400,
                )
                for _ in range(3):
                    arm_result_continuation(
                        db,
                        company_id=self.company_id,
                        owner_agent_id=self.founder_id,
                        dependency_asset_id="dep-dedupe-r14",
                        due_at_ns=1_500,
                        now_ns=1_450,
                    )
                req, _ = open_peer_request(
                    db,
                    company_id=self.company_id,
                    requester_work_item_id=scoped_item.work_item_id,
                    requester_run_id=scoped_run.run_id,
                    fencing_token=scoped_run.fencing_token,
                    recipient_agent_id=peer_id,
                    question="Check R14 dedupe",
                    expected_decision="confirm",
                    evidence_refs=[],
                    deadline_ns=100_000,
                    request_id="req-r14-dedupe",
                    project_id="proj-sec-r14",
                    now_ns=1_500,
                )
                resp_run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=req.response_work_item_id,
                    lease_owner="peer-worker-r14",
                    lease_ttl_ns=60_000,
                    now_ns=1_550,
                )
                record_peer_request_read(
                    db,
                    company_id=self.company_id,
                    request_id=req.request_id,
                    actor_id=peer_id,
                    response_run_id=resp_run.run_id,
                    response_fencing_token=resp_run.fencing_token,
                    now_ns=1_580,
                )
                resolve_peer_request(
                    db,
                    company_id=self.company_id,
                    request_id=req.request_id,
                    actor_id=peer_id,
                    resolution_state="answered",
                    response_payload={"answer": "Done"},
                    response_run_id=resp_run.run_id,
                    response_fencing_token=resp_run.fencing_token,
                    now_ns=1_600,
                )
                for _ in range(3):
                    set_condition(
                        db,
                        company_id=self.company_id,
                        owner_agent_id=self.founder_id,
                        responsibility_id=disc_resp.responsibility_id,
                        trigger_kind="peer_response",
                        source_identity=f"peer_response:{req.request_id}",
                        now_ns=1_650,
                    )
                admitted_all = evaluate_conditions(db, self.company_id, now_ns=2_000)
                dep_items = [
                    it for it in admitted_all if "result_dep:" in it.payload_json
                ]
                peer_items = [
                    it
                    for it in admitted_all
                    if f"peer_response:{req.request_id}" in it.payload_json
                ]
                self.assertEqual(len(dep_items), 1)
                self.assertEqual(len(peer_items), 1)

    def test_pr191_r16_security_work_decision_action_kind_enum_and_board_projection_redaction(
        self,
    ) -> None:
        """PR #191 R16 Security P2: constrain action_kind to server enum and redact legacy/adjacent free-form fields in Board projections."""
        from sourdough.ipc.projections import (
            read_agent_detail_projection,
            read_system_audit_projection,
        )
        from sourdough.runtime.proactive_tools import (
            build_core_proactive_tool_contribution,
        )

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
            contrib = build_core_proactive_tool_contribution(writer=w, claim=claim)
            # 1. Free-form action_kind is rejected by record_work_decision!
            with self.assertRaisesRegex(
                WorkKernelRefused, "Unsupported work_decision action_kind"
            ):
                contrib.handlers["record_work_decision"](
                    {
                        "action_kind": "PRIVATE_DRAFT_CANARY_IN_ACTION_KIND_8819",
                        "rationale": "Attempted exfil",
                        "evidence": [],
                    }
                )
            # 2. Free-form source_identity on timer condition is rejected by set_condition!
            with self.assertRaisesRegex(
                WorkKernelRefused, "Timer conditions assign source_identity"
            ):
                contrib.handlers["set_condition"](
                    {
                        "trigger_kind": "timer",
                        "due_at_ns": 5_000,
                        "source_identity": "PRIVATE_TIMER_CANARY_666",
                    }
                )
            # 3. Valid enum action_kind succeeds cleanly!
            ok_dec = contrib.handlers["record_work_decision"](
                {
                    "action_kind": "hold",
                    "rationale": "Valid decision",
                    "action_id": "CANARY_ACTION_ID_777",
                    "evidence": [],
                }
            )
            self.assertEqual(ok_dec["action_kind"], "hold")

            # 4. Seed legacy rows with unconstrained action_kind, outcome_json, and timer source_identity
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    """INSERT INTO work_decision (
                           decision_id, company_id, work_item_id, run_id, agent_id,
                           action_id, action_kind, evidence_refs_json, evidence_digest,
                           rationale, next_condition_id, active_snapshot_id, decision_at_ns
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', 'd0', 'legacy', NULL, NULL, 2_500)""",
                    (
                        "dec-legacy-r16",
                        self.company_id,
                        item.work_item_id,
                        run.run_id,
                        self.founder_id,
                        "LEGACY_ACTION_ID_CANARY_555",
                        "PRIVATE_DRAFT_CANARY_IN_ACTION_KIND_8819",
                    ),
                )
                db.execute(
                    """UPDATE work_runs
                       SET outcome_json = ?
                       WHERE company_id = ? AND run_id = ?""",
                    (
                        json.dumps(
                            {"action_kind": "PRIVATE_DRAFT_CANARY_IN_ACTION_KIND_8819"}
                        ),
                        self.company_id,
                        run.run_id,
                    ),
                )
                db.execute(
                    """INSERT INTO work_conditions (
                           condition_id, company_id, owner_agent_id, responsibility_id,
                           work_item_id, condition_revision, trigger_kind, due_at_ns,
                           source_identity, expected_revision, expires_at_ns,
                           cooldown_ns, max_fires, fire_count, status,
                           created_at_ns, updated_at_ns
                       ) VALUES ('cond-legacy-r16', ?, ?, NULL, ?, 1, 'timer', 9999,
                                 'PRIVATE_TIMER_CANARY_666', NULL, NULL, 0, NULL, 0, 'armed', 2000, 2000)""",
                    (self.company_id, self.founder_id, item.work_item_id),
                )

        detail, _ = read_agent_detail_projection(
            self.store, self.company_id, self.founder_id, domain_plugins=()
        )
        audit, _ = read_system_audit_projection(
            self.store, self.company_id, domain_plugins=()
        )
        assert detail is not None
        detail_dump = json.dumps(detail)
        audit_dump = json.dumps(audit)
        for canary in (
            "PRIVATE_DRAFT_CANARY_IN_ACTION_KIND_8819",
            "CANARY_ACTION_ID_777",
            "LEGACY_ACTION_ID_CANARY_555",
            "PRIVATE_TIMER_CANARY_666",
        ):
            self.assertNotIn(canary, detail_dump)
            self.assertNotIn(canary, audit_dump)

        by_id = {d["decision_id"]: d for d in detail["tabs"]["work_decisions"]}
        self.assertEqual(by_id["dec-legacy-r16"]["action_kind"], "redacted")
        self.assertTrue(by_id["dec-legacy-r16"]["action_kind_redacted"])
        self.assertEqual(by_id[ok_dec["decision_id"]]["action_kind"], "hold")
        self.assertFalse(by_id[ok_dec["decision_id"]]["action_kind_redacted"])
        self.assertTrue(by_id[ok_dec["decision_id"]]["action_id_redacted"])

    def test_pr191_r17_security_result_continuations_bound_to_owner_capped_and_budgeted(
        self,
    ) -> None:
        """PR #191 R17 Security P2: arm_result_continuation binds to bounded owner, enforces caps, and charges discovery wake budgets."""
        from sourdough.runtime.proactive_tools import (
            build_core_proactive_tool_contribution,
        )
        from sourdough.runtime.work_kernel import (
            DEFAULT_DISCOVERY_WAKE_BUDGET,
            arm_result_continuation,
        )
        from sourdough.storage.kernel import append_audit_event

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                for idx in range(5):
                    append_audit_event(
                        db,
                        company_id=self.company_id,
                        domain="assets",
                        entity_type="shared_publication",
                        entity_id=f"r17-dep-asset-{idx}",
                        event_type="asset.published",
                        actor_id=self.founder_id,
                        payload={"asset_id": f"r17-dep-asset-{idx}"},
                        recorded_at_ns=500 + idx,
                    )

                # Part 1: Unscoped discovery shift -> arm_result_continuation with omitted owners
                # auto-binds to claim.duty_id and permits at most ONE continuation per item!
                unscoped_item, _, unscoped_claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
            unscoped_tool = build_core_proactive_tool_contribution(
                writer=w, claim=unscoped_claim
            ).handlers["arm_result_continuation"]
            first_dep = unscoped_tool(
                {"dependency_asset_id": "r17-dep-asset-0", "due_at_ns": 1_500}
            )
            self.assertEqual(first_dep["work_item_id"], unscoped_item.work_item_id)
            self.assertIsNone(first_dep["responsibility_id"])
            # 2nd call for a DIFFERENT authorized asset in the same shift MUST be refused!
            with self.assertRaisesRegex(WorkKernelRefused, "at most one armed"):
                unscoped_tool(
                    {"dependency_asset_id": "r17-dep-asset-1", "due_at_ns": 1_500}
                )

            # Part 2: Responsibility-backed discovery shift -> auto-binds responsibility_id,
            # caps armed result_dep continuations at wake_budget, and writes work_discovery_state rows!
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db,
                    unscoped_claim.run_id,
                    unscoped_claim.fencing_token,
                    outcome={"done": True},
                    now_ns=1_200,
                )
                disc_resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Bounded result deps",
                    readable_scope={
                        "max_wakes_per_window": 2,
                        "wake_window_ns": 100_000,
                    },
                    now_ns=1_250,
                )
                scoped_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="condition",
                    source_id="r17-resp-origin",
                    payload={"responsibility_id": disc_resp.responsibility_id},
                    now_ns=1_300,
                )
                scoped_run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=scoped_item.work_item_id,
                    lease_owner="test-r17",
                    lease_ttl_ns=100_000,
                    now_ns=1_350,
                )
                scoped_claim = WorkRunClaim(
                    company_id=self.company_id,
                    founder_id=self.founder_id,
                    duty_id=scoped_item.work_item_id,
                    run_id=scoped_run.run_id,
                    fencing_token=scoped_run.fencing_token,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    purpose="discovery_review",
                    _clock=1_400,
                )
            scoped_tool = build_core_proactive_tool_contribution(
                writer=w, claim=scoped_claim
            ).handlers["arm_result_continuation"]
            dep_r1 = scoped_tool(
                {"dependency_asset_id": "r17-dep-asset-1", "due_at_ns": 1_500}
            )
            dep_r2 = scoped_tool(
                {"dependency_asset_id": "r17-dep-asset-2", "due_at_ns": 1_500}
            )
            self.assertEqual(dep_r1["responsibility_id"], disc_resp.responsibility_id)
            self.assertEqual(dep_r2["responsibility_id"], disc_resp.responsibility_id)
            self.assertIsNone(dep_r1["work_item_id"])
            # 3rd armed result_dep exceeds max_wakes_per_window=2 -> MUST be refused!
            with self.assertRaisesRegex(WorkKernelRefused, "cap reached"):
                scoped_tool(
                    {"dependency_asset_id": "r17-dep-asset-3", "due_at_ns": 1_500}
                )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                admitted_resp = evaluate_conditions(db, self.company_id, now_ns=2_000)
                # 1 unscoped + 2 responsibility-backed admitted (3 total)
                self.assertEqual(len(admitted_resp), 3)
                budget_row = db.execute(
                    """SELECT window_wakes FROM work_discovery_state
                       WHERE company_id = ? AND responsibility_id = ?""",
                    (self.company_id, disc_resp.responsibility_id),
                ).fetchone()
                self.assertEqual(budget_row, (2,))
                # Arming one more after budget=2 is spent MUST NOT admit!
                arm_result_continuation(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    dependency_asset_id="r17-dep-asset-3",
                    due_at_ns=2_100,
                    responsibility_id=disc_resp.responsibility_id,
                    now_ns=2_050,
                )
                self.assertEqual(
                    evaluate_conditions(db, self.company_id, now_ns=2_200), ()
                )

                # Part 3: Unscoped/parentless result_dep continuations are ALSO capped at
                # DEFAULT_DISCOVERY_WAKE_BUDGET in the rolling window!
                for idx in range(DEFAULT_DISCOVERY_WAKE_BUDGET + 2):
                    append_audit_event(
                        db,
                        company_id=self.company_id,
                        domain="assets",
                        entity_type="shared_publication",
                        entity_id=f"r17-unscoped-dep-{idx}",
                        event_type="asset.published",
                        actor_id=self.founder_id,
                        payload={"asset_id": f"r17-unscoped-dep-{idx}"},
                        recorded_at_ns=2_300 + idx,
                    )
                    arm_result_continuation(
                        db,
                        company_id=self.company_id,
                        owner_agent_id=self.founder_id,
                        dependency_asset_id=f"r17-unscoped-dep-{idx}",
                        due_at_ns=2_400,
                        now_ns=2_350 + idx,
                    )
                admitted_unscoped_burst = evaluate_conditions(
                    db, self.company_id, now_ns=2_500
                )
                # 1 unscoped was already admitted at t=2_000 -> at most DEFAULT_DISCOVERY_WAKE_BUDGET - 1 more!
                self.assertEqual(
                    len(admitted_unscoped_burst), DEFAULT_DISCOVERY_WAKE_BUDGET - 1
                )

    def test_pr191_r18_private_draft_citations_persisted_as_opaque_refs_in_company_sqlite(
        self,
    ) -> None:
        """PR #191 R18 P2: private draft names must be stored as opaque digests in method_delta and work_decision."""
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, run, f_claim = _open_work_run_claim(
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
                f_claim,
                f_claim.purpose,
                name="secret-acquisition-plan",
                content="Confidential M&A notes.",
                expected_revision=None,
            )
            # 1. Valid private_draft citation on one_agent method_delta -> stored as opaque digest!
            cand = distill_method_delta(
                w,
                f_claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-r18-priv-ref",
                trigger_spec={"duty_kind": "work.discovery_review"},
                body="Private heuristic.",
                audience_scope="one_agent",
                source_refs=["private_draft:secret-acquisition-plan"],
                now_ns=1_500,
            )
            self.assertEqual(cand.status, "candidate")
            self.assertTrue(cand.source_refs[0].startswith("private_draft:opaque:"))
            self.assertNotIn("secret-acquisition-plan", cand.source_refs[0])

            # 2. Missing private_draft citation -> uncertainty_note also uses opaque digest!
            missing_cand = distill_method_delta(
                w,
                f_claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-r18-missing-ref",
                trigger_spec={"duty_kind": "work.discovery_review"},
                body="Missing draft heuristic.",
                audience_scope="one_agent",
                source_refs=["private_draft:missing-secret-target"],
                now_ns=1_600,
            )
            self.assertEqual(missing_cand.status, "disputed")
            self.assertNotIn("missing-secret-target", missing_cand.uncertainty_note)

            # 3. Redaction report stripped_items also uses opaque refs (no raw filenames!).
            red = redact_for_shared_scope(
                w,
                f_claim,
                raw_body="See desks/agent/secret-acquisition-plan.json for details.",
                source_refs=[
                    "private_draft:secret-acquisition-plan",
                    f"work_run:{f_claim.run_id}",
                ],
                target_scope="company",
                now_ns=1_700,
            )
            self.assertNotIn("secret-acquisition-plan", json.dumps(red.stripped_items))

            # 4. work_decision citing private_draft validates real file, persists opaque ref!
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="Verified private draft.",
                    evidence=(
                        DecisionEvidence(
                            "private_draft",
                            "secret-acquisition-plan",
                            draft.revision,
                            1_800,
                            1_800,
                        ),
                    ),
                    decision_at_ns=1_900,
                )

        with closing(open_reader(self.store)) as ro_db, read_tx(ro_db):
            md_rows = ro_db.execute(
                "SELECT source_refs_json, uncertainty_note, redaction_report_json FROM method_delta"
            ).fetchall()
            wd_rows = ro_db.execute(
                "SELECT evidence_refs_json FROM work_decision"
            ).fetchall()
            audit_rows = ro_db.execute(
                "SELECT payload_json FROM audit_event"
            ).fetchall()
        combined = json.dumps({"md": md_rows, "wd": wd_rows, "audit": audit_rows})
        self.assertNotIn("secret-acquisition-plan", combined)
        self.assertNotIn("missing-secret-target", combined)
        self.assertIn("private_draft:opaque:", json.dumps(wd_rows))

    def test_pr191_r24_security_decision_action_id_opaque_mapped_in_audit_and_db(
        self,
    ) -> None:
        """PR #191 R24 Security #21: record_work_decision opaque-maps action_id before audit persistence and LLM tool storage."""
        now_ns = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="r24-action-id-canary",
                    now_ns=now_ns,
                )

        seen: dict[str, Any] = {}

        class _CanaryActionIdAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ) -> ShiftResult:
                r1 = broker.invoke(
                    "dec-1",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "action_id": "PRIVATE_DRAFT_ACTION_ID_CANARY_314159",
                        "rationale": "First attempt.",
                        "evidence": [],
                    },
                )
                r2 = broker.invoke(
                    "dec-2",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "action_id": "PRIVATE_DRAFT_ACTION_ID_CANARY_314159",
                        "rationale": "First attempt.",
                        "evidence": [],
                    },
                )
                seen["r1"] = r1
                seen["r2"] = r2
                return ShiftResult(
                    output_text="Done.",
                    usage=TokenUsage(input_tokens=20, output_tokens=5),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=broker.call_count,
                    events=(),
                )

        areg = AgentAdapterRegistry()
        areg.register(_CanaryActionIdAdapter())
        dreg = build_production_duty_registry(
            adapter_registry=areg,
            profile_bindings={self.founder_id: self.profile},
        )
        rt = CompanyRuntime(
            self.store, self.company_id, dreg, clock=lambda: now_ns + 500
        )
        rt.step(
            max_dispatches=1,
            now_ns=now_ns + 500,
            target_work_item_id=item.work_item_id,
        )
        self.assertTrue(seen["r1"].get("ok"))
        self.assertEqual(seen["r1"].get("decision_id"), seen["r2"].get("decision_id"))
        with closing(open_reader(self.store)) as ro_db, read_tx(ro_db):
            audits = ro_db.execute(
                "SELECT payload_json FROM audit_event WHERE event_type = 'work.decision_recorded'"
            ).fetchall()
            decs = ro_db.execute("SELECT action_id FROM work_decision").fetchall()
        self.assertNotIn("PRIVATE_DRAFT_ACTION_ID_CANARY_314159", json.dumps(audits))
        self.assertNotIn("PRIVATE_DRAFT_ACTION_ID_CANARY_314159", json.dumps(decs))
        self.assertTrue(
            json.loads(str(audits[0][0]))["action_id"].startswith("action:")
        )


if __name__ == "__main__":
    unittest.main()
