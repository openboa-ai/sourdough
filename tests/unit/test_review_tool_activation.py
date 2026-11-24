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
    quarantine_review_tool_lineage,
)
from sourdough.storage.db import open_reader, read_tx
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

    def test_command_receipt_audit_event_and_lineage_dag_quarantine_rollback(self):
        repro = EvidenceClass.REPRODUCTION
        self.assets["descendant"] = ReviewRule((SOURCE, VERIFIED, repro))
        self.assets["child_of_faulted"] = ReviewRule((SOURCE, repro))
        descendant_cases = (
            ReviewCase((SOURCE, VERIFIED), (repro,)),
            ReviewCase((SOURCE, VERIFIED, repro), ()),
        )
        child_cases = (
            ReviewCase((SOURCE,), (repro,)),
            ReviewCase((SOURCE, repro), ()),
        )

        q1 = self.qualification("base", None, ORIGINAL_CASES)
        r1 = self.apply(
            ReviewActivation("act-1", "activate", "base", q1, 0), ORIGINAL_CASES
        )
        self.assertEqual(r1.revision, 1)

        with self.hold_writer() as writer:
            q2 = record_review_qualification(
                writer,
                "candidate",
                "base",
                resolve=self.asset,
                cases=CANDIDATE_CASES,
                parent_asset_id="base",
            ).receipt_id
        r2 = self.apply(
            ReviewActivation("act-2", "activate", "candidate", q2, 1, "base"),
            CANDIDATE_CASES,
        )
        self.assertEqual(r2.revision, 2)

        # Reusing request_id with conflicting payload must fail closed.
        with self.assertRaisesRegex(ReviewRuleRefused, "already in use"):
            self.apply(
                ReviewActivation("act-2", "activate", "candidate", q2, 0, "base"),
                CANDIDATE_CASES,
            )

        with self.hold_writer() as writer:
            q3 = record_review_qualification(
                writer,
                "descendant",
                "candidate",
                resolve=self.asset,
                cases=descendant_cases,
                parent_asset_id="candidate",
            ).receipt_id
        r3 = self.apply(
            ReviewActivation("act-3", "activate", "descendant", q3, 2, "candidate"),
            descendant_cases,
        )
        self.assertEqual(r3.revision, 3)
        self.assertEqual(self.inspect(3).missing, (VERIFIED, repro))

        # Quarantine intermediate node "candidate" -> cascades to "descendant", restores "base".
        with self.hold_writer() as writer:
            quarantined = quarantine_review_tool_lineage(
                writer,
                request_id="quar-1",
                faulted_asset_id="candidate",
                reason="regression-in-candidate",
                authorize=self.authority,
                resolve=self.asset,
            )
            replayed = quarantine_review_tool_lineage(
                writer,
                request_id="quar-1",
                faulted_asset_id="candidate",
                reason="regression-in-candidate",
                authorize=self.authority,
                resolve=self.asset,
            )
        self.assertEqual(replayed, quarantined)
        self.assertEqual(quarantined.quarantined_assets, ("candidate", "descendant"))
        self.assertEqual(quarantined.restored_candidate_id, "base")
        self.assertEqual(quarantined.restored_revision, 4)

        # Restored revision 4 uses "base"; quarantined revision 3 is no longer effective.
        self.assertEqual(self.inspect(4).missing, ())
        with self.assertRaisesRegex(ReviewRuleRefused, "not effective"):
            self.inspect(3)

        # Activating a quarantined asset or a new child of a quarantined ancestor must refuse.
        with self.hold_writer() as writer:
            q_child = record_review_qualification(
                writer,
                "child_of_faulted",
                "base",
                resolve=self.asset,
                cases=child_cases,
                parent_asset_id="candidate",
            ).receipt_id
        with self.assertRaisesRegex(ReviewRuleRefused, "quarantined"):
            self.apply(
                ReviewActivation(
                    "act-blocked-child",
                    "activate",
                    "child_of_faulted",
                    q_child,
                    4,
                    "candidate",
                ),
                child_cases,
            )
        with self.assertRaisesRegex(ReviewRuleRefused, "quarantined"):
            self.apply(
                ReviewActivation(
                    "act-blocked-self", "activate", "candidate", q2, 4, "base"
                ),
                CANDIDATE_CASES,
            )

        # Verify command_receipt and audit_event persisted atomically for every transition.
        with closing(open_reader(self.store)) as db, read_tx(db):
            cmd_types = [
                row
                for row in db.execute(
                    """SELECT command_id, command_type, state
                       FROM command_receipt
                       WHERE company_id = ?
                       ORDER BY created_at_ns, command_id""",
                    (self.company_id,),
                ).fetchall()
            ]
            self.assertEqual(
                cmd_types,
                [
                    ("act-1", "review_tool.activate", "applied"),
                    ("act-2", "review_tool.activate", "applied"),
                    ("act-3", "review_tool.activate", "applied"),
                    ("quar-1", "review_tool.quarantine", "applied"),
                ],
            )
            audit_types = [
                row[0]
                for row in db.execute(
                    """SELECT event_type FROM audit_event
                       WHERE company_id = ? AND entity_type = 'review_tool_activation'
                       ORDER BY event_seq""",
                    (self.company_id,),
                ).fetchall()
            ]
            self.assertEqual(
                audit_types,
                [
                    "ops.review_tool_activated",
                    "ops.review_tool_activated",
                    "ops.review_tool_activated",
                    "ops.review_lineage_quarantined",
                ],
            )

        # Quarantining root "base" leaves zero healthy fallback and fails closed on inspect.
        with self.hold_writer() as writer:
            root_quarantine = quarantine_review_tool_lineage(
                writer,
                request_id="quar-root",
                faulted_asset_id="base",
                reason="root-defect",
                authorize=self.authority,
                resolve=self.asset,
            )
        self.assertIsNone(root_quarantine.restored_candidate_id)
        self.assertIsNone(root_quarantine.restored_revision)
        self.assertEqual(
            root_quarantine.quarantined_assets, ("base", "candidate", "descendant")
        )
        with self.assertRaisesRegex(ReviewRuleRefused, "quarantined"):
            self.inspect(4)
