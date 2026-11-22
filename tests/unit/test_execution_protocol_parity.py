import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import DutyRefused, claim_first_duty
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_execution import (
    PaperIntent,
    VenueExecution,
    VenuePosition,
    VenueRefused,
    observe_bound_position,
    require_separate_journal,
)
from sourdough.paper_order import OrderRefused, submit_paper_order
from sourdough.paper_reconcile import reconcile_paper_orders
from sourdough.paper_venue import FixedQuoteVenue


FIXTURE = Path(__file__).resolve().parents[1] / "flows/flow-07/protocol_emulator.py"


class WireVenue:
    def __init__(self, journal, withhold):
        self.journal = journal
        self.call("initialize", withhold=withhold)

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
        fact = next(iter(response["trades"]), None) if account_sync else response
        if not fact:
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
            ),
        )

    def read_cancellation(self, account_id, order_id, *, account_sync=False):
        return None

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
            outside = "SELECT client_id,amount,execution_id FROM remote_order"
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
        self.assertEqual(
            rows(store, "SELECT name FROM sqlite_schema WHERE name='paper_order'"), []
        )
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
