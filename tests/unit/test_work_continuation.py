import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough.company import FoundingTerms, _connect, found
from sourdough.duty_run import DutyRefused
from sourdough.trading.duty import (
    claim_paper_first_duty as claim_first_duty,
    claim_paper_follow_up as claim_follow_up,
    finish_paper_first_duty as finish_first_duty,
    require_paper_authority,
    settle_paper_review as settle_review,
)
from sourdough.work import (
    ResultReference,
    ReviewDecision,
    read_follow_up_result,
)


class WorkContinuation(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.terms = FoundingTerms("setup", "charter", "paper", "runtime")

    def founder(self):
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

    def test_shared_profile_does_not_make_a_second_agent_the_founder(self):
        self.founding = found(self.store, self.terms)
        self.company_id = self.founding.company_id
        with closing(sqlite3.connect(self.store)) as connection, connection:
            connection.execute(
                "INSERT INTO agent VALUES ('colleague', ?, 'runtime')",
                (self.company_id,),
            )
            connection.execute(
                "INSERT INTO standing_grant VALUES ('second-grant', 'colleague', 'paper')"
            )
        claim = claim_first_duty(self.store, self.company_id, "runtime")
        self.assertEqual(claim.founder_id, self.founding.founder_id)
        self.assertEqual(claim.duty_id, self.founding.due_work_id)
        self.assertEqual(found(self.store, self.terms), self.founding)
        with closing(sqlite3.connect(self.store)) as connection:
            with self.assertRaisesRegex(DutyRefused, "identity mismatch"):
                require_paper_authority(
                    connection,
                    replace(claim, founder_id="colleague", grant_id="second-grant"),
                )

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
        self.assertEqual(self.rows("SELECT COUNT(*) FROM duty_run"), [(1,)])

    def test_successor_rolls_back_if_original_settlement_fails(self):
        original = self.founder()

        def connect_with_temp_abort(store, mode, **kwargs):
            conn = _connect(store, mode, **kwargs)
            if mode == "rw":
                conn.execute(
                    """CREATE TEMP TRIGGER refuse_settle BEFORE UPDATE ON duty
                       WHEN NEW.state = 'settled' BEGIN SELECT RAISE(ABORT, 'injected'); END"""
                )
            return conn

        with patch("sourdough.work._connect", side_effect=connect_with_temp_abort):
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
