"""Unit and integration tests for M2 Features #8-#10: Conversations, Peer Response & Handoff CAS."""

import json
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.agent_desk import ensure_private_desk, save_draft
from sourdough.company import FoundingTerms, found
from sourdough.company_assets import (
    Selection,
    assign_project,
    enroll_project_participant,
    publish_selection,
)
from sourdough.conversation_delivery import acknowledge_delivery, read_inbox
from sourdough.conversations import (
    PER_RUN_MESSAGE_CAP,
    Post,
    append_message,
    read_messages,
)
from sourdough.duty_run import DutyRefused, WorkRunClaim
from sourdough.runtime.work_kernel import (
    MAX_PEER_CHAIN_DEPTH,
    PAIR_WINDOW_NS,
    PAIRWISE_RATE_LIMIT_COUNT,
    RunFenced,
    WorkKernelRefused,
    claim_work_run,
    complete_work_run,
    create_work_item,
    list_project_peer_responders,
    resolve_peer_request,
    wake_waiting_runs,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.work_handoff import (
    HandoffOffer,
    accept_handoff,
    accepted_handoff,
    decline_handoff,
    discover_handoffs,
    expire_handoffs,
    offer_handoff,
    withdraw_handoff,
)
from sourdough.work_peer_response import (
    MAX_PEER_CHAIN_DEPTH as WPR_MAX_DEPTH,
    PAIR_WINDOW_NS as WPR_PAIR_WINDOW_NS,
    PAIRWISE_RATE_LIMIT_COUNT as WPR_RATE_LIMIT,
    PeerQuestion,
    answer_peer_question,
    claim_peer_response,
    claim_resumed_peer_work,
    has_waiting_peer_work,
    peer_turn_has_disposition,
    read_peer_outcome,
    read_peer_question,
    recover_unanswered_peer_runs,
    request_peer_response,
)
from sourdough.writer_lease import hold_company_writer


def _sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class _M2CollabHarness:
    def __init__(self, tmp: str) -> None:
        self.root = Path(tmp)
        self.store = self.root / "company.sqlite"
        self.leases = self.root / "leases"
        self.receipt = found(
            self.store,
            FoundingTerms("m2-co", "charter-v1", "manage-company", "profile-v1"),
        )
        self.company_id = self.receipt.company_id
        self.founder_id = self.receipt.founder_id
        self.founder_grant_id = self.receipt.grant_id
        self._hire_seq = 0

    def writer(self):
        return hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        )

    def hire(
        self,
        name: str,
        *,
        grant_scope: str = "assessment",
        execution_profile: str = "profile-v1",
        allocated_turns: int = 32,
    ) -> tuple[str, str, str]:
        del allocated_turns
        self._hire_seq += 1
        agent_id = f"agent-{name}-{self._hire_seq}"
        grant_id = f"grant-{name}-{self._hire_seq}"
        with self.writer() as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "INSERT INTO agent VALUES (?, ?, ?)",
                    (agent_id, self.company_id, execution_profile),
                )
                db.execute(
                    "INSERT INTO standing_grant VALUES (?, ?, ?)",
                    (grant_id, agent_id, grant_scope),
                )
                ensure_private_desk(db, self.company_id, agent_id, execution_profile)
        return agent_id, grant_id, grant_scope

    def open_claim(
        self,
        agent_id: str,
        grant_id: str,
        grant_scope: str,
        *,
        duty_kind: str = "work.assessment_review",
        purpose: str = "assessment_review",
        now_ns: int = 1_000,
    ) -> WorkRunClaim:
        with self.writer() as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=agent_id,
                    duty_kind=duty_kind,
                    source_kind="unit",
                    source_id=f"src-{agent_id}-{duty_kind}-{now_ns}-{id(object())}",
                    now_ns=now_ns,
                )
                run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="unit-test",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=now_ns,
                )
        return WorkRunClaim(
            company_id=self.company_id,
            founder_id=agent_id,
            duty_id=item.work_item_id,
            run_id=run.run_id,
            fencing_token=run.fencing_token,
            grant_id=grant_id,
            grant_scope=grant_scope,
            purpose=purpose,
            _clock=now_ns,
        )

    def complete_claim(self, claim: WorkRunClaim, *, now_ns: int = 2_000) -> None:
        with self.writer() as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db,
                    claim.run_id,
                    claim.fencing_token,
                    outcome={"done": True},
                    now_ns=now_ns,
                )


class M2ConversationsAndRoutesTests(unittest.TestCase):
    def test_per_run_message_cap_replay_and_cross_project_asset_rejection(self) -> None:
        self.assertEqual(PER_RUN_MESSAGE_CAP, 8)
        with TemporaryDirectory(prefix="sd-m2-conv-") as tmp:
            h = _M2CollabHarness(tmp)
            a, ag, as_ = h.hire("alpha")
            b, bg, bs = h.hire("beta")
            with h.writer() as w:
                rev_a = assign_project(
                    w,
                    h.founder_grant_id,
                    "proj-a",
                    (h.founder_id, a, b),
                    publishers=(a, b),
                )
                rev_b = assign_project(
                    w,
                    h.founder_grant_id,
                    "proj-b",
                    (h.founder_id, a, b),
                    publishers=(b,),
                )
            ca = h.open_claim(a, ag, as_)
            cb = h.open_claim(b, bg, bs)
            with h.writer() as w:
                db_draft = save_draft(
                    w,
                    cb,
                    cb.purpose,
                    name="b-only-proj",
                    content="From B",
                    expected_revision=None,
                )
                b_asset = publish_selection(
                    w,
                    cb,
                    cb.purpose,
                    Selection(
                        "pub-b",
                        "b-only-proj",
                        db_draft.revision,
                        "proj-b",
                        rev_b,
                        (a, b),
                        0,
                        6,
                    ),
                )
                # Sender 'a' is in both proj-a and proj-b and can read b_asset,
                # but linking a proj-b asset into proj-a MUST fail closed.
                with self.assertRaises(DutyRefused):
                    append_message(
                        w,
                        ca,
                        ca.purpose,
                        Post(
                            "proj-a",
                            rev_a,
                            "cross-proj",
                            "Cross link",
                            b_asset.asset_id,
                        ),
                    )
                for i in range(PER_RUN_MESSAGE_CAP):
                    msg = append_message(
                        w,
                        ca,
                        ca.purpose,
                        Post("proj-a", rev_a, f"req-{i}", f"Body {i}", ""),
                    )
                # Idempotent replay of the 8th message succeeds even at cap.
                replay = append_message(
                    w,
                    ca,
                    ca.purpose,
                    Post(
                        "proj-a",
                        rev_a,
                        f"req-{PER_RUN_MESSAGE_CAP - 1}",
                        f"Body {PER_RUN_MESSAGE_CAP - 1}",
                        "",
                    ),
                )
                self.assertEqual(replay.message_id, msg.message_id)
                # 9th distinct message fails atomically with DutyRefused.
                with self.assertRaises(DutyRefused):
                    append_message(
                        w,
                        ca,
                        ca.purpose,
                        Post("proj-a", rev_a, "req-over", "Overflow", ""),
                    )

            with closing(open_reader(h.store)) as db, read_tx(db):
                cnt = db.execute(
                    "SELECT COUNT(*) FROM project_message WHERE run_id = ?",
                    (ca.run_id,),
                ).fetchone()[0]
            self.assertEqual(cnt, PER_RUN_MESSAGE_CAP)

    def test_expected_membership_revision_fencing_and_non_acking_inbox(self) -> None:
        with TemporaryDirectory(prefix="sd-m2-rev-") as tmp:
            h = _M2CollabHarness(tmp)
            a, ag, as_ = h.hire("alpha")
            b, bg, bs = h.hire("beta")
            c, _, _ = h.hire("gamma")
            with h.writer() as w:
                rev1 = assign_project(
                    w,
                    h.founder_grant_id,
                    "proj-fence",
                    (h.founder_id, a, b),
                    publishers=(a,),
                )
            ca = h.open_claim(a, ag, as_)
            cb = h.open_claim(b, bg, bs)
            with h.writer() as w:
                append_message(
                    w,
                    ca,
                    ca.purpose,
                    Post("proj-fence", rev1, "m1", "Hello", ""),
                    expected_membership_revision=rev1,
                )
                rev2 = assign_project(
                    w,
                    h.founder_grant_id,
                    "proj-fence",
                    (h.founder_id, a, b, c),
                    publishers=(a,),
                    expected_revision=rev1,
                )
                with self.assertRaises(DutyRefused):
                    read_messages(
                        w,
                        cb,
                        cb.purpose,
                        "proj-fence",
                        "",
                        expected_membership_revision=rev1,
                    )
                inb, _ = read_inbox(w, cb, cb.purpose, "")
                self.assertEqual(len(inb), 1)
                self.assertIsNone(inb[0].read_run)
                with self.assertRaises(DutyRefused):
                    acknowledge_delivery(
                        w,
                        cb,
                        cb.purpose,
                        inb[0].delivery_id,
                        expected_membership_revision=rev1,
                    )
                acked = acknowledge_delivery(
                    w,
                    cb,
                    cb.purpose,
                    inb[0].delivery_id,
                    expected_membership_revision=rev2,
                )
                self.assertEqual(acked.read_run, cb.run_id)
                msgs, _ = read_messages(
                    w,
                    cb,
                    cb.purpose,
                    "proj-fence",
                    "",
                    expected_membership_revision=rev2,
                )
                self.assertEqual(len(msgs), 1)

    def test_peer_responders_sync_revocation_and_enrollment_preservation(self) -> None:
        with TemporaryDirectory(prefix="sd-m2-routes-") as tmp:
            h = _M2CollabHarness(tmp)
            a, _, _ = h.hire("alpha")
            b, _, _ = h.hire("beta")
            c, _, _ = h.hire("gamma")
            with h.writer() as w:
                # Non-member responder refused
                with self.assertRaises(DutyRefused):
                    assign_project(
                        w,
                        h.founder_grant_id,
                        "proj-bad",
                        (h.founder_id, a),
                        publishers=(a,),
                        peer_responders=(b,),
                    )
                # String responder refused
                with self.assertRaises(DutyRefused):
                    assign_project(
                        w,
                        h.founder_grant_id,
                        "proj-bad-str",
                        (h.founder_id, a),
                        publishers=(a,),
                        peer_responders=a,
                    )
                rev1 = assign_project(
                    w,
                    h.founder_grant_id,
                    "proj-p",
                    (h.founder_id, a, b),
                    publishers=(a, b),
                    peer_responders=(a, b),
                )
                # Idempotent replay with identical peer_responders returns rev1
                rev1_replay = assign_project(
                    w,
                    h.founder_grant_id,
                    "proj-p",
                    (h.founder_id, a, b),
                    publishers=(a, b),
                    peer_responders=(b, a),
                )
                self.assertEqual(rev1, rev1_replay)
                # Enroll C via onboarding seam -> preserves existing peer_responders (a, b)
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    enroll_project_participant(
                        db, h.company_id, h.founder_grant_id, "proj-p", c
                    )
                    rows = db.execute(
                        """SELECT owner_agent_id, status, readable_scope_json
                           FROM work_responsibilities
                           WHERE company_id = ? AND duty_kind = 'work.peer_response'""",
                        (h.company_id,),
                    ).fetchall()
                    by_owner = {r[0]: (r[1], json.loads(r[2])) for r in rows}
                    self.assertEqual(by_owner[a][0], "active")
                    self.assertEqual(by_owner[b][0], "active")
                    self.assertIn(c, by_owner[a][1]["allowed_requesters"])
                    self.assertIn(c, by_owner[b][1]["allowed_requesters"])

                # Update project removing 'a' from peer_responders -> 'a' responsibility revoked
                _, _, _ = rev1, rev1_replay, by_owner
                rev_after = open_reader(h.store)
                current_rev = rev_after.execute(
                    "SELECT revision FROM shared_project WHERE project_id = 'proj-p'"
                ).fetchone()[0]
                rev_after.close()
                assign_project(
                    w,
                    h.founder_grant_id,
                    "proj-p",
                    (h.founder_id, a, b, c),
                    publishers=(a, b),
                    expected_revision=current_rev,
                    peer_responders=(b,),
                )
            with closing(open_reader(h.store)) as db, read_tx(db):
                statuses = dict(
                    db.execute(
                        """SELECT owner_agent_id, status
                           FROM work_responsibilities
                           WHERE company_id = ? AND duty_kind = 'work.peer_response'""",
                        (h.company_id,),
                    ).fetchall()
                )
            self.assertEqual(statuses, {a: "revoked", b: "active"})

    def test_explicit_empty_peer_responders_preserved_across_enrollment(self) -> None:
        with TemporaryDirectory(prefix="sd-m2-empty-resp-") as tmp:
            h = _M2CollabHarness(tmp)
            a, _, _ = h.hire("alpha")
            b, _, _ = h.hire("beta")
            with h.writer() as w:
                assign_project(
                    w,
                    h.founder_grant_id,
                    "proj-empty",
                    (h.founder_id, a),
                    publishers=(a,),
                    peer_responders=(),
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    self.assertEqual(
                        list_project_peer_responders(
                            db, company_id=h.company_id, project_id="proj-empty"
                        ),
                        (),
                    )
                    enroll_project_participant(
                        db, h.company_id, h.founder_grant_id, "proj-empty", b
                    )
                    self.assertEqual(
                        list_project_peer_responders(
                            db, company_id=h.company_id, project_id="proj-empty"
                        ),
                        (),
                    )


class M2PeerResponseCircuitBreakerTests(unittest.TestCase):
    def test_exported_constants_and_multi_project_route_resolution(self) -> None:
        self.assertEqual(MAX_PEER_CHAIN_DEPTH, 4)
        self.assertEqual(PAIRWISE_RATE_LIMIT_COUNT, 6)
        self.assertEqual(PAIR_WINDOW_NS, 3600 * 1_000_000_000)
        self.assertEqual(WPR_MAX_DEPTH, MAX_PEER_CHAIN_DEPTH)
        self.assertEqual(WPR_RATE_LIMIT, PAIRWISE_RATE_LIMIT_COUNT)
        self.assertEqual(WPR_PAIR_WINDOW_NS, PAIR_WINDOW_NS)

        # Verify _authorize_peer_route when recipient is responder on 2 projects and project_id is None
        with TemporaryDirectory(prefix="sd-m2-multiroute-") as tmp:
            h = _M2CollabHarness(tmp)
            a, ag, as_ = h.hire("alpha")
            b, bg, bs = h.hire("beta")
            c, cg, cs = h.hire("gamma")
            with h.writer() as w:
                assign_project(
                    w,
                    h.founder_grant_id,
                    "p-1",
                    (h.founder_id, b, c),
                    publishers=(b,),
                    peer_responders=(b,),
                )
                assign_project(
                    w,
                    h.founder_grant_id,
                    "p-2",
                    (h.founder_id, a, b),
                    publishers=(a,),
                    peer_responders=(b,),
                )
            ca = h.open_claim(a, ag, as_, now_ns=1_000_000_000_000)
            with h.writer() as w:
                wait = request_peer_response(
                    w,
                    ca,
                    PeerQuestion("q-p2", b, "Can B answer from p-2?", "yes/no", 2000),
                    now=1000,
                )
                self.assertTrue(wait.response_duty)

    def test_pairwise_rate_limit_window_and_reset(self) -> None:
        with TemporaryDirectory(prefix="sd-m2-rate-") as tmp:
            h = _M2CollabHarness(tmp)
            a, ag, as_ = h.hire("alpha", allocated_turns=40)
            b, bg, bs = h.hire("beta", allocated_turns=40)
            c, cg, cs = h.hire("gamma", allocated_turns=40)
            with h.writer() as w:
                assign_project(
                    w,
                    h.founder_grant_id,
                    "p-rl",
                    (h.founder_id, a, b, c),
                    publishers=(a,),
                    peer_responders=(b, c),
                )
            now = 5_000_000
            for i in range(PAIRWISE_RATE_LIMIT_COUNT):
                ca = h.open_claim(a, ag, as_, now_ns=(now + i * 10) * 1_000_000_000)
                with h.writer() as w:
                    request_peer_response(
                        w,
                        ca,
                        PeerQuestion(f"k-{i}", b, f"Q{i}", "ok", now + 5000),
                        now=now + i * 10,
                    )
                    cb = claim_peer_response(w, "profile-v1", now=now + i * 10 + 1)
                    self.assertIsNotNone(cb)
                    read_peer_question(w, cb, now=now + i * 10 + 2)
                    answer_peer_question(w, cb, "answered", "ok", now=now + i * 10 + 3)
            ca_over = h.open_claim(a, ag, as_, now_ns=(now + 200) * 1_000_000_000)
            with h.writer() as w:
                with self.assertRaises(DutyRefused):
                    request_peer_response(
                        w,
                        ca_over,
                        PeerQuestion("k-over", b, "Over cap", "ok", now + 5000),
                        now=now + 200,
                    )
                # Different pair (A -> C) succeeds
                wait_c = request_peer_response(
                    w,
                    ca_over,
                    PeerQuestion("k-c", c, "C ok", "ok", now + 5000),
                    now=now + 201,
                )
                self.assertTrue(wait_c.response_duty)

            # After sliding window elapses, A -> B is permitted again
            after_window = now + (PAIR_WINDOW_NS // 1_000_000_000) + 500
            ca_after = h.open_claim(a, ag, as_, now_ns=after_window * 1_000_000_000)
            with h.writer() as w:
                wait_after = request_peer_response(
                    w,
                    ca_after,
                    PeerQuestion(
                        "k-after", b, "Window cleared", "ok", after_window + 500
                    ),
                    now=after_window,
                )
                self.assertTrue(wait_after.response_duty)

    def test_stale_thread_cas_late_answer_and_recovery(self) -> None:
        with TemporaryDirectory(prefix="sd-m2-cas-peer-") as tmp:
            h = _M2CollabHarness(tmp)
            a, ag, as_ = h.hire("alpha")
            b, bg, bs = h.hire("beta")
            with h.writer() as w:
                assign_project(
                    w,
                    h.founder_grant_id,
                    "p-cas",
                    (h.founder_id, a, b),
                    publishers=(a,),
                    peer_responders=(b,),
                )
            now = 10_000
            ca = h.open_claim(a, ag, as_, now_ns=now * 1_000_000_000)
            with h.writer() as w:
                wait = request_peer_response(
                    w,
                    ca,
                    PeerQuestion("k-cas", b, "Need check", "ok", now + 30),
                    now=now,
                )
                cb = claim_peer_response(w, "profile-v1", now=now + 2)
                self.assertIsNotNone(cb)
                self.assertTrue(has_waiting_peer_work(w))
                self.assertFalse(peer_turn_has_disposition(w, cb, now=now + 3))
                # Answer before read fails
                with self.assertRaises(DutyRefused):
                    answer_peer_question(w, cb, "answered", "No read", now=now + 3)
                read_peer_question(w, cb, now=now + 4)
                # Direct kernel resolve_peer_request at deadline_ns fails CAS & returns False
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    req_id = db.execute(
                        "SELECT request_id FROM work_peer_requests WHERE response_work_item_id = ?",
                        (wait.response_duty,),
                    ).fetchone()[0]
                    with self.assertRaises(WorkKernelRefused):
                        resolve_peer_request(
                            db,
                            company_id=h.company_id,
                            request_id=req_id,
                            actor_id=b,
                            resolution_state="answered",
                            response_payload={"answer": "Too late"},
                            response_run_id=cb.run_id,
                            response_fencing_token=cb.fencing_token,
                            now_ns=(now + 30) * 1_000_000_000,
                        )
                recover_unanswered_peer_runs(w)
                # After recovery, unanswered open item can be claimed again
                cb2 = claim_peer_response(w, "profile-v1", now=now + 10)
                self.assertIsNotNone(cb2)
                read_peer_question(w, cb2, now=now + 11)
                answer_peer_question(w, cb2, "answered", "Recovered ok", now=now + 12)
                self.assertTrue(peer_turn_has_disposition(w, cb2, now=now + 13))
                resumed = claim_resumed_peer_work(w, "profile-v1", now=now + 14)
                self.assertIsNotNone(resumed)
                out = read_peer_outcome(w, resumed)
                self.assertEqual(out["outcome"], "answered")
                self.assertEqual(out["answer"], "Recovered ok")

    def test_wake_waiting_runs_expiry_cancels_and_fences_running_response_item(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-m2-wake-running-") as tmp:
            h = _M2CollabHarness(tmp)
            a, ag, as_ = h.hire("alpha")
            b, bg, bs = h.hire("beta")
            with h.writer() as w:
                assign_project(
                    w,
                    h.founder_grant_id,
                    "p-exp-run",
                    (h.founder_id, a, b),
                    publishers=(a,),
                    peer_responders=(b,),
                )
            now = 20_000
            ca = h.open_claim(a, ag, as_, now_ns=now * 1_000_000_000)
            with h.writer() as w:
                wait = request_peer_response(
                    w,
                    ca,
                    PeerQuestion("k-exp-run", b, "Need check", "ok", now + 30),
                    now=now,
                )
                cb = claim_peer_response(w, "profile-v1", now=now + 5)
                self.assertIsNotNone(cb)
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    woken = wake_waiting_runs(
                        db, h.company_id, now_ns=(now + 35) * 1_000_000_000
                    )
                    self.assertEqual(woken, (ca.duty_id,))
                    req_state = db.execute(
                        "SELECT state FROM work_peer_requests WHERE response_work_item_id = ?",
                        (wait.response_duty,),
                    ).fetchone()[0]
                    resp_item_state = db.execute(
                        "SELECT state FROM work_items WHERE work_item_id = ?",
                        (wait.response_duty,),
                    ).fetchone()[0]
                    resp_run_status = db.execute(
                        "SELECT status FROM work_runs WHERE run_id = ?",
                        (cb.run_id,),
                    ).fetchone()[0]
                    # Recipient B's active slot was released so B can claim new work immediately
                    b_other, _ = create_work_item(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=b,
                        duty_kind="work.assessment_review",
                        source_kind="unit",
                        source_id="after-exp",
                        now_ns=(now + 36) * 1_000_000_000,
                    )
                    b_other_run = claim_work_run(
                        db,
                        company_id=h.company_id,
                        work_item_id=b_other.work_item_id,
                        lease_owner="unit",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=(now + 36) * 1_000_000_000,
                    )
                self.assertEqual(req_state, "expired")
                self.assertEqual(resp_item_state, "cancelled")
                self.assertEqual(resp_run_status, "fenced")
                self.assertEqual(b_other_run.status, "running")
            _ = (bg, bs)

    def test_nanosecond_expires_at_rejected_with_duty_refused(self) -> None:
        with TemporaryDirectory(prefix="sd-m2-nano-sec-") as tmp:
            h = _M2CollabHarness(tmp)
            a, ag, as_ = h.hire("alpha")
            b, _, _ = h.hire("beta")
            with h.writer() as w:
                assign_project(
                    w,
                    h.founder_grant_id,
                    "p-nano-wpr",
                    (h.founder_id, a, b),
                    publishers=(a,),
                    peer_responders=(b,),
                )
            ca = h.open_claim(a, ag, as_, now_ns=1_000 * 1_000_000_000)
            too_large_sec = ((2**63 - 1) // 1_000_000_000) + 1
            with h.writer() as w:
                with self.assertRaises(DutyRefused):
                    request_peer_response(
                        w,
                        ca,
                        PeerQuestion("k-nano", b, "Need check", "ok", too_large_sec),
                        now=1_000,
                    )


class M2HandoffCasTests(unittest.TestCase):
    def test_handoff_cas_accept_successor_execution_and_late_fencing(self) -> None:
        with TemporaryDirectory(prefix="sd-m2-ho-") as tmp:
            h = _M2CollabHarness(tmp)
            a, ag, as_ = h.hire("alpha")
            b, bg, bs = h.hire("beta")
            with h.writer() as w:
                rev = assign_project(
                    w,
                    h.founder_grant_id,
                    "p-ho",
                    (h.founder_id, a, b),
                    publishers=(a, b),
                )
            ca = h.open_claim(a, ag, as_, now_ns=1_000_000_000_000)
            cb = h.open_claim(b, bg, bs, now_ns=1_000_000_000_000)
            with h.writer() as w:
                draft = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="ho-doc",
                    content="Shared artifact for handoff",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "pub-ho",
                        "ho-doc",
                        draft.revision,
                        "p-ho",
                        rev,
                        (a, b),
                        0,
                        27,
                    ),
                )
                offer_id = offer_handoff(
                    w,
                    ca,
                    HandoffOffer("ho-1", b, asset.asset_id, 2000, "Verify artifact"),
                    now=1000,
                )
                offers = discover_handoffs(w, cb, cb.purpose, now=1005)
                self.assertEqual([o["offer_id"] for o in offers], [offer_id])
                # Stale state version rejected
                with self.assertRaises(DutyRefused):
                    accept_handoff(
                        w,
                        cb,
                        cb.purpose,
                        offer_id,
                        now=1006,
                        expected_state_version=99,
                    )
                succ_id = accept_handoff(
                    w,
                    cb,
                    cb.purpose,
                    offer_id,
                    now=1006,
                    expected_state_version=1,
                )
                # Idempotent replay on same cb succeeds
                self.assertEqual(
                    accept_handoff(w, cb, cb.purpose, offer_id, now=1007), succ_id
                )
                # Late sender callback fails fenced
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    with self.assertRaises(RunFenced):
                        complete_work_run(
                            db,
                            ca.run_id,
                            ca.fencing_token,
                            outcome={"late": True},
                            now_ns=1008 * 1_000_000_000,
                        )
            # Complete cb, then claim successor work item as b
            h.complete_claim(cb, now_ns=1009 * 1_000_000_000)
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    succ_run = claim_work_run(
                        db,
                        company_id=h.company_id,
                        work_item_id=succ_id,
                        lease_owner="succ-exec",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=1010 * 1_000_000_000,
                    )
                    succ_claim = WorkRunClaim(
                        company_id=h.company_id,
                        founder_id=b,
                        duty_id=succ_id,
                        run_id=succ_run.run_id,
                        fencing_token=succ_run.fencing_token,
                        grant_id=bg,
                        grant_scope=bs,
                        purpose="assessment_review",
                        _clock=1010 * 1_000_000_000,
                    )
                    info = accepted_handoff(db, succ_claim, asset.asset_id)
            self.assertEqual(
                info,
                {
                    "offer_id": offer_id,
                    "sender_id": a,
                    "expected_result": "Verify artifact",
                },
            )

    def test_decline_withdraw_and_expire_handoffs_cas(self) -> None:
        with TemporaryDirectory(prefix="sd-m2-ho-cas-") as tmp:
            h = _M2CollabHarness(tmp)
            a, ag, as_ = h.hire("alpha")
            b, bg, bs = h.hire("beta")
            with h.writer() as w:
                rev = assign_project(
                    w,
                    h.founder_grant_id,
                    "p-ho2",
                    (h.founder_id, a, b),
                    publishers=(a,),
                )
            ca = h.open_claim(a, ag, as_, now_ns=1_000_000_000_000)
            cb = h.open_claim(b, bg, bs, now_ns=1_000_000_000_000)
            with h.writer() as w:
                draft = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="ho-doc2",
                    content="Shared evidence",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "pub-ho2",
                        "ho-doc2",
                        draft.revision,
                        "p-ho2",
                        rev,
                        (a, b),
                        0,
                        15,
                    ),
                )
                o_dec = offer_handoff(
                    w,
                    ca,
                    HandoffOffer("ho-d", b, asset.asset_id, 2000, "Dec"),
                    now=1000,
                )
                o_wd = offer_handoff(
                    w,
                    ca,
                    HandoffOffer("ho-w", b, asset.asset_id, 2000, "Wd"),
                    now=1000,
                )
                o_exp = offer_handoff(
                    w,
                    ca,
                    HandoffOffer("ho-e", b, asset.asset_id, 1050, "Exp"),
                    now=1000,
                )
                decline_handoff(
                    w, cb, cb.purpose, o_dec, now=1005, reason="Out of scope"
                )
                with self.assertRaises(DutyRefused):
                    accept_handoff(w, cb, cb.purpose, o_dec, now=1006)
                with self.assertRaises(DutyRefused):
                    withdraw_handoff(w, ca, o_dec, now=1006)
                withdraw_handoff(w, ca, o_wd, now=1006)
                with self.assertRaises(DutyRefused):
                    decline_handoff(w, cb, cb.purpose, o_wd, now=1007)
                expired = expire_handoffs(w, now=1050)
                self.assertEqual(expired, (o_exp,))
                self.assertEqual(discover_handoffs(w, cb, cb.purpose, now=1050), [])
            with closing(open_reader(h.store)) as db, read_tx(db):
                states = dict(
                    db.execute(
                        "SELECT offer_id, state || ':' || state_version FROM work_handoff"
                    ).fetchall()
                )
            self.assertEqual(
                states,
                {
                    o_dec: "declined:2",
                    o_wd: "withdrawn:2",
                    o_exp: "expired:2",
                },
            )

    def test_accept_handoff_on_waiting_sender_fences_run_and_prevents_spurious_wake(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-m2-ho-wait-") as tmp:
            h = _M2CollabHarness(tmp)
            a, ag, as_ = h.hire("alpha")
            b, bg, bs = h.hire("beta")
            c, _, _ = h.hire("gamma")
            with h.writer() as w:
                rev = assign_project(
                    w,
                    h.founder_grant_id,
                    "p-ho-wait",
                    (h.founder_id, a, b, c),
                    publishers=(a,),
                    peer_responders=(c,),
                )
            now = 30_000
            ca = h.open_claim(a, ag, as_, now_ns=now * 1_000_000_000)
            cb = h.open_claim(b, bg, bs, now_ns=now * 1_000_000_000)
            with h.writer() as w:
                draft = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="ho-wait-doc",
                    content="Shared draft before wait",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "pub-ho-wait",
                        "ho-wait-doc",
                        draft.revision,
                        "p-ho-wait",
                        rev,
                        (a, b, c),
                        0,
                        24,
                    ),
                )
                offer_id = offer_handoff(
                    w,
                    ca,
                    HandoffOffer(
                        "ho-wait-1", b, asset.asset_id, now + 1000, "Take over"
                    ),
                    now=now + 1,
                )
                wait = request_peer_response(
                    w,
                    ca,
                    PeerQuestion("ho-peer-1", c, "Check detail?", "ok", now + 500),
                    now=now + 2,
                )
                succ_id = accept_handoff(
                    w, cb, cb.purpose, offer_id, now=now + 3, expected_state_version=1
                )
                self.assertTrue(succ_id)
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    a_item_state = db.execute(
                        "SELECT state FROM work_items WHERE work_item_id = ?",
                        (ca.duty_id,),
                    ).fetchone()[0]
                    a_run_row = db.execute(
                        """SELECT status, wait_until_ns, wait_event_key
                           FROM work_runs WHERE run_id = ?""",
                        (ca.run_id,),
                    ).fetchone()
                    peer_state = db.execute(
                        "SELECT state FROM work_peer_requests WHERE response_work_item_id = ?",
                        (wait.response_duty,),
                    ).fetchone()[0]
                    resp_state = db.execute(
                        "SELECT state FROM work_items WHERE work_item_id = ?",
                        (wait.response_duty,),
                    ).fetchone()[0]
                    woken = wake_waiting_runs(
                        db, h.company_id, now_ns=(now + 600) * 1_000_000_000
                    )
                    a_run_after = db.execute(
                        "SELECT status FROM work_runs WHERE run_id = ?",
                        (ca.run_id,),
                    ).fetchone()[0]
                self.assertEqual(a_item_state, "cancelled")
                self.assertEqual(a_run_row, ("fenced", None, None))
                self.assertEqual(peer_state, "expired")
                self.assertEqual(resp_state, "cancelled")
                self.assertEqual(woken, ())
                self.assertEqual(a_run_after, "fenced")


if __name__ == "__main__":
    unittest.main()
