import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.runtime.work_kernel import (
    RunFenced,
    WorkKernelRefused,
    assert_run_fencing,
    cancel_condition,
    claim_work_run,
    complete_work_run,
    create_work_item,
    evaluate_conditions,
    fail_work_run,
    list_conditions,
    open_peer_request,
    reclaim_expired_runs,
    register_responsibility,
    replace_condition,
    resolve_peer_request,
    revoke_responsibility,
    set_condition,
    wake_waiting_runs,
    yield_run_to_wait,
)
from sourdough.storage.db import open_writer, write_tx


class WorkKernel(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.receipt = found(
            self.store, FoundingTerms("setup", "charter-v1", "manage", "profile-v1")
        )
        self.company_id = self.receipt.company_id
        self.founder_id = self.receipt.founder_id
        self.peer_id = "colleague-1"
        with closing(open_writer(self.store)) as conn, write_tx(conn):
            conn.execute(
                "INSERT INTO agent VALUES (?, ?, ?)",
                (self.peer_id, self.company_id, "profile-v2"),
            )
            conn.execute(
                "INSERT INTO standing_grant VALUES ('grant-peer-1', ?, 'manage')",
                (self.peer_id,),
            )
            self.peer_responsibility = register_responsibility(
                conn,
                company_id=self.company_id,
                owner_agent_id=self.peer_id,
                duty_kind="work.peer_response",
                remit="Review authorized founder peer requests",
                readable_scope={
                    "allowed_requesters": [self.founder_id],
                    "evidence_prefixes": ["asset:"],
                    "required_scope": "manage",
                    "max_open_requests": 2,
                },
                now_ns=500,
            )

    def test_active_uniqueness_fencing_lease_reclamation_and_zero_compute_wait(self):
        with closing(open_writer(self.store)) as conn:
            with write_tx(conn):
                item1, dup1 = create_work_item(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.analyze",
                    source_kind="manual",
                    source_id="src-1",
                    dedupe_key="item-1",
                    now_ns=1_000,
                )
                item1_again, dup2 = create_work_item(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.analyze",
                    source_kind="manual",
                    source_id="src-1",
                    dedupe_key="item-1",
                    now_ns=1_000,
                )
                item2, _ = create_work_item(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.analyze",
                    source_kind="manual",
                    source_id="src-2",
                    now_ns=1_001,
                )
            self.assertFalse(dup1)
            self.assertTrue(dup2)
            self.assertEqual(item1, item1_again)

            # Claim item1 -> active running slot owned by founder_id
            with write_tx(conn):
                run1 = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=item1.work_item_id,
                    lease_owner="worker-a",
                    lease_ttl_ns=10_000,
                    now_ns=2_000,
                )
            self.assertEqual(run1.fencing_token, 1)

            # Same agent cannot hold two active running runs simultaneously
            with self.assertRaisesRegex(WorkKernelRefused, "active running"):
                with write_tx(conn):
                    claim_work_run(
                        conn,
                        company_id=self.company_id,
                        work_item_id=item2.work_item_id,
                        lease_owner="worker-b",
                        lease_ttl_ns=10_000,
                        now_ns=2_500,
                    )

            # Yielding run1 to zero-compute durable wait releases founder's active slot!
            with write_tx(conn):
                yield_run_to_wait(
                    conn,
                    run1.run_id,
                    run1.fencing_token,
                    wait_until_ns=50_000,
                    wait_event_key="asset:doc-1",
                    now_ns=3_000,
                )
                run2 = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=item2.work_item_id,
                    lease_owner="worker-a",
                    lease_ttl_ns=5_000,
                    now_ns=3_100,
                )
            self.assertEqual(run2.fencing_token, 1)

            # Anti-self-wake: founder's own event AND actorless replay must NOT wake run1
            with write_tx(conn):
                self.assertEqual(
                    wake_waiting_runs(
                        conn,
                        self.company_id,
                        now_ns=4_000,
                        event_key="asset:doc-1",
                        triggering_actor_id=self.founder_id,
                    ),
                    (),
                )
                self.assertEqual(
                    wake_waiting_runs(
                        conn,
                        self.company_id,
                        now_ns=4_050,
                        event_key="asset:doc-1",
                        triggering_actor_id=None,
                    ),
                    (),
                )
                # Peer event wakes run1 cleanly
                woken = wake_waiting_runs(
                    conn,
                    self.company_id,
                    now_ns=4_100,
                    event_key="asset:doc-1",
                    triggering_actor_id=self.peer_id,
                )
            self.assertEqual(woken, (item1.work_item_id,))

            # Expire run2 lease -> reclaim fences run2 and allows fresh fencing_token=2
            with write_tx(conn):
                fenced = reclaim_expired_runs(conn, self.company_id, now_ns=9_000)
            self.assertEqual(fenced, (run2.run_id,))
            with self.assertRaises(RunFenced):
                assert_run_fencing(conn, run2.run_id, run2.fencing_token)

            with write_tx(conn):
                run2_retry = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=item2.work_item_id,
                    lease_owner="worker-b",
                    lease_ttl_ns=10_000,
                    now_ns=9_100,
                )
            self.assertEqual(run2_retry.fencing_token, 2)

            # Stale run2 cannot complete; fresh run2_retry settles cleanly
            with self.assertRaises(RunFenced):
                with write_tx(conn):
                    complete_work_run(
                        conn, run2.run_id, run2.fencing_token, outcome={"ok": False}
                    )
            with write_tx(conn):
                completed, _ = complete_work_run(
                    conn,
                    run2_retry.run_id,
                    run2_retry.fencing_token,
                    outcome={"ok": True},
                    now_ns=9_500,
                )
            self.assertEqual(completed.status, "completed")

    def test_standing_responsibilities_mutable_conditions_dedup_and_revocation(self):
        with closing(open_writer(self.store)) as conn:
            with write_tx(conn):
                resp = register_responsibility(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Audit shared policy memos",
                    readable_scope=["project:alpha"],
                    now_ns=1_000,
                )
                timer_cond = set_condition(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="timer",
                    due_at_ns=5_000,
                    now_ns=1_000,
                )
                # Replace timer with a designated shared asset revision trigger
                asset_cond = replace_condition(
                    conn,
                    timer_cond.condition_id,
                    owner_agent_id=self.founder_id,
                    trigger_kind="shared_asset_revision",
                    source_identity="asset:memo-1",
                    expected_revision=None,
                    now_ns=2_000,
                )
                extra_cond = set_condition(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="timer",
                    due_at_ns=6_000,
                    now_ns=2_000,
                )
                cancel_condition(
                    conn,
                    extra_cond.condition_id,
                    owner_agent_id=self.founder_id,
                    now_ns=2_100,
                )
            self.assertEqual(asset_cond.condition_revision, 2)
            self.assertEqual(
                len(list_conditions(conn, self.company_id, self.founder_id)), 3
            )

            # Superseded timer (5_000) and cancelled timer (6_000) must NOT fire!
            with write_tx(conn):
                admitted_none = evaluate_conditions(
                    conn, self.company_id, now_ns=10_000
                )
                # Self-authored asset revision rev-1 must NOT wake owner (anti-self-wake)
                admitted_self = evaluate_conditions(
                    conn,
                    self.company_id,
                    now_ns=10_000,
                    observed_assets={"asset:memo-1": "rev-1"},
                    triggering_actor_id=self.founder_id,
                )
                # Subsequent actor-less sweep observing the same self-authored rev-1 MUST stay suppressed
                admitted_self_later = evaluate_conditions(
                    conn,
                    self.company_id,
                    now_ns=10_200,
                    observed_assets={"asset:memo-1": "rev-1"},
                    triggering_actor_id=None,
                )
            self.assertEqual(admitted_none, ())
            self.assertEqual(admitted_self, ())
            self.assertEqual(admitted_self_later, ())

            # Peer-authored rev-2 fires once and deduplicates on replay
            with write_tx(conn):
                admitted = evaluate_conditions(
                    conn,
                    self.company_id,
                    now_ns=10_500,
                    observed_assets={"asset:memo-1": "rev-2"},
                    triggering_actor_id=self.peer_id,
                )
                replayed = evaluate_conditions(
                    conn,
                    self.company_id,
                    now_ns=10_600,
                    observed_assets={"asset:memo-1": "rev-2"},
                    triggering_actor_id=self.peer_id,
                )
            self.assertEqual(len(admitted), 1)
            self.assertEqual(admitted[0].duty_kind, "work.discovery_review")
            self.assertEqual(replayed, ())

            # Revoking responsibility cancels future armed conditions
            with write_tx(conn):
                set_condition(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="timer",
                    due_at_ns=20_000,
                    now_ns=11_000,
                )
                revoke_responsibility(
                    conn, self.company_id, resp.responsibility_id, now_ns=12_000
                )
                after_revoke = evaluate_conditions(conn, self.company_id, now_ns=25_000)
            self.assertEqual(after_revoke, ())

    def test_blocking_peer_request_response_expiry_and_late_answer_ignored(self):
        with closing(open_writer(self.store)) as conn:
            with write_tx(conn):
                item, _ = create_work_item(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.assessment",
                    source_kind="project",
                    source_id="proj-1",
                    now_ns=1_000,
                )
                run = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="worker-a",
                    lease_ttl_ns=60_000,
                    now_ns=1_500,
                )
                req, replayed = open_peer_request(
                    conn,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    recipient_agent_id=self.peer_id,
                    question="Approve option A or B?",
                    expected_decision="option_choice",
                    evidence_refs=["asset:draft-1"],
                    deadline_ns=50_000,
                    request_id="req-1",
                    now_ns=2_000,
                )
                # Exact replay returns identical request without creating another duty
                req_replay, replayed2 = open_peer_request(
                    conn,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    recipient_agent_id=self.peer_id,
                    question="Approve option A or B?",
                    expected_decision="option_choice",
                    evidence_refs=["asset:draft-1"],
                    deadline_ns=50_000,
                    request_id="req-1",
                    now_ns=2_100,
                )
            self.assertFalse(replayed)
            self.assertTrue(replayed2)
            self.assertEqual(req, req_replay)

            # Premature peer_response event while request is still open MUST NOT wake requester
            with write_tx(conn):
                self.assertEqual(
                    wake_waiting_runs(
                        conn,
                        self.company_id,
                        now_ns=2_150,
                        event_key="peer_response:req-1",
                        triggering_actor_id=self.peer_id,
                    ),
                    (),
                )

            # Claim recipient's response duty, fence a stale run, and verify only the
            # active fenced recipient run can answer or decline
            with write_tx(conn):
                stale_resp_run = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=req.response_work_item_id,
                    lease_owner="stale-peer-worker",
                    lease_ttl_ns=500,
                    now_ns=2_200,
                )
                reclaim_expired_runs(conn, self.company_id, now_ns=2_800)

            # Stale fenced recipient run cannot answer
            with self.assertRaises(RunFenced):
                with write_tx(conn):
                    resolve_peer_request(
                        conn,
                        company_id=self.company_id,
                        request_id="req-1",
                        actor_id=self.peer_id,
                        resolution_state="answered",
                        response_payload={"choice": "stale"},
                        response_run_id=stale_resp_run.run_id,
                        response_fencing_token=stale_resp_run.fencing_token,
                        now_ns=2_900,
                    )

            # Requester cannot answer its own request
            with self.assertRaisesRegex(WorkKernelRefused, "named recipient"):
                with write_tx(conn):
                    resolve_peer_request(
                        conn,
                        company_id=self.company_id,
                        request_id="req-1",
                        actor_id=self.founder_id,
                        resolution_state="answered",
                        response_payload={"choice": "A"},
                        response_run_id=stale_resp_run.run_id,
                        response_fencing_token=stale_resp_run.fencing_token,
                        now_ns=3_000,
                    )

            # Arm a peer_response condition and verify it does NOT fire while open
            with write_tx(conn):
                peer_cond = set_condition(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    work_item_id=item.work_item_id,
                    trigger_kind="peer_response",
                    source_identity="peer_response:req-1",
                    expected_revision="answered",
                    now_ns=3_050,
                )
                self.assertEqual(
                    evaluate_conditions(conn, self.company_id, now_ns=3_100), ()
                )
                active_resp_run = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=req.response_work_item_id,
                    lease_owner="active-peer-worker",
                    lease_ttl_ns=60_000,
                    now_ns=3_200,
                )
                self.assertEqual(active_resp_run.fencing_token, 2)

            # Recipient answers with active fenced run -> wakes requester once; late answer ignored
            with write_tx(conn):
                resolved, changed = resolve_peer_request(
                    conn,
                    company_id=self.company_id,
                    request_id="req-1",
                    actor_id=self.peer_id,
                    resolution_state="answered",
                    response_payload={"choice": "A"},
                    response_run_id=active_resp_run.run_id,
                    response_fencing_token=active_resp_run.fencing_token,
                    now_ns=3_500,
                )
                late, changed_late = resolve_peer_request(
                    conn,
                    company_id=self.company_id,
                    request_id="req-1",
                    actor_id=self.peer_id,
                    resolution_state="declined",
                    response_payload={"choice": "B"},
                    response_run_id=active_resp_run.run_id,
                    response_fencing_token=active_resp_run.fencing_token,
                    now_ns=4_000,
                )
                # Armed peer_response condition now admits follow-up once and deduplicates on replay
                admitted_from_peer = evaluate_conditions(
                    conn,
                    self.company_id,
                    now_ns=4_100,
                    triggering_actor_id=self.peer_id,
                )
                admitted_replay = evaluate_conditions(
                    conn,
                    self.company_id,
                    now_ns=4_200,
                    triggering_actor_id=self.peer_id,
                )
                # Recipient arming a peer_response condition against its own answer must NOT
                # self-wake on subsequent ordinary sweeps (even with triggering_actor_id=None)
                set_condition(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.peer_id,
                    work_item_id=req.response_work_item_id,
                    trigger_kind="peer_response",
                    source_identity="peer_response:req-1",
                    expected_revision="answered",
                    now_ns=4_250,
                )
                self_wake_after_sweep = evaluate_conditions(
                    conn,
                    self.company_id,
                    now_ns=4_300,
                    triggering_actor_id=None,
                )
            self.assertEqual(len(admitted_from_peer), 1)
            self.assertEqual(admitted_from_peer[0].source_id, peer_cond.condition_id)
            self.assertEqual(admitted_from_peer[0].duty_kind, "work.assessment")
            self.assertEqual(admitted_replay, ())
            self.assertEqual(self_wake_after_sweep, ())
            self.assertTrue(changed)
            self.assertFalse(changed_late)
            self.assertEqual(resolved.state, "answered")
            self.assertEqual(late.state, "answered")
            item_state = conn.execute(
                "SELECT state, owner_agent_id FROM work_items WHERE work_item_id = ?",
                (item.work_item_id,),
            ).fetchone()
            self.assertEqual(item_state, ("due", self.founder_id))

    def test_peer_request_deadline_wake_expires_open_request_and_enforces_ownership(
        self,
    ):
        with closing(open_writer(self.store)) as conn:
            with write_tx(conn):
                item, _ = create_work_item(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.assessment",
                    source_kind="project",
                    source_id="proj-timeout",
                    dedupe_key="timeout-item",
                    payload={"step": 1},
                    now_ns=1_000,
                )
                # Conflicting payload on same dedupe_key must fail closed
                with self.assertRaisesRegex(WorkKernelRefused, "conflicting terms"):
                    create_work_item(
                        conn,
                        company_id=self.company_id,
                        owner_agent_id=self.founder_id,
                        duty_kind="work.assessment",
                        source_kind="project",
                        source_id="proj-timeout",
                        dedupe_key="timeout-item",
                        payload={"step": 2},
                        now_ns=1_001,
                    )
                # Another agent cannot attach a condition to founder's work item
                with self.assertRaisesRegex(WorkKernelRefused, "belongs to another"):
                    set_condition(
                        conn,
                        company_id=self.company_id,
                        owner_agent_id=self.peer_id,
                        work_item_id=item.work_item_id,
                        trigger_kind="timer",
                        due_at_ns=5_000,
                        now_ns=1_002,
                    )
                run = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="worker-a",
                    lease_ttl_ns=60_000,
                    now_ns=1_500,
                )
                open_peer_request(
                    conn,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    recipient_agent_id=self.peer_id,
                    question="Need review?",
                    expected_decision="approve",
                    evidence_refs=["asset:r1"],
                    deadline_ns=10_000,
                    request_id="req-timeout-1",
                    now_ns=2_000,
                )

            # Timer wake past deadline must wake item, expire open peer request, AND
            # cancel its pending response work item so recipient never burns a turn
            with write_tx(conn):
                woken = wake_waiting_runs(conn, self.company_id, now_ns=12_000)
                self.assertEqual(woken, (item.work_item_id,))
                req_row = conn.execute(
                    "SELECT state, response_work_item_id FROM work_peer_requests WHERE request_id = 'req-timeout-1'"
                ).fetchone()
                self.assertEqual(req_row[0], "expired")
                resp_item_state = conn.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (req_row[1],),
                ).fetchone()
                self.assertEqual(resp_item_state, ("cancelled",))
                # Requester can now re-claim and open a fresh peer request without conflict
                run2 = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="worker-a",
                    lease_ttl_ns=60_000,
                    now_ns=12_100,
                )
                req2, _ = open_peer_request(
                    conn,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run2.run_id,
                    fencing_token=run2.fencing_token,
                    recipient_agent_id=self.peer_id,
                    question="Retry review?",
                    expected_decision="approve",
                    evidence_refs=["asset:r1"],
                    deadline_ns=30_000,
                    request_id="req-timeout-2",
                    now_ns=12_200,
                )
                self.assertEqual(req2.state, "open")

    def test_peer_request_authorizes_route_evidence_scope_and_responsibility(self):
        unrelated_id = "unrelated-agent"
        with closing(open_writer(self.store)) as conn:
            with write_tx(conn):
                conn.execute(
                    "INSERT INTO agent VALUES (?, ?, ?)",
                    (unrelated_id, self.company_id, "profile-unrelated"),
                )
                conn.execute(
                    "INSERT INTO standing_grant VALUES ('grant-unrelated', ?, 'manage')",
                    (unrelated_id,),
                )
                item, _ = create_work_item(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.assessment",
                    source_kind="project",
                    source_id="proj-auth",
                    now_ns=1_000,
                )
                run = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="worker-a",
                    lease_ttl_ns=60_000,
                    now_ns=1_100,
                )
                # 1. Unrelated agent without peer_response responsibility is refused before creating work
                with self.assertRaisesRegex(
                    WorkKernelRefused, "no active peer_response responsibility"
                ):
                    open_peer_request(
                        conn,
                        company_id=self.company_id,
                        requester_work_item_id=item.work_item_id,
                        requester_run_id=run.run_id,
                        fencing_token=run.fencing_token,
                        recipient_agent_id=unrelated_id,
                        question="Unauthorized route",
                        expected_decision="yes_no",
                        evidence_refs=[],
                        deadline_ns=50_000,
                        request_id="req-unauth-route",
                        now_ns=1_200,
                    )
                # 2. Out-of-scope evidence ref is refused before creating work
                with self.assertRaisesRegex(WorkKernelRefused, "not authorized"):
                    open_peer_request(
                        conn,
                        company_id=self.company_id,
                        requester_work_item_id=item.work_item_id,
                        requester_run_id=run.run_id,
                        fencing_token=run.fencing_token,
                        recipient_agent_id=self.peer_id,
                        question="Leak secret?",
                        expected_decision="yes_no",
                        evidence_refs=["secret:private-key"],
                        deadline_ns=50_000,
                        request_id="req-bad-ref",
                        now_ns=1_250,
                    )
                # Verify zero recipient work items were created by refused attempts
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM work_items WHERE duty_kind = 'work.peer_response'"
                    ).fetchone()[0],
                    0,
                )
                # 3. Valid authorized request succeeds; revoking responsibility mid-run
                # refuses committing the answer AND refuses any new claim
                req, _ = open_peer_request(
                    conn,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    recipient_agent_id=self.peer_id,
                    question="Authorized review?",
                    expected_decision="approve",
                    evidence_refs=["asset:public-memo"],
                    deadline_ns=50_000,
                    request_id="req-auth-ok",
                    now_ns=1_300,
                )
                claimed_resp = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=req.response_work_item_id,
                    lease_owner="peer-worker",
                    lease_ttl_ns=5_000,
                    now_ns=1_350,
                )
                # Submitting an answer at or after deadline_ns must fail closed even
                # while the recipient's response run is still active
                with self.assertRaisesRegex(WorkKernelRefused, "deadline has elapsed"):
                    resolve_peer_request(
                        conn,
                        company_id=self.company_id,
                        request_id="req-auth-ok",
                        actor_id=self.peer_id,
                        resolution_state="answered",
                        response_payload={"choice": "late"},
                        response_run_id=claimed_resp.run_id,
                        response_fencing_token=claimed_resp.fencing_token,
                        now_ns=50_000,
                    )
                revoke_responsibility(
                    conn,
                    self.company_id,
                    self.peer_responsibility.responsibility_id,
                    now_ns=1_400,
                )
                with self.assertRaisesRegex(
                    WorkKernelRefused, "no active peer_response responsibility"
                ):
                    resolve_peer_request(
                        conn,
                        company_id=self.company_id,
                        request_id="req-auth-ok",
                        actor_id=self.peer_id,
                        resolution_state="answered",
                        response_payload={"choice": "approve"},
                        response_run_id=claimed_resp.run_id,
                        response_fencing_token=claimed_resp.fencing_token,
                        now_ns=1_450,
                    )
                # Reclaiming the expired run before deadline and attempting a fresh claim must fail
                reclaim_expired_runs(conn, self.company_id, now_ns=7_000)
                with self.assertRaisesRegex(
                    WorkKernelRefused, "no active peer_response responsibility"
                ):
                    claim_work_run(
                        conn,
                        company_id=self.company_id,
                        work_item_id=req.response_work_item_id,
                        lease_owner="peer-worker-2",
                        lease_ttl_ns=5_000,
                        now_ns=7_100,
                    )
                # Claiming after deadline must also fail closed
                with self.assertRaisesRegex(WorkKernelRefused, "deadline has elapsed"):
                    claim_work_run(
                        conn,
                        company_id=self.company_id,
                        work_item_id=req.response_work_item_id,
                        lease_owner="peer-worker-3",
                        lease_ttl_ns=5_000,
                        now_ns=50_100,
                    )

    def test_expired_lease_enforced_by_default_fencing_and_fail_work_run(self):
        with closing(open_writer(self.store)) as conn:
            with write_tx(conn):
                item, _ = create_work_item(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.lease_check",
                    source_kind="manual",
                    source_id="lc-1",
                    now_ns=1_000,
                )
                run = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="worker-1",
                    lease_ttl_ns=5_000,
                    now_ns=1_000,
                )
            # Even before reclaim_expired_runs sweeps, omitting now_ns uses real clock
            # (far past lease_expires_at_ns=6_000) and fails closed!
            with self.assertRaisesRegex(RunFenced, "lease has expired"):
                assert_run_fencing(conn, run.run_id, run.fencing_token)
            # Passing an expired timestamp to fail_work_run must also refuse!
            with self.assertRaisesRegex(RunFenced, "lease has expired"):
                with write_tx(conn):
                    fail_work_run(
                        conn,
                        run.run_id,
                        run.fencing_token,
                        error_summary="late_crash",
                        now_ns=6_000,
                    )
            # Before expiration, fail_work_run succeeds cleanly
            with write_tx(conn):
                failed = fail_work_run(
                    conn,
                    run.run_id,
                    run.fencing_token,
                    error_summary="timely_crash",
                    now_ns=5_000,
                )
            self.assertEqual(failed.status, "failed")

    def test_complete_work_run_advances_and_preserves_recurring_condition_revision(
        self,
    ):
        with closing(open_writer(self.store)) as conn:
            with write_tx(conn):
                resp = register_responsibility(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.recurring",
                    remit="Recurring periodic check",
                    readable_scope=["shared"],
                    now_ns=1_000,
                )
                set_condition(
                    conn,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="timer",
                    source_identity="timer:daily-check",
                    due_at_ns=5_000,
                    condition_revision=1,
                    now_ns=1_000,
                )
                occ1 = evaluate_conditions(conn, self.company_id, now_ns=5_000)
                self.assertEqual(len(occ1), 1)
                run1 = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=occ1[0].work_item_id,
                    lease_owner="worker-1",
                    lease_ttl_ns=60_000,
                    now_ns=5_100,
                )
                # Omitting condition_revision with stable source_identity auto-advances to rev 2
                _, cond2 = complete_work_run(
                    conn,
                    run1.run_id,
                    run1.fencing_token,
                    outcome={"round": 1},
                    next_condition={
                        "trigger_kind": "timer",
                        "responsibility_id": resp.responsibility_id,
                        "source_identity": "timer:daily-check",
                        "due_at_ns": 10_000,
                    },
                    now_ns=5_200,
                )
                self.assertIsNotNone(cond2)
                self.assertEqual(cond2.condition_revision, 2)
                occ2 = evaluate_conditions(conn, self.company_id, now_ns=10_000)
                self.assertEqual(len(occ2), 1)
                run2 = claim_work_run(
                    conn,
                    company_id=self.company_id,
                    work_item_id=occ2[0].work_item_id,
                    lease_owner="worker-1",
                    lease_ttl_ns=60_000,
                    now_ns=10_100,
                )
                # Explicitly supplying condition_revision=5 preserves exact revision 5
                _, cond3 = complete_work_run(
                    conn,
                    run2.run_id,
                    run2.fencing_token,
                    outcome={"round": 2},
                    next_condition={
                        "trigger_kind": "timer",
                        "responsibility_id": resp.responsibility_id,
                        "source_identity": "timer:daily-check",
                        "due_at_ns": 20_000,
                        "condition_revision": 5,
                    },
                    now_ns=10_200,
                )
                self.assertIsNotNone(cond3)
                self.assertEqual(cond3.condition_revision, 5)
                occ3 = evaluate_conditions(conn, self.company_id, now_ns=20_000)
                self.assertEqual(len(occ3), 1)
