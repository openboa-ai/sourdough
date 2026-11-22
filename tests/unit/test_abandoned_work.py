import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.abandoned_work import requeue_uncommitted_work
from sourdough.company import FoundingTerms, found
from sourdough.duty_run import DutyRefused
from sourdough.first_duty import claim_first_duty, require_paper_authority
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_order import PaperIntent, submit_paper_order
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.work import (
    ResultReference,
    ReviewDecision,
    claim_follow_up,
    finish_first_duty,
    read_follow_up_result,
    settle_review,
)
from sourdough.writer_lease import hold_company_writer


class RecoverAbandonedWork(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.company_id = found(
            self.store, FoundingTerms("setup", "charter", "paper", "profile")
        ).company_id

    def requeue(self):
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.root / "lease"
        ) as writer:
            return requeue_uncommitted_work(writer)

    def start(self):
        return claim_first_duty(self.store, self.company_id, "profile")

    def scheduled(self):
        return claim_follow_up(self.store, self.company_id, "profile", now=100)

    def test_original_duty_requeued_once_and_old_claim_cannot_add_risk(self):
        departed = self.start()
        self.assertEqual(self.requeue(), (departed.run_id,))
        self.assertEqual(self.requeue(), ())
        recovered = self.start()
        self.assertEqual(recovered.duty_id, departed.duty_id)
        self.assertEqual(recovered.founder_id, departed.founder_id)
        self.assertNotEqual(recovered.run_id, departed.run_id)
        with closing(sqlite3.connect(self.store)) as connection:
            with self.assertRaisesRegex(DutyRefused, "no longer current"):
                require_paper_authority(connection, departed)
            require_paper_authority(connection, recovered)

    def test_requeued_review_keeps_previous_read_but_requires_a_new_read(self):
        opening = self.start()
        result = ResultReference("document", "result", "observed", 100)
        finish_first_duty(self.store, opening, result, due_at=100)
        old = self.scheduled()

        def project(connection, ref):
            return {"revision": ref.revision}

        read_follow_up_result(self.store, old, now=100, read=project)
        self.assertEqual(self.requeue(), (old.claim.run_id,))
        new = self.scheduled()
        proposal = ReviewDecision(
            "new-decision", result.revision, "hold", "Checked.", 60
        )
        with self.assertRaisesRegex(DutyRefused, "no longer current"):
            settle_review(self.store, old, proposal, now=101)
        with self.assertRaisesRegex(DutyRefused, "authorized result read"):
            settle_review(self.store, new, proposal, now=101)
        read_follow_up_result(self.store, new, now=101, read=project)
        follow_up = settle_review(self.store, new, proposal, now=102)
        self.assertEqual(follow_up.due_at, 162)
        with closing(sqlite3.connect(self.store)) as connection:
            self.assertCountEqual(
                connection.execute("SELECT run_id FROM work_result_read").fetchall(),
                [(old.claim.run_id,), (new.claim.run_id,)],
            )

    def test_duty_with_external_effect_is_not_requeued(self):
        venue = FixedQuoteVenue(self.root / "venue.sqlite")
        account = venue.provision(
            PaperAccount("paper", "local", "rules", "USD", 1000, "paper"), 100
        )
        bind(
            self.store,
            PaperMandate("mandate", self.company_id, "charter", account, 500),
            venue,
        )
        opening = self.start()
        order = submit_paper_order(
            self.store, opening, PaperIntent("action", "rules", 1, 100), venue
        )
        self.assertEqual(self.requeue(), ())
        self.assertIsNotNone(venue.observe_execution(order.order_id))
        with closing(sqlite3.connect(self.store)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM duty_run").fetchone(), (1,)
            )
            self.assertEqual(
                connection.execute("SELECT state FROM duty").fetchone(), ("running",)
            )
