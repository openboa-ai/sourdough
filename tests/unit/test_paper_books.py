import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import claim_first_duty
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import (
    PostingRefused,
    capture_position,
    position_quantity,
    post_confirmed_fill,
    read_fill_accounting_source,
)
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
        account = self.venue.provision(account, 100, fee_bps=100)
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

    def test_accounting_source_reopens_with_the_original_fee_and_binding(self):
        order_id = self.submit(110)
        witness = self.venue.observe_execution(order_id)
        self.assertIsNone(
            read_fill_accounting_source(self.store, self.company_id, witness.fill_id)
        )
        posting = self.post(order_id)
        source = read_fill_accounting_source(
            self.store, self.company_id, posting.fill_id
        )
        self.assertIsNotNone(source)
        self.assertEqual(
            (source.destination_id, source.account_id, source.fill_id, source.order_id),
            (
                self.venue.observe_account("account").destination_id,
                "account",
                witness.fill_id,
                order_id,
            ),
        )
        self.assertEqual(
            (source.binding_revision, source.currency, source.mapping_revision),
            ("mandate", "USD", "paper-quote-fee-v1"),
        )
        self.assertEqual(source.fee_minor_units, 2)
        self.assertEqual(source.fee_minor_units, witness.fee_minor_units)
        self.assertGreater(source.recorded_at_ns, 0)
        self.assertEqual(self.post(order_id), posting)
        self.assertEqual(
            read_fill_accounting_source(self.store, self.company_id, posting.fill_id),
            source,
        )
        with self.assertRaisesRegex(PostingRefused, "identity mismatch"):
            read_fill_accounting_source(self.store, "other-company", posting.fill_id)
        with closing(sqlite3.connect(self.store)) as book, book:
            book.execute("DROP TABLE paper_fill_accounting_source")
        self.assertEqual(self.post(order_id), posting)
        self.assertIsNone(
            read_fill_accounting_source(self.store, self.company_id, posting.fill_id)
        )

    def test_accounting_source_failure_rolls_back_the_new_fill_not_the_venue(self):
        order_id = self.submit(110)
        with closing(sqlite3.connect(self.store)) as book, book:
            book.execute(
                """CREATE TRIGGER refuse_reservation_update
                   BEFORE UPDATE OF reserved_minor_units ON paper_order
                   BEGIN SELECT RAISE(ABORT, 'source transaction interrupted'); END"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "transaction interrupted"):
            self.post(order_id)
        with closing(sqlite3.connect(self.store)) as book, book:
            self.assertEqual(
                book.execute(
                    """SELECT name FROM sqlite_schema
                       WHERE name IN ('paper_fill', 'paper_fill_fee', 'paper_fill_accounting_source')"""
                ).fetchall(),
                [],
            )
            self.assertEqual(
                book.execute("SELECT reserved_minor_units FROM paper_order").fetchone(),
                (220,),
            )
            book.execute("DROP TRIGGER refuse_reservation_update")
        self.assertEqual(self.venue.observe_execution(order_id).fee_minor_units, 2)
        result = self.post(order_id)
        self.assertIsNotNone(
            read_fill_accounting_source(self.store, self.company_id, result.fill_id)
        )
        with closing(sqlite3.connect(self.journal)) as venue:
            self.assertEqual(
                venue.execute("SELECT count(*) FROM venue_execution").fetchone(),
                (1,),
            )

    def test_unfilled_and_unattributed_orders_do_not_book_a_position(self):
        self.assertIsNone(self.post(self.submit(99)))
        self.venue.submit(
            "not-admitted", "account", PaperIntent("other", "rules", 2, 110)
        )
        with self.assertRaises(PostingRefused):
            self.post("not-admitted")
        self.assertEqual(position_quantity(self.store, self.company_id), 0)

    def test_book_observation_is_scoped_and_replays_the_captured_revision(self):
        order_id = self.submit(110)
        posting = self.post(order_id)
        with self.assertRaisesRegex(PostingRefused, "does not exist"):
            capture_position(self.store, self.company_id, "unposted", now=101)
        observation = capture_position(
            self.store, self.company_id, posting.fill_id, now=101
        )
        self.assertEqual(observation.account_id, "account")
        self.assertEqual(observation.instrument_rules_digest, "rules")
        self.assertEqual(observation.order_id, order_id)
        self.assertEqual(observation.position_quantity, 2)
        self.assertEqual(observation.fill_quantity, 2)
        self.assertEqual(observation.available_at, 101)
        replay = capture_position(self.store, self.company_id, posting.fill_id, now=102)
        self.assertEqual(replay, observation)
        self.assertEqual(replay.revision, observation.revision)
        with self.assertRaisesRegex(PostingRefused, "identity mismatch"):
            capture_position(self.store, "other-company", posting.fill_id, now=102)

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
