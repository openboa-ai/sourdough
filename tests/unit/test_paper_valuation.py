import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep, time_ns
from unittest.mock import Mock, patch

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import claim_first_duty
from sourdough.market_worker import SpotInstrumentTerms
from sourdough.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.nautilus_runtime import VALUATION_SOURCE_REVISION
from sourdough.paper_account import PaperMandate, bind
from sourdough.paper_execution import PaperIntent, VenueRefused
from sourdough.paper_order import submit_paper_order
from sourdough.paper_valuation import (
    BEST_BID_POLICY,
    ValuationRefused,
    ValuationRequest,
    capture_paper_valuation,
    capture_window_sample,
    read_paper_valuation,
    read_window_samples,
    read_window_valuation,
)
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.paper_window import WindowPlan, WindowRefused, register_window
from sourdough.paper_window_movement import read_paper_movement
from sourdough.trading_engine import VERSION
from sourdough.window_sampler import WindowSampler, register_managed_window
from sourdough.writer_lease import hold_company_writer


class PaperValuationTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        store, engine = root / "company.sqlite", root / "engine"
        runtime = engine / f"nautilus-{VERSION}"
        runtime.mkdir(parents=True)
        venue = NautilusPaperVenue(runtime / "sandbox.sqlite")
        company = found(store, FoundingTerms("nav", "paper", "paper", "profile"))
        company_id = company.company_id
        terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        initial = 100_000_000_000
        venue.provision(SandboxPlan(company_id, "paper", terms, initial, 0))
        venue.prove_account("venue", "BINANCE-001", "USDT", venue.start_generation())
        bind(
            store,
            PaperMandate(
                "binding", company_id, "paper", venue.observe_account("paper"), 10**10
            ),
            venue,
        )
        self.store, self.engine, self.venue = store, engine, venue
        self.company_id, self.terms, self.initial = company_id, terms, initial
        self.opening = ValuationRequest("opening", "binding", BEST_BID_POLICY)

    def capture(self, request):
        with hold_company_writer(self.store, self.company_id) as writer:
            return capture_paper_valuation(writer, self.engine, request)

    def plan(self, opens, closes, *, cadence=20):
        return WindowPlan(
            window_id="first",
            binding_revision="binding",
            operating_revision="incumbent",
            evaluation_mandate_revision="mandate",
            source_revision=VALUATION_SOURCE_REVISION,
            valuation_revision=BEST_BID_POLICY,
            cost_inventory_revision="costs",
            execution_assumptions_revision="simulation",
            cash_reference_revision="reference",
            contingency_revision="contingency",
            opens_ns=opens,
            closes_ns=closes,
            cadence_ns=cadence,
            minimum_observations=2,
            max_exposure_minor=10**10,
            max_spend_minor=1,
            max_drawdown_bps=100,
            hurdle_bps=1,
        )

    def test_capture_pins_independent_cash_and_bid_without_revaluing_a_retry(self):
        store, venue = self.store, self.venue
        company_id, terms, initial = self.company_id, self.terms, self.initial
        opening = self.opening
        closing_cut = replace(opening, capture_id="closing")
        now = time_ns()
        plan = self.plan(now, now + 10**10, cadence=10**9)
        with (
            patch("sourdough.paper_window.time_ns", return_value=now - 1),
            hold_company_writer(store, company_id) as writer,
        ):
            window = register_window(writer, plan)
        stamp = time_ns()
        ask = 59_000_000
        venue.publish_quote("venue", ask, stamp, stamp, bid_atoms_per_lot=ask - 100)
        before = self.capture(opening)
        self.assertEqual(before.trading_nav_atoms, initial)
        claim = claim_first_duty(store, company_id, "profile")
        intent = PaperIntent("order", terms.revision, 20, ask)
        with ThreadPoolExecutor(max_workers=1) as pool:
            submission = pool.submit(submit_paper_order, store, claim, intent, venue)
            command = None
            deadline = monotonic() + 4
            while command is None and monotonic() < deadline:
                command = venue.claim_command("venue")
                sleep(0.01)
            self.assertIsNotNone(command)
            fill_time = time_ns()
            venue.record_fill(
                "venue", command.order_id, "fill", 20, ask, 4, effective_ns=fill_time
            )
            self.assertEqual(submission.result(timeout=2).order_id, command.order_id)
        with self.assertRaisesRegex(VenueRefused, "missing a corroborated"):
            self.capture(closing_cut)
        self.assertIsNone(read_paper_valuation(store, company_id, closing_cut))
        cash = initial - 20 * ask - 4
        self.assertTrue(venue.record_account("venue", command.order_id, 20, cash))
        stamp = time_ns()
        bid = ask + 30
        venue.publish_quote("venue", ask + 50, stamp, stamp, bid_atoms_per_lot=bid)
        after = self.capture(closing_cut)
        self.assertEqual(after.trading_nav_atoms, initial + 596)
        witness = json.loads(after.evidence)["observation"]
        self.assertEqual(witness["balance"], {"cash_atoms": cash, "position_lots": 20})
        self.assertEqual(
            (witness["quote_event_ns"], witness["bid_atoms_per_lot"]), (stamp, bid)
        )
        venue.invalidate_quote()
        self.assertEqual(self.capture(closing_cut), after)
        self.assertEqual(read_paper_valuation(store, company_id, opening), before)
        movement = read_paper_movement(
            store, company_id, self.engine, plan.window_id, opening, closing_cut
        )
        self.assertEqual(movement.window_revision, window.revision)
        self.assertEqual(
            (movement.opening_revision, movement.closing_revision),
            (before.revision, after.revision),
        )
        self.assertEqual(
            (
                movement.currency,
                movement.cash_change_atoms,
                movement.lot_change,
                movement.fee_atoms,
                movement.fill_ids,
            ),
            ("USDT", cash - initial, 20, 4, ("fill",)),
        )
        with self.assertRaisesRegex(ValuationRefused, "ordered cuts"):
            read_paper_movement(
                store, company_id, self.engine, plan.window_id, closing_cut, opening
            )
        with closing(sqlite3.connect(venue.journal)) as db, db:
            db.execute(
                "UPDATE sandbox_fill_time SET effective_ns=? WHERE fill_id='fill'",
                (fill_time - 1,),
            )
        with self.assertRaisesRegex(VenueRefused, "no longer matches the cut"):
            read_paper_movement(
                store, company_id, self.engine, plan.window_id, opening, closing_cut
            )
        with self.assertRaisesRegex(ValuationRefused, "original request"):
            self.capture(replace(closing_cut, binding_revision="new"))
        with self.assertRaisesRegex(ValuationRefused, "another company"):
            read_paper_valuation(store, "unrelated", closing_cut)
        with closing(sqlite3.connect(store)) as database:
            self.assertEqual(
                database.execute("SELECT count(*) FROM paper_valuation").fetchone(),
                (2,),
            )

    def test_window_keeps_both_receipt_revisions_and_rejects_outside_evidence(self):
        now = time_ns()
        opens, closes = now + 10, now + 100
        plan = self.plan(opens, closes)
        with (
            patch("sourdough.paper_window.time_ns", return_value=now),
            hold_company_writer(self.store, self.company_id) as writer,
        ):
            window = register_window(writer, plan)
            register_window(
                writer,
                replace(
                    plan,
                    window_id="other-source",
                    source_revision="unsupported-source",
                    opens_ns=closes + 1,
                    closes_ns=closes + 100,
                ),
            )

        def observe(name, event, received, trade, observed):
            self.venue.publish_quote(
                "venue",
                50_000_000,
                received,
                trade,
                event_ns=event,
                bid_atoms_per_lot=49_900_000,
            )
            request = replace(self.opening, capture_id=name)
            with patch("sourdough.nautilus_paper.time_ns", return_value=observed):
                return self.capture(request)

        def qualified(window_id, request):
            return read_window_valuation(
                self.store, self.company_id, window_id, request
            )

        with self.assertRaisesRegex(ValuationRefused, "no registered receipt"):
            qualified("not-registered", self.opening)
        with self.assertRaisesRegex(ValuationRefused, "no captured receipt"):
            qualified(plan.window_id, self.opening)
        inside = observe("inside", opens, opens + 1, opens, opens + 2)
        admitted = qualified(plan.window_id, inside.request)
        self.assertEqual(admitted.window_revision, window.revision)
        self.assertEqual(admitted.valuation, inside)
        with self.assertRaisesRegex(ValuationRefused, "sealed account and policies"):
            qualified("other-source", inside.request)

        before = observe("before", opens - 1, opens, opens, opens + 1)
        trade_before = observe("old-trade", opens, opens, opens - 1, opens + 1)
        late = observe("late", closes - 2, closes - 1, closes - 2, closes + 1)
        for cut in (before, trade_before, late):
            with (
                self.subTest(cut=cut.request.capture_id),
                self.assertRaisesRegex(ValuationRefused, "outside the sealed window"),
            ):
                qualified(plan.window_id, cut.request)
        self.venue.invalidate_quote()
        self.assertEqual(qualified(plan.window_id, inside.request), admitted)

    def test_schedule_keeps_first_evidence_and_reports_unfilled_slots(self):
        now = time_ns()
        opens = now + 100
        plan = self.plan(opens, opens + 98)
        with (
            patch("sourdough.paper_window.time_ns", return_value=now),
            hold_company_writer(self.store, self.company_id) as writer,
        ):
            window = register_window(writer, plan)

        def report():
            return read_window_samples(self.store, self.company_id, plan.window_id)

        def capture_at(slot, event, available, observed, bid=49_900_000):
            self.venue.publish_quote(
                "venue",
                50_000_000,
                available,
                opens,
                event_ns=event,
                bid_atoms_per_lot=bid,
            )
            with (
                patch("sourdough.nautilus_paper.time_ns", return_value=observed),
                hold_company_writer(self.store, self.company_id) as writer,
            ):
                return capture_window_sample(writer, self.engine, plan.window_id, slot)

        self.assertEqual(report().missing_ranges, ((0, 4),))
        first = capture_at(0, opens, opens + 1, opens + 2)
        self.assertEqual(
            capture_at(0, opens + 3, opens + 4, opens + 5, bid=49_950_000), first
        )
        with self.assertRaisesRegex(ValuationRefused, "outside its scheduled slot"):
            capture_at(1, opens + 19, opens + 20, opens + 21)
        third = capture_at(2, opens + 40, opens + 41, opens + 42)
        with self.assertRaisesRegex(ValuationRefused, "outside its scheduled slot"):
            capture_at(1, opens + 43, opens + 44, opens + 45)
        self.assertEqual(capture_at(0, opens + 100, opens + 101, opens + 102), first)
        with self.assertRaisesRegex(ValuationRefused, "outside the sealed window"):
            capture_at(1, opens + 100, opens + 101, opens + 102)
        with self.assertRaisesRegex(ValuationRefused, "not part of this sealed window"):
            capture_at(5, opens + 100, opens + 101, opens + 102)
        with self.assertRaisesRegex(ValuationRefused, "cannot be assigned explicitly"):
            self.capture(first.valuation.request)
        self.venue.invalidate_quote()
        coverage = report()
        self.assertEqual(coverage.window_revision, window.revision)
        self.assertEqual(
            coverage.captures, ((0, first.valuation), (2, third.valuation))
        )
        self.assertEqual(coverage.missing_ranges, ((1, 1), (3, 4)))

    def test_sampler_uses_the_active_managed_destination_and_stops_with_writer(self):
        now = time_ns()
        plan = self.plan(now + 100, now + 198)
        started, stopped = Event(), Event()
        sampler = WindowSampler(self.engine, plan.window_id)

        def collect(writer, window, stop):
            self.assertEqual(window.plan, plan)
            writer.verify()
            started.set()
            self.assertTrue(stop.wait(2))
            writer.verify()
            stopped.set()

        with (
            patch("sourdough.paper_window.time_ns", return_value=now),
            hold_company_writer(self.store, self.company_id) as writer,
        ):
            with self.assertRaisesRegex(WindowRefused, "original managed"):
                register_managed_window(
                    writer, self.engine, replace(plan, source_revision="other")
                )
            window = register_managed_window(writer, self.engine, plan)
            with self.assertRaisesRegex(
                WindowRefused, "separate execution destination"
            ):
                with sampler.activate(
                    writer, FixedQuoteVenue(self.store.parent / "fixture.sqlite")
                ):
                    self.fail("Started a sampler for a fixture destination")
            with (
                patch.object(WindowSampler, "_collect", side_effect=collect),
                sampler.activate(writer, self.venue),
            ):
                self.assertTrue(started.wait(2))
                self.assertFalse(stopped.is_set())
            self.assertTrue(stopped.is_set())
            self.assertEqual(window, register_managed_window(writer, self.engine, plan))

    def test_sampler_retries_only_the_current_slot_and_skips_elapsed_slots(self):
        now = time_ns()
        opens = now + 100
        plan = self.plan(opens, opens + 98)
        sampler = WindowSampler(self.engine, plan.window_id)
        with (
            patch("sourdough.paper_window.time_ns", return_value=now),
            hold_company_writer(self.store, self.company_id) as writer,
        ):
            window = register_managed_window(writer, self.engine, plan)
            stop = Mock(spec=Event)
            stop.is_set.return_value = False
            with (
                patch(
                    "sourdough.window_sampler.time_ns",
                    side_effect=(
                        opens - 1,
                        opens + 1,
                        opens + 2,
                        opens + 3,
                        opens + 45,
                        opens + 100,
                    ),
                ),
                patch(
                    "sourdough.window_sampler.capture_window_sample",
                    side_effect=(VenueRefused("no fresh quote"), None, None),
                ) as capture,
            ):
                sampler._collect(writer, window, stop)
            self.assertEqual(
                [call.args[-1] for call in capture.call_args_list], [0, 0, 2]
            )
