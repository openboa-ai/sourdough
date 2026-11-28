"""Peer request fencing, work handoff lifecycle, inbox cursor validation, and CEO messaging hardening tests."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from hashlib import sha256
from time import time_ns

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
from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    bind_board_principal,
    post_ceo_message,
)
from sourdough.company_assets import Selection, assign_project, publish_selection
from sourdough.conversation_delivery import read_inbox
from sourdough.conversations import Post, append_message, read_messages
from sourdough.duty_run import DutyRefused
from sourdough.retirement import (
    TERMINATION_SCOPE,
    Retirement,
    employment_revision,
    retire_agent,
)
from sourdough.runtime import (
    CompanyRuntime,
    build_production_duty_registry,
    create_work_item,
)
from sourdough.runtime.collab_tools import build_core_collab_tool_contribution
from sourdough.runtime.handlers import sync_board_triggers
from sourdough.runtime.work_kernel import (
    RunFenced,
    complete_work_run,
    evaluate_conditions,
    open_peer_request,
    set_condition,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.work_handoff import HandoffOffer, accept_handoff, offer_handoff
from sourdough.work_peer_response import (
    answer_peer_question,
    claim_peer_response,
    read_peer_question,
)
from sourdough.writer_lease import hold_company_writer

from ._fixtures import (
    HardeningTestCase,
    _hire_active_agent,
    _open_work_run_claim,
)


class TestHardeningCollabPeerHandoff(HardeningTestCase):
    def test_p2_01_open_peer_request_fencing_before_replay(self) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="peer-p201",
                profile_id=self.profile.profile_id(),
            )
            assign_project(
                w,
                self.grant_id,
                "proj-p201",
                (self.founder_id, peer_id),
                publishers=(self.founder_id,),
                peer_responders=(peer_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, run, _claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
                req, replayed = open_peer_request(
                    db,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    recipient_agent_id=peer_id,
                    question="Check Q?",
                    expected_decision="yes_no",
                    evidence_refs=[],
                    deadline_ns=90_000,
                    request_id="req-p201-1",
                    request_key="rk-p201",
                    project_id="proj-p201",
                    now_ns=1_500,
                )
                self.assertFalse(replayed)
                # Stale fencing_token must raise RunFenced rather than returning replay
                with self.assertRaises(RunFenced):
                    open_peer_request(
                        db,
                        company_id=self.company_id,
                        requester_work_item_id=item.work_item_id,
                        requester_run_id=run.run_id,
                        fencing_token=999999,
                        recipient_agent_id=peer_id,
                        question="Check Q?",
                        expected_decision="yes_no",
                        evidence_refs=[],
                        deadline_ns=90_000,
                        request_id="req-p201-1",
                        request_key="rk-p201",
                        project_id="proj-p201",
                        now_ns=1_600,
                    )
                # Legitimate waiting-run replay with exact token still succeeds
                req2, replayed2 = open_peer_request(
                    db,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    recipient_agent_id=peer_id,
                    question="Check Q?",
                    expected_decision="yes_no",
                    evidence_refs=[],
                    deadline_ns=90_000,
                    request_id="req-p201-dup",
                    request_key="rk-p201",
                    project_id="proj-p201",
                    now_ns=1_600,
                )
                self.assertTrue(replayed2)
                self.assertEqual(req2.request_id, req.request_id)

    def test_p2_02_handoff_acceptance_cancels_armed_work_conditions(self) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            sender_id, sender_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="sender-p202",
                profile_id=self.profile.profile_id(),
            )
            recip_id, recip_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="recip-p202",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-p202",
                (sender_id, recip_id),
                publishers=(sender_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                s_item, _, cs = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=sender_id,
                    grant_id=sender_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
                cond = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=sender_id,
                    work_item_id=s_item.work_item_id,
                    trigger_kind="timer",
                    due_at_ns=5_000,
                    now_ns=1_500,
                )
            d = save_draft(
                w,
                cs,
                cs.purpose,
                name="ho-asset",
                content="Shared handoff artifact",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                cs,
                cs.purpose,
                Selection(
                    "pub-p202",
                    "ho-asset",
                    d.revision,
                    "proj-p202",
                    proj_rev,
                    (sender_id, recip_id),
                    0,
                    len(d.content),
                ),
            )
            offer_id = offer_handoff(
                w,
                cs,
                HandoffOffer(
                    "ho-req-p202", recip_id, asset.asset_id, 100, "Verify artifact"
                ),
                now=2,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, cr = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=recip_id,
                    grant_id=recip_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=3_000,
                )
            accept_handoff(w, cr, cr.purpose, offer_id, now=3)
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                admitted = evaluate_conditions(db, self.company_id, now_ns=10_000)
                c_status = db.execute(
                    "SELECT status FROM work_conditions WHERE condition_id = ?",
                    (cond.condition_id,),
                ).fetchone()[0]
            self.assertEqual(admitted, ())
            self.assertEqual(c_status, "cancelled")

    def test_p2_03_answer_peer_question_replay_enforces_active_agent_and_grant(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            req_id, req_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="req-p203",
                profile_id=self.profile.profile_id(),
            )
            resp_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="resp-p203",
                profile_id=self.profile.profile_id(),
            )
            assign_project(
                w,
                self.grant_id,
                "proj-p203",
                (req_id, resp_id),
                publishers=(req_id,),
                peer_responders=(resp_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, run, _ = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=req_id,
                    grant_id=req_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
                open_peer_request(
                    db,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    recipient_agent_id=resp_id,
                    question="Approve?",
                    expected_decision="yes_no",
                    evidence_refs=[],
                    deadline_ns=90_000_000_000,
                    request_id="pr-p203",
                    request_key="rk-p203",
                    project_id="proj-p203",
                    now_ns=2_000,
                )
            cb = claim_peer_response(w, self.profile.profile_id(), now=5)
            assert cb is not None
            read_peer_question(w, cb, now=6)
            res = answer_peer_question(w, cb, "answered", "Looks good", now=7)
            self.assertEqual(res["outcome"], "answered")

            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "UPDATE work_runs SET status = 'cancelled' WHERE run_id = ?",
                    (cb.run_id,),
                )
            with self.assertRaises(DutyRefused):
                answer_peer_question(w, cb, "answered", "Looks good", now=8)
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "UPDATE work_runs SET status = 'completed' WHERE run_id = ?",
                    (cb.run_id,),
                )
            self.assertEqual(
                answer_peer_question(w, cb, "answered", "Looks good", now=8)["outcome"],
                "answered",
            )

            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "UPDATE standing_grant SET scope = ? WHERE id = ?",
                    (TERMINATION_SCOPE, self.grant_id),
                )
            emp_rev = employment_revision(w, self.founder_id, self.grant_id, resp_id)
            retire_agent(
                w,
                self.founder_id,
                self.grant_id,
                Retirement(
                    request_id="ret-p203",
                    agent_id=resp_id,
                    employment_revision=emp_rev,
                    reason="offboard",
                ),
            )
            with self.assertRaises(DutyRefused):
                answer_peer_question(w, cb, "answered", "Looks good", now=9)

    def test_p2_04_read_inbox_scoped_project_pagination_and_cursor_validation(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, peer_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="peer-p204",
                profile_id=self.profile.profile_id(),
            )
            rev_other = assign_project(
                w,
                self.grant_id,
                "proj-other",
                (self.founder_id, peer_id),
                publishers=(self.founder_id,),
            )
            rev_peer = assign_project(
                w,
                self.grant_id,
                "proj-peer",
                (self.founder_id, peer_id),
                publishers=(self.founder_id,),
            )
            ca = None
            for idx in range(17):
                if idx % 8 == 0:
                    if ca is not None:
                        with (
                            closing(open_writer(self.store, lease=w)) as db,
                            write_tx(db),
                        ):
                            complete_work_run(
                                db,
                                ca.run_id,
                                ca.fencing_token,
                                outcome={"ok": True},
                                now_ns=time_ns() + idx,
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
                            now_ns=time_ns() + idx,
                        )
                assert ca is not None
                append_message(
                    w,
                    ca,
                    ca.purpose,
                    Post("proj-other", rev_other, f"req-other-{idx}", f"Msg {idx}", ""),
                )
            assert ca is not None
            target_msg = append_message(
                w,
                ca,
                ca.purpose,
                Post("proj-peer", rev_peer, "req-peer-1", "Target msg", ""),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db,
                    ca.run_id,
                    ca.fencing_token,
                    outcome={"ok": True},
                    now_ns=time_ns() + 100,
                )
                _, _, cp = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=peer_id,
                    grant_id=peer_g,
                    grant_scope="assessment",
                    duty_kind="work.peer_response",
                    purpose="peer_response",
                    now_ns=time_ns() + 200,
                )
            contrib = build_core_collab_tool_contribution(
                writer=w,
                claim=cp,
                purpose=cp.purpose,
                scoped_project_id="proj-peer",
            )
            page1 = contrib.handlers["read_inbox"]({})
            self.assertEqual(len(page1["deliveries"]), 1)
            self.assertEqual(page1["deliveries"][0]["project_id"], "proj-peer")
            self.assertEqual(page1["next_cursor"], "")

            with self.assertRaises(DutyRefused):
                read_inbox(w, cp, cp.purpose, "f" * 64)
            with self.assertRaises(DutyRefused):
                contrib.handlers["read_inbox"](
                    {
                        "after": sha256(
                            f"{target_msg.message_id}:{peer_id}".encode()
                        ).hexdigest()[:-1]
                        + (
                            "0"
                            if sha256(
                                f"{target_msg.message_id}:{peer_id}".encode()
                            ).hexdigest()[-1]
                            != "0"
                            else "1"
                        )
                    }
                )

    def test_p2_04_read_messages_rejects_unknown_foreign_and_non_audience_cursors(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, peer_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="peer-msg-cur",
                profile_id=self.profile.profile_id(),
            )
            third_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="third-msg-cur",
                profile_id=self.profile.profile_id(),
            )
            rev_a_initial = assign_project(
                w,
                self.grant_id,
                "proj-msg-a",
                (self.founder_id, third_id),
                publishers=(self.founder_id,),
            )
            rev_b = assign_project(
                w,
                self.grant_id,
                "proj-msg-b",
                (self.founder_id, peer_id),
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
                    now_ns=time_ns(),
                )
            msg_a_hidden_from_peer = append_message(
                w,
                ca,
                ca.purpose,
                Post(
                    "proj-msg-a",
                    rev_a_initial,
                    "req-hid-1",
                    "Only third before peer joined",
                    "",
                ),
            )
            rev_a = assign_project(
                w,
                self.grant_id,
                "proj-msg-a",
                (self.founder_id, peer_id, third_id),
                publishers=(self.founder_id,),
                expected_revision=rev_a_initial,
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
                    now_ns=time_ns(),
                )
            msg_a_visible = append_message(
                w,
                ca,
                ca.purpose,
                Post("proj-msg-a", rev_a, "req-vis-1", "Visible to all", ""),
            )
            msg_b_other_proj = append_message(
                w,
                ca,
                ca.purpose,
                Post("proj-msg-b", rev_b, "req-b-1", "Other proj", ""),
            )

            # Valid visible cursor returns subsequent messages cleanly
            page, _ = read_messages(
                w, cp, cp.purpose, "proj-msg-a", msg_a_visible.message_id
            )
            self.assertEqual(page, ())

            # Unknown, cross-project, and non-audience cursors must raise DutyRefused
            for bad_cursor in (
                "nonexistent-msg-cursor",
                msg_b_other_proj.message_id,
                msg_a_hidden_from_peer.message_id,
            ):
                with self.subTest(bad_cursor=bad_cursor):
                    with self.assertRaises(DutyRefused):
                        read_messages(w, cp, cp.purpose, "proj-msg-a", bad_cursor)

    def test_p2_05_discovery_review_excludes_handoff_tools(self) -> None:
        seen_tools: dict[str, list[str]] = {}

        class _InspectAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                seen_tools[request.run_id] = [t.name for t in request.tools]
                if "record_work_decision" in seen_tools[request.run_id]:
                    broker.invoke(
                        "c1",
                        "record_work_decision",
                        {
                            "action_kind": "noop",
                            "rationale": "Checked surface",
                            "evidence": [],
                            "action_id": f"act-{request.run_id}",
                        },
                    )
                return ShiftResult(
                    output_text="ok",
                    usage=TokenUsage(1, 1),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=1,
                    events=(),
                )

        reg = AgentAdapterRegistry()
        reg.register(_InspectAdapter())
        duty_reg = build_production_duty_registry(
            adapter_registry=reg, profile_bindings={self.founder_id: self.profile}
        )
        rt = CompanyRuntime(self.store, self.company_id, duty_reg, clock=lambda: 5_000)

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                disc_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="disc-surface",
                    now_ns=4_000,
                )
                assess_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.assessment_review",
                    source_kind="manual",
                    source_id="assess-surface",
                    now_ns=4_100,
                )
        s1 = rt.step(max_dispatches=2, now_ns=5_000)
        self.assertEqual(len(s1.executed_runs), 2)
        with closing(open_reader(self.store)) as db, read_tx(db):
            runs_by_item = {
                str(r[0]): str(r[1])
                for r in db.execute(
                    "SELECT work_item_id, run_id FROM work_runs WHERE company_id = ?",
                    (self.company_id,),
                ).fetchall()
            }
        disc_tools = seen_tools[runs_by_item[disc_item.work_item_id]]
        assess_tools = seen_tools[runs_by_item[assess_item.work_item_id]]
        for ho_tool in (
            "offer_handoff",
            "discover_handoffs",
            "accept_handoff",
            "decline_handoff",
        ):
            self.assertNotIn(ho_tool, disc_tools)
            self.assertIn(ho_tool, assess_tools)

    def test_p2_06_accept_handoff_same_run_replay_with_original_state_version(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            sender_id, sender_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="sender-p206",
                profile_id=self.profile.profile_id(),
            )
            recip_id, recip_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="recip-p206",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-p206",
                (sender_id, recip_id),
                publishers=(sender_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, cs = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=sender_id,
                    grant_id=sender_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            d = save_draft(
                w,
                cs,
                cs.purpose,
                name="ho-206",
                content="Handoff data",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                cs,
                cs.purpose,
                Selection(
                    "pub-206",
                    "ho-206",
                    d.revision,
                    "proj-p206",
                    proj_rev,
                    (sender_id, recip_id),
                    0,
                    12,
                ),
            )
            offer_id = offer_handoff(
                w,
                cs,
                HandoffOffer("ho-req-206", recip_id, asset.asset_id, 100, "Follow up"),
                now=2,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, cr = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=recip_id,
                    grant_id=recip_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=3_000,
                )
            succ1 = accept_handoff(
                w, cr, cr.purpose, offer_id, now=3, expected_state_version=1
            )
            succ2 = accept_handoff(
                w, cr, cr.purpose, offer_id, now=4, expected_state_version=1
            )
            succ3 = accept_handoff(
                w, cr, cr.purpose, offer_id, now=4, expected_state_version=2
            )
            self.assertEqual(succ1, succ2)
            self.assertEqual(succ1, succ3)
            with self.assertRaises(DutyRefused):
                accept_handoff(
                    w, cr, cr.purpose, offer_id, now=4, expected_state_version=99
                )

    def test_gp3_4_ceo_respond_message_idempotent_across_crash_recovery(self) -> None:
        class _CeoAdapter(AgentRuntimeAdapter):
            def __init__(self) -> None:
                self.calls = 0

            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                self.calls += 1
                return ShiftResult(
                    output_text="Confirmed by CEO.",
                    usage=TokenUsage(2, 3),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=0,
                    events=(),
                )

        reg = AgentAdapterRegistry()
        reg.register(_CeoAdapter())
        duty_reg = build_production_duty_registry(
            adapter_registry=reg, profile_bindings={self.founder_id: self.profile}
        )
        now_box = [10_000]
        rt = CompanyRuntime(
            self.store,
            self.company_id,
            duty_reg,
            lease_ttl_ns=1_000,
            clock=lambda: now_box[0],
        )
        with hold_company_writer(self.store, self.company_id) as w:
            bind_board_principal(
                w, principal_id="board-1", role_name=BOARD_CONTROL_ROLE
            )
            res = post_ceo_message(
                w,
                command_id="cmd-board-1",
                target_agent_id=self.founder_id,
                charter_revision="charter-v1",
                body="Status update?",
                principal_hint="board-1",
                now_ns=5_000,
            )
            inbound_id = res.result["message_id"]
            sync_board_triggers(w, self.company_id, now_ns=5_000)

        # Crash on first complete_work_run AFTER record_ceo_reply commits
        import sourdough.runtime.engine as eng_mod

        orig_complete = eng_mod.complete_work_run
        crash_once = [True]

        def _crash_once(*args, **kwargs):
            if crash_once[0]:
                crash_once[0] = False
                raise sqlite3.OperationalError(
                    "simulated crash before complete_work_run"
                )
            return orig_complete(*args, **kwargs)

        eng_mod.complete_work_run = _crash_once
        try:
            with self.assertRaises(sqlite3.OperationalError):
                rt.step(max_dispatches=1, now_ns=10_000)
        finally:
            eng_mod.complete_work_run = orig_complete

        # Advance past lease TTL -> reclaim & re-run -> idempotent!
        now_box[0] = 20_000
        rt.step(max_dispatches=1, now_ns=20_000)
        with closing(open_reader(self.store)) as db, read_tx(db):
            replies = db.execute(
                """SELECT message_id, body FROM ceo_message
                   WHERE company_id = ? AND sender_kind = 'ceo'""",
                (self.company_id,),
            ).fetchall()
            inbound_state = db.execute(
                "SELECT delivery_state FROM ceo_message WHERE message_id = ?",
                (inbound_id,),
            ).fetchone()[0]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0][1], "Confirmed by CEO.")
        self.assertEqual(inbound_state, "delivered")

    def test_pr191_r2_migration_preserves_accepted_handoff_state(self) -> None:
        """PR #191 R2 P2: migrating legacy work_handoff rows preserves accepted state."""
        from sourdough.storage.migration import rebuild_core_v1_tables
        from sourdough.work_handoff import accepted_handoff

        with hold_company_writer(self.store, self.company_id) as w:
            recip_id, recip_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="mig-recip",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "mig-proj",
                (self.founder_id, recip_id),
                publishers=(self.founder_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                orig_item, orig_run, cs = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
                succ_item, succ_run, cr = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=recip_id,
                    grant_id=recip_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=2_000,
                )
            draft = save_draft(
                w,
                cs,
                cs.purpose,
                name="mig-doc",
                content="Handoff migration content",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                cs,
                cs.purpose,
                Selection(
                    "mig-pub",
                    "mig-doc",
                    draft.revision,
                    "mig-proj",
                    proj_rev,
                    (self.founder_id, recip_id),
                    0,
                    25,
                ),
            )
            # Simulate pre-state-machine work_handoff table (no state / state_version columns)
            with closing(sqlite3.connect(self.store, isolation_level=None)) as raw:
                raw.execute("PRAGMA foreign_keys = OFF")
                raw.execute("BEGIN IMMEDIATE")
                raw.execute("DROP TABLE work_handoff")
                raw.execute(
                    """CREATE TABLE work_handoff (
                        offer_id TEXT PRIMARY KEY NOT NULL,
                        company_id TEXT NOT NULL REFERENCES company(id),
                        request_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL REFERENCES agent(id),
                        sender_run_id TEXT NOT NULL REFERENCES work_runs(run_id),
                        original_work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
                        recipient_id TEXT NOT NULL REFERENCES agent(id),
                        asset_id TEXT NOT NULL REFERENCES shared_publication(asset_id),
                        expires_at INTEGER NOT NULL,
                        expected_result TEXT NOT NULL,
                        successor_work_item_id TEXT REFERENCES work_items(work_item_id),
                        accepted_run_id TEXT REFERENCES work_runs(run_id),
                        UNIQUE (sender_id, request_id)
                    ) STRICT"""
                )
                raw.execute(
                    """INSERT INTO work_handoff VALUES
                       ('off-acc', ?, 'req-acc', ?, ?, ?, ?, ?, 1000, 'deliver', ?, ?),
                       ('off-pen', ?, 'req-pen', ?, ?, ?, ?, ?, 1000, 'pending', NULL, NULL)""",
                    (
                        self.company_id,
                        self.founder_id,
                        orig_run.run_id,
                        orig_item.work_item_id,
                        recip_id,
                        asset.asset_id,
                        succ_item.work_item_id,
                        succ_run.run_id,
                        self.company_id,
                        self.founder_id,
                        orig_run.run_id,
                        orig_item.work_item_id,
                        recip_id,
                        asset.asset_id,
                    ),
                )
                rebuild_core_v1_tables(raw)
                raw.execute("COMMIT")
            with closing(open_reader(self.store)) as ro, read_tx(ro):
                rows = dict(
                    ro.execute(
                        "SELECT offer_id, state || ':' || state_version FROM work_handoff"
                    ).fetchall()
                )
                self.assertEqual(rows["off-acc"], "accepted:2")
                self.assertEqual(rows["off-pen"], "offered:1")
                acc = accepted_handoff(ro, cr, asset.asset_id)
                self.assertIsNotNone(acc)
                assert acc is not None
                self.assertEqual(acc["offer_id"], "off-acc")

    def test_pr191_r17_project_peer_policy_matches_exact_project_id_not_colon_prefix(
        self,
    ) -> None:
        """PR #191 R17 P2: colon-prefixed project IDs ('risk' vs 'risk:review') must not cross-contaminate peer responders."""
        from sourdough.company_assets import enroll_project_participant
        from sourdough.runtime.work_kernel import (
            list_project_peer_responders,
            sync_project_peer_responsibilities,
        )

        with hold_company_writer(self.store, self.company_id) as w:
            alice_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="alice-risk-r17",
                profile_id=self.profile.profile_id(),
            )
            bob_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="bob-risk-r17",
                profile_id=self.profile.profile_id(),
            )
            assign_project(
                w,
                self.grant_id,
                "risk",
                (self.founder_id,),
                publishers=(self.founder_id,),
            )
            assign_project(
                w,
                self.grant_id,
                "risk:review",
                (self.founder_id, alice_id),
                publishers=(self.founder_id, alice_id),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                sync_project_peer_responsibilities(
                    db,
                    company_id=self.company_id,
                    project_id="risk",
                    members=(self.founder_id, bob_id),
                    active_responders=(self.founder_id,),
                    now_ns=1_000,
                )
                sync_project_peer_responsibilities(
                    db,
                    company_id=self.company_id,
                    project_id="risk:review",
                    members=(self.founder_id, alice_id),
                    active_responders=(alice_id,),
                    now_ns=1_000,
                )
                self.assertEqual(
                    list_project_peer_responders(
                        db, company_id=self.company_id, project_id="risk"
                    ),
                    (self.founder_id,),
                )
                self.assertEqual(
                    list_project_peer_responders(
                        db, company_id=self.company_id, project_id="risk:review"
                    ),
                    (alice_id,),
                )

            # Enrolling Bob into 'risk' MUST NOT pull Alice from 'risk:review' into 'risk' responders!
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                enroll_project_participant(
                    db,
                    self.company_id,
                    self.grant_id,
                    "risk",
                    bob_id,
                )
            with closing(open_reader(self.store)) as ro_db, read_tx(ro_db):
                risk_responders = list_project_peer_responders(
                    ro_db, company_id=self.company_id, project_id="risk"
                )
                review_responders = list_project_peer_responders(
                    ro_db, company_id=self.company_id, project_id="risk:review"
                )
            self.assertEqual(risk_responders, (self.founder_id,))
            self.assertNotIn(alice_id, risk_responders)
            self.assertEqual(review_responders, (alice_id,))


if __name__ == "__main__":
    unittest.main()
