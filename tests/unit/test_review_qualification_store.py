import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.evidence_review import (
    PASS_K_TRIALS,
    REVIEW_FORMAT,
    EvidenceClass,
    ReviewCase,
    ReviewRule,
    ReviewRuleRefused,
    qualify_review_rule,
)
from sourdough.review_qualification_store import (
    CHANGE_QUALIFY_DUTY_KIND,
    PublishedReviewRule,
    read_review_qualification,
    read_review_qualification_record,
    record_review_qualification,
    register_change_qualification_duty,
)
from sourdough.runtime.engine import CompanyRuntime
from sourdough.runtime.registry import DutyRegistry
from sourdough.runtime.work_kernel import create_work_item
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer


SOURCE = EvidenceClass.SOURCE_REFERENCE
VERIFICATION = EvidenceClass.INDEPENDENT_VERIFICATION


class QualificationStore(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.company = found(
            self.store, FoundingTerms("setup", "charter", "company", "runtime")
        )
        self.assets = {
            "original": PublishedReviewRule(
                self.company.company_id, "original", ReviewRule((SOURCE,))
            ),
            "new": PublishedReviewRule(
                self.company.company_id, "new", ReviewRule((SOURCE, VERIFICATION))
            ),
            "other": PublishedReviewRule(
                "unrelated-company", "other", ReviewRule((SOURCE,))
            ),
        }
        self.cases = (
            ReviewCase((SOURCE,), (VERIFICATION,)),
            ReviewCase((SOURCE, VERIFICATION), ()),
        )

    def resolve(self, database, company_id, asset_id):
        self.assertTrue(database.in_transaction)
        self.assertEqual(company_id, self.company.company_id)
        return self.assets.get(asset_id)

    def record(self, candidate, baseline=None, cases=None):
        with hold_company_writer(
            self.store, self.company.company_id, lease_directory=self.root / "leases"
        ) as writer:
            return record_review_qualification(
                writer,
                candidate,
                baseline,
                resolve=self.resolve,
                cases=self.cases if cases is None else cases,
            )

    def test_reopened_receipts_retain_both_verdicts_and_all_pins(self):
        rejected = self.record("original")
        accepted = self.record("new", "original")
        self.assertFalse(rejected.qualification.passed)
        self.assertTrue(accepted.qualification.passed)
        self.assertEqual(accepted, self.record("new", "original"))
        changed = self.record("new", "original", (ReviewCase((SOURCE,), ()),))
        self.assertFalse(changed.qualification.passed)
        self.assertNotEqual(
            accepted.qualification.criteria_digest,
            changed.qualification.criteria_digest,
        )
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            self.assertEqual(
                read_review_qualification(db, rejected.receipt_id), rejected
            )
            self.assertEqual(
                read_review_qualification(db, accepted.receipt_id), accepted
            )
            self.assertEqual(read_review_qualification(db, changed.receipt_id), changed)
            self.assertEqual(
                db.execute("SELECT count(*) FROM review_qualification").fetchone(), (3,)
            )
        self.assertEqual(accepted.qualification.outcomes[0].present, (SOURCE,))
        self.assertEqual(
            accepted.qualification.baseline_digest, self.assets["original"].rule.digest
        )

    def test_asset_owner_scope_and_receipt_integrity_are_checked(self):
        for candidate, baseline in (
            ("other", None),
            ("new", "other"),
            ("unknown", None),
        ):
            with self.subTest(candidate=candidate, baseline=baseline):
                with self.assertRaisesRegex(ReviewRuleRefused, "unavailable"):
                    self.record(candidate, baseline)
        receipt = self.record("new")
        with closing(sqlite3.connect(self.store)) as db, db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM review_qualification").fetchone(), (1,)
            )
            db.execute("UPDATE review_qualification SET evidence='{}'")
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            with self.assertRaisesRegex(ReviewRuleRefused, "integrity"):
                read_review_qualification(db, receipt.receipt_id)
        with self.assertRaisesRegex(ReviewRuleRefused, "integrity"):
            self.record("new")

    def test_ops_change_qualify_duty_runs_pass3_and_blocks_self_evaluation(self):
        leases = self.root / "leases"
        with hold_company_writer(
            self.store, self.company.company_id, lease_directory=leases
        ) as writer:
            with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
                db.execute(
                    "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                    ("evaluator-agent", self.company.company_id, "runtime"),
                )
                db.execute(
                    "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                    ("eval-grant", "evaluator-agent", "qualify-change"),
                )
                author_work, _ = create_work_item(
                    db,
                    company_id=self.company.company_id,
                    owner_agent_id=self.company.founder_id,
                    duty_kind="ops.publish_rule",
                    source_kind="manual",
                    source_id="publish-v1",
                    state="scheduled",
                )
                db.execute(
                    """INSERT INTO shared_project (project_id, company_id, revision, members, publishers)
                       VALUES (?, ?, 'rev-1', ?, ?)""",
                    (
                        "proj-review",
                        self.company.company_id,
                        json.dumps([self.company.founder_id, "evaluator-agent"]),
                        json.dumps([self.company.founder_id]),
                    ),
                )
                db.execute(
                    """INSERT INTO shared_publication (
                           asset_id, company_id, project_id, author_id, work_item_id,
                           request_id, request_digest, audience, content
                       ) VALUES (?, ?, ?, ?, ?, 'pub-req-1', 'digest-1', ?, ?)""",
                    (
                        "shared-rule-v1",
                        self.company.company_id,
                        "proj-review",
                        self.company.founder_id,
                        author_work.work_item_id,
                        json.dumps([self.company.founder_id, "evaluator-agent"]),
                        json.dumps(
                            {
                                "format": REVIEW_FORMAT,
                                "required": [SOURCE.value, VERIFICATION.value],
                            }
                        ),
                    ),
                )
                payload = {
                    "candidate_asset_id": "shared-rule-v1",
                    "baseline_asset_id": None,
                    "cases": [
                        {
                            "present": [SOURCE.value],
                            "expected_missing": [VERIFICATION.value],
                        },
                        {
                            "present": [SOURCE.value, VERIFICATION.value],
                            "expected_missing": [],
                        },
                    ],
                    "trials": PASS_K_TRIALS,
                }
                self_item, _ = create_work_item(
                    db,
                    company_id=self.company.company_id,
                    owner_agent_id=self.company.founder_id,
                    duty_kind=CHANGE_QUALIFY_DUTY_KIND,
                    source_kind="change_proposal",
                    source_id="self-eval-attempt",
                    payload=payload,
                )
                eval_item, _ = create_work_item(
                    db,
                    company_id=self.company.company_id,
                    owner_agent_id="evaluator-agent",
                    duty_kind=CHANGE_QUALIFY_DUTY_KIND,
                    source_kind="change_proposal",
                    source_id="independent-eval",
                    payload=payload,
                )

        registry = DutyRegistry()
        register_change_qualification_duty(registry)
        runtime = CompanyRuntime(
            self.store,
            self.company.company_id,
            registry,
            lease_directory=leases,
        )
        summary = runtime.step(max_dispatches=4)
        self.assertIn(self_item.work_item_id, summary.blocked_items)
        self.assertEqual(len(summary.executed_runs), 2)

        with closing(open_reader(self.store)) as db, read_tx(db):
            self_state = db.execute(
                "SELECT state FROM work_items WHERE work_item_id = ?",
                (self_item.work_item_id,),
            ).fetchone()
            eval_state = db.execute(
                "SELECT state FROM work_items WHERE work_item_id = ?",
                (eval_item.work_item_id,),
            ).fetchone()
            self.assertEqual(self_state, ("blocked",))
            self.assertEqual(eval_state, ("settled",))
            row = db.execute(
                """SELECT receipt_id, passed, trial_count
                   FROM review_qualification
                   WHERE company_id = ? AND candidate_asset_id = 'shared-rule-v1'""",
                (self.company.company_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            receipt_id, passed, trial_count = row
            self.assertEqual((passed, trial_count), (1, PASS_K_TRIALS))
            record = read_review_qualification_record(
                db, receipt_id, company_id=self.company.company_id
            )
            self.assertIsNotNone(record)
            self.assertTrue(record.passed)
            self.assertEqual(record.trial_count, PASS_K_TRIALS)
            events = [
                r[0]
                for r in db.execute(
                    """SELECT event_type FROM audit_event
                       WHERE company_id = ? AND domain = 'ops'
                       ORDER BY event_seq""",
                    (self.company.company_id,),
                ).fetchall()
            ]
            self.assertIn("ops.review_self_evaluation_blocked", events)
            self.assertIn("ops.review_qualified", events)

        # Flaky pass^3 trial must record passed=0 with a distinct receipt_id.
        def flaky(rule, base, eval_cases, attempt):
            if attempt == 2:
                return qualify_review_rule(ReviewRule((SOURCE,)), base, eval_cases)
            return qualify_review_rule(rule, base, eval_cases)

        with hold_company_writer(
            self.store, self.company.company_id, lease_directory=leases
        ) as writer:
            flaky_receipt = record_review_qualification(
                writer,
                "new",
                "original",
                resolve=self.resolve,
                cases=self.cases,
                trials=PASS_K_TRIALS,
                trial_evaluator=flaky,
            )
        with closing(open_reader(self.store)) as db, read_tx(db):
            flaky_record = read_review_qualification_record(
                db, flaky_receipt.receipt_id, company_id=self.company.company_id
            )
            self.assertIsNotNone(flaky_record)
            self.assertFalse(flaky_record.passed)
            self.assertEqual(flaky_record.trial_count, PASS_K_TRIALS)
