import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough import company
from sourdough.company import (
    FoundingRefused,
    FoundingTerms,
    found,
    reopen,
    upgrade_work_schema,
)
from sourdough.first_duty import DutyRefused, claim_first_duty, require_paper_authority
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import post_confirmed_fill
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


class WorkContinuation(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.terms = FoundingTerms("setup", "charter", "paper", "runtime")

    def founder(self, *, legacy=False):
        if legacy:
            schema = tuple(
                statement.replace(
                    "founder_id TEXT NOT NULL REFERENCES",
                    "founder_id TEXT NOT NULL UNIQUE REFERENCES",
                )
                if statement == company.DUTY_SCHEMA
                else statement
                for statement in company.SCHEMA
                if statement != company.FOLLOW_UP_SCHEMA
            )
            with (
                patch.object(company, "SCHEMA", schema),
                patch.object(company, "SCHEMA_REVISION", 1),
            ):
                self.founding = found(self.store, self.terms)
        else:
            self.founding = found(self.store, self.terms)
        self.company_id = self.founding.company_id
        return claim_first_duty(self.store, self.company_id, "runtime")

    def rows(self, statement):
        with closing(
            sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            return connection.execute(statement).fetchall()

    def claim(self, now, profile="runtime"):
        return claim_follow_up(self.store, self.company_id, profile, now=now)

    def test_migrate_populated_v1_without_changing_external_effects(self):
        original = self.founder(legacy=True)
        venue = FixedQuoteVenue(self.store.with_name("venue.sqlite"))
        account = PaperAccount("account", "local", "rules", "USD", 5000, "paper")
        account = venue.provision(account, 100)
        bind(
            self.store,
            PaperMandate("mandate", self.company_id, "charter", account, 1000),
            venue,
        )
        order = submit_paper_order(
            self.store, original, PaperIntent("action", "rules", 2, 110), venue
        )
        posting = post_confirmed_fill(
            self.store, self.company_id, order.order_id, venue
        )
        self.assertIsNotNone(posting)
        self.assertEqual(reopen(self.store, self.company_id).due_work_ids, ())
        self.assertEqual(self.rows("SELECT schema_revision FROM company"), [(1,)])
        queries = [
            "SELECT * FROM founding_receipt",
            "SELECT * FROM first_duty_claim",
            "SELECT * FROM paper_order",
            "SELECT * FROM paper_fill",
        ]
        before = [self.rows(query) for query in queries]
        upgrade_work_schema(self.store, self.company_id)
        upgrade_work_schema(self.store, self.company_id)
        self.assertEqual(self.rows("SELECT schema_revision FROM company"), [(2,)])
        ref = ResultReference("book", posting.fill_id, "rev", 100)
        follow = finish_first_duty(self.store, original, ref, due_at=101)
        self.assertEqual(found(self.store, self.terms), self.founding)
        self.assertEqual([self.rows(query) for query in queries], before)
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])
        self.assertIsNone(self.claim(100))
        served = self.claim(101)
        self.assertEqual(served.claim.duty_id, follow.duty_id)
        self.assertEqual(served.claim.founder_id, original.founder_id)
        self.assertEqual(served.claim.execution_profile, original.execution_profile)
        self.assertNotEqual(served.claim.run_id, original.run_id)
        self.assertEqual(served.result, ref)
        self.assertEqual(
            venue.observe_execution(order.order_id).fill_id, posting.fill_id
        )

    def test_migration_failure_rolls_back_schema_and_preserves_store(self):
        self.founder(legacy=True)
        with closing(sqlite3.connect(self.store)) as connection, connection:
            connection.execute("UPDATE duty SET founder_id = 'dangling'")
        before = self.store.read_bytes()
        with self.assertRaisesRegex(FoundingRefused, "foreign-key"):
            upgrade_work_schema(self.store, self.company_id)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(self.rows("SELECT schema_revision FROM company"), [(1,)])

    def test_early_wake_and_replay_share_one_successor(self):
        original = self.founder()
        self.assertIsNone(self.claim(100))
        ref = ResultReference("document", "assessment", "rev", 120)
        follow = finish_first_duty(self.store, original, ref, due_at=100)
        self.assertIsNone(self.claim(119))
        self.assertIsNone(self.claim(120, profile="different"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(self.claim, [120, 120]))
        (served,) = [result for result in claims if result is not None]
        self.assertEqual(served.claim.duty_id, follow.duty_id)
        with closing(sqlite3.connect(self.store)) as connection:
            with self.assertRaises(DutyRefused):
                require_paper_authority(connection, served.claim)
        self.assertEqual(
            finish_first_duty(self.store, original, ref, due_at=100), follow
        )
        with self.assertRaisesRegex(DutyRefused, "conflicts"):
            finish_first_duty(self.store, original, ref, due_at=101)
        self.assertEqual(self.rows("SELECT count(*) FROM work_follow_up"), [(1,)])

    def test_stale_claim_and_lost_grant_never_admit_work(self):
        original = self.founder()
        ref = ResultReference("book", "position", "rev", 100)
        with self.assertRaisesRegex(DutyRefused, "identity"):
            finish_first_duty(
                self.store, replace(original, run_id="stale"), ref, due_at=100
            )
        self.assertEqual(self.rows("SELECT * FROM work_follow_up"), [])
        finish_first_duty(self.store, original, ref, due_at=100)
        with closing(sqlite3.connect(self.store)) as connection, connection:
            connection.execute("UPDATE standing_grant SET scope = 'revoked'")
        self.assertIsNone(self.claim(100))
        self.assertEqual(self.rows("SELECT run_id FROM work_follow_up"), [(None,)])

    def test_successor_rolls_back_if_original_settlement_fails(self):
        original = self.founder()
        with closing(sqlite3.connect(self.store)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER refuse_settle BEFORE UPDATE ON duty
                   WHEN NEW.state = 'settled' BEGIN SELECT RAISE(ABORT, 'injected'); END"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            finish_first_duty(
                self.store,
                original,
                ResultReference("book", "position", "rev", 100),
                due_at=100,
            )
        self.assertEqual(
            self.rows("SELECT id, state FROM duty"), [(original.duty_id, "running")]
        )
        self.assertEqual(self.rows("SELECT * FROM work_follow_up"), [])

    def test_review_and_next_responsibility_persist_once_with_current_grant(self):
        original = self.founder()
        ref = ResultReference("book", "position", "observed", 100)
        finish_first_duty(self.store, original, ref, due_at=100)
        follow_up = self.claim(100)
        decision = ReviewDecision("pick", ref.revision, "hold", "Known position", 60)
        with self.assertRaisesRegex(DutyRefused, "preceding"):
            settle_review(self.store, follow_up, decision, now=100)
        observation = {"revision": ref.revision, "quantity": 2}

        def producer(connection, request):
            self.assertEqual(request, ref)
            return observation

        self.assertEqual(
            read_follow_up_result(self.store, follow_up, now=102, read=producer),
            observation,
        )
        with self.assertRaisesRegex(DutyRefused, "preceding"):
            settle_review(self.store, follow_up, decision, now=101)
        next_duty = settle_review(self.store, follow_up, decision, now=103)
        self.assertEqual(next_duty.due_at, 163)
        self.assertEqual(
            settle_review(self.store, follow_up, decision, now=104), next_duty
        )
        with self.assertRaisesRegex(DutyRefused, "conflicts"):
            settle_review(
                self.store, follow_up, replace(decision, action="investigate"), now=104
            )
        self.assertEqual(self.rows("SELECT COUNT(*) FROM work_review_decision"), [(1,)])
        self.assertIsNone(self.claim(162))
        new_claim = self.claim(163)
        self.assertEqual(new_claim.claim.duty_id, next_duty.duty_id)
        read_follow_up_result(self.store, new_claim, now=163, read=producer)
        with closing(sqlite3.connect(self.store)) as connection, connection:
            connection.execute("UPDATE standing_grant SET scope = 'revoked'")
        with self.assertRaisesRegex(DutyRefused, "no longer current"):
            read_follow_up_result(self.store, new_claim, now=164, read=producer)
        with self.assertRaisesRegex(DutyRefused, "no longer current"):
            settle_review(
                self.store, new_claim, replace(decision, call_id="second"), now=164
            )
        self.assertEqual(self.rows("SELECT COUNT(*) FROM work_review_decision"), [(1,)])
