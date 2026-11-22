import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import claim_first_duty
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import PostingRefused, position_quantity, post_confirmed_fill
from sourdough.paper_order import OrderRefused, PaperIntent, submit_paper_order
from sourdough.paper_venue import FixedQuoteVenue


class PaperBooksTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.journal = directory / "venue.sqlite"
        founder = found(
            self.store, FoundingTerms("setup", "charter", "paper", "runtime")
        )
        self.company_id = founder.company_id
        self.venue = FixedQuoteVenue(self.journal)
        account = PaperAccount("account", "local-v1", "rules", "USD", 5000, "paper")
        account = self.venue.provision(account, 100)
        bind(
            self.store,
            PaperMandate("mandate", self.company_id, "charter", account, 1000),
            self.venue,
        )
        self.claim = claim_first_duty(self.store, self.company_id, "runtime")

    def submit(self, limit):
        return submit_paper_order(
            self.store,
            self.claim,
            PaperIntent("action", "rules", 2, limit),
            self.venue,
        ).order_id

    def post(self, order_id):
        return post_confirmed_fill(
            self.store, self.company_id, order_id, FixedQuoteVenue(self.journal)
        )

    def test_post_and_replay_match_independent_venue_journal(self):
        order_id = self.submit(110)
        posting = self.post(order_id)
        self.assertIsNotNone(posting)
        self.assertEqual(self.post(order_id), posting)
        self.assertEqual(position_quantity(self.store, self.company_id), 2)
        with (
            closing(
                sqlite3.connect(f"{self.store.as_uri()}?mode=ro", uri=True)
            ) as book,
            closing(
                sqlite3.connect(f"{self.journal.as_uri()}?mode=ro", uri=True)
            ) as venue,
        ):
            self.assertEqual(
                book.execute(
                    "SELECT order_id, fill_id, quantity FROM paper_fill"
                ).fetchall(),
                venue.execute(
                    "SELECT order_id, fill_id, quantity FROM venue_execution"
                ).fetchall(),
            )
            self.assertEqual(
                book.execute("SELECT reserved_minor_units FROM paper_order").fetchone(),
                (200,),
            )

    def test_unfilled_and_unattributed_orders_do_not_book_a_position(self):
        self.assertIsNone(self.post(self.submit(99)))
        self.venue.submit(
            "not-admitted", "account", PaperIntent("other", "rules", 2, 110)
        )
        with self.assertRaises(PostingRefused):
            self.post("not-admitted")
        self.assertEqual(position_quantity(self.store, self.company_id), 0)

    def test_replacement_journal_cannot_inherit_dispatch_authority(self):
        replacement = FixedQuoteVenue(self.journal.with_name("replacement.sqlite"))
        replacement.provision(
            PaperAccount("account", "local-v1", "rules", "USD", 5000, "paper"), 50
        )
        with self.assertRaises(OrderRefused):
            submit_paper_order(
                self.store,
                self.claim,
                PaperIntent("action", "rules", 2, 110),
                replacement,
            )
        with closing(sqlite3.connect(replacement.journal)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM venue_execution").fetchone(), (0,)
            )
