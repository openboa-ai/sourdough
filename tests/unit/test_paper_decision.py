import sqlite3
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, _connect, found
from sourdough.duty_run import PAPER_EXECUTION, DutyRefused, start_run
from sourdough.first_duty import claim_first_duty
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import capture_position, post_confirmed_fill, read_position
from sourdough.paper_decision import PaperDecision, persist_paper_decision
from sourdough.paper_order import OrderRefused, PaperIntent, submit_paper_order
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.work import (
    FollowUpClaim,
    ResultReference,
    claim_follow_up,
    finish_first_duty,
    read_follow_up_result,
)


class ReceivedThenUnknown(FixedQuoteVenue):
    def __init__(self, journal):
        super().__init__(journal)
        self.sends = []

    def submit(self, order_id, account_id, intent):
        self.sends.append(order_id)
        super().submit(order_id, account_id, intent)
        raise TimeoutError("receipt not delivered")


class LaterPaperDecision(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.store = root / "company.sqlite"
        self.venue = FixedQuoteVenue(root / "venue.sqlite")
        self.founding = found(
            self.store, FoundingTerms("setup", "charter", "paper", "runtime")
        )
        self.company_id = self.founding.company_id
        account = self.venue.provision(
            PaperAccount("account", "local", "instrument", "USD", 5000, "paper"), 100
        )
        bind(
            self.store,
            PaperMandate("mandate", self.company_id, "charter", account, 1000),
            self.venue,
        )
        first = claim_first_duty(self.store, self.company_id, "runtime")
        order = submit_paper_order(
            self.store, first, PaperIntent("first", "instrument", 1, 100), self.venue
        )
        fill = post_confirmed_fill(
            self.store, self.company_id, order.order_id, self.venue
        )
        position = capture_position(self.store, self.company_id, fill.fill_id, now=100)
        self.result = ResultReference(
            "paper_position", fill.fill_id, position.revision, 100
        )
        scheduled = finish_first_duty(
            self.store, first, self.result, due_at=100, purpose=PAPER_EXECUTION
        )
        self.assertIsNone(
            claim_follow_up(self.store, self.company_id, "runtime", now=100)
        )
        self.work = claim_follow_up(
            self.store, self.company_id, "runtime", now=100, allow_paper_execution=True
        )
        self.assertEqual(self.work.claim.duty_id, scheduled.duty_id)
        self.proposal = PaperDecision(
            "decision-tool", position.revision, "One unit is booked.", 60, "buy", 2, 100
        )

    def start(self, duty_id, *, fence=None):
        with closing(_connect(self.store, "rw")) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if fence:
                db.execute(
                    "UPDATE duty_run SET is_current = 0 WHERE run_id = ?", (fence,)
                )
                db.execute(
                    "UPDATE duty SET state = 'scheduled' WHERE id = ?", (duty_id,)
                )
            return start_run(
                db,
                self.company_id,
                self.founding.founder_id,
                self.founding.grant_id,
                duty_id,
                "runtime",
                purpose=PAPER_EXECUTION,
                grant_scope="paper",
            )

    def read(self):
        def project(connection, reference):
            position = read_position(connection, self.company_id, reference.resource_id)
            return asdict(position) | {"revision": position.revision}

        return read_follow_up_result(
            self.store, self.work, now=101, read=project, purpose=PAPER_EXECUTION
        )

    def submit(self, action_id, *, quantity=2, limit=100, venue=None, work=None):
        return submit_paper_order(
            self.store,
            (work or self.work).claim,
            PaperIntent(action_id, "instrument", quantity, limit),
            venue or self.venue,
        )

    def counts(self):
        with closing(
            sqlite3.connect(self.venue.journal.as_uri() + "?mode=ro", uri=True)
        ) as db:
            return db.execute("SELECT count(*) FROM venue_execution").fetchone()[0]

    def test_bound_decision_must_be_observed_and_exact_before_admission(self):
        with self.assertRaisesRegex(OrderRefused, "persisted decision"):
            self.submit("unrecorded")
        with self.assertRaisesRegex(DutyRefused, "authorized result read"):
            persist_paper_decision(self.store, self.work, self.proposal, now=101)
        self.assertEqual(self.read()["position_quantity"], 1)
        with self.assertRaisesRegex(DutyRefused, "authorized result read"):
            persist_paper_decision(
                self.store,
                self.work,
                replace(self.proposal, observation_revision="wrong"),
                now=102,
            )
        receipt = persist_paper_decision(self.store, self.work, self.proposal, now=102)
        self.assertEqual(
            persist_paper_decision(self.store, self.work, self.proposal, now=103),
            receipt,
        )
        with self.assertRaisesRegex(DutyRefused, "conflicts"):
            persist_paper_decision(
                self.store, self.work, replace(self.proposal, quantity=3), now=103
            )
        with self.assertRaisesRegex(OrderRefused, "exact terms"):
            self.submit(receipt.decision_id, quantity=3)
        self.assertEqual(self.counts(), 1)
        order = self.submit(receipt.decision_id)
        self.assertTrue(order.dispatched_now)
        self.assertEqual(self.counts(), 2)

    def test_unknown_reply_and_replacement_return_the_original_order_without_resend(
        self,
    ):
        self.read()
        decision = persist_paper_decision(self.store, self.work, self.proposal, now=102)
        destination = ReceivedThenUnknown(self.venue.journal)
        with self.assertRaises(TimeoutError):
            self.submit(decision.decision_id, venue=destination)
        replacement = FollowUpClaim(
            self.start(self.work.claim.duty_id, fence=self.work.claim.run_id),
            self.result,
            PAPER_EXECUTION,
        )
        historical = self.submit(
            decision.decision_id, venue=destination, work=replacement
        )
        self.assertFalse(historical.dispatched_now)
        self.assertEqual(destination.sends, [historical.order_id])
        self.assertEqual(self.counts(), 2)
        with self.assertRaisesRegex(OrderRefused, "different terms"):
            self.submit(
                decision.decision_id, quantity=3, venue=destination, work=replacement
            )
        with closing(_connect(self.store, "ro")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT reserved_minor_units FROM paper_order WHERE order_id = ?",
                    (historical.order_id,),
                ).fetchone(),
                (200,),
            )

    def test_hold_cannot_be_reinterpreted_as_a_buy(self):
        self.read()
        hold = replace(
            self.proposal, action="hold", quantity=None, limit_minor_units=None
        )
        receipt = persist_paper_decision(self.store, self.work, hold, now=102)
        with self.assertRaisesRegex(OrderRefused, "exact terms"):
            self.submit(receipt.decision_id)
        self.assertEqual(self.counts(), 1)
