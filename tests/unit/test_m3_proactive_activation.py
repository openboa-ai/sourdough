"""Unit and integration tests for M3 Feature #12: Event Propagation, Coalescing & Hysteresis."""

from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.agent_desk import ensure_private_desk, save_draft
from sourdough.company import FoundingTerms, found
from sourdough.company_assets import Selection, assign_project, publish_selection
from sourdough.conversations import Post, append_message
from sourdough.duty_run import WorkRunClaim
from sourdough.runtime import (
    CompanyRuntime,
    DutyOutcome,
    ResidentCompanyService,
    WorkKernelRefused,
    build_default_registry,
    cancel_condition,
    claim_work_run,
    complete_work_run,
    create_work_item,
    drain_work_event_cursor,
    evaluate_conditions,
    record_discovery_hysteresis,
    register_responsibility,
    replace_condition,
    set_condition,
    wake_waiting_runs,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.storage.kernel import append_audit_event
from sourdough.work_handoff import HandoffOffer, offer_handoff
from sourdough.writer_lease import hold_company_writer


class ProactiveActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.leases = self.root / "leases"
        self.profile_id = "adapter-v1:openai:gpt-5-mini:test"
        self.receipt = found(
            self.store,
            FoundingTerms("m3-f12", "charter-v1", "manage-company", self.profile_id),
        )
        self.company_id = self.receipt.company_id
        self.founder_id = self.receipt.founder_id

    def _hire(self, name: str, scope: str = "desk:write") -> tuple[str, str, str]:
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                agent_id = f"agent-{name}"
                grant_id = f"grant-{name}"
                db.execute(
                    "INSERT INTO agent VALUES (?, ?, ?)",
                    (agent_id, self.company_id, self.profile_id),
                )
                db.execute(
                    "INSERT INTO standing_grant VALUES (?, ?, ?)",
                    (grant_id, agent_id, scope),
                )
                ensure_private_desk(db, self.company_id, agent_id, self.profile_id)
                return agent_id, grant_id, scope

    def _open_claim(
        self,
        agent_id: str,
        grant_id: str,
        scope: str,
        *,
        duty_kind: str = "work.discovery_review",
    ) -> tuple[str, str, WorkRunClaim]:
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=agent_id,
                    duty_kind=duty_kind,
                    source_kind="test",
                    source_id=f"src-{agent_id}-{duty_kind}",
                    now_ns=1_000,
                )
                run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=1_000,
                )
                claim = WorkRunClaim(
                    company_id=self.company_id,
                    founder_id=agent_id,
                    duty_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    grant_id=grant_id,
                    grant_scope=scope,
                    purpose=duty_kind,
                    _clock=1_000,
                )
                return item.work_item_id, run.run_id, claim

    def test_cursor_drain_handles_asset_and_peer_events_ignores_chat_and_batches(
        self,
    ) -> None:
        a, ga, sa = self._hire("alice", scope="assessment")
        b, gb, sb = self._hire("bob", scope="assessment")
        _, _, ca = self._open_claim(a, ga, sa)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            rev = assign_project(
                w,
                self.receipt.grant_id,
                "proj-1",
                (self.founder_id, a, b),
                publishers=(a, b),
            )
            draft = save_draft(
                w,
                ca,
                ca.purpose,
                name="note",
                content="Shared finding",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                ca,
                ca.purpose,
                Selection(
                    "pub-1", "note", draft.revision, "proj-1", rev, (a, b), 0, 14
                ),
            )
            append_message(
                w, ca, ca.purpose, Post("proj-1", rev, "msg-1", "hi @bob", "")
            )
            offer_handoff(
                w,
                ca,
                HandoffOffer("ho-1", b, asset.asset_id, 9_999_999_999, "review"),
                now=2_000,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                append_audit_event(
                    db,
                    company_id=self.company_id,
                    domain="assets",
                    entity_type="shared_publication",
                    entity_id="synthetic-res",
                    event_type="asset.published",
                    actor_id=b,
                    payload={"asset_id": "synthetic-res", "revision": "rev-syn"},
                    recorded_at_ns=3_000,
                )
                append_audit_event(
                    db,
                    company_id=self.company_id,
                    domain="work",
                    entity_type="peer_request",
                    entity_id="req-99",
                    event_type="work.peer_request_answered",
                    actor_id=b,
                    payload={"request_id": "req-99"},
                    recorded_at_ns=3_500,
                )
                batch1 = drain_work_event_cursor(
                    db, self.company_id, now_ns=4_000, max_events=2
                )
                self.assertTrue(batch1.has_more)
                self.assertEqual(batch1.events_processed, 2)
                batch2 = drain_work_event_cursor(
                    db, self.company_id, now_ns=4_100, max_events=64
                )
                self.assertFalse(batch2.has_more)
                self.assertGreater(batch2.last_event_seq, batch1.last_event_seq)

                all_assets = {**batch1.observed_assets, **batch2.observed_assets}
                all_authors = {**batch1.asset_authors, **batch2.asset_authors}
                all_peers = {**batch1.peer_events, **batch2.peer_events}
                self.assertEqual(
                    all_assets[asset.asset_id],
                    sha256(b"Shared finding").hexdigest(),
                )
                self.assertEqual(all_authors[asset.asset_id], a)
                self.assertEqual(all_assets["synthetic-res"], "rev-syn")
                self.assertEqual(all_authors["synthetic-res"], b)
                self.assertEqual(all_peers, {"peer_response:req-99": b})
                # Re-drain with no new events is a no-op
                batch3 = drain_work_event_cursor(db, self.company_id, now_ns=4_200)
                self.assertEqual(batch3.events_processed, 0)
                self.assertEqual(batch3.last_event_seq, batch2.last_event_seq)

    def test_multi_agent_simultaneous_publish_per_asset_anti_self_wake(self) -> None:
        a, ga, sa = self._hire("agent-a")
        b, gb, sb = self._hire("agent-b")
        _, run_a, ca = self._open_claim(a, ga, sa)
        _, run_b, cb = self._open_claim(b, gb, sb)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            rev = assign_project(
                w,
                self.receipt.grant_id,
                "proj-multi",
                (self.founder_id, a, b),
                publishers=(a, b),
            )
            da = save_draft(
                w, ca, ca.purpose, name="a", content="From A", expected_revision=None
            )
            db_draft = save_draft(
                w, cb, cb.purpose, name="b", content="From B", expected_revision=None
            )
            asset_a = publish_selection(
                w,
                ca,
                ca.purpose,
                Selection("pa", "a", da.revision, "proj-multi", rev, (a, b), 0, 6),
            )
            asset_b = publish_selection(
                w,
                cb,
                cb.purpose,
                Selection(
                    "pb", "b", db_draft.revision, "proj-multi", rev, (a, b), 0, 6
                ),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db, run_a, ca.fencing_token, outcome={"ok": True}, now_ns=1_500
                )
                complete_work_run(
                    db, run_b, cb.fencing_token, outcome={"ok": True}, now_ns=1_500
                )
                ra = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind="work.discovery_review",
                    remit="Watch A and B",
                    readable_scope=["shared"],
                    now_ns=1_500,
                ).responsibility_id
                rb = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=b,
                    duty_kind="work.discovery_review",
                    remit="Watch A and B",
                    readable_scope=["shared"],
                    now_ns=1_500,
                ).responsibility_id
                for owner, resp in ((a, ra), (b, rb)):
                    for aid in (asset_a.asset_id, asset_b.asset_id):
                        set_condition(
                            db,
                            company_id=self.company_id,
                            owner_agent_id=owner,
                            responsibility_id=resp,
                            trigger_kind="shared_asset_revision",
                            source_identity=aid,
                            now_ns=1_600,
                        )

        woken_pairs: set[tuple[str, str]] = set()
        rt = CompanyRuntime(
            self.store,
            self.company_id,
            build_default_registry(
                {
                    "work.discovery_review": lambda ctx: (
                        woken_pairs.add(
                            (
                                ctx.work_item.owner_agent_id,
                                json.loads(ctx.work_item.payload_json)[
                                    "source_identity"
                                ],
                            )
                        ),
                        DutyOutcome("completed", result={"ok": True}),
                    )[1]
                }
            ),
            lease_directory=self.leases,
        )
        summary = rt.step(max_dispatches=8, now_ns=3_000)
        self.assertEqual(len(summary.admitted_items), 2)
        self.assertEqual(woken_pairs, {(a, asset_b.asset_id), (b, asset_a.asset_id)})
        with closing(open_reader(self.store)) as db, read_tx(db):
            suppressed = db.execute(
                """SELECT COUNT(*) FROM audit_event
                   WHERE company_id = ? AND event_type = 'work.self_authored_revision_suppressed'""",
                (self.company_id,),
            ).fetchone()[0]
            self.assertEqual(suppressed, 2)

    def test_project_inbox_trigger_strictly_rejected(self) -> None:
        a, _, _ = self._hire("a")
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                ra = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind="work.discovery_review",
                    remit="Watch",
                    readable_scope=["shared"],
                    now_ns=1_000,
                ).responsibility_id
                with self.assertRaises(WorkKernelRefused):
                    set_condition(
                        db,
                        company_id=self.company_id,
                        owner_agent_id=a,
                        responsibility_id=ra,
                        trigger_kind="project_inbox",
                        source_identity="proj-1",
                        now_ns=1_000,
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        """INSERT INTO work_conditions (
                               condition_id, company_id, owner_agent_id, responsibility_id,
                               work_item_id, condition_revision, trigger_kind, status,
                               created_at_ns, updated_at_ns
                           ) VALUES ('bad', ?, ?, ?, NULL, 1, 'project_inbox', 'armed', 1000, 1000)""",
                        (self.company_id, a, ra),
                    )

    def test_cooldown_multi_fire_coalescing_wake_budget_and_mandatory_protection(
        self,
    ) -> None:
        a, ga, sa = self._hire("watcher")
        b, gb, sb = self._hire("publisher")
        _, run_b, cb = self._open_claim(b, gb, sb)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            rev = assign_project(
                w,
                self.receipt.grant_id,
                "proj-cool",
                (self.founder_id, a, b),
                publishers=(b,),
            )
            d1 = save_draft(
                w,
                cb,
                cb.purpose,
                name="spec",
                content="Spec v1",
                expected_revision=None,
            )
            asset1 = publish_selection(
                w,
                cb,
                cb.purpose,
                Selection("s1", "spec", d1.revision, "proj-cool", rev, (a, b), 0, 7),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                ra = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind="work.discovery_review",
                    remit="Review specs",
                    readable_scope={
                        "max_wakes_per_window": 2,
                        "wake_window_ns": 100_000,
                    },
                    now_ns=1_000,
                ).responsibility_id
                cond = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    responsibility_id=ra,
                    trigger_kind="shared_asset_revision",
                    source_identity=asset1.asset_id,
                    cooldown_ns=10_000,
                    max_fires=2,
                    now_ns=1_000,
                )
                # Fire 1 at t=2_000
                ad1 = evaluate_conditions(db, self.company_id, now_ns=2_000)
                self.assertEqual(len(ad1), 1)
                # While item 1 is still 'due' and cooldown active, burst at t=5_000 is suppressed
                ad_cd = evaluate_conditions(
                    db,
                    self.company_id,
                    now_ns=5_000,
                    observed_assets={asset1.asset_id: "r2"},
                    triggering_actor_id=b,
                )
                self.assertEqual(ad_cd, ())
                # Even after cooldown expires at t=15_000, while ad1[0] is still 'due', coalesces onto ad1[0]!
                ad_coal = evaluate_conditions(
                    db,
                    self.company_id,
                    now_ns=15_000,
                    observed_assets={asset1.asset_id: "r3"},
                    triggering_actor_id=b,
                )
                self.assertEqual(ad_coal, ())
                coalesced_payload = json.loads(
                    db.execute(
                        "SELECT payload_json FROM work_items WHERE work_item_id = ?",
                        (ad1[0].work_item_id,),
                    ).fetchone()[0]
                )
                self.assertEqual(coalesced_payload["observed_revision"], "r3")
                self.assertEqual(len(coalesced_payload["coalesced_revisions"]), 2)
                self.assertEqual(
                    coalesced_payload["coalesced_revisions"][-1]["observed_revision"],
                    "r3",
                )

                # Settle item 1, then Fire 2 at t=20_000 admits item 2 and marks condition triggered
                r1 = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=ad1[0].work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=16_000,
                )
                complete_work_run(
                    db, r1.run_id, r1.fencing_token, outcome={"ok": True}, now_ns=16_500
                )
                ad2 = evaluate_conditions(
                    db,
                    self.company_id,
                    now_ns=20_000,
                    observed_assets={asset1.asset_id: "r4"},
                    triggering_actor_id=b,
                )
                self.assertEqual(len(ad2), 1)
                c_row = db.execute(
                    "SELECT status, fire_count FROM work_conditions WHERE condition_id = ?",
                    (cond.condition_id,),
                ).fetchone()
                self.assertEqual(c_row, ("triggered", 2))

                # Budget exhausted (2 wakes in window) -> arm new discretionary condition, should be throttled,
                # while mandatory work.assessment_review timer still admits!
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    responsibility_id=ra,
                    trigger_kind="timer",
                    due_at_ns=21_000,
                    now_ns=20_500,
                )
                mand_parent, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind="work.assessment_review",
                    source_kind="test",
                    source_id="mand-1",
                    state="due",
                    now_ns=20_500,
                )
                mand_run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=mand_parent.work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=20_500,
                )
                complete_work_run(
                    db,
                    mand_run.run_id,
                    mand_run.fencing_token,
                    outcome={"ok": True},
                    now_ns=20_600,
                )
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    work_item_id=mand_parent.work_item_id,
                    trigger_kind="timer",
                    due_at_ns=21_000,
                    now_ns=20_500,
                )
                ad_mand = evaluate_conditions(db, self.company_id, now_ns=22_000)
                self.assertEqual(
                    [i.duty_kind for i in ad_mand], ["work.assessment_review"]
                )

    def test_discovery_hysteresis_backoff_reset_and_cas_fencing(self) -> None:
        a, _, _ = self._hire("hyst-a")
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                ra = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind="work.discovery_review",
                    remit="Check",
                    readable_scope=["shared"],
                    now_ns=1_000,
                ).responsibility_id
                s1 = record_discovery_hysteresis(
                    db,
                    company_id=self.company_id,
                    responsibility_id=ra,
                    action_kind="defer",
                    now_ns=2_000,
                    base_cooldown_ns=1_000,
                )
                self.assertEqual(
                    (s1.consecutive_noops, s1.cooldown_until_ns), (1, 3_000)
                )
                s2 = record_discovery_hysteresis(
                    db,
                    company_id=self.company_id,
                    responsibility_id=ra,
                    action_kind="noop",
                    now_ns=3_000,
                    base_cooldown_ns=1_000,
                )
                self.assertEqual(
                    (s2.consecutive_noops, s2.cooldown_until_ns), (2, 5_000)
                )
                s3 = record_discovery_hysteresis(
                    db,
                    company_id=self.company_id,
                    responsibility_id=ra,
                    action_kind="investigate",
                    now_ns=5_000,
                    base_cooldown_ns=1_000,
                )
                self.assertEqual(s3.consecutive_noops, 0)

                cond = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    responsibility_id=ra,
                    trigger_kind="timer",
                    due_at_ns=10_000,
                    now_ns=1_000,
                )
                with self.assertRaises(WorkKernelRefused):
                    replace_condition(
                        db,
                        cond.condition_id,
                        owner_agent_id=a,
                        trigger_kind="timer",
                        due_at_ns=20_000,
                        expected_condition_revision=99,
                        now_ns=2_000,
                    )
                with self.assertRaises(WorkKernelRefused):
                    cancel_condition(
                        db,
                        cond.condition_id,
                        owner_agent_id=a,
                        expected_condition_revision=99,
                        now_ns=2_000,
                    )
                cond2 = replace_condition(
                    db,
                    cond.condition_id,
                    owner_agent_id=a,
                    trigger_kind="timer",
                    due_at_ns=20_000,
                    expected_condition_revision=1,
                    now_ns=2_000,
                )
                self.assertEqual(cond2.condition_revision, 2)
                cancel_condition(
                    db,
                    cond2.condition_id,
                    owner_agent_id=a,
                    expected_condition_revision=2,
                    now_ns=3_000,
                )

    def test_wake_waiting_runs_supports_per_event_actors_and_anti_self_wake(
        self,
    ) -> None:
        a, ga, sa = self._hire("req-a")
        b, gb, sb = self._hire("resp-b")
        c, gc, sc = self._hire("resp-c")
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                for target, key_scope in ((b, a), (c, a)):
                    register_responsibility(
                        db,
                        company_id=self.company_id,
                        owner_agent_id=target,
                        duty_kind="work.peer_response",
                        remit="Respond",
                        readable_scope={"allowed_requesters": [key_scope]},
                        now_ns=1_000,
                    )
                from sourdough.runtime.work_kernel import yield_run_to_wait

                ia, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind="work.discovery_review",
                    source_kind="test",
                    source_id="w1",
                    now_ns=1_000,
                )
                run_a = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=ia.work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=1_000,
                )
                yield_run_to_wait(
                    db,
                    run_a.run_id,
                    run_a.fencing_token,
                    wait_event_key="custom:peer-1",
                    reason="peer-wait",
                    now_ns=1_200,
                )
                # Also create a second waiting run on C that tries to self-wake
                ic, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=c,
                    duty_kind="work.discovery_review",
                    source_kind="test",
                    source_id="w2",
                    now_ns=1_000,
                )
                run_c = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=ic.work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=1_000,
                )
                yield_run_to_wait(
                    db,
                    run_c.run_id,
                    run_c.fencing_token,
                    wait_event_key="custom:self",
                    reason="self-test",
                    now_ns=1_200,
                )
                woken = wake_waiting_runs(
                    db,
                    self.company_id,
                    now_ns=2_000,
                    event_actors={
                        "custom:peer-1": b,
                        "custom:self": c,
                    },
                )
                self.assertEqual(woken, (ia.work_item_id,))
                suppressed = db.execute(
                    """SELECT COUNT(*) FROM audit_event
                       WHERE company_id = ? AND event_type = 'work.self_authored_event_suppressed'""",
                    (self.company_id,),
                ).fetchone()[0]
                self.assertEqual(suppressed, 1)

    def test_resident_company_service_drains_cursor_autonomously(self) -> None:
        a, ga, sa = self._hire("res-a")
        b, gb, sb = self._hire("res-b")
        _, run_b, cb = self._open_claim(b, gb, sb)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            rev = assign_project(
                w,
                self.receipt.grant_id,
                "proj-res",
                (self.founder_id, a, b),
                publishers=(b,),
            )
            draft = save_draft(
                w,
                cb,
                cb.purpose,
                name="d",
                content="Autonomous trigger",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                cb,
                cb.purpose,
                Selection("sr", "d", draft.revision, "proj-res", rev, (a, b), 0, 18),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db, run_b, cb.fencing_token, outcome={"ok": True}, now_ns=1_000
                )
                ra = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind="work.discovery_review",
                    remit="Autonomous watch",
                    readable_scope=["shared"],
                    now_ns=1_000,
                ).responsibility_id
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    responsibility_id=ra,
                    trigger_kind="shared_asset_revision",
                    source_identity=asset.asset_id,
                    now_ns=1_000,
                )

        seen: list[str] = []
        reg = build_default_registry(
            {
                "work.discovery_review": lambda ctx: (
                    seen.append(ctx.work_item.owner_agent_id),
                    DutyOutcome("completed", result={"ok": True}),
                )[1]
            }
        )
        sock_dir = Path(
            self.enterContext(TemporaryDirectory(prefix="sd-m3p-", dir="/tmp"))
        )
        with ResidentCompanyService(
            self.store,
            self.company_id,
            reg,
            socket_dir=sock_dir,
            lease_directory=self.leases,
            tick_interval_seconds=0.1,
            enable_board_trigger_intake=False,
        ) as svc:
            svc.wait_for(lambda h: h.runs_executed >= 1, timeout_seconds=5.0)
        self.assertEqual(seen, [a])

    def test_multi_watcher_simultaneous_publish_wakes_all_non_authors_without_collision(
        self,
    ) -> None:
        a, ga, sa = self._hire("mw-a")
        b, gb, sb = self._hire("mw-b")
        c, gc, sc = self._hire("mw-c")
        _, run_a, ca = self._open_claim(a, ga, sa)
        _, run_b, cb = self._open_claim(b, gb, sb)
        _, run_c, cc = self._open_claim(c, gc, sc)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            rev = assign_project(
                w,
                self.receipt.grant_id,
                "proj-mw",
                (self.founder_id, a, b, c),
                publishers=(a, b, c),
            )
            da = save_draft(
                w, ca, ca.purpose, name="a", content="Doc A", expected_revision=None
            )
            db_d = save_draft(
                w, cb, cb.purpose, name="b", content="Doc B", expected_revision=None
            )
            dc = save_draft(
                w, cc, cc.purpose, name="c", content="Doc C", expected_revision=None
            )
            asset_a = publish_selection(
                w,
                ca,
                ca.purpose,
                Selection("pa", "a", da.revision, "proj-mw", rev, (a, b, c), 0, 5),
            )
            asset_b = publish_selection(
                w,
                cb,
                cb.purpose,
                Selection("pb", "b", db_d.revision, "proj-mw", rev, (a, b, c), 0, 5),
            )
            asset_c = publish_selection(
                w,
                cc,
                cc.purpose,
                Selection("pc", "c", dc.revision, "proj-mw", rev, (a, b, c), 0, 5),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                for run_id, claim in ((run_a, ca), (run_b, cb), (run_c, cc)):
                    complete_work_run(
                        db,
                        run_id,
                        claim.fencing_token,
                        outcome={"ok": True},
                        now_ns=1_500,
                    )
                for agent_id in (a, b, c):
                    for aid in (asset_a.asset_id, asset_b.asset_id, asset_c.asset_id):
                        rid = register_responsibility(
                            db,
                            company_id=self.company_id,
                            owner_agent_id=agent_id,
                            duty_kind="work.discovery_review",
                            remit=f"Watch deliverable {aid}",
                            readable_scope=["shared"],
                            now_ns=1_500,
                        ).responsibility_id
                        set_condition(
                            db,
                            company_id=self.company_id,
                            owner_agent_id=agent_id,
                            responsibility_id=rid,
                            trigger_kind="shared_asset_revision",
                            source_identity=aid,
                            now_ns=1_600,
                        )
                batch = drain_work_event_cursor(db, self.company_id, now_ns=2_000)
                admitted = evaluate_conditions(
                    db, self.company_id, now_ns=2_000, _cursor_batch=batch
                )
                self.assertEqual(len(admitted), 6)
                pairs = {
                    (
                        item.owner_agent_id,
                        json.loads(item.payload_json)["source_identity"],
                    )
                    for item in admitted
                }
                self.assertEqual(
                    pairs,
                    {
                        (a, asset_b.asset_id),
                        (a, asset_c.asset_id),
                        (b, asset_a.asset_id),
                        (b, asset_c.asset_id),
                        (c, asset_a.asset_id),
                        (c, asset_b.asset_id),
                    },
                )
                # Replay on the same revisions must still deduplicate per condition
                replayed = evaluate_conditions(db, self.company_id, now_ns=2_100)
                self.assertEqual(replayed, ())

    def test_coalesced_due_revision_does_not_refire_after_settle_while_running_refires(
        self,
    ) -> None:
        a, _, _ = self._hire("coal-watcher")
        b, gb, sb = self._hire("coal-pub")
        _, run_b, cb = self._open_claim(b, gb, sb)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            rev = assign_project(
                w,
                self.receipt.grant_id,
                "proj-coal",
                (self.founder_id, a, b),
                publishers=(b,),
            )
            d1 = save_draft(
                w, cb, cb.purpose, name="doc", content="v1", expected_revision=None
            )
            asset = publish_selection(
                w,
                cb,
                cb.purpose,
                Selection("p1", "doc", d1.revision, "proj-coal", rev, (a, b), 0, 2),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                ra = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind="work.discovery_review",
                    remit="Watch doc",
                    readable_scope={
                        "max_wakes_per_window": 10,
                        "wake_window_ns": 1_000_000,
                    },
                    now_ns=1_000,
                ).responsibility_id
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    responsibility_id=ra,
                    trigger_kind="shared_asset_revision",
                    source_identity=asset.asset_id,
                    cooldown_ns=5_000,
                    max_fires=5,
                    now_ns=1_000,
                )
                ad1 = evaluate_conditions(db, self.company_id, now_ns=2_000)
                self.assertEqual(len(ad1), 1)
                item1 = ad1[0]

                # Update shared_publication to v2 while item1 is still 'due'
                v2_rev = sha256(b"v2-content").hexdigest()
                db.execute(
                    "UPDATE shared_publication SET content = ? WHERE asset_id = ?",
                    ("v2-content", asset.asset_id),
                )
                append_audit_event(
                    db,
                    company_id=self.company_id,
                    domain="assets",
                    entity_type="shared_publication",
                    entity_id=asset.asset_id,
                    event_type="asset.published",
                    actor_id=b,
                    payload={"asset_id": asset.asset_id, "revision": v2_rev},
                    recorded_at_ns=7_500,
                )
                ad_coal = evaluate_conditions(db, self.company_id, now_ns=8_000)
                self.assertEqual(ad_coal, ())
                r1 = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item1.work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=9_000,
                )
                complete_work_run(
                    db, r1.run_id, r1.fencing_token, outcome={"ok": True}, now_ns=9_500
                )
                # No new revision after settlement -> v2 must NOT re-fire
                ad_after = evaluate_conditions(db, self.company_id, now_ns=15_000)
                self.assertEqual(ad_after, ())

                # Publish v3 -> fires item2, claim it ('running'), then publish v4 while running
                v3_rev = sha256(b"v3-content").hexdigest()
                db.execute(
                    "UPDATE shared_publication SET content = ? WHERE asset_id = ?",
                    ("v3-content", asset.asset_id),
                )
                append_audit_event(
                    db,
                    company_id=self.company_id,
                    domain="assets",
                    entity_type="shared_publication",
                    entity_id=asset.asset_id,
                    event_type="asset.published",
                    actor_id=b,
                    payload={"asset_id": asset.asset_id, "revision": v3_rev},
                    recorded_at_ns=19_500,
                )
                ad2 = evaluate_conditions(db, self.company_id, now_ns=20_000)
                self.assertEqual(len(ad2), 1)
                item2 = ad2[0]
                r2 = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item2.work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=21_000,
                )
                v4_rev = sha256(b"v4-content").hexdigest()
                db.execute(
                    "UPDATE shared_publication SET content = ? WHERE asset_id = ?",
                    ("v4-content", asset.asset_id),
                )
                append_audit_event(
                    db,
                    company_id=self.company_id,
                    domain="assets",
                    entity_type="shared_publication",
                    entity_id=asset.asset_id,
                    event_type="asset.published",
                    actor_id=b,
                    payload={"asset_id": asset.asset_id, "revision": v4_rev},
                    recorded_at_ns=25_500,
                )
                # While item2 is running, v4 is skipped without swallowing occurrence
                ad_run = evaluate_conditions(db, self.company_id, now_ns=26_000)
                self.assertEqual(ad_run, ())
                complete_work_run(
                    db, r2.run_id, r2.fencing_token, outcome={"ok": True}, now_ns=27_000
                )
                # Once item2 settles and cooldown elapses, v4 DOES trigger item3
                ad3 = evaluate_conditions(db, self.company_id, now_ns=32_000)
                self.assertEqual(len(ad3), 1)
                self.assertEqual(
                    json.loads(ad3[0].payload_json)["observed_revision"],
                    v4_rev,
                )

    def test_non_discretionary_cooldown_and_hysteresis_triggered_condition_fallback(
        self,
    ) -> None:
        a, _, _ = self._hire("nd-a")
        b, _, _ = self._hire("nd-b")
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                parent, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind="work.assessment_review",
                    source_kind="test",
                    source_id="nd-parent",
                    now_ns=1_000,
                )
                cond = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    work_item_id=parent.work_item_id,
                    trigger_kind="shared_asset_revision",
                    source_identity="asset-nd",
                    cooldown_ns=10_000,
                    max_fires=3,
                    now_ns=1_000,
                )
                ad1 = evaluate_conditions(
                    db,
                    self.company_id,
                    now_ns=2_000,
                    observed_assets={"asset-nd": "v1"},
                    triggering_actor_id=b,
                )
                self.assertEqual(len(ad1), 1)
                # Before cooldown expires at t=5_000 -> suppressed even for non-discretionary condition
                ad_sup = evaluate_conditions(
                    db,
                    self.company_id,
                    now_ns=5_000,
                    observed_assets={"asset-nd": "v2"},
                    triggering_actor_id=b,
                )
                self.assertEqual(ad_sup, ())
                # After cooldown expires at t=12_500 -> fires v2
                ad2 = evaluate_conditions(
                    db,
                    self.company_id,
                    now_ns=12_500,
                    observed_assets={"asset-nd": "v2"},
                    triggering_actor_id=b,
                )
                self.assertEqual(len(ad2), 1)
                self.assertEqual(
                    db.execute(
                        "SELECT fire_count FROM work_conditions WHERE condition_id = ?",
                        (cond.condition_id,),
                    ).fetchone()[0],
                    2,
                )

                # One-shot timer condition transitions to 'triggered' on admission;
                # record_work_decision('defer') must still inherit its cooldown_ns via condition_id fallback.
                resp_id = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind="work.discovery_review",
                    remit="Check fallback",
                    readable_scope=["shared"],
                    now_ns=1_000,
                ).responsibility_id
                one_shot = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    responsibility_id=resp_id,
                    trigger_kind="timer",
                    due_at_ns=15_000,
                    cooldown_ns=5_000,
                    now_ns=1_000,
                )
                ad_timer = evaluate_conditions(db, self.company_id, now_ns=15_000)
                self.assertEqual(len(ad_timer), 1)
                self.assertEqual(
                    db.execute(
                        "SELECT status FROM work_conditions WHERE condition_id = ?",
                        (one_shot.condition_id,),
                    ).fetchone()[0],
                    "triggered",
                )
                run_t = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=ad_timer[0].work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=15_100,
                )
                from sourdough.runtime import record_work_decision

                record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=ad_timer[0].work_item_id,
                    run_id=run_t.run_id,
                    fencing_token=run_t.fencing_token,
                    agent_id=a,
                    action_kind="defer",
                    rationale="Deferring after one-shot timer",
                    decision_at_ns=16_000,
                )
                disc_row = db.execute(
                    """SELECT consecutive_noops, cooldown_until_ns
                       FROM work_discovery_state
                       WHERE company_id = ? AND responsibility_id = ?""",
                    (self.company_id, resp_id),
                ).fetchone()
                self.assertEqual(disc_row, (1, 21_000))


if __name__ == "__main__":
    unittest.main()
