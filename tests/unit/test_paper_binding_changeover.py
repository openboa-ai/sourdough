import json
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time_ns

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import DutyRefused, claim_first_duty
from sourdough.market_worker import SpotInstrumentTerms
from sourdough.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.paper_account import (
    BindingRefused,
    PaperMandate,
    bind,
    change_paper_binding,
    reopen_binding,
)
from sourdough.paper_execution import PaperIntent, VenueRefused
from sourdough.paper_order import OrderRefused, submit_paper_order
from sourdough.paper_research import (
    ResearchTools,
    find_unsubmitted_opening,
    stage_research_inputs,
)
from sourdough.writer_lease import hold_company_writer


REOPEN = """
import json, sys
from dataclasses import asdict
from pathlib import Path
from time import time
from types import SimpleNamespace
import sourdough.company_run as company_run
from sourdough.nautilus_paper import NautilusPaperVenue

def review(read):
    result = read()
    if repeat:
        assert result["account_id"] == "old"
        assert result["current_observation"]["account_id"] is None
    return {
        "tool": "record_review_decision",
        "call_id": "historical-review" if repeat else "next-duty", "action": "hold",
        "rationale": "Handle the original cleared order.", "next_check_seconds": 300,
        "observation_revision": result["revision"],
    }
def trade(read):
    result = read()
    assert result["account_id"] == "new" and result["position_quantity"] == 0
    assert result["binding_revision"] == "revision-b"
    return {
        "tool": "record_paper_decision", "call_id": "new-account-decision", "action": "hold",
        "rationale": "Use the verified baseline and the current account only.",
        "next_check_seconds": 300, "observation_revision": result["revision"],
    }
store, company_id, journal, idle, offset, repeat = sys.argv[1:]
repeat = repeat == "True"
company_run.time = lambda: time() + int(offset)
result = company_run.run_paper_company(
    Path(store), company_id, NautilusPaperVenue(Path(journal)),
    SimpleNamespace(profile="employee", review=review, trade=trade),
    turn_budget=1, idle_seconds=int(idle), repeat_paper=repeat,
)
print(json.dumps(asdict(result)))
"""


def rows(store, sql):
    with closing(sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)) as database:
        return database.execute(sql).fetchall()


class LostReply(NautilusPaperVenue):
    def submit(self, *args):
        super().submit(*args)
        raise TimeoutError("Original order acknowledgement withheld")


class ManagedPaperChangeover(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.company = found(
            self.store, FoundingTerms("switch", "charter", "paper", "employee")
        )
        self.company_id = self.company.company_id
        self.terms = SpotInstrumentTerms(
            "BTCUSD", "BTC", "USD", "0.01", "0.000001", "0.000001", "0.000001"
        )
        self.old = self.destination("old", LostReply)
        self.new = self.destination("new", NautilusPaperVenue)
        self.current = self.mandate(self.old, "old", "revision-a")
        self.target = self.mandate(self.new, "new", "revision-b")
        bind(self.store, self.current, self.old)

    def destination(self, name, adapter):
        venue = adapter(self.root / f"{name}.sqlite")
        venue.provision(SandboxPlan(self.company_id, name, self.terms, 1000, 0))
        venue.prove_account(name, "BINANCE-001", "USD", venue.start_generation())
        stamp = time_ns()
        venue.publish_quote(name, 100, stamp, stamp)
        return venue

    def mandate(self, venue, account_id, revision):
        return PaperMandate(
            revision,
            self.company_id,
            "charter",
            venue.observe_account(account_id),
            800,
        )

    def switch(self, target=None):
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.root / "leases"
        ) as writer:
            return change_paper_binding(
                writer,
                self.current.revision,
                target or self.target,
                self.old,
                self.new,
            )

    def accept(self, claim, intent, venue, generation, *, withhold=False):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(submit_paper_order, self.store, claim, intent, venue)
            deadline = monotonic() + 4
            command = venue.claim_command(generation)
            while command is None and not pending.done() and monotonic() < deadline:
                sleep(0.01)
                command = venue.claim_command(generation)
            self.assertIsNotNone(command)
            if withhold:
                venue.mark_unresolved(generation, command.order_id)
                with self.assertRaises(TimeoutError):
                    pending.result(timeout=2)
            else:
                venue.record_fill(
                    generation, command.order_id, "fill-in-new", 2, 100, 0
                )
                self.assertEqual(pending.result(timeout=2).order_id, command.order_id)
            return command.order_id

    def reopen_process(self, venue, *, check=True, idle=0, offset=0, repeat=False):
        process = subprocess.run(
            [
                sys.executable,
                "-IB",
                "-c",
                REOPEN,
                str(self.store),
                self.company_id,
                str(venue.journal),
                str(idle),
                str(offset),
                str(repeat),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if check:
            self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout) if check else process

    def test_pending_switch_preserves_the_original_order_until_its_scope_is_drained(
        self,
    ):
        claim = claim_first_duty(self.store, self.company_id, "employee")
        intent = PaperIntent("original", self.terms.revision, 2, 110)
        order_id = self.accept(claim, intent, self.old, "old", withhold=True)
        pending = self.switch()
        self.assertIsNone(pending.effective_at_ns)
        self.assertEqual(pending.blocked_by, "outstanding-company-effect")
        self.assertEqual(reopen_binding(self.store, self.company_id), self.current)
        self.assertEqual(self.switch(), pending)
        with self.assertRaisesRegex(BindingRefused, "conflicts"):
            self.switch(replace(self.target, exposure_limit_minor_units=700))
        with self.assertRaisesRegex(OrderRefused, "new sends are fenced"):
            submit_paper_order(
                self.store, claim, replace(intent, action_id="new-risk"), self.old
            )
        self.assertNotEqual(self.reopen_process(self.new, check=False).returncode, 0)
        self.assertEqual(
            rows(self.new.journal, "SELECT count(*) FROM sandbox_order"), [(0,)]
        )

        awaiting = self.reopen_process(self.old)
        self.assertEqual(awaiting["reconciliation"]["unresolved"], 1)
        self.assertEqual(
            rows(self.store, "SELECT reserved_minor_units FROM paper_order"), [(220,)]
        )
        self.old.record_cancellation("old", order_id, "original-terminal", 0)
        self.assertTrue(self.old.record_account("old", order_id, 0, 1000))
        still_pending = self.switch()
        self.assertIsNone(still_pending.effective_at_ns)
        settled = self.reopen_process(self.old, idle=5)
        self.assertEqual(settled["reconciliation"]["unresolved"], 0)
        self.assertEqual(settled["review"]["claim"]["founder_id"], claim.founder_id)
        applied = self.switch()
        self.assertGreaterEqual(applied.effective_at_ns, pending.requested_at_ns)
        self.assertEqual(applied.requested_at_ns, pending.requested_at_ns)
        self.assertEqual(self.switch(), applied)
        self.assertEqual(reopen_binding(self.store, self.company_id), self.target)
        self.assertEqual(
            rows(
                self.store,
                "SELECT order_id,account_id,mandate_revision,reserved_minor_units FROM paper_order",
            ),
            [(order_id, "old", "revision-a", 0)],
        )
        original = submit_paper_order(self.store, claim, intent, self.new)
        self.assertFalse(original.dispatched_now)
        self.assertEqual(original.order_id, order_id)
        self.assertEqual(
            rows(self.new.journal, "SELECT count(*) FROM sandbox_order"), [(0,)]
        )
        self.assertEqual(
            rows(self.old.journal, "SELECT count(*) FROM sandbox_order"), [(1,)]
        )
        historical = self.reopen_process(self.new, offset=400, repeat=True)
        self.assertEqual(
            historical["review"]["next_duty"]["result"]["kind"], "paper_binding"
        )
        self.assertEqual(
            historical["review"]["next_duty"]["result"]["resource_id"], "revision-b"
        )
        active = self.reopen_process(self.new, offset=900, repeat=True)
        [new_duty] = active["later_trades"]
        self.assertEqual(new_duty["claim"]["founder_id"], claim.founder_id)
        self.assertEqual(new_duty["decision"]["decision"]["action"], "hold")
        self.assertIsNone(new_duty["order_id"])

    def test_clean_switch_leaves_only_the_new_account_eligible_for_new_work(self):
        self.assertIsNotNone(self.switch().effective_at_ns)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.root / "leases"
        ) as writer:
            with self.assertRaisesRegex(BindingRefused, "retired binding"):
                change_paper_binding(
                    writer, self.target.revision, self.current, self.new, self.old
                )
        claim = claim_first_duty(self.store, self.company_id, "employee")
        intent = PaperIntent("fresh-action", self.terms.revision, 2, 110)
        with self.assertRaises(OrderRefused):
            submit_paper_order(self.store, claim, intent, self.old)
        new_id = self.accept(claim, intent, self.new, "new")
        self.assertEqual(
            rows(self.old.journal, "SELECT count(*) FROM sandbox_order"), [(0,)]
        )
        self.assertEqual(
            rows(
                self.store,
                "SELECT order_id,account_id,mandate_revision FROM paper_order",
            ),
            [(new_id, "new", "revision-b")],
        )

    def test_drained_evidence_is_not_inferred_without_the_original_venue(self):
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.root / "leases"
        ) as writer:
            with self.assertRaises(VenueRefused):
                change_paper_binding(
                    writer, self.current.revision, self.target, self.new, self.new
                )
        self.assertEqual(reopen_binding(self.store, self.company_id), self.current)

    @staticmethod
    def observations(prefix):
        return [
            {
                "observation_id": f"{prefix}-first",
                "price_minor_units": 90,
                "observed_at": 60,
                "available_at": 61,
            },
            {
                "observation_id": f"{prefix}-last",
                "price_minor_units": 100,
                "observed_at": 70,
                "available_at": 71,
            },
        ]

    def test_old_research_receipt_cannot_authorize_risk_on_the_new_account(self):
        original_plan = stage_research_inputs(
            self.store, self.company_id, self.observations("old")
        )
        claim = claim_first_duty(self.store, self.company_id, "employee")
        tools = ResearchTools(self.store, claim, 100, 0, 800)
        inputs = tools.invoke("read_research_inputs", {}, now=100)
        finding = tools.invoke(
            "publish_research_finding",
            {
                "snapshot_revision": inputs["revision"],
                "hypothesis": "The observed increase exceeds the quoted synthetic costs.",
                "start_id": "old-first",
                "end_id": "old-last",
                "gross_change_minor_units": 10,
                "round_trip_fee_minor_units": 0,
                "net_change_minor_units": 10,
                "uncertainty": "No future price guarantee.",
            },
            now=101,
        )
        decision = tools.invoke(
            "record_research_decision",
            {
                "finding_id": finding["finding_id"],
                "finding_revision": finding["revision"],
                "call_id": "original-decision",
                "action": "buy",
                "quantity": 2,
                "limit_minor_units": 110,
                "next_check_seconds": 60,
                "rationale": "Bound the experiment.",
            },
            now=102,
        )
        self.assertIsNotNone(self.switch().effective_at_ns)
        self.assertIsNone(find_unsubmitted_opening(self.store, self.company_id))
        with self.assertRaisesRegex(DutyRefused, "another binding"):
            tools.invoke("read_research_inputs", {}, now=103)
        current_plan = stage_research_inputs(
            self.store, self.company_id, self.observations("new")
        )
        self.assertNotEqual(current_plan["revision"], original_plan["revision"])
        with self.assertRaises(OrderRefused):
            submit_paper_order(
                self.store,
                claim,
                PaperIntent(decision["decision_id"], self.terms.revision, 2, 110),
                self.new,
            )
        self.assertEqual(
            rows(self.new.journal, "SELECT count(*) FROM sandbox_order"), [(0,)]
        )
        self.assertEqual(
            rows(
                self.store,
                "SELECT account_id,revision FROM paper_research_plan ORDER BY account_id",
            ),
            [("new", current_plan["revision"]), ("old", original_plan["revision"])],
        )

    def test_recognizes_legacy_plan_and_reseals_only_the_new_scope(self):
        old_plan = stage_research_inputs(
            self.store, self.company_id, self.observations("old")
        )
        with closing(sqlite3.connect(self.store)) as database, database:
            database.execute("ALTER TABLE paper_research_plan RENAME TO seeded_plan")
            database.execute(
                "CREATE TABLE paper_research_plan (company_id TEXT PRIMARY KEY, revision TEXT, inputs TEXT)"
            )
            database.execute(
                "INSERT INTO paper_research_plan SELECT company_id,revision,inputs FROM seeded_plan"
            )
            database.execute("DROP TABLE seeded_plan")
        self.assertIsNotNone(self.switch().effective_at_ns)
        new_plan = stage_research_inputs(
            self.store, self.company_id, self.observations("new")
        )
        claim = claim_first_duty(self.store, self.company_id, "employee")
        snapshot = ResearchTools(self.store, claim, 100, 0, 800).invoke(
            "read_research_inputs", {}, now=100
        )
        self.assertEqual(
            (snapshot["account_id"], snapshot["source_plan_revision"]),
            ("new", new_plan["revision"]),
        )
        self.assertEqual(
            rows(
                self.store,
                "SELECT account_id,revision FROM paper_research_plan ORDER BY account_id",
            ),
            [("new", new_plan["revision"]), ("old", old_plan["revision"])],
        )
