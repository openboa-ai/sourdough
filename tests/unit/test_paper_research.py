import sqlite3
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.duty_run import DutyRefused
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_execution import PaperOrderSizing
from sourdough.paper_order import OrderRefused, PaperIntent, submit_paper_order
from sourdough.paper_research import (
    ResearchTools,
    reopen_research_decision,
    stage_research_inputs,
)
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.trading.duty import claim_paper_first_duty as claim_first_duty


class PaperResearch(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.store = root / "company.sqlite"
        self.venue = FixedQuoteVenue(root / "venue.sqlite")
        self.company_id = found(
            self.store, FoundingTerms("setup", "charter", "paper", "runtime")
        ).company_id
        account = self.venue.provision(
            PaperAccount("account", "synthetic", "contract", "USD", 50000, "paper"),
            5000,
            fee_bps=4,
        )
        bind(
            self.store,
            PaperMandate("mandate", self.company_id, "charter", account, 25000),
            self.venue,
        )
        self.inputs = [
            {
                "observation_id": "early",
                "price_minor_units": 4900,
                "observed_at": 80,
                "available_at": 82,
            },
            {
                "observation_id": "late",
                "price_minor_units": 5000,
                "observed_at": 90,
                "available_at": 92,
            },
            {
                "observation_id": "unknown_at_cut",
                "price_minor_units": 5100,
                "observed_at": 98,
                "available_at": 105,
            },
        ]
        self.plan = stage_research_inputs(self.store, self.company_id, self.inputs)
        self.claim = claim_first_duty(self.store, self.company_id, "runtime")
        self.tools = ResearchTools(self.store, self.claim, 5000, 4, 25000)
        self.proposal = {
            "call_id": "decision-one",
            "action": "buy",
            "rationale": "A 100-unit observation change less 4 units of fees leaves 96; exercise one bounded paper order.",
            "next_check_seconds": 60,
            "quantity": 1,
            "limit_minor_units": 5000,
        }

    def test_research_reopens_the_original_destination_sizing(self):
        rules = PaperOrderSizing(
            "BTCUSDT", "0.00001", "USDT", 100_000_000, 10, 1, 500_000_000
        )
        tools = replace(self.tools, sizing=rules)
        snapshot = tools.invoke("read_research_inputs", {}, now=100)
        self.assertEqual(snapshot["order_sizing"], asdict(rules))
        changed = replace(tools, sizing=replace(rules, minimum_lots=20))
        self.assertEqual(changed.invoke("read_research_inputs", {}, now=106), snapshot)

    def finding(self):
        snapshot = self.tools.invoke("read_research_inputs", {}, now=100)
        self.assertEqual(
            [item["observation_id"] for item in snapshot["observations"]],
            ["early", "late"],
        )
        self.assertEqual(
            snapshot, self.tools.invoke("read_research_inputs", {}, now=106)
        )
        return {
            "snapshot_revision": snapshot["revision"],
            "hypothesis": "Does the observed price move exceed fees?",
            "start_id": "early",
            "end_id": "late",
            "gross_change_minor_units": 100,
            "round_trip_fee_minor_units": 4,
            "net_change_minor_units": 96,
            "uncertainty": "Two synthetic observations do not predict market returns.",
        }

    def submit(self, action, *, quantity=1):
        return submit_paper_order(
            self.store,
            self.claim,
            PaperIntent(action, "contract", quantity, 5000),
            self.venue,
        )

    def test_order_requires_original_finding_and_decision_committed_before_send(self):
        with self.assertRaisesRegex(OrderRefused, "committed research decision"):
            self.submit("unrecorded")
        with self.assertRaisesRegex(DutyRefused, "Publish a finding"):
            self.tools.invoke("record_research_decision", self.proposal, now=100)
        finding = self.finding()
        published = self.tools.invoke("publish_research_finding", finding, now=101)
        self.assertEqual(
            self.tools.invoke("publish_research_finding", finding, now=102), published
        )
        proposal = self.proposal | {
            "finding_id": published["finding_id"],
            "finding_revision": published["revision"],
        }
        receipt = self.tools.invoke("record_research_decision", proposal, now=102)
        self.assertEqual(
            self.tools.invoke("record_research_decision", proposal, now=103), receipt
        )
        with self.assertRaisesRegex(OrderRefused, "exact terms"):
            self.submit(receipt["decision_id"], quantity=2)
        with closing(sqlite3.connect(self.venue.journal)) as source:
            self.assertEqual(
                source.execute("SELECT count(*) FROM venue_execution").fetchone(), (0,)
            )
        committed = reopen_research_decision(self.store, self.claim)
        self.assertEqual(committed.finding_id, published["finding_id"])
        self.assertEqual(committed.next_due_at, 162)
        admitted = self.submit(receipt["decision_id"])
        self.assertTrue(admitted.dispatched_now)
        self.assertFalse(self.submit(receipt["decision_id"]).dispatched_now)
        actual = self.venue.observe_execution(admitted.order_id)
        self.assertEqual((actual.quantity, actual.fee_minor_units), (1, 2))

    def test_wrong_arithmetic_future_observation_and_foreign_lineage_are_denied(self):
        finding = self.finding()
        with self.assertRaisesRegex(DutyRefused, "available at its cut"):
            self.tools.invoke(
                "publish_research_finding",
                finding | {"end_id": "unknown_at_cut"},
                now=106,
            )
        with self.assertRaisesRegex(DutyRefused, "arithmetic"):
            self.tools.invoke(
                "publish_research_finding",
                finding | {"net_change_minor_units": 97},
                now=102,
            )
        result = self.tools.invoke("publish_research_finding", finding, now=102)
        with self.assertRaisesRegex(DutyRefused, "earlier finding"):
            self.tools.invoke(
                "record_research_decision",
                self.proposal
                | {"finding_id": "foreign", "finding_revision": result["revision"]},
                now=103,
            )
        with self.assertRaisesRegex(DutyRefused, "earlier finding"):
            self.tools.invoke(
                "record_research_decision",
                self.proposal
                | {
                    "finding_id": result["finding_id"],
                    "finding_revision": result["revision"],
                },
                now=101,
            )

    def test_inputs_cannot_change_and_stale_run_cannot_publish(self):
        self.assertEqual(
            stage_research_inputs(self.store, self.company_id, self.inputs), self.plan
        )
        with self.assertRaisesRegex(DutyRefused, "cannot be replaced"):
            stage_research_inputs(self.store, self.company_id, self.inputs[:2])
        finding = self.finding()
        with closing(sqlite3.connect(self.store)) as connection, connection:
            connection.execute("UPDATE duty_run SET is_current = 0")
        with self.assertRaisesRegex(DutyRefused, "no longer current"):
            self.tools.invoke("publish_research_finding", finding, now=105)
