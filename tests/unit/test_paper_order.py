import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import DutyRefused, claim_first_duty
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_order import OrderRefused, PaperIntent, submit_paper_order


class ObservingDestination:
    def __init__(self, store, account):
        self.store = store
        self.account = account
        self.submissions = []
        self.lose_reply = False

    def observe_account(self, account_id):
        return self.account

    def check_order_terms(self, account_id, intent):
        return self.observe_account(account_id)

    def submit(self, order_id, account_id, intent):
        with closing(sqlite3.connect(f"{self.store.as_uri()}?mode=ro", uri=True)) as db:
            reservation = db.execute(
                "SELECT reserved_minor_units FROM paper_order WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        self.submissions.append((order_id, account_id, reservation))
        if self.lose_reply:
            raise TimeoutError


class PaperOrderTests(unittest.TestCase):
    def setUp(self):
        self.store = Path(self.enterContext(TemporaryDirectory())) / "company.sqlite"
        company = found(
            self.store, FoundingTerms("setup", "charter", "paper", "runtime")
        )
        account = PaperAccount(
            "account", "local-v1", "rules", "USD", 5000, "paper", "venue-id"
        )
        self.destination = ObservingDestination(self.store, account)
        bind(
            self.store,
            PaperMandate("mandate", company.company_id, "charter", account, 1000),
            self.destination,
        )
        self.claim = claim_first_duty(self.store, company.company_id, "runtime")
        self.intent = PaperIntent("first-action", "rules", 4, 200)

    def submit(self, intent):
        return submit_paper_order(self.store, self.claim, intent, self.destination)

    def test_lost_reply_preserves_exposure_without_resending(self):
        self.destination.lose_reply = True
        with self.assertRaises(TimeoutError):
            self.submit(self.intent)
        replay = self.submit(self.intent)
        self.assertFalse(replay.dispatched_now)
        self.assertEqual(
            self.destination.submissions, [(replay.order_id, "account", (800,))]
        )
        with self.assertRaises(OrderRefused):
            self.submit(replace(self.intent, quantity=3))
        with self.assertRaises(OrderRefused):
            self.submit(replace(self.intent, action_id="second-action"))
        self.assertEqual(len(self.destination.submissions), 1)

    def test_wrong_instrument_and_stale_run_do_not_dispatch(self):
        with self.assertRaises(OrderRefused):
            self.submit(replace(self.intent, instrument_rules_digest="other"))
        self.claim = replace(self.claim, run_id="stale")
        with self.assertRaises(DutyRefused):
            self.submit(self.intent)
        self.assertEqual(self.destination.submissions, [])

    def test_preflight_cannot_substitute_a_different_environment(self):
        self.destination.account = replace(self.destination.account, environment="live")
        with self.assertRaisesRegex(OrderRefused, "destination mismatch"):
            self.submit(self.intent)
        self.assertEqual(self.destination.submissions, [])
        with closing(sqlite3.connect(f"{self.store.as_uri()}?mode=ro", uri=True)) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='paper_order'"
                ).fetchone()
            )

    def test_revoked_grant_replays_only_the_original_receipt(self):
        self.destination.lose_reply = True
        with self.assertRaises(TimeoutError):
            self.submit(self.intent)
        with closing(sqlite3.connect(self.store)) as db, db:
            db.execute("UPDATE standing_grant SET scope = 'read'")
        replay = self.submit(self.intent)
        self.assertFalse(replay.dispatched_now)
        self.assertEqual(replay.order_id, self.destination.submissions[0][0])
        with self.assertRaises(DutyRefused):
            self.submit(replace(self.intent, action_id="new-action"))
        self.assertEqual(len(self.destination.submissions), 1)
