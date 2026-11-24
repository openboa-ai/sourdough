import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.duty_run import count_company_runs, has_turn_capacity
from sourdough.paper_execution import VenueRefused
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.runtime.engine import CompanyRuntime, build_default_registry
from sourdough.runtime.registry import (
    DutyContext,
    DutyDescriptor,
    DutyOutcome,
    RuntimeRefused,
)
from sourdough.runtime.venue_coordinator import (
    BoundedVenueCoordinator,
    VenueSegmentPolicy,
)
from sourdough.runtime.work_kernel import (
    claim_work_run,
    create_work_item,
    open_peer_request,
    register_responsibility,
    resolve_peer_request,
    set_condition,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx


class UnifiedCompanyRuntime(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.leases = self.root / "leases"
        self.store = self.root / "company.sqlite"
        self.receipt = found(
            self.store, FoundingTerms("setup", "charter-v1", "manage", "profile-v1")
        )
        self.company_id = self.receipt.company_id
        self.founder_id = self.receipt.founder_id

    def test_duty_registry_guards_duplicates_and_unknown_kinds(self):
        registry = build_default_registry(
            {"work.a": lambda _ctx: DutyOutcome("completed")}
        )
        self.assertEqual(registry.registered_kinds(), ("work.a",))
        with self.assertRaisesRegex(RuntimeRefused, "Duplicate"):
            registry.register(
                DutyDescriptor("work.a", lambda _ctx: DutyOutcome("completed"))
            )
        with self.assertRaisesRegex(RuntimeRefused, "Unregistered"):
            registry.get("work.missing")

    def test_runtime_step_reclaims_evaluates_dispatches_yields_and_wakes(self):
        events: list[tuple[str, int]] = []

        def handle_discovery(ctx: DutyContext) -> DutyOutcome:
            events.append((ctx.work_item.work_item_id, ctx.run.fencing_token))
            if ctx.run.fencing_token == 1:
                return DutyOutcome(
                    disposition="waiting",
                    wait_until_ns=20_000,
                    wait_reason="cooldown",
                )
            return DutyOutcome(
                disposition="completed",
                result={"decision": "verified"},
            )

        registry = build_default_registry({"work.discovery_review": handle_discovery})
        runtime = CompanyRuntime(
            self.store,
            self.company_id,
            registry,
            lease_directory=self.leases,
        )

        # Seed a standing responsibility + armed timer + an expired running claim to reclaim
        with closing(open_writer(self.store)) as conn, write_tx(conn):
            resp = register_responsibility(
                conn,
                company_id=self.company_id,
                owner_agent_id=self.founder_id,
                duty_kind="work.discovery_review",
                remit="Periodic check",
                readable_scope=["shared"],
                now_ns=1_000,
            )
            set_condition(
                conn,
                company_id=self.company_id,
                owner_agent_id=self.founder_id,
                responsibility_id=resp.responsibility_id,
                trigger_kind="timer",
                due_at_ns=5_000,
                now_ns=1_000,
            )
            stale_item, _ = create_work_item(
                conn,
                company_id=self.company_id,
                owner_agent_id=self.founder_id,
                duty_kind="work.discovery_review",
                source_kind="recovery",
                source_id="stale-1",
                now_ns=500,
            )
            stale_run = claim_work_run(
                conn,
                company_id=self.company_id,
                work_item_id=stale_item.work_item_id,
                lease_owner="dead-worker",
                lease_ttl_ns=1_000,
                now_ns=500,
            )

        # Step 1 at t=6_000: fences stale_run, admits condition item, runs both
        summary1 = runtime.step(max_dispatches=4, now_ns=6_000)
        self.assertEqual(summary1.fenced_runs, (stale_run.run_id,))
        self.assertEqual(len(summary1.admitted_items), 1)
        self.assertEqual(len(summary1.executed_runs), 2)
        # stale_item got fencing_token=2 -> completed; admitted item got token=1 -> waiting
        self.assertIn((stale_item.work_item_id, 2), events)
        self.assertIn((summary1.admitted_items[0], 1), events)

        # Step 2 at t=25_000: wakes waiting admitted item -> runs on fencing_token=2 -> settled
        summary2 = runtime.step(
            max_dispatches=4, now_ns=25_000, truncate_wal_on_exit=True
        )
        self.assertEqual(summary2.woken_items, summary1.admitted_items)
        self.assertEqual(len(summary2.executed_runs), 1)
        self.assertIn((summary1.admitted_items[0], 2), events)

        with closing(open_reader(self.store)) as reader, read_tx(reader):
            states = reader.execute(
                "SELECT state FROM work_items WHERE company_id = ?",
                (self.company_id,),
            ).fetchall()
            self.assertEqual(states, [("settled",), ("settled",)])

    def test_bounded_venue_coordinator_enforces_independence_and_segment_bounds(self):
        with self.assertRaises(VenueRefused):
            VenueSegmentPolicy(segment_seconds=7)
        with self.assertRaises(VenueRefused):
            VenueSegmentPolicy(segment_seconds=601)
        with self.assertRaises(VenueRefused):
            VenueSegmentPolicy(segment_seconds=10, drain_margin_seconds=4)
        with self.assertRaises(VenueRefused):
            VenueSegmentPolicy(segment_seconds=8, drain_margin_seconds=8)

        # Same file as company store must be refused
        with self.assertRaises(VenueRefused):
            BoundedVenueCoordinator(self.store, FixedQuoteVenue(self.store))

        venue = FixedQuoteVenue(self.root / "venue.sqlite")
        coordinator = BoundedVenueCoordinator(
            self.store,
            venue,
            policy=VenueSegmentPolicy(segment_seconds=60, drain_margin_seconds=5),
        )
        # 124s splits into 60 + 56 + 8 (never leaving a <8s stub!)
        segments = coordinator.plan_segments(124)
        self.assertEqual([seg.active_seconds for seg in segments], [60, 56, 8])
        self.assertEqual(sum(seg.active_seconds for seg in segments), 124)
        self.assertTrue(all(seg.work_seconds >= 3 for seg in segments))

    def test_runtime_step_releases_agent_slot_on_handler_exception_and_peer_wait(self):
        peer_id = "peer-colleague"
        with closing(open_writer(self.store)) as conn, write_tx(conn):
            conn.execute(
                "INSERT INTO agent VALUES (?, ?, ?)",
                (peer_id, self.company_id, "profile-peer"),
            )
            conn.execute(
                "INSERT INTO standing_grant VALUES ('grant-peer-colleague', ?, 'manage')",
                (peer_id,),
            )
            item, _ = create_work_item(
                conn,
                company_id=self.company_id,
                owner_agent_id=self.founder_id,
                duty_kind="work.flaky",
                source_kind="manual",
                source_id="flaky-1",
                now_ns=1_000,
            )

        attempts = 0

        def flaky_handler(ctx: DutyContext) -> DutyOutcome:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient tool crash")
            if attempts == 2:
                with closing(open_writer(self.store, lease=ctx.writer)) as conn:
                    with write_tx(conn):
                        open_peer_request(
                            conn,
                            company_id=ctx.company_id,
                            requester_work_item_id=ctx.work_item.work_item_id,
                            requester_run_id=ctx.run.run_id,
                            fencing_token=ctx.run.fencing_token,
                            recipient_agent_id=peer_id,
                            question="Confirm?",
                            expected_decision="yes_no",
                            evidence_refs=["asset:a"],
                            deadline_ns=50_000,
                            request_id="req-in-step",
                            now_ns=ctx.now_ns,
                        )
                return DutyOutcome(disposition="waiting", wait_reason="peer")
            return DutyOutcome(disposition="completed", result={"ok": True})

        def peer_handler(ctx: DutyContext) -> DutyOutcome:
            with closing(open_writer(self.store, lease=ctx.writer)) as conn:
                with write_tx(conn):
                    resolve_peer_request(
                        conn,
                        company_id=ctx.company_id,
                        request_id="req-in-step",
                        actor_id=peer_id,
                        resolution_state="answered",
                        response_payload={"answer": "yes"},
                        response_run_id=ctx.run.run_id,
                        response_fencing_token=ctx.run.fencing_token,
                        now_ns=ctx.now_ns,
                    )
            return DutyOutcome(disposition="completed", result={"answered": True})

        runtime = CompanyRuntime(
            self.store,
            self.company_id,
            build_default_registry(
                {
                    "work.flaky": flaky_handler,
                    "work.peer_response": peer_handler,
                }
            ),
            lease_directory=self.leases,
        )

        # Attempt 1 raises -> run marked failed, work item returned to due immediately
        with self.assertRaisesRegex(RuntimeError, "transient tool crash"):
            runtime.step(max_dispatches=2, now_ns=2_000)
        with closing(open_reader(self.store)) as reader, read_tx(reader):
            self.assertEqual(
                reader.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item.work_item_id,),
                ).fetchone(),
                ("due",),
            )
            self.assertEqual(
                reader.execute(
                    "SELECT COUNT(*) FROM work_runs WHERE status = 'running'"
                ).fetchone()[0],
                0,
            )

        # Attempt 2 opens peer request (already yields to waiting) -> peer answers -> attempt 3 settles!
        s2 = runtime.step(max_dispatches=4, now_ns=3_000)
        self.assertEqual(len(s2.executed_runs), 3)
        with closing(open_reader(self.store)) as reader, read_tx(reader):
            self.assertEqual(
                reader.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item.work_item_id,),
                ).fetchone(),
                ("settled",),
            )

    def test_hired_agent_turn_allocation_enforced_and_counted_by_work_kernel(self):
        from sourdough.runtime.work_kernel import WorkKernelRefused

        hired_id = "hired-1-turn"
        with closing(open_writer(self.store)) as conn, write_tx(conn):
            conn.execute(
                "INSERT INTO agent VALUES (?, ?, ?)",
                (hired_id, self.company_id, "profile-hired"),
            )
            conn.execute(
                "INSERT INTO standing_grant VALUES ('g-hired', ?, 'assessment')",
                (hired_id,),
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS hire_receipt (
                       request_id TEXT PRIMARY KEY NOT NULL,
                       choice_id TEXT NOT NULL UNIQUE,
                       manager_grant_id TEXT NOT NULL,
                       agent_id TEXT NOT NULL UNIQUE REFERENCES agent(id),
                       grant_id TEXT NOT NULL UNIQUE REFERENCES standing_grant(id),
                       allocated_turns INTEGER NOT NULL CHECK (allocated_turns > 0),
                       terms TEXT NOT NULL,
                       hired_at INTEGER NOT NULL
                   )"""
            )
            conn.execute(
                """INSERT INTO hire_receipt VALUES (
                       'req-h1', 'ch-h1', ?, ?, 'g-hired', 1, '{}', 100
                   )""",
                (self.receipt.grant_id, hired_id),
            )
            item1, _ = create_work_item(
                conn,
                company_id=self.company_id,
                owner_agent_id=hired_id,
                duty_kind="work.task",
                source_kind="manual",
                source_id="t1",
                now_ns=1_000,
            )
            item2, _ = create_work_item(
                conn,
                company_id=self.company_id,
                owner_agent_id=hired_id,
                duty_kind="work.task",
                source_kind="manual",
                source_id="t2",
                now_ns=1_001,
            )

        ran: list[str] = []
        runtime = CompanyRuntime(
            self.store,
            self.company_id,
            build_default_registry(
                {
                    "work.task": lambda ctx: (
                        ran.append(ctx.work_item.work_item_id)
                        or DutyOutcome("completed", {"done": True})
                    )
                }
            ),
            lease_directory=self.leases,
        )
        before_runs = count_company_runs(self.store, self.company_id)
        summary = runtime.step(max_dispatches=4, now_ns=2_000)
        self.assertEqual(ran, [item1.work_item_id])
        self.assertEqual(summary.blocked_items, (item2.work_item_id,))
        self.assertEqual(
            count_company_runs(self.store, self.company_id), before_runs + 1
        )

        # Direct claim_work_run and has_turn_capacity must also refuse item2
        with closing(open_writer(self.store)) as conn:
            self.assertFalse(has_turn_capacity(conn, hired_id))
            with self.assertRaisesRegex(WorkKernelRefused, "remaining turn allocation"):
                with write_tx(conn):
                    claim_work_run(
                        conn,
                        company_id=self.company_id,
                        work_item_id=item2.work_item_id,
                        lease_owner="direct-worker",
                        lease_ttl_ns=10_000,
                        now_ns=2_500,
                    )

    def test_revoked_standing_grant_refuses_kernel_claim_and_runtime_dispatch(self):
        from sourdough.runtime.work_kernel import WorkKernelRefused

        with closing(open_writer(self.store)) as conn, write_tx(conn):
            item, _ = create_work_item(
                conn,
                company_id=self.company_id,
                owner_agent_id=self.founder_id,
                duty_kind="work.guarded",
                source_kind="manual",
                source_id="g1",
                now_ns=1_000,
            )
            conn.execute(
                "UPDATE standing_grant SET scope = 'revoked' WHERE agent_id = ?",
                (self.founder_id,),
            )

        executed = False

        def guarded_handler(_ctx: DutyContext) -> DutyOutcome:
            nonlocal executed
            executed = True
            return DutyOutcome("completed")

        runtime = CompanyRuntime(
            self.store,
            self.company_id,
            build_default_registry({"work.guarded": guarded_handler}),
            lease_directory=self.leases,
        )
        summary = runtime.step(max_dispatches=2, now_ns=2_000)
        self.assertFalse(executed)
        self.assertEqual(summary.executed_runs, ())
        self.assertEqual(summary.blocked_items, (item.work_item_id,))

        with closing(open_writer(self.store)) as conn:
            with self.assertRaisesRegex(WorkKernelRefused, "no active standing grant"):
                with write_tx(conn):
                    claim_work_run(
                        conn,
                        company_id=self.company_id,
                        work_item_id=item.work_item_id,
                        lease_owner="direct-worker",
                        lease_ttl_ns=10_000,
                        now_ns=2_500,
                    )

    def test_cross_path_active_agent_exclusion_and_expired_peer_response_skip(self):
        from sourdough.first_duty import DutyRefused, claim_first_duty
        from sourdough.runtime.work_kernel import WorkKernelRefused

        # Upgrade founding grant to paper scope so claim_first_duty is otherwise eligible
        with closing(open_writer(self.store)) as conn, write_tx(conn):
            conn.execute(
                "UPDATE standing_grant SET scope = 'paper' WHERE agent_id = ?",
                (self.founder_id,),
            )
            kernel_item, _ = create_work_item(
                conn,
                company_id=self.company_id,
                owner_agent_id=self.founder_id,
                duty_kind="work.active",
                source_kind="manual",
                source_id="a1",
                now_ns=1_000,
            )
            active_run = claim_work_run(
                conn,
                company_id=self.company_id,
                work_item_id=kernel_item.work_item_id,
                lease_owner="kernel-worker",
                lease_ttl_ns=60_000,
                now_ns=1_100,
            )

        # Legacy claim_first_duty MUST refuse while kernel run is active for the same Agent
        with self.assertRaisesRegex(DutyRefused, "active running execution"):
            claim_first_duty(self.store, self.company_id, "profile-v1")

        # Yield kernel run to peer wait, then claim legacy duty -> kernel must now exclude founder
        peer_id = "peer-expired"
        with closing(open_writer(self.store)) as conn, write_tx(conn):
            conn.execute(
                "INSERT INTO agent VALUES (?, ?, ?)",
                (peer_id, self.company_id, "profile-peer"),
            )
            conn.execute(
                "INSERT INTO standing_grant VALUES ('grant-peer-exp', ?, 'manage')",
                (peer_id,),
            )
            open_peer_request(
                conn,
                company_id=self.company_id,
                requester_work_item_id=kernel_item.work_item_id,
                requester_run_id=active_run.run_id,
                fencing_token=active_run.fencing_token,
                recipient_agent_id=peer_id,
                question="Too late?",
                expected_decision="yes_no",
                evidence_refs=[],
                deadline_ns=5_000,
                request_id="req-expired-step",
                now_ns=1_200,
            )
        claim_first_duty(self.store, self.company_id, "profile-v1")

        peer_ran = False
        founder_ran = False

        def peer_handler(_ctx: DutyContext) -> DutyOutcome:
            nonlocal peer_ran
            peer_ran = True
            return DutyOutcome("completed")

        def founder_handler(_ctx: DutyContext) -> DutyOutcome:
            nonlocal founder_ran
            founder_ran = True
            return DutyOutcome("completed")

        runtime = CompanyRuntime(
            self.store,
            self.company_id,
            build_default_registry(
                {
                    "work.active": founder_handler,
                    "work.peer_response": peer_handler,
                }
            ),
            lease_directory=self.leases,
        )
        # Step past peer deadline:
        # 1) Peer request expires & cancels response item -> peer_handler MUST NOT run
        # 2) Founder still has active legacy duty -> founder_handler MUST NOT run either
        s_exp = runtime.step(max_dispatches=4, now_ns=10_000)
        self.assertFalse(peer_ran)
        self.assertFalse(founder_ran)
        self.assertEqual(s_exp.executed_runs, ())
        with closing(open_writer(self.store)) as conn:
            with self.assertRaisesRegex(WorkKernelRefused, "active running execution"):
                with write_tx(conn):
                    claim_work_run(
                        conn,
                        company_id=self.company_id,
                        work_item_id=kernel_item.work_item_id,
                        lease_owner="direct-worker",
                        lease_ttl_ns=10_000,
                        now_ns=10_100,
                    )
