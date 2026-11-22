import json
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest.mock import Mock

from sourdough.assessment_run import run_assessment_company
from sourdough.company import FoundingTerms, found
from sourdough.company_assessment import (
    AssessmentProposal,
    SourceRecord,
    pending_publications,
    publish_assessment,
    read_assessment_inputs,
    read_assessment_result,
    seal_assessment_source,
    settle_assessment,
)
from sourdough.duty_run import ASSESSMENT_REVIEW, DutyRefused, count_company_runs
from sourdough.first_trade import TradeRefused
from sourdough.onboarding import (
    accept_first_duty,
    claim_hired_first,
    provision_hire,
    read_first_duty,
)
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
from sourdough.staffing_tools import StaffingTools
from sourdough.work import ReviewDecision, claim_assessment_review, settle_review
from sourdough.writer_lease import hold_company_writer


class Staffing(unittest.TestCase):
    def setUp(self):
        self.store = Path(self.enterContext(TemporaryDirectory())) / "company.sqlite"
        self.founding = found(
            self.store, FoundingTerms("setup", "charter", "paper", "profile")
        )
        self.lease = self.enterContext(ExitStack())
        self.writer = self.lease.enter_context(
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

    def test_hired_identity_reopens_the_same_private_desk_and_accepts_its_duty(self):
        manager, choice = self.ready("hire")
        choice = replace(
            choice, evidence_revision=read_need(self.writer, manager)["revision"]
        )
        choice_id = record_choice(self.writer, manager, choice)
        receipt = admit_hire(
            self.writer,
            manager,
            HireRequest("hire", choice_id, "reviewer", "profile", "assessment", 4),
        )
        self.assertIsNone(claim_hired_first(self.writer, receipt.agent_id, "profile"))
        probes = []

        def blocked(profile):
            probes.append(profile)
            return False

        first = provision_hire(
            self.writer, receipt.agent_id, "assessment", blocked, now=self.now
        )
        self.assertEqual(first.state, "blocked")
        self.assertIsNone(claim_hired_first(self.writer, receipt.agent_id, "profile"))
        self.assertEqual(
            provision_hire(
                self.writer, receipt.agent_id, "assessment", blocked, now=self.now + 1
            ),
            first,
        )
        self.assertEqual(probes, ["profile"])
        prepared = provision_hire(
            self.writer,
            receipt.agent_id,
            "assessment",
            lambda profile: profile == "profile",
            now=first.retry_at,
        )
        self.assertEqual(prepared.state, "ready")
        self.assertIsNone(
            claim_hired_first(self.writer, receipt.agent_id, "replacement")
        )
        hire = claim_hired_first(self.writer, receipt.agent_id, "profile")
        self.assertEqual(hire.duty_id, prepared.first_duty_id)
        self.assertEqual(hire.founder_id, receipt.agent_id)
        with self.assertRaises(DutyRefused):
            read_first_duty(self.writer, replace(hire, founder_id=manager.founder_id))
        with self.assertRaisesRegex(DutyRefused, "own current desk"):
            accept_first_duty(self.writer, hire, "unread", "Accept the work.")
        context = read_first_duty(self.writer, hire)
        self.assertEqual(context["charter_revision"], "charter")
        self.assertEqual(context["assignment_summary"], "Investigate book exceptions")
        self.assertEqual(context["granted_scope"], "assessment")
        self.assertEqual(read_first_duty(self.writer, hire), context)
        for attempt in range(2):
            self.assertEqual(
                accept_first_duty(
                    self.writer,
                    hire,
                    context["revision"],
                    "Accept the capacity review.",
                ),
                hire.duty_id,
            )
        self.assertEqual(self.rows("SELECT count(*) FROM private_desk"), [(1,)])
        self.assertEqual(
            self.rows("SELECT count(*) FROM onboarding_acceptance"), [(1,)]
        )
        self.assertEqual(
            self.rows("SELECT state FROM hire_onboarding"), [("accepted",)]
        )
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])

        seal_assessment_source(
            self.writer,
            (
                SourceRecord("known", "Reviewed documents", 8, 6, self.now - 1),
                SourceRecord("future", "Late review", 12, 11, self.now + 90),
            ),
        )
        untouched = self.rows("SELECT * FROM assessment_source")
        with self.assertRaises(DutyRefused):
            read_assessment_inputs(self.writer, manager, now=self.now)
        inputs = read_assessment_inputs(self.writer, hire, now=self.now)
        self.assertEqual([row["record_id"] for row in inputs["records"]], ["known"])
        proposal = AssessmentProposal(
            inputs["revision"],
            "known",
            "recommend_change",
            "Six verified documents rather than the eight reported.",
            60,
        )
        with self.assertRaisesRegex(DutyRefused, "record read"):
            publish_assessment(
                self.writer, hire, replace(proposal, record_id="future"), now=self.now
            )
        published = publish_assessment(self.writer, hire, proposal, now=self.now)
        self.assertEqual(
            publish_assessment(self.writer, hire, proposal, now=self.now + 1), published
        )
        self.assertEqual(
            self.rows("SELECT author_id, audience_id FROM assessment_publication"),
            [(receipt.agent_id, manager.founder_id)],
        )
        self.assertEqual(self.rows("SELECT * FROM assessment_source"), untouched)
        self.assertEqual(pending_publications(self.writer), ((hire, published),))
        follow_up = settle_assessment(self.writer, hire, published)
        self.assertEqual(settle_assessment(self.writer, hire, published), follow_up)
        self.assertEqual(pending_publications(self.writer), ())
        self.assertIsNone(
            claim_assessment_review(
                self.store,
                hire.company_id,
                receipt.agent_id,
                "profile",
                now=published.review_at - 1,
            )
        )
        self.assertIsNone(
            claim_assessment_review(
                self.store,
                hire.company_id,
                manager.founder_id,
                "profile",
                now=self.now + 100,
            )
        )
        next_turn = claim_assessment_review(
            self.store, hire.company_id, receipt.agent_id, "profile", now=self.now + 100
        )
        self.assertNotEqual(next_turn.claim.duty_id, hire.duty_id)
        self.assertEqual(next_turn.claim.founder_id, hire.founder_id)
        evidence = read_assessment_result(self.writer, next_turn, now=self.now + 100)
        self.assertEqual(evidence["assessment"]["record_id"], "known")
        self.assertEqual(len(evidence["current_records"]), 2)
        successor = settle_review(
            self.store,
            next_turn,
            ReviewDecision(
                "review",
                evidence["revision"],
                "investigate",
                "The original discrepancy remains, and one new record is visible.",
                60,
            ),
            now=self.now + 100,
            successor_purpose=ASSESSMENT_REVIEW,
        )
        self.assertEqual(successor.due_at, self.now + 160)
        self.assertEqual(
            self.rows("SELECT count(*) FROM assessment_publication"), [(1,)]
        )
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])

    def arrange_due_assessment(self, turns):
        manager, choice = self.ready("initial-staffing")
        choice_id = record_choice(
            self.writer,
            manager,
            replace(
                choice, evidence_revision=read_need(self.writer, manager)["revision"]
            ),
        )
        hire = admit_hire(
            self.writer,
            manager,
            HireRequest("hire", choice_id, "reviewer", "profile", "assessment", turns),
        )
        provision_hire(
            self.writer, hire.agent_id, "assessment", lambda _: True, now=self.now
        )
        claim = claim_hired_first(self.writer, hire.agent_id, "profile")
        context = read_first_duty(self.writer, claim)
        accept_first_duty(
            self.writer, claim, context["revision"], "Review the records."
        )
        cut = self.now - 61
        seal_assessment_source(
            self.writer, (SourceRecord("book", "Verified counts", 8, 6, cut),)
        )
        evidence = read_assessment_inputs(self.writer, claim, now=cut)
        receipt = publish_assessment(
            self.writer,
            claim,
            AssessmentProposal(
                evidence["revision"], "book", "investigate", "A count differs.", 60
            ),
            now=cut,
        )
        return hire, settle_assessment(self.writer, claim, receipt)

    def activate_assessment(self, employee, *, budget_from=0):
        return run_assessment_company(
            self.store,
            self.founding.company_id,
            lambda: employee,
            turn_budget=4,
            idle_seconds=0,
            budget_from=budget_from,
            store_key=self.writer.store_key,
            on_ready=lambda _: None,
        )

    @staticmethod
    def offer_review(read):
        evidence = read()
        return {
            "tool": "record_review_decision",
            "call_id": "review-call",
            "observation_revision": evidence["revision"],
            "action": "investigate",
            "rationale": "The verified count still differs.",
            "next_check_seconds": 60,
        }

    def test_interrupted_assessment_review_reclaims_its_duty_and_rereads(self):
        hire, follow_up = self.arrange_due_assessment(turns=3)
        self.lease.close()
        employee = Mock(profile="profile")

        def interrupted(read):
            read()
            raise TradeRefused("Lost employee session before review settlement")

        employee.review_assessment.side_effect = interrupted
        with self.assertRaisesRegex(TradeRefused, "Lost employee session"):
            self.activate_assessment(employee)
        (departed,) = self.rows(
            "SELECT run_id, is_current FROM duty_run WHERE purpose='assessment_review'"
        )
        self.assertEqual(departed[1], 1)

        employee.review_assessment.side_effect = self.offer_review
        result = self.activate_assessment(employee)
        replacement = result.review.claim
        self.assertEqual(replacement.founder_id, hire.agent_id)
        self.assertEqual(replacement.duty_id, follow_up.duty_id)
        self.assertNotEqual(replacement.run_id, departed[0])
        self.assertCountEqual(
            self.rows(
                "SELECT run_id, is_current FROM duty_run WHERE purpose='assessment_review'"
            ),
            [(departed[0], 0), (replacement.run_id, 1)],
        )
        self.assertCountEqual(
            self.rows("SELECT run_id FROM work_result_read"),
            [(departed[0],), (replacement.run_id,)],
        )
        self.assertEqual(
            self.rows("SELECT run_id FROM work_review_decision"),
            [(replacement.run_id,)],
        )
        self.assertEqual(
            self.rows("SELECT count(*) FROM assessment_publication"), [(1,)]
        )

    def test_exhausted_hire_does_not_block_a_second_staffing_assignment(self):
        self.authority = replace(self.authority, slots=2)
        first, _ = self.arrange_due_assessment(turns=2)
        self.stage("next-staffing")
        self.lease.close()
        employee = Mock(profile="profile")
        employee.review_assessment.side_effect = self.offer_review
        first_run = self.activate_assessment(employee)
        self.assertEqual(first_run.review.claim.founder_id, first.agent_id)

        def assess_capacity(invoke):
            evidence = invoke("read_staffing_need", {})
            self.assertEqual(evidence["available_slots"], 1)
            return invoke(
                "record_staffing_choice",
                {
                    "action": "defer",
                    "alternatives": ["reassign", "hire"],
                    "evidence_revision": evidence["revision"],
                    "rationale": "Reassess before taking on more work.",
                    "next_check_seconds": 60,
                },
            )

        employee.staffing.side_effect = assess_capacity
        later = self.activate_assessment(
            employee,
            budget_from=count_company_runs(self.store, self.founding.company_id),
        )
        self.assertEqual(later.hiring.manager_claim.duty_id, "next-staffing")
        self.assertIsNone(later.review)
        employee.review_assessment.assert_called_once()

    def test_manager_sees_only_staffing_tools_and_requires_a_separate_hire(self):
        manager, _ = self.ready("need")
        tools = StaffingTools(self.writer, manager)
        observation = tools.invoke("read_staffing_need", {})
        request = tools.invoke(
            "record_staffing_choice",
            {
                "action": "defer",
                "alternatives": ["reassign", "hire"],
                "evidence_revision": observation["revision"],
                "rationale": "Defer while testing the current team's capacity.",
                "next_check_seconds": 300,
            },
        )
        with self.assertRaisesRegex(DutyRefused, "choice to hire"):
            tools.invoke(
                "request_hire",
                {
                    "request_id": "unwarranted",
                    "choice_id": request["choice_id"],
                    "role": "reviewer",
                    "execution_profile": observation["profile"],
                    "scope": observation["scope"],
                    "turns": 2,
                },
            )
        with self.assertRaisesRegex(DutyRefused, "not available"):
            tools.invoke("paper_buy", {"quantity": 1, "limit_minor_units": 100})
        self.assertEqual(self.rows("SELECT count(*) FROM agent"), [(1,)])
        script = Path(__file__).resolve().parents[2] / "tools/codex_employee.py"
        response = subprocess.run(
            [
                sys.executable,
                "-IB",
                script,
                "--mcp-staffing",
                "unused",
                self.store.with_suffix(".json"),
            ],
            input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
        registered = json.loads(response.stdout)["result"]["tools"]
        self.assertEqual(
            [definition["name"] for definition in registered],
            ["read_staffing_need", "record_staffing_choice", "request_hire"],
        )
