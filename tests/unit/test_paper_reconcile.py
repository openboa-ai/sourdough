import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import claim_first_duty
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import PostingRefused
from sourdough.paper_order import PaperIntent, submit_paper_order
from sourdough.paper_reconcile import MAX_LOOKUP_ATTEMPTS, reconcile_paper_orders
from sourdough.paper_venue import FixedQuoteVenue


class RecoveryView(FixedQuoteVenue):
    def __init__(self, journal, *, visible=True):
        super().__init__(journal)
        self.visible = visible
        self.reads = []

    def submit(self, order_id, account_id, intent):
        raise AssertionError("Reconciliation tried to send a new order")

    def observe_execution(self, order_id):
        self.reads.append(("history", order_id))
        if self.visible:
            return super().observe_execution(order_id)
        raise TimeoutError("history is not available")

    def sync_account_execution(self, account_id, order_id):
        self.reads.append(("account_sync", order_id))
        if self.visible:
            return super().sync_account_execution(account_id, order_id)
        return None


class ReconcilePaper(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.journal = directory / "venue.sqlite"
        self.venue = FixedQuoteVenue(self.journal)
        self.company_id = found(
            self.store, FoundingTerms("setup", "charter", "paper", "runtime")
        ).company_id
        self.account = PaperAccount("account", "local", "rules", "USD", 5000, "paper")
        bound = self.venue.provision(self.account, 100, fee_bps=50)
        bind(
            self.store,
            PaperMandate("mandate", self.company_id, "charter", bound, 1000),
            self.venue,
        )
        self.claim = claim_first_duty(self.store, self.company_id, "runtime")
        self.order_id = submit_paper_order(
            self.store, self.claim, PaperIntent("action", "rules", 2, 110), self.venue
        ).order_id

    def rows(self, path, sql):
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute(sql).fetchall()

    def recover(self, edge, now):
        return reconcile_paper_orders(self.store, self.company_id, edge, now=now)

    def test_original_fill_replays_from_both_sources_without_an_order_send(self):
        original = self.rows(
            self.journal, "SELECT order_id, fill_id, quantity FROM venue_execution"
        )
        self.assertEqual(
            self.rows(self.store, "SELECT reserved_minor_units FROM paper_order"),
            [(220,)],
        )
        with closing(sqlite3.connect(self.store)) as db, db:
            db.execute("UPDATE standing_grant SET scope = 'revoked'")
        edge = RecoveryView(self.journal)
        recovery = self.recover(edge, 100)
        self.assertEqual(recovery.unresolved, 0)
        (resolved,) = recovery.recovered
        self.assertEqual(resolved.original_claim, self.claim)
        self.assertEqual(resolved.sources, ("history", "account_sync"))
        self.assertEqual(resolved.posting.fill_id, original[0][1])
        self.assertEqual(resolved.posting.fee_minor_units, 1)
        self.assertEqual(
            self.rows(self.journal, "SELECT fee_minor_units FROM venue_fill_fee"),
            self.rows(self.store, "SELECT fee_minor_units FROM paper_fill_fee"),
        )
        self.assertEqual(
            self.rows(self.store, "SELECT order_id, fill_id, quantity FROM paper_fill"),
            original,
        )
        self.assertEqual(
            self.rows(self.store, "SELECT reserved_minor_units FROM paper_order"),
            [(200,)],
        )
        self.assertEqual(self.recover(edge, 101).recovered, recovery.recovered)
        self.assertEqual(
            edge.reads,
            [("history", self.order_id), ("account_sync", self.order_id)] * 2,
        )
        self.assertEqual(
            self.rows(
                self.journal, "SELECT order_id, fill_id, quantity FROM venue_execution"
            ),
            original,
        )
        self.assertEqual(
            self.rows(self.store, "SELECT count(*) FROM paper_fill"), [(1,)]
        )
        self.assertEqual(
            self.rows(self.store, "SELECT count(*) FROM paper_fill_fee"), [(1,)]
        )

    def test_unknown_remains_reserved_and_retries_from_durable_deadline(self):
        obscured = RecoveryView(self.journal, visible=False)
        first = self.recover(obscured, 100)
        self.assertEqual(
            (first.recovered, first.unresolved, first.next_query_at), ((), 1, 102)
        )
        fresh_view = RecoveryView(self.journal)
        self.assertEqual(self.recover(fresh_view, 101), first)
        self.assertEqual(fresh_view.reads, [])
        self.assertEqual(
            self.rows(self.store, "SELECT reserved_minor_units FROM paper_order"),
            [(220,)],
        )
        resolved = self.recover(fresh_view, 102)
        self.assertEqual((resolved.unresolved, resolved.next_query_at), (0, None))
        self.assertEqual(resolved.recovered[0].posting.order_id, self.order_id)

    def test_retry_budget_exhaustion_never_clears_reservation(self):
        obscured = RecoveryView(self.journal, visible=False)
        now = 100
        for _ in range(MAX_LOOKUP_ATTEMPTS):
            pending = self.recover(obscured, now)
            now = pending.next_query_at or (now + 1000)
        self.assertEqual(len(obscured.reads), 2 * MAX_LOOKUP_ATTEMPTS)
        self.assertEqual((pending.unresolved, pending.next_query_at), (1, None))
        self.assertEqual(self.recover(obscured, now), pending)
        self.assertEqual(len(obscured.reads), 2 * MAX_LOOKUP_ATTEMPTS)
        self.assertEqual(
            self.rows(self.store, "SELECT reserved_minor_units FROM paper_order"),
            [(220,)],
        )

    def test_alternate_destination_cannot_recover_the_original_order(self):
        other = FixedQuoteVenue(self.journal.with_name("other.sqlite"))
        other.provision(self.account, 100)
        with self.assertRaisesRegex(PostingRefused, "original paper destination"):
            self.recover(other, 100)
        self.assertEqual(
            self.rows(self.store, "SELECT reserved_minor_units FROM paper_order"),
            [(220,)],
        )
