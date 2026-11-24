import json
import os
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time_ns

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import DutyRefused, claim_first_duty
from sourdough.market_worker import SpotInstrumentTerms
from sourdough.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.paper_account import (
    BindingRefused,
    PaperAccount,
    PaperMandate,
    bind,
    reopen_binding,
)
from sourdough.paper_books import PostingRefused
from sourdough.paper_execution import (
    PaperIntent,
    VenueCancellation,
    VenueExecution,
    VenuePosition,
    VenueRefused,
    observe_bound_position,
    require_separate_journal,
)
from sourdough.paper_order import OrderRefused, submit_paper_order
from sourdough.paper_reconcile import MAX_LOOKUP_ATTEMPTS, reconcile_paper_orders
from sourdough.paper_venue import FixedQuoteVenue


FIXTURE = Path(__file__).resolve().parents[1] / "flows/flow-07/protocol_emulator.py"


class WireVenue:
    def __init__(
        self, journal, withhold=None, *, partial=False, delay=False, profile_fields=None
    ):
        self.journal = journal
        if withhold is not None:
            self.call(
                "initialize",
                withhold=withhold,
                partial=partial,
                delay=delay,
                profile_fields=profile_fields or {},
            )

    def call(self, operation, **fields):
        response = subprocess.run(
            [sys.executable, "-IB", str(FIXTURE), str(self.journal)],
            input=json.dumps({"op": operation, **fields}),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            env={},
        )
        return json.loads(response.stdout)

    def observe_account(self, account_id):
        raw = self.call("account")
        account = PaperAccount(
            *(
                raw[field]
                for field in (
                    "ledger",
                    "endpoint",
                    "termsHash",
                    "settlement",
                    "funded",
                    "mode",
                    "endpointId",
                )
            )
        )
        if account_id != account.account_id:
            raise VenueRefused("Not the original emulated account")
        return account

    def check_order_terms(self, account_id, intent):
        account = self.observe_account(account_id)
        if intent.instrument_rules_digest != account.instrument_rules_digest:
            raise VenueRefused("Wrong instrument terms")
        return account

    def require_independent_from(self, store):
        require_separate_journal(store, self.journal)

    def observe_order_sizing(self, account_id):
        self.observe_account(account_id)

    def observe_quote(self, account_id):
        self.observe_account(account_id)
        return self.call("account")["mark"]

    def observe_research_quote(self, account_id):
        return self.observe_quote(account_id), None

    def observe_fee_bps(self, account_id):
        self.observe_account(account_id)
        return self.call("account")["takerBps"]

    def submit(self, order_id, account_id, intent):
        ack = self.call(
            "send",
            ledger=account_id,
            clientId=order_id,
            clientAction=intent.action_id,
            termsHash=intent.instrument_rules_digest,
            amount=intent.quantity,
            ceiling=intent.limit_minor_units,
        )["ack"]
        if ack != order_id:
            raise TimeoutError("Original acknowledgement not available")

    def read_fills(self, account_id, order_id, *, account_sync=False):
        response = self.call(
            "accountSync" if account_sync else "history",
            ledger=account_id,
            clientId=order_id,
        )
        facts = response["trades" if account_sync else "history"]
        if not facts:
            return ()
        account = self.observe_account(account_id)
        return (
            VenueExecution(
                fact["clientId"],
                account_id,
                account.instrument_rules_digest,
                fact["amount"],
                fact["ceiling"],
                fact["executionId"],
                fact["price"],
                fact["charge"],
                fact["executed"],
            )
            for fact in facts
        )

    def read_cancellation(self, account_id, order_id, *, account_sync=False):
        event = self.call(
            "terminal", ledger=account_id, clientId=order_id, sync=account_sync
        )
        if not event:
            return None
        account = self.observe_account(account_id)
        return VenueCancellation(
            order_id,
            account_id,
            account.instrument_rules_digest,
            event["eventId"],
            event["cumExecuted"],
        )

    def read_order_position(self, account_id, order_id):
        response = self.call("position", ledger=account_id, clientId=order_id)
        if not response:
            return None
        account = self.observe_account(account_id)
        return VenuePosition(
            order_id, account_id, account.instrument_rules_digest, response["openLots"]
        )

    def publish(self):
        self.call("publish")


class PaperFixture(FixedQuoteVenue):
    def __init__(self, journal, withhold):
        super().__init__(journal)
        self.withhold = withhold

    def submit(self, order_id, account_id, intent):
        super().submit(order_id, account_id, intent)
        if self.withhold:
            raise TimeoutError("Original acknowledgement not available")

    def read_fills(self, account_id, order_id, *, account_sync=False):
        if self.withhold:
            return ()
        return super().read_fills(account_id, order_id, account_sync=account_sync)

    def publish(self):
        self.withhold = False


def rows(store, sql):
    with closing(sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)) as database:
        return database.execute(sql).fetchall()


class ExecutionProtocolParity(unittest.TestCase):
    def test_substituted_destination_receives_no_send_or_original_lookup(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        store = root / "company.sqlite"
        original = WireVenue(root / "original.sqlite", True)
        account = original.observe_account("shadow-cash")
        company_id = found(
            store, FoundingTerms("isolation", "charter", "paper", "employee")
        ).company_id
        mandate = PaperMandate("mandate", company_id, "charter", account, 300)
        bind(store, mandate, original)
        claim = claim_first_duty(store, company_id, "employee")
        intent = PaperIntent("original", account.instrument_rules_digest, 2, 110)
        live = WireVenue(
            root / "live-marker.sqlite", False, profile_fields={"mode": "live"}
        )
        substitute = WireVenue(
            root / "different-endpoint.sqlite",
            False,
            profile_fields={"endpointId": "other"},
        )
        for wrong in (live, substitute):
            with self.subTest(destination=wrong.journal.name):
                with self.assertRaises(BindingRefused):
                    bind(store, mandate, wrong)
                with self.assertRaisesRegex(OrderRefused, "destination mismatch"):
                    submit_paper_order(store, claim, intent, wrong)
        self.assertEqual(rows(store, "SELECT COUNT(*) FROM paper_order"), [(0,)])

        with self.assertRaises(TimeoutError):
            submit_paper_order(store, claim, intent, original)
        [(order_id,)] = rows(original.journal, "SELECT client_id FROM remote_order")
        for wrong in (live, substitute):
            with self.subTest(recovery=wrong.journal.name):
                with self.assertRaisesRegex(
                    PostingRefused, "original paper destination"
                ):
                    reconcile_paper_orders(store, company_id, wrong, now=100)
                self.assertEqual(
                    rows(wrong.journal, "SELECT * FROM remote_attempt"), []
                )
        self.assertEqual(reopen_binding(store, company_id), mandate)
        self.assertEqual(
            rows(
                store,
                "SELECT account_id,mandate_revision,reserved_minor_units FROM paper_order",
            ),
            [(account.account_id, mandate.revision, 220)],
        )

        original.publish()
        result = reconcile_paper_orders(store, company_id, original, now=100)
        self.assertEqual(result.unresolved, 0)
        self.assertEqual(result.recovered[0].posting.order_id, order_id)
        self.assertEqual(
            rows(
                original.journal, "SELECT * FROM remote_attempt WHERE operation='send'"
            ),
            [("send",)],
        )

    def test_empty_ledger_cannot_seal_a_delayed_original_submission(self):
        for delivers in (False, True):
            with self.subTest(delivers=delivers), TemporaryDirectory() as directory:
                self.observe_unfenced_absence(Path(directory), delivers)

    def observe_unfenced_absence(self, root, delivers):
        store, journal = root / "company.sqlite", root / "venue.sqlite"
        venue = WireVenue(journal, False, delay=True)
        account = venue.observe_account("shadow-cash")
        company_id = found(
            store, FoundingTerms("delay", "charter", "paper", "employee")
        ).company_id
        bind(store, PaperMandate("mandate", company_id, "charter", account, 300), venue)
        claim = claim_first_duty(store, company_id, "employee")
        intent = PaperIntent("delayed-action", account.instrument_rules_digest, 2, 110)
        with self.assertRaises(TimeoutError):
            submit_paper_order(store, claim, intent, venue)
        original = submit_paper_order(store, claim, intent, venue)
        self.assertFalse(original.dispatched_now)
        self.assertEqual(
            rows(journal, "SELECT client_id FROM deferred_send"), [(original.order_id,)]
        )
        self.assertEqual(rows(journal, "SELECT client_id FROM remote_order"), [])
        now = 100
        for _attempt in range(1 if delivers else MAX_LOOKUP_ATTEMPTS):
            receipt = self.reconcile_reopened(store, company_id, journal, "wire", now)[
                "receipt"
            ]
            self.assertEqual((receipt["unresolved"], receipt["recovered"]), (1, []))
            self.assertEqual(
                rows(store, "SELECT reserved_minor_units FROM paper_order"), [(220,)]
            )
            self.assertEqual(rows(journal, "SELECT client_id FROM remote_order"), [])
            now = receipt["next_query_at"]
        with self.assertRaises(OrderRefused):
            submit_paper_order(
                store,
                claim,
                replace(intent, action_id="replacement", quantity=1),
                venue,
            )
        self.assertEqual(
            rows(journal, "SELECT client_id FROM deferred_send"), [(original.order_id,)]
        )
        if not delivers:
            self.assertIsNone(now)
            after = self.reconcile_reopened(store, company_id, journal, "wire", 10_000)[
                "receipt"
            ]
            self.assertEqual((after["unresolved"], after["next_query_at"]), (1, None))
            self.assertEqual(
                rows(store, "SELECT reserved_minor_units FROM paper_order"), [(220,)]
            )
            return
        self.assertIsNotNone(now)
        venue.call(
            "deliverFixture", ledger=account.account_id, clientId=original.order_id
        )
        self.assertEqual(
            rows(journal, "SELECT client_id FROM remote_order"), [(original.order_id,)]
        )
        self.assertEqual(rows(journal, "SELECT client_id FROM deferred_send"), [])
        resolved = self.reconcile_reopened(store, company_id, journal, "wire", now)[
            "receipt"
        ]
        self.assertEqual(resolved["unresolved"], 0)
        [applied] = resolved["recovered"]
        self.assertEqual(applied["posting"]["order_id"], original.order_id)
        self.assertEqual(rows(store, "SELECT count(*) FROM paper_fill"), [(1,)])
        self.assertEqual(
            rows(store, "SELECT reserved_minor_units FROM paper_order"), [(200,)]
        )

    def test_common_admission_denial_and_original_effect_on_two_destinations(self):
        for withhold in (False, True):
            results = []
            for external in (False, True):
                with (
                    self.subTest(withhold=withhold, external=external),
                    TemporaryDirectory() as directory,
                ):
                    results.append(self.observe(Path(directory), external, withhold))
            self.assertEqual(results, [(2, 100, 1, 200, 2), (2, 100, 1, 200, 2)])

    def observe(self, root, external, withhold):
        store, journal = root / "company.sqlite", root / "venue.sqlite"
        venue = (WireVenue if external else PaperFixture)(journal, withhold)
        if external:
            account = venue.observe_account("shadow-cash")
            outside = "SELECT client_id,amount,execution_id FROM remote_order JOIN remote_fill USING(client_id)"
        else:
            account = venue.provision(
                PaperAccount(
                    "paper-cash",
                    "fixed-quote",
                    "sealed-common-terms",
                    "USD",
                    1000,
                    "paper",
                ),
                100,
                fee_bps=50,
            )
            outside = "SELECT order_id,quantity,fill_id FROM venue_execution"
        company = found(store, FoundingTerms("parity", "charter", "paper", "employee"))
        company_id = company.company_id
        venue.require_independent_from(store)
        bind(store, PaperMandate("mandate", company_id, "charter", account, 500), venue)
        claim = claim_first_duty(store, company_id, "employee")
        with self.assertRaises(OrderRefused):
            submit_paper_order(
                store,
                claim,
                PaperIntent("too-large", account.instrument_rules_digest, 5, 110),
                venue,
            )
        with self.assertRaises(DutyRefused):
            submit_paper_order(
                store,
                replace(claim, run_id="stale"),
                PaperIntent("denied", account.instrument_rules_digest, 1, 110),
                venue,
            )
        self.assertEqual(rows(journal, outside), [])
        self.assertEqual(rows(store, "SELECT COUNT(*) FROM paper_order"), [(0,)])
        intent = PaperIntent("company-action", account.instrument_rules_digest, 2, 110)
        if withhold:
            with self.assertRaises(TimeoutError):
                submit_paper_order(store, claim, intent, venue)
        else:
            self.assertTrue(
                submit_paper_order(store, claim, intent, venue).dispatched_now
            )
        replay = submit_paper_order(store, claim, intent, venue)
        self.assertFalse(replay.dispatched_now)
        [(order_id, quantity, fill_id)] = rows(journal, outside)
        self.assertEqual((order_id, quantity), (replay.order_id, 2))
        self.assertEqual(
            rows(store, "SELECT reserved_minor_units,action_id FROM paper_order"),
            [(220, intent.action_id)],
        )
        reconciled = reconcile_paper_orders(store, company_id, venue, now=100)
        if withhold:
            self.assertEqual(
                (reconciled.unresolved, reconciled.recovered, reconciled.next_query_at),
                (1, (), 102),
            )
            self.assertEqual(
                rows(store, "SELECT reserved_minor_units FROM paper_order"), [(220,)]
            )
            venue.publish()
            reconciled = reconcile_paper_orders(store, company_id, venue, now=102)
        self.assertEqual(reconciled.unresolved, 0)
        [booked] = reconciled.recovered
        self.assertEqual(booked.original_claim, claim)
        self.assertEqual(booked.posting.fill_id, fill_id)
        self.assertEqual(booked.sources, ("history", "account_sync"))
        again = reconcile_paper_orders(store, company_id, venue, now=103)
        self.assertEqual(again.recovered[0].posting, booked.posting)
        self.assertEqual(rows(journal, outside), [(order_id, quantity, fill_id)])
        [(booked_quantity, price, fee, remaining)] = rows(
            store,
            """SELECT fill.quantity,fill.price_minor_units,fee.fee_minor_units,orders.reserved_minor_units
               FROM paper_fill fill JOIN paper_fill_fee fee USING(fill_id)
               JOIN paper_order orders USING(order_id)""",
        )
        position = observe_bound_position(venue, account, order_id)
        self.assertIsNotNone(position)
        return booked_quantity, price, fee, remaining, position.position_quantity

    def test_partial_then_late_fill_and_cancellation_across_company_restarts(self):
        results = []
        for adapter in ("managed", "wire"):
            with self.subTest(adapter=adapter), TemporaryDirectory() as directory:
                results.append(self.observe_partial(Path(directory), adapter))
        self.assertEqual(results, [([(2, 100, 0), (1, 100, 0)], 300, 3)] * 2)

    def observe_partial(self, root, adapter):
        store, journal = root / "company.sqlite", root / "venue.sqlite"
        company = found(store, FoundingTerms("partial", "charter", "paper", "employee"))
        company_id = company.company_id
        if adapter == "wire":
            venue = WireVenue(journal, False, partial=True)
            account = venue.observe_account("shadow-cash")
            orders_sql = "SELECT client_id,amount FROM remote_order"
            fills_sql = "SELECT execution_id,executed,price,charge FROM remote_fill ORDER BY rowid"
            cancel_sql = "SELECT event_id,executed FROM remote_cancel"
        else:
            venue = NautilusPaperVenue(journal)
            terms = SpotInstrumentTerms(
                "BTCUSD", "BTC", "USD", "0.01", "0.000001", "0.000001", "0.000001"
            )
            venue.provision(SandboxPlan(company_id, "managed", terms, 1000, 0))
            venue.prove_account(
                "generation", "BINANCE-001", "USD", venue.start_generation()
            )
            stamp = time_ns()
            venue.publish_quote("generation", 100, stamp, stamp)
            account = venue.observe_account("managed")
            orders_sql = "SELECT order_id,quantity FROM sandbox_order"
            fills_sql = "SELECT fill_id,quantity,price_atoms,fee_atoms FROM sandbox_fill_observation ORDER BY rowid"
            cancel_sql = "SELECT event_id,filled_quantity FROM sandbox_cancellation"
        bind(store, PaperMandate("mandate", company_id, "charter", account, 800), venue)
        claim = claim_first_duty(store, company_id, "employee")
        intent = PaperIntent("partial-action", account.instrument_rules_digest, 5, 110)
        if adapter == "wire":
            order = submit_paper_order(store, claim, intent, venue).order_id
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(submit_paper_order, store, claim, intent, venue)
                deadline = monotonic() + 4
                command = venue.claim_command("generation")
                while command is None and not pending.done() and monotonic() < deadline:
                    sleep(0.01)
                    command = venue.claim_command("generation")
                self.assertIsNotNone(command)
                venue.record_fill("generation", command.order_id, "first", 2, 100, 0)
                venue.mark_unresolved("generation", command.order_id)
                order = pending.result(timeout=2).order_id
        self.assertEqual(rows(journal, orders_sql), [(order, 5)])

        first = self.reconcile_reopened(store, company_id, journal, adapter, 100)
        self.assertEqual(first["receipt"]["unresolved"], 1)
        self.assertEqual(rows(store, "SELECT quantity FROM paper_fill"), [(2,)])
        self.assertEqual(
            rows(store, "SELECT reserved_minor_units FROM paper_order"), [(530,)]
        )
        if adapter == "wire":
            venue.call("cancelFixture", ledger=account.account_id, clientId=order)
        else:
            venue.record_cancellation("generation", order, "terminal", 3)
        [(terminal, reported_total)] = rows(journal, cancel_sql)
        self.assertEqual(reported_total, 3)
        awaiting = self.reconcile_reopened(store, company_id, journal, adapter, 102)
        self.assertEqual(awaiting["receipt"]["unresolved"], 1)
        self.assertEqual(rows(store, "SELECT count(*) FROM paper_fill"), [(1,)])
        self.assertEqual(
            rows(store, "SELECT reserved_minor_units FROM paper_order"), [(530,)]
        )
        self.assertEqual(
            rows(store, "SELECT event_id,released FROM paper_cancellation"),
            [(terminal, 0)],
        )

        if adapter == "wire":
            venue.call("lateFillFixture", ledger=account.account_id, clientId=order)
        else:
            venue.record_fill("generation", order, "late", 1, 100, 0)
            self.assertTrue(venue.record_account("generation", order, 3, 700))
        final = self.reconcile_reopened(store, company_id, journal, adapter, 106)
        self.assertEqual(final["receipt"]["unresolved"], 0)
        self.assertEqual(
            final["receipt"]["recovered"][0]["sources"], ["history", "account_sync"]
        )
        replay = self.reconcile_reopened(store, company_id, journal, adapter, 107)
        self.assertEqual(
            len({item["pid"] for item in (first, awaiting, final, replay)}), 4
        )
        self.assertEqual(
            replay["receipt"]["recovered"][0]["posting"],
            final["receipt"]["recovered"][0]["posting"],
        )
        self.assertEqual(
            rows(store, "SELECT event_id,released FROM paper_cancellation"),
            [(terminal, 1)],
        )
        internal = rows(
            store,
            """SELECT fill_id,quantity,price_minor_units,fee_minor_units
                                  FROM paper_fill JOIN paper_fill_fee USING(fill_id) ORDER BY paper_fill.rowid""",
        )
        self.assertEqual(internal, rows(journal, fills_sql))
        self.assertEqual(rows(journal, orders_sql), [(order, 5)])
        [(reserved,)] = rows(store, "SELECT reserved_minor_units FROM paper_order")
        position = observe_bound_position(venue, account, order)
        return [fill[1:] for fill in internal], reserved, position.position_quantity

    def reconcile_reopened(self, store, company_id, journal, adapter, now):
        result = subprocess.run(
            [
                sys.executable,
                "-IB",
                __file__,
                str(store),
                company_id,
                str(journal),
                adapter,
                str(now),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            env={},
        )
        return json.loads(result.stdout)


if __name__ == "__main__":
    store_name, company_id, journal_name, adapter, clock = sys.argv[1:]
    journal = Path(journal_name)
    venue = (NautilusPaperVenue if adapter == "managed" else WireVenue)(journal)
    receipt = reconcile_paper_orders(
        Path(store_name), company_id, venue, now=int(clock)
    )
    print(json.dumps({"pid": os.getpid(), "receipt": asdict(receipt)}))
