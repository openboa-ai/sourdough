import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.evidence_review import (
    EvidenceClass,
    ReviewCase,
    ReviewRule,
    ReviewRuleRefused,
)
from sourdough.review_qualification_store import (
    PublishedReviewRule,
    record_review_qualification,
)
from sourdough.review_tool_activation import (
    ReviewActivation,
    ReviewToolAuthority,
    ReviewWorkInput,
    apply_review_activation,
    inspect_with_review_tool,
)
from sourdough.writer_lease import hold_company_writer


SOURCE = EvidenceClass.SOURCE_REFERENCE
VERIFIED = EvidenceClass.INDEPENDENT_VERIFICATION
ORIGINAL_CASES = (ReviewCase((), (SOURCE,)), ReviewCase((SOURCE,), ()))
CANDIDATE_CASES = (
    ReviewCase((SOURCE,), (VERIFIED,)),
    ReviewCase((SOURCE, VERIFIED), ()),
)


class ReviewToolActivation(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.company_id = found(
            self.store, FoundingTerms("setup", "charter", "company", "runtime")
        ).company_id
        self.assets = {
            "base": ReviewRule((SOURCE,)),
            "candidate": ReviewRule((SOURCE, VERIFIED)),
        }
        self.owner = "operating-owner"
        self.grant = "deployment-grant"
        self.can_deploy = True
        self.can_inspect = True

    def hold_writer(self):
        return hold_company_writer(
            self.store, self.company_id, lease_directory=self.root / "leases"
        )

    def asset(self, database, company_id, asset_id):
        self.assertTrue(database.in_transaction)
        self.assertEqual(company_id, self.company_id)
        rule = self.assets.get(asset_id)
        return PublishedReviewRule(company_id, asset_id, rule) if rule else None

    def authority(self, database, company_id, operation, asset_id):
        self.assertTrue(database.in_transaction)
        self.assertEqual(company_id, self.company_id)
        if asset_id not in self.assets:
            return None
        if operation == "inspect":
            if self.can_inspect:
                return ReviewToolAuthority(
                    "inspection-agent", "inspect-grant", self.owner
                )
        elif self.can_deploy:
            return ReviewToolAuthority("deployment-agent", self.grant, self.owner)
        return None

    def work_input(self, db, company_id, agent_id, result_id):
        self.assertTrue(db.in_transaction)
        self.assertEqual(agent_id, "inspection-agent")
        if result_id != "published-result":
            return None
        return ReviewWorkInput(company_id, result_id, "result-v1", (SOURCE,))

    def qualification(self, candidate, baseline, cases):
        with self.hold_writer() as writer:
            return record_review_qualification(
                writer, candidate, baseline, resolve=self.asset, cases=cases
            ).receipt_id

    def apply(self, request, cases):
        with self.hold_writer() as writer:
            return apply_review_activation(
                writer,
                request,
                authorize=self.authority,
                resolve=self.asset,
                cases=cases,
            )

    def inspect(self, revision, result_id="published-result"):
        return inspect_with_review_tool(
            self.store,
            self.company_id,
            revision,
            result_id,
            authorize=self.authority,
            resolve=self.asset,
            read_input=self.work_input,
        )

    def operations(self):
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute(
                "SELECT revision,operation,candidate_id FROM review_tool_activation ORDER BY revision"
            ).fetchall()

    def test_activate_inspect_and_revert_after_reopening_writer(self):
        original = self.qualification("base", None, ORIGINAL_CASES)
        first = self.apply(
            ReviewActivation("first", "activate", "base", original, 0), ORIGINAL_CASES
        )
        self.assertEqual(first.revision, 1)
        self.assertEqual(self.inspect(1).missing, ())
        qualified = self.qualification("candidate", "base", CANDIDATE_CASES)
        change = ReviewActivation("deploy", "activate", "candidate", qualified, 1)
        applied = self.apply(change, CANDIDATE_CASES)
        self.assertEqual((applied.owner_id, applied.grant_id), (self.owner, self.grant))
        inspection = self.inspect(2)
        self.assertNotEqual(inspection.principal_id, applied.principal_id)
        self.assertEqual(
            (inspection.result_revision, inspection.missing), ("result-v1", (VERIFIED,))
        )
        with self.assertRaisesRegex(ReviewRuleRefused, "not effective"):
            self.inspect(1)
        with self.assertRaisesRegex(ReviewRuleRefused, "unavailable"):
            self.inspect(2, "other-work")

        revert_qualification = self.qualification("base", "candidate", ORIGINAL_CASES)
        reverted = self.apply(
            ReviewActivation("revert", "revert", "base", revert_qualification, 2),
            ORIGINAL_CASES,
        )
        self.assertEqual(reverted.revision, 3)
        self.assertEqual(self.inspect(3).missing, ())
        self.assertEqual(inspection.missing, (VERIFIED,))
        self.assertEqual(self.apply(change, CANDIDATE_CASES), applied)
        self.assertEqual(
            self.operations(),
            [
                (1, "activate", "base"),
                (2, "activate", "candidate"),
                (3, "revert", "base"),
            ],
        )

    def test_negative_changed_stale_and_revoked_inputs_do_not_change_effective_state(
        self,
    ):
        original = self.qualification("base", None, ORIGINAL_CASES)
        self.apply(
            ReviewActivation("first", "activate", "base", original, 0), ORIGINAL_CASES
        )
        good = self.qualification("candidate", "base", CANDIDATE_CASES)
        bad = self.qualification("base", "base", CANDIDATE_CASES)
        command = ReviewActivation("attempt", "activate", "candidate", good, 1)
        with self.assertRaisesRegex(ReviewRuleRefused, "qualification"):
            self.apply(replace(command, qualification_id=bad), CANDIDATE_CASES)
        with self.assertRaisesRegex(ReviewRuleRefused, "qualification"):
            self.apply(
                replace(command, candidate_id="base", qualification_id=bad),
                CANDIDATE_CASES,
            )
        with self.assertRaisesRegex(ReviewRuleRefused, "stale"):
            self.apply(command, ORIGINAL_CASES)
        with self.assertRaisesRegex(ReviewRuleRefused, "revision has changed"):
            self.apply(replace(command, expected_revision=0), CANDIDATE_CASES)
        self.can_deploy = False
        with self.assertRaisesRegex(ReviewRuleRefused, "authority"):
            self.apply(command, CANDIDATE_CASES)
        self.can_deploy = True
        self.assets["candidate"] = ReviewRule((VERIFIED,))
        with self.assertRaisesRegex(ReviewRuleRefused, "changed after qualification"):
            self.apply(command, CANDIDATE_CASES)
        self.can_inspect = False
        with self.assertRaisesRegex(ReviewRuleRefused, "authority"):
            self.inspect(1)
        self.assertEqual(self.operations(), [(1, "activate", "base")])
