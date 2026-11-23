import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
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
    register_responsibility,
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
