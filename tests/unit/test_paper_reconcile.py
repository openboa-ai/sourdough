import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from sourdough.company import FoundingTerms, found
from sourdough.company_run import run_paper_company
from sourdough.first_duty import claim_first_duty
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import PostingRefused, post_confirmed_fill
from sourdough.paper_execution import VenueCancellation
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
        replay = self.recover(edge, 101).recovered
        self.assertEqual(replay[0].posting, resolved.posting)
        self.assertEqual(replay[0].sources, ("book",))
        self.assertEqual(
            edge.reads,
            [("history", self.order_id), ("account_sync", self.order_id)],
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

    def test_reopened_company_assigns_same_agent_a_review_after_zero_fill(self):
        self._assert_zero_fill_continues(repeat_paper=False)

    def test_reopened_company_persists_the_same_agents_next_trading_decision_after_zero_fill(
        self,
    ):
        self._assert_zero_fill_continues(repeat_paper=True)

    def test_zero_fill_retries_the_position_in_the_same_activation(self):
        self._assert_zero_fill_continues(repeat_paper=False, delay_position=True)

    def _assert_zero_fill_continues(self, *, repeat_paper, delay_position=False):
        store = self.store.with_name("cancelled-company.sqlite")
        journal = self.journal.with_name("cancelled-venue.sqlite")
        company = found(
            store, FoundingTerms("cancelled", "charter", "paper", "runtime")
        )
        original = FixedQuoteVenue(journal)
        account = original.provision(self.account, 100)
        bind(
            store,
            PaperMandate("mandate", company.company_id, "charter", account, 1000),
            original,
        )
        claim = claim_first_duty(store, company.company_id, "runtime")
        order_id = submit_paper_order(
            store, claim, PaperIntent("no-fill", "rules", 2, 99), original
        ).order_id

        class TerminalView(RecoveryView):
            position_reads = 0

            def read_cancellation(self, account_id, order_id, *, account_sync=False):
                return VenueCancellation(
                    order_id, account_id, "rules", "original-cancel", 0
                )

            def read_order_position(self, account_id, order_id):
                self.position_reads += 1
                if delay_position and self.position_reads <= 2:
                    return None
                return super().read_order_position(account_id, order_id)

        post_confirmed_fill(store, company.company_id, order_id, TerminalView(journal))
        self.assertEqual(
            self.rows(store, "SELECT count(*) FROM work_follow_up"), [(0,)]
        )
        observed = []

        def decide(read_result):
            result = read_result()
            observed.append(result)
            return {
                "tool": "record_paper_decision"
                if repeat_paper
                else "record_review_decision",
                "call_id": "next",
                "observation_revision": result["revision"],
                "action": "hold",
                "rationale": "No fill; check again next shift.",
                "next_check_seconds": 300,
            }

        replaced_venue = TerminalView(journal)
        result = run_paper_company(
            store,
            company.company_id,
            replaced_venue,
            SimpleNamespace(profile="runtime", trade=decide, review=decide),
            repeat_paper=repeat_paper,
            turn_budget=1,
            idle_seconds=5 if delay_position else 0,
        )
        self.assertEqual(result.blocked_work, ())
        if repeat_paper:
            [follow_up] = result.later_trades
            self.assertIsNone(follow_up.order_id)
            self.assertEqual(
                self.rows(store, "SELECT account_id,action FROM paper_decision"),
                [(account.account_id, "hold")],
            )
        else:
            follow_up = result.review
        self.assertEqual(follow_up.claim.founder_id, claim.founder_id)
        self.assertNotEqual(follow_up.claim.run_id, claim.run_id)
        self.assertEqual(follow_up.next_duty.result.kind, "paper_no_fill")
        [receipt] = observed
        self.assertEqual(
            (
                receipt["order_id"],
                receipt["event_id"],
                receipt["fill_quantity"],
                receipt["position_quantity"],
                receipt["status"],
            ),
            (order_id, "original-cancel", 0, 0, "cancelled"),
        )
        if delay_position:
            self.assertGreater(replaced_venue.position_reads, 2)
        else:
            self.assertEqual(replaced_venue.reads, [])
        self.assertEqual(
            self.rows(journal, "SELECT order_id,fill_id FROM venue_execution"),
            [(order_id, None)],
        )
        self.assertEqual(self.rows(store, "SELECT count(*) FROM paper_fill"), [(0,)])

    def test_alternate_destination_cannot_recover_the_original_order(self):
        other = FixedQuoteVenue(self.journal.with_name("other.sqlite"))
        other.provision(self.account, 100)
        with self.assertRaisesRegex(PostingRefused, "original paper destination"):
            self.recover(other, 100)
        self.assertEqual(
            self.rows(self.store, "SELECT reserved_minor_units FROM paper_order"),
            [(220,)],
        )

    def test_booked_fill_is_usable_if_worker_died_before_settling_and_venue_is_unavailable(
        self,
    ):
        posting = post_confirmed_fill(
            self.store, self.company_id, self.order_id, self.venue
        )

        class Offline(RecoveryView):
            def observe_account(self, account_id):
                raise AssertionError(
                    "Already booked effects must not require venue availability"
                )

        offline = Offline(self.journal, visible=False)
        (resolved,) = self.recover(offline, 100).recovered
        self.assertEqual(resolved.posting, posting)
        self.assertEqual(resolved.sources, ("book",))
        self.assertEqual(offline.reads, [])
        self.assertEqual(
            self.rows(self.store, "SELECT count(*) FROM paper_fill_fee"), [(1,)]
        )

    def test_replacement_retries_unresolved_lookup_after_agent_budget_is_exhausted(
        self,
    ):
        class BecomesVisible(RecoveryView):
            def sync_account_execution(self, account_id, order_id):
                observed = super().sync_account_execution(account_id, order_id)
                self.visible = True
                return observed

        edge = BecomesVisible(self.journal, visible=False)
        result = run_paper_company(
            self.store,
            self.company_id,
            edge,
            SimpleNamespace(profile="runtime"),
            budget_from=0,
            turn_budget=1,
            idle_seconds=4,
        )
        self.assertIsNone(result.review)
        self.assertEqual(result.later_trades, ())
        self.assertEqual(result.reconciliation.unresolved, 0)
        self.assertEqual(
            result.reconciliation.recovered[0].posting.order_id, self.order_id
        )
        self.assertEqual(
            edge.reads,
            [("history", self.order_id), ("account_sync", self.order_id)] * 2,
        )
        self.assertEqual(self.rows(self.store, "SELECT count(*) FROM duty_run"), [(1,)])
        self.assertEqual(
            self.rows(self.store, "SELECT count(*) FROM duty WHERE state='scheduled'"),
            [(1,)],
        )
