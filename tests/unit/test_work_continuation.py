import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import (
    FoundingRefused,
    FoundingTerms,
    found,
    reopen,
    upgrade_work_schema,
)
from sourdough.duty_run import DutyClaim
from sourdough.first_duty import DutyRefused, claim_first_duty, require_paper_authority
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_books import post_confirmed_fill
from sourdough.paper_order import PaperIntent, submit_paper_order
from sourdough.paper_risk import PaperRiskRefused, PaperSuspension, suspend_paper_risk
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.work import (
    FollowUpClaim,
    ResultReference,
    ReviewDecision,
    claim_follow_up,
    finish_first_duty,
    read_follow_up_result,
    settle_review,
)
from sourdough.writer_lease import hold_company_writer


class WorkContinuation(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = directory / "company.sqlite"
        self.terms = FoundingTerms("setup", "charter", "paper", "runtime")

    def founder(self, *, legacy=0):
        self.founding = found(self.store, self.terms)
        self.company_id = self.founding.company_id
        if legacy:
            with closing(sqlite3.connect(self.store)) as connection, connection:
                connection.execute("ALTER TABLE agent RENAME TO founder")
                connection.execute(
                    "CREATE UNIQUE INDEX initial_company ON founder(company_id)"
                )
                for table in ("standing_grant", "duty"):
                    connection.execute(
                        f"ALTER TABLE {table} RENAME COLUMN agent_id TO founder_id"
                    )
                connection.execute("DROP TABLE duty_run")
                if legacy == 1:
                    connection.execute("DROP TABLE work_follow_up")
                    connection.execute(
                        "CREATE UNIQUE INDEX first_owner ON duty(founder_id)"
                    )
                else:
                    connection.execute("ALTER TABLE work_follow_up DROP COLUMN purpose")
                    connection.execute(
                        "ALTER TABLE work_follow_up ADD COLUMN run_id TEXT"
                    )
                    connection.execute(
                        "CREATE UNIQUE INDEX follow_up_run ON work_follow_up(run_id)"
                    )
                connection.execute("UPDATE company SET schema_revision = ?", (legacy,))
                connection.execute(
                    """CREATE TABLE first_duty_claim (
                        duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
                        run_id TEXT NOT NULL UNIQUE
                    )"""
                )
                connection.execute(
                    "INSERT INTO first_duty_claim VALUES (?, 'original-run')",
                    (self.founding.due_work_id,),
                )
                connection.execute("UPDATE duty SET state = 'running'")
            return DutyClaim(
                self.company_id,
                self.founding.founder_id,
                self.founding.grant_id,
                self.founding.due_work_id,
                "original-run",
                "runtime",
            )
        return claim_first_duty(self.store, self.company_id, "runtime")

    def rows(self, statement):
        with closing(
            sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            return connection.execute(statement).fetchall()

    def claim(self, now, profile="runtime"):
        return claim_follow_up(self.store, self.company_id, profile, now=now)

    def test_suspension_requires_migrated_company_work_tables(self):
        original = self.founder(legacy=1)
        command = PaperSuspension("suspend", "charter", 0, "Risk review")

        def authorize(*args):
            return "verification-principal"

        with hold_company_writer(self.store, self.company_id) as writer:
            before = self.store.read_bytes()
            with self.assertRaisesRegex(PaperRiskRefused, "must be upgraded"):
                suspend_paper_risk(writer, command, authorize)
            self.assertEqual(self.store.read_bytes(), before)
            upgrade_work_schema(self.store, self.company_id)
            receipt = suspend_paper_risk(writer, command, authorize)
        self.assertEqual(receipt.disposition, "applied")
        self.assertEqual(
            self.rows("SELECT id,execution_profile FROM agent"),
            [(original.founder_id, "runtime")],
        )
        self.assertEqual(self.rows("SELECT run_id FROM duty_run"), [(original.run_id,)])

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

    def test_migrate_populated_v1_without_changing_external_effects(self):
        original = self.founder(legacy=1)
        venue = FixedQuoteVenue(self.store.with_name("venue.sqlite"))
        account = PaperAccount("account", "local", "rules", "USD", 5000, "paper")
        account = venue.provision(account, 100)
        bind(
            self.store,
            PaperMandate("mandate", self.company_id, "charter", account, 1000),
            venue,
        )
        intent = PaperIntent("action", "rules", 2, 110)
        with closing(sqlite3.connect(self.store)) as connection, connection:
            connection.execute(
                """CREATE TABLE paper_order (
                    order_id TEXT PRIMARY KEY NOT NULL,
                    run_id TEXT NOT NULL REFERENCES first_duty_claim(run_id),
                    mandate_revision TEXT NOT NULL, account_id TEXT NOT NULL,
                    action_id TEXT NOT NULL, instrument_rules_digest TEXT NOT NULL,
                    quantity INTEGER NOT NULL, limit_minor_units INTEGER NOT NULL,
                    reserved_minor_units INTEGER NOT NULL, UNIQUE (run_id, action_id)
                )"""
            )
            connection.execute(
                "INSERT INTO paper_order VALUES ('original-order', ?, 'mandate', 'account', 'action', 'rules', 2, 110, 220)",
                (original.run_id,),
            )
        venue.submit("original-order", "account", intent)
        posting = post_confirmed_fill(
            self.store, self.company_id, "original-order", venue
        )
        self.assertIsNotNone(posting)
        self.assertEqual(reopen(self.store, self.company_id).due_work_ids, ())
        self.assertEqual(self.rows("SELECT schema_revision FROM company"), [(1,)])
        queries = [
            "SELECT * FROM founding_receipt",
            "SELECT * FROM paper_order",
            "SELECT * FROM paper_fill",
        ]
        before = [self.rows(query) for query in queries]
        upgrade_work_schema(self.store, self.company_id)
        upgrade_work_schema(self.store, self.company_id)
        self.assertEqual(self.rows("SELECT schema_revision FROM company"), [(6,)])
        self.assertEqual(
            self.rows("SELECT duty_id, run_id FROM duty_run"),
            [(original.duty_id, original.run_id)],
        )
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
        historical = submit_paper_order(self.store, original, intent, venue)
        self.assertEqual(historical.order_id, "original-order")
        self.assertFalse(historical.dispatched_now)
        self.assertEqual(
            venue.observe_execution("original-order").fill_id, posting.fill_id
        )

    def test_migration_failure_rolls_back_schema_and_preserves_store(self):
        self.founder(legacy=1)
        with closing(sqlite3.connect(self.store)) as connection, connection:
            connection.execute("UPDATE duty SET founder_id = 'dangling'")
        before = self.store.read_bytes()
        with self.assertRaisesRegex(FoundingRefused, "foreign-key"):
            upgrade_work_schema(self.store, self.company_id)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(self.rows("SELECT schema_revision FROM company"), [(1,)])

    def test_migrate_v2_with_a_claimed_review_and_its_read(self):
        original = self.founder(legacy=2)
        with closing(sqlite3.connect(self.store)) as connection, connection:
            connection.execute("UPDATE duty SET state = 'settled'")
            connection.execute(
                "INSERT INTO duty VALUES ('review', ?, 'running')",
                (original.founder_id,),
            )
            connection.execute(
                """INSERT INTO work_follow_up VALUES (?, 'review', ?, 'paper', 100,
                        'book', 'position', 'observed', 100, 'review-run')""",
                (original.duty_id, original.grant_id),
            )
            connection.execute(
                """CREATE TABLE work_result_read (
                       duty_id TEXT PRIMARY KEY NOT NULL REFERENCES work_follow_up(duty_id),
                       run_id TEXT NOT NULL, result_revision TEXT NOT NULL, read_at INTEGER NOT NULL
                   )"""
            )
            connection.execute(
                "INSERT INTO work_result_read VALUES ('review', 'review-run', 'observed', 100)"
            )
        upgrade_work_schema(self.store, self.company_id)
        review = FollowUpClaim(
            replace(original, duty_id="review", run_id="review-run"),
            ResultReference("book", "position", "observed", 100),
        )
        with closing(sqlite3.connect(self.store)) as connection:
            with self.assertRaises(DutyRefused):
                require_paper_authority(connection, review.claim)
        self.assertEqual(
            self.rows("SELECT run_id, purpose FROM duty_run ORDER BY run_id"),
            [("original-run", "paper_execution"), ("review-run", "position_review")],
        )
        decision = ReviewDecision("action", "observed", "hold", "Known position", 60)
        successor = settle_review(self.store, review, decision, now=101)
        self.assertEqual(successor.due_at, 161)
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])

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
