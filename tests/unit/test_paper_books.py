import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
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
    read_booked_fill,
)
from sourdough.paper_execution import VenueCancellation
from sourdough.paper_order import OrderRefused, PaperIntent, submit_paper_order
from sourdough.paper_reconcile import MAX_LOOKUP_ATTEMPTS, reconcile_paper_orders
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.work_projection import read_local_board_timeline


class ObservedFillVenue(FixedQuoteVenue):
    def __init__(self, journal, fill_id, quantity, price, fee):
        super().__init__(journal)
        self.terms = {
            "fill_id": fill_id,
            "filled_quantity": quantity,
            "fill_price_minor_units": price,
            "fee_minor_units": fee,
        }

    def observe_execution(self, order_id):
        return replace(super().observe_execution(order_id), **self.terms)

    def sync_account_execution(self, account_id, order_id):
        return replace(
            super().sync_account_execution(account_id, order_id), **self.terms
        )


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

    def submit(self, limit, quantity=2):
        return submit_paper_order(
            self.store,
            self.claim,
            PaperIntent("action", "rules", quantity, limit),
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

    def test_partial_fills_resume_by_original_order_and_hold_the_unfilled_reservation(
        self,
    ):
        order_id = self.submit(99)
        first = ObservedFillVenue(self.journal, "first", 1, 80, 1)
        initial = reconcile_paper_orders(self.store, self.company_id, first, now=100)
        self.assertEqual(initial.recovered, ())
        self.assertEqual(initial.unresolved, 1)
        self.assertEqual(position_quantity(self.store, self.company_id), 1)
        self.assertIsNone(read_booked_fill(self.store, self.company_id, order_id))
        projection = read_local_board_timeline(self.store, self.company_id)["paper"]
        [order] = projection["orders"]
        self.assertEqual(
            (
                order["reserved_minor_units"],
                order["open_reservation_minor_units"],
                order["remaining_quantity"],
            ),
            (179, 99, 1),
        )
        self.assertEqual(projection["unresolved_orders"], 1)
        for _attempt in range(MAX_LOOKUP_ATTEMPTS):
            self.assertIsNotNone(initial.next_query_at)
            initial = reconcile_paper_orders(
                self.store, self.company_id, first, now=initial.next_query_at
            )
            self.assertEqual((initial.recovered, initial.unresolved), ((), 1))
        self.assertIsNotNone(initial.next_query_at)
        with self.assertRaisesRegex(PostingRefused, "exceed the admitted order"):
            post_confirmed_fill(
                self.store,
                self.company_id,
                order_id,
                ObservedFillVenue(self.journal, "overfill", 2, 90, 1),
            )
        with self.assertRaisesRegex(PostingRefused, "conflicts"):
            post_confirmed_fill(
                self.store,
                self.company_id,
                order_id,
                ObservedFillVenue(self.journal, "first", 1, 81, 1),
            )
        second = ObservedFillVenue(self.journal, "second", 1, 90, 2)
        complete = reconcile_paper_orders(
            self.store, self.company_id, second, now=initial.next_query_at
        )
        [recovered] = complete.recovered
        self.assertEqual(
            (recovered.posting.fill_id, complete.unresolved), ("second", 0)
        )
        self.assertEqual(recovered.sources, ("history", "account_sync"))
        self.assertEqual(
            post_confirmed_fill(self.store, self.company_id, order_id, first),
            recovered.posting,
        )
        self.assertEqual(position_quantity(self.store, self.company_id), 2)
        projection = read_local_board_timeline(self.store, self.company_id)["paper"]
        [order] = projection["orders"]
        self.assertEqual(
            (
                order["reserved_minor_units"],
                order["open_reservation_minor_units"],
                order["fee_minor_units"],
            ),
            (170, 0, 3),
        )
        self.assertEqual(projection["booked_position"], 2)
        with closing(sqlite3.connect(self.journal)) as observed:
            self.assertEqual(
                observed.execute("SELECT order_id FROM venue_execution").fetchall(),
                [(order_id,)],
            )

    def test_legacy_single_fill_preserves_posting_fees_and_position_snapshot(self):
        order_id = self.submit(110)
        original = self.venue.observe_execution(order_id)
        with closing(sqlite3.connect(self.store)) as book, book:
            book.execute(
                """CREATE TABLE paper_fill (fill_id TEXT PRIMARY KEY NOT NULL,
                   order_id TEXT UNIQUE NOT NULL REFERENCES paper_order(order_id),
                   quantity INTEGER NOT NULL, price_minor_units INTEGER NOT NULL)"""
            )
            book.execute(
                """CREATE TABLE paper_fill_fee (fill_id TEXT PRIMARY KEY NOT NULL
                   REFERENCES paper_fill(fill_id), fee_minor_units INTEGER NOT NULL)"""
            )
            book.execute(
                "INSERT INTO paper_fill VALUES (?,?,?,?)",
                (
                    original.fill_id,
                    order_id,
                    original.quantity,
                    original.fill_price_minor_units,
                ),
            )
            book.execute(
                "INSERT INTO paper_fill_fee VALUES (?,?)",
                (original.fill_id, original.fee_minor_units),
            )
        before = capture_position(
            self.store, self.company_id, original.fill_id, now=101
        )
        self.assertEqual(self.post(order_id).fill_id, original.fill_id)
        self.assertEqual(
            capture_position(self.store, self.company_id, original.fill_id, now=102),
            before,
        )
        with closing(sqlite3.connect(self.store)) as book:
            self.assertEqual(book.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                book.execute("SELECT * FROM paper_fill_fee").fetchall(),
                [(original.fill_id, original.fee_minor_units)],
            )

    def test_history_batch_rolls_back_if_one_fill_conflicts(self):
        order_id = self.submit(99)
        unfilled = self.venue.observe_execution(order_id)

        class History(FixedQuoteVenue):
            def read_fills(self, account_id, order_id, *, account_sync=False):
                return self.history

        history = History(self.journal)
        first = replace(
            unfilled, fill_id="first", filled_quantity=1, fill_price_minor_units=80
        )
        second = replace(first, fill_id="second", fill_price_minor_units=90)
        history.history = (first, replace(second, filled_quantity=2))
        with self.assertRaisesRegex(PostingRefused, "exceed the admitted order"):
            post_confirmed_fill(self.store, self.company_id, order_id, history)
        self.assertEqual(position_quantity(self.store, self.company_id), 0)
        history.history = (first, second)
        completed = post_confirmed_fill(self.store, self.company_id, order_id, history)
        self.assertEqual(completed.fill_id, "second")
        self.assertEqual(position_quantity(self.store, self.company_id), 2)

    def test_cancel_release_waits_for_late_fill_and_is_idempotent(self):
        order_id = self.submit(99, quantity=3)

        class TerminalVenue(ObservedFillVenue):
            def read_cancellation(self, account_id, order_id, *, account_sync=False):
                return VenueCancellation(
                    order_id, account_id, "rules", "cancel-event", 2
                )

        first = TerminalVenue(self.journal, "first", 1, 80, 1)
        early = reconcile_paper_orders(self.store, self.company_id, first, now=100)
        self.assertEqual((early.recovered, early.unresolved), ((), 1))
        [position] = read_local_board_timeline(self.store, self.company_id)["paper"][
            "orders"
        ]
        self.assertEqual(
            (
                position["reserved_minor_units"],
                position["open_reservation_minor_units"],
            ),
            (278, 198),
        )
        late = TerminalVenue(self.journal, "late", 1, 90, 2)
        recovered = reconcile_paper_orders(
            self.store, self.company_id, late, now=early.next_query_at
        )
        self.assertEqual(recovered.unresolved, 0)
        [completed] = recovered.recovered
        self.assertEqual(completed.posting.fill_id, "late")
        self.assertEqual(
            post_confirmed_fill(self.store, self.company_id, order_id, first),
            completed.posting,
        )
        [position] = read_local_board_timeline(self.store, self.company_id)["paper"][
            "orders"
        ]
        self.assertEqual(
            (
                position["status"],
                position["reserved_minor_units"],
                position["open_reservation_minor_units"],
                position["remaining_quantity"],
            ),
            ("cancelled", 170, 0, 1),
        )
        conflicting = TerminalVenue(self.journal, "extra", 1, 90, 0)
        with self.assertRaisesRegex(PostingRefused, "exceeds recorded terminal"):
            post_confirmed_fill(self.store, self.company_id, order_id, conflicting)
        self.assertEqual(position_quantity(self.store, self.company_id), 2)

    def test_cancel_with_zero_fills_releases_without_creating_a_fill(self):
        order_id = self.submit(99)

        class TerminalVenue(FixedQuoteVenue):
            def read_cancellation(self, account_id, order_id, *, account_sync=False):
                return VenueCancellation(
                    order_id, account_id, "rules", "cancel-event", 0
                )

        venue = TerminalVenue(self.journal)
        outcome = reconcile_paper_orders(self.store, self.company_id, venue, now=100)
        self.assertEqual((outcome.unresolved, outcome.next_query_at), (0, None))
        [recovered] = outcome.recovered
        self.assertIsNone(recovered.posting)
        self.assertEqual(
            (recovered.no_fill.order_id, recovered.no_fill.fill_quantity), (order_id, 0)
        )
        self.assertIsNone(read_booked_fill(self.store, self.company_id, order_id))
        self.assertEqual(position_quantity(self.store, self.company_id), 0)
        [order] = read_local_board_timeline(self.store, self.company_id)["paper"][
            "orders"
        ]
        self.assertEqual(
            (order["status"], order["reserved_minor_units"]), ("cancelled", 0)
        )

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
