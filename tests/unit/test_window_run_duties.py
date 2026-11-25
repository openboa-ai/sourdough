import argparse
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from unittest.mock import patch

from sourdough.cli import _run_paper_window
from sourdough.company import FoundingTerms, found
from sourdough.general_ledger import JournalRefused
from sourdough.market_worker import SpotInstrumentTerms
from sourdough.nautilus_paper import NautilusPaperVenue, SandboxFeeEvidence, SandboxPlan
from sourdough.nautilus_runtime import paper_venue
from sourdough.paper_account import PaperMandate, bind
from sourdough.paper_fee_bridge import FeeChart, deliver_managed_paper_fee
from sourdough.paper_valuation import (
    BEST_BID_POLICY,
    ValuationRefused,
    ValuationRequest,
)
from sourdough.paper_window import WindowPlan, WindowRefused
from sourdough.runtime.registry import DutyContext, DutyRegistry
from sourdough.runtime.work_kernel import WorkItem, WorkRun
from sourdough.window_run import (
    RECONCILE_DUTY_KIND,
    WINDOW_CLOSE_DUTY_KIND,
    register_window_duties,
)
from sourdough.writer_lease import hold_company_writer


class WindowRunDutiesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.engine = self.root / "engine"
        self.engine.mkdir()
        self.leases = self.root / "leases"
        self.company = found(
            self.store, FoundingTerms("window-duties", "charter", "paper", "employee")
        ).company_id
        self.terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        self.venue = paper_venue(self.store, self.engine)
        self.venue.journal.parent.mkdir(parents=True, exist_ok=True)
        self.venue.provision(
            SandboxPlan(self.company, "paper", self.terms, 100_000_000_000, 0)
        )
        self.venue.prove_account(
            "original", "BINANCE-001", "USDT", self.venue.start_generation()
        )
        self.account = self.venue.observe_account("paper")
        self.binding = bind(
            self.store,
            PaperMandate(
                "binding-1", self.company, "charter", self.account, 10_000_000_000
            ),
            self.venue,
        )

    def _context(self, writer, duty_kind: str, payload_json: str) -> DutyContext:
        now = time_ns()
        item = WorkItem(
            work_item_id="item-1",
            company_id=self.company,
            owner_agent_id="agent-1",
            duty_kind=duty_kind,
            state="running",
            source_kind="manual",
            source_id="src-1",
            parent_work_item_id=None,
            dedupe_key=None,
            payload_json=payload_json,
            created_at_ns=now,
            updated_at_ns=now,
        )
        run = WorkRun(
            run_id="run-1",
            company_id=self.company,
            work_item_id="item-1",
            agent_id="agent-1",
            duty_kind=duty_kind,
            status="running",
            fencing_token=1,
            lease_owner="worker-1",
            lease_expires_at_ns=now + 60 * 10**9,
            wait_until_ns=None,
            wait_event_key=None,
            budget_ceiling_minor=0,
            dedupe_key=None,
            outcome_json=None,
            created_at_ns=now,
            updated_at_ns=now,
        )
        return DutyContext(
            writer=writer,
            company_id=self.company,
            work_item=item,
            run=run,
            _clock=now,
        )

    def test_ops_reconcile_handler_runs_with_clock_and_recovered_count(self):
        registry = DutyRegistry()
        register_window_duties(registry, engine_directory=self.engine)
        descriptor = registry.get(RECONCILE_DUTY_KIND)
        with hold_company_writer(
            self.store, self.company, lease_directory=self.leases
        ) as writer:
            outcome = descriptor.execute(
                self._context(writer, RECONCILE_DUTY_KIND, "{}")
            )
        self.assertEqual(outcome.disposition, "completed")
        self.assertEqual(outcome.result, {"settled_orders": 0, "unresolved": 0})

    def test_quant_window_close_rejects_malformed_and_non_string_payloads(self):
        registry = DutyRegistry()
        register_window_duties(registry, engine_directory=self.engine)
        descriptor = registry.get(WINDOW_CLOSE_DUTY_KIND)
        bad_payloads = (
            "{not-json",
            "[]",
            json.dumps({"request_id": None, "window_id": "w1"}),
            json.dumps({"request_id": "r1", "window_id": 123}),
            json.dumps(
                {"request_id": "r1", "window_id": "w1", "accounting_close_id": 42}
            ),
            json.dumps({"request_id": "r1", "window_id": "w1", "supersedes": {}}),
        )
        with hold_company_writer(
            self.store, self.company, lease_directory=self.leases
        ) as writer:
            for raw in bad_payloads:
                with self.assertRaises(WindowRefused):
                    descriptor.execute(
                        self._context(writer, WINDOW_CLOSE_DUTY_KIND, raw)
                    )

    def test_run_paper_window_keeps_idle_seconds_through_reconciliation_runway(self):
        captured: list[int] = []

        def fake_start(options, *, emit):
            captured.append(options.idle_seconds)
            return {"ok": True}

        now = 1_000 * 10**9
        through_ns = now + 120 * 10**9
        work_before_ns = through_ns - 150 * 10**9

        def fake_run_window(
            store, company_id, directory, window_id, company, *, segment_seconds, emit
        ):
            company(through_ns, work_before_ns, 120)
            return {"done": True}

        args = argparse.Namespace(
            store=self.store,
            company_id=self.company,
            directory=self.engine,
            window_id="w1",
            segment_seconds=600,
            employee="emp",
            repeat_paper=False,
            turn_budget=1,
        )
        with (
            patch("sourdough.cli.run_window", side_effect=fake_run_window),
            patch("sourdough.cli._start_supervisor", side_effect=fake_start),
            patch("sourdough.cli.time_ns", return_value=now),
        ):
            _run_paper_window(args)
        self.assertEqual(captured, [120])

    def test_whitespace_padded_identifiers_and_overflow_timestamps_are_refused(self):
        with self.assertRaises(WindowRefused):
            WindowPlan(
                window_id=" " * 200 + "w1",
                binding_revision="b",
                operating_revision="o",
                evaluation_mandate_revision="e",
                source_revision="s",
                valuation_revision="v",
                cost_inventory_revision="c",
                execution_assumptions_revision="x",
                cash_reference_revision="r",
                contingency_revision="k",
                opens_ns=100,
                closes_ns=200,
                cadence_ns=25,
                minimum_observations=2,
                max_exposure_minor=500,
                max_spend_minor=50,
                max_drawdown_bps=250,
                hurdle_bps=10,
            )
        with self.assertRaises(ValuationRefused):
            ValuationRequest(" " * 200 + "cap", "binding", BEST_BID_POLICY)
        chart = FeeChart(
            "chart", "paper-quote-fee-v1", "USDT", 100_000_000, "fees", "cash"
        )
        overflow_evidence = SandboxFeeEvidence(
            self.account.destination_id,
            self.account.account_id,
            "order-1",
            "fill-1",
            10**22,
            "USDT",
            100_000_000,
            10,
        )
        with (
            hold_company_writer(
                self.store, self.company, lease_directory=self.leases
            ) as writer,
            patch.object(
                NautilusPaperVenue, "read_fill_fee", return_value=overflow_evidence
            ),
            self.assertRaisesRegex(JournalRefused, "outside calendar range"),
        ):
            deliver_managed_paper_fee(writer, self.venue, "paper", "fill-1", chart)
