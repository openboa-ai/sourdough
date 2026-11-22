import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

from sourdough.company import FoundingTerms, found
from sourdough.duty_run import DutyRefused
from sourdough.staffing import (
    HireRequest,
    StaffingAuthority,
    StaffingChoice,
    StaffingNeed,
    admit_hire,
    claim_staffing,
    read_need,
    record_choice,
    stage_staffing,
)
from sourdough.writer_lease import hold_company_writer


class Staffing(unittest.TestCase):
    def setUp(self):
        self.store = Path(self.enterContext(TemporaryDirectory())) / "company.sqlite"
        self.founding = found(
            self.store, FoundingTerms("setup", "charter", "paper", "profile")
        )
        self.writer = self.enterContext(
            hold_company_writer(self.store, self.founding.company_id)
        )
        self.authority = StaffingAuthority(
            self.founding.grant_id, "profile", "assessment", slots=1, turns=4
        )
        self.now = int(time())

    def rows(self, query):
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute(query).fetchall()

    def stage(self, name):
        return stage_staffing(
            self.writer,
            self.authority,
            StaffingNeed(name, "Investigate book exceptions", 5, 1, 1, self.now),
        )

    def ready(self, name):
        revision = self.stage(name)
        claim = claim_staffing(self.writer, "profile", now=self.now)
        self.assertEqual(claim.duty_id, name)
        self.assertEqual(claim.founder_id, self.founding.founder_id)
        choice = StaffingChoice(
            revision,
            "hire",
            "The five cases exceed available capacity.",
            ("reassign", "defer"),
            120,
        )
        return claim, choice

    def test_staffing_choice_is_not_a_hire_and_request_replays(self):
        claim, choice = self.ready("capacity")
        with self.assertRaisesRegex(DutyRefused, "evidence read"):
            record_choice(self.writer, claim, choice)
        observation = read_need(self.writer, claim)
        self.assertEqual(observation["backlog"], 5)
        choice = replace(choice, evidence_revision=observation["revision"])
        choice_id = record_choice(self.writer, claim, choice)
        self.assertEqual(record_choice(self.writer, claim, choice), choice_id)
        self.assertEqual(self.rows("SELECT count(*) FROM agent"), [(1,)])
        request = HireRequest(
            "request", choice_id, "reviewer", "profile", "assessment", 4
        )
        with self.assertRaisesRegex(DutyRefused, "delegated scope"):
            admit_hire(self.writer, claim, replace(request, scope="paper"))
        receipt = admit_hire(self.writer, claim, request)
        self.assertNotEqual(receipt.agent_id, self.founding.founder_id)
        self.assertEqual(receipt, admit_hire(self.writer, claim, request))
        with self.assertRaisesRegex(DutyRefused, "replay conflicts"):
            admit_hire(self.writer, claim, replace(request, execution_profile="other"))
        self.assertEqual(self.rows("SELECT count(*) FROM agent"), [(2,)])
        self.assertEqual(self.rows("SELECT count(*) FROM standing_grant"), [(2,)])
        self.assertEqual(self.rows("SELECT allocated_turns FROM hire_receipt"), [(4,)])
        self.assertEqual(self.rows("SELECT count(*) FROM duty"), [(2,)])
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])

    def test_second_committed_choice_cannot_reuse_the_first_allocation(self):
        first, first_choice = self.ready("first")
        second, second_choice = self.ready("second")
        observations = [read_need(self.writer, claim) for claim in (first, second)]
        self.assertEqual(
            [observation["available_slots"] for observation in observations], [1, 1]
        )
        first_id = record_choice(
            self.writer,
            first,
            replace(first_choice, evidence_revision=observations[0]["revision"]),
        )
        second_id = record_choice(
            self.writer,
            second,
            replace(second_choice, evidence_revision=observations[1]["revision"]),
        )
        request = HireRequest(
            "request", first_id, "reviewer", "profile", "assessment", 4
        )
        contenders = (
            (first, request),
            (second, replace(request, request_id="later", choice_id=second_id)),
        )

        def try_hire(contender):
            claim, terms = contender
            try:
                return admit_hire(self.writer, claim, terms)
            except DutyRefused as error:
                self.assertIn("delegated scope or allocation", str(error))
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(try_hire, contenders))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM agent"), [(2,)])
        self.assertEqual(self.rows("SELECT count(*) FROM hire_receipt"), [(1,)])

    def test_revocation_before_claim_does_not_reissue_staffing_authority(self):
        self.stage("due-before-revocation")
        self.revoke_manager()
        self.assertIsNone(claim_staffing(self.writer, "profile", now=self.now))
        self.assertEqual(self.rows("SELECT count(*) FROM duty_run"), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM agent"), [(1,)])

    def test_historical_receipt_reopens_after_revocation_without_new_admission(self):
        first, first_choice = self.ready("first")
        later, later_choice = self.ready("later")
        first_id = record_choice(
            self.writer,
            first,
            replace(
                first_choice,
                evidence_revision=read_need(self.writer, first)["revision"],
            ),
        )
        later_id = record_choice(
            self.writer,
            later,
            replace(
                later_choice,
                evidence_revision=read_need(self.writer, later)["revision"],
            ),
        )
        request = HireRequest(
            "original", first_id, "reviewer", "profile", "assessment", 4
        )
        original = admit_hire(self.writer, first, request)
        self.revoke_manager()
        self.assertEqual(admit_hire(self.writer, first, request), original)
        with self.assertRaisesRegex(DutyRefused, "replay conflicts"):
            admit_hire(self.writer, first, replace(request, turns=3))
        with self.assertRaisesRegex(DutyRefused, "no longer current"):
            admit_hire(
                self.writer,
                later,
                replace(request, request_id="second", choice_id=later_id),
            )
        self.assertEqual(self.rows("SELECT count(*) FROM hire_receipt"), [(1,)])

    def revoke_manager(self):
        with closing(sqlite3.connect(self.store)) as db, db:
            db.execute(
                "UPDATE standing_grant SET scope='revoked' WHERE id=?",
                (self.founding.grant_id,),
            )
