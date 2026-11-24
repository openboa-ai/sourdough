"""Connected Flow 12: company-owned change qualification, activation, inspection, and lineage rollback."""

import json
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
    ReviewRuleRefused,
)
from sourdough.review_qualification_store import (
    CHANGE_QUALIFY_DUTY_KIND,
    read_review_qualification_record,
    register_change_qualification_duty,
    resolve_shared_review_rule,
)
from sourdough.review_tool_activation import (
    ReviewActivation,
    ReviewToolAuthority,
    ReviewWorkInput,
    apply_review_activation,
    inspect_with_review_tool,
    quarantine_review_tool_lineage,
)
from sourdough.runtime.engine import CompanyRuntime
from sourdough.runtime.registry import DutyRegistry
from sourdough.runtime.work_kernel import create_work_item
from sourdough.storage.db import (
    SCHEMA_VERSION,
    open_reader,
    open_writer,
    read_tx,
    write_tx,
)
from sourdough.writer_lease import hold_company_writer


SOURCE = EvidenceClass.SOURCE_REFERENCE
REPRO = EvidenceClass.REPRODUCTION
VERIFIED = EvidenceClass.INDEPENDENT_VERIFICATION

V1_CASES = (
    ReviewCase((), (SOURCE,)),
    ReviewCase((SOURCE,), ()),
)
V2_CASES = (
    ReviewCase((SOURCE,), (VERIFIED,)),
    ReviewCase((SOURCE, VERIFIED), ()),
)
V3_CASES = (
    ReviewCase((SOURCE, VERIFIED), (REPRO,)),
    ReviewCase((SOURCE, VERIFIED, REPRO), ()),
)
V4_CASES = (
    ReviewCase((SOURCE,), (REPRO,)),
    ReviewCase((SOURCE, REPRO), ()),
)


def _serialize_cases(cases: tuple[ReviewCase, ...]) -> list[dict[str, list[str]]]:
    return [
        {
            "present": [item.value for item in case.present],
            "expected_missing": [item.value for item in case.expected_missing],
        }
        for case in cases
    ]


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with TemporaryDirectory(prefix="sd-flow12-") as tmp:
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            company = found(
                store,
                FoundingTerms(
                    setup_identity="flow12-setup",
                    charter_revision="charter-v1",
                    founder_scope="manage-company",
                    execution_profile="runtime-v1",
                ),
            )
            company_id = company.company_id
            proposer_id = company.founder_id
            evaluator_id = "agent-evaluator"
            deployer_id = "agent-deployer"
            inspector_id = "agent-inspector"

            # Provision independent Agents, grants, shared project, and immutable published review rules.
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    for agent_id, grant_id, scope in (
                        (evaluator_id, "grant-eval", "qualify-change"),
                        (deployer_id, "grant-deploy", "deploy-change"),
                        (inspector_id, "grant-inspect", "inspect-work"),
                    ):
                        db.execute(
                            "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                            (agent_id, company_id, "runtime-v1"),
                        )
                        db.execute(
                            "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                            (grant_id, agent_id, scope),
                        )
                    pub_work, _ = create_work_item(
                        db,
                        company_id=company_id,
                        owner_agent_id=proposer_id,
                        duty_kind="ops.publish_review_rule",
                        source_kind="proposal",
                        source_id="flow12-proposals",
                        state="scheduled",
                    )
                    members = [proposer_id, evaluator_id, deployer_id, inspector_id]
                    db.execute(
                        """INSERT INTO shared_project (project_id, company_id, revision, members, publishers)
                           VALUES ('proj-ops-change', ?, 'rev-1', ?, ?)""",
                        (company_id, json.dumps(members), json.dumps([proposer_id])),
                    )
                    published_rules = (
                        ("rule-v1", [SOURCE.value]),
                        ("rule-v2", [SOURCE.value, VERIFIED.value]),
                        ("rule-v3", [SOURCE.value, VERIFIED.value, REPRO.value]),
                        ("rule-v4-clean", [SOURCE.value, REPRO.value]),
                    )
                    for idx, (asset_id, required) in enumerate(
                        published_rules, start=1
                    ):
                        db.execute(
                            """INSERT INTO shared_publication (
                                   asset_id, company_id, project_id, author_id, work_item_id,
                                   request_id, request_digest, audience, content
                               ) VALUES (?, ?, 'proj-ops-change', ?, ?, ?, ?, ?, ?)""",
                            (
                                asset_id,
                                company_id,
                                proposer_id,
                                pub_work.work_item_id,
                                f"pub-req-{idx}",
                                f"pub-digest-{idx}",
                                json.dumps(members),
                                json.dumps(
                                    {"format": REVIEW_FORMAT, "required": required}
                                ),
                            ),
                        )

            # Register ops.change_qualify on DutyRegistry and execute through CompanyRuntime.
            registry = DutyRegistry()
            register_change_qualification_duty(registry)
            runtime = CompanyRuntime(
                store, company_id, registry, lease_directory=leases
            )

            def schedule_qualification(
                owner_id: str,
                source_id: str,
                candidate_id: str,
                baseline_id: str | None,
                parent_id: str | None,
                cases: tuple[ReviewCase, ...],
            ) -> str:
                with hold_company_writer(
                    store, company_id, lease_directory=leases
                ) as writer:
                    with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                        item, _ = create_work_item(
                            db,
                            company_id=company_id,
                            owner_agent_id=owner_id,
                            duty_kind=CHANGE_QUALIFY_DUTY_KIND,
                            source_kind="change_proposal",
                            source_id=source_id,
                            payload={
                                "candidate_asset_id": candidate_id,
                                "baseline_asset_id": baseline_id,
                                "parent_asset_id": parent_id,
                                "cases": _serialize_cases(cases),
                                "trials": PASS_K_TRIALS,
                            },
                        )
                        return item.work_item_id

            def latest_qualification_id(
                candidate_id: str, baseline_id: str | None
            ) -> str:
                with closing(open_reader(store)) as db, read_tx(db):
                    row = db.execute(
                        """SELECT receipt_id FROM review_qualification
                           WHERE company_id = ?
                             AND candidate_asset_id = ?
                             AND ((? IS NULL AND baseline_asset_id IS NULL)
                                  OR baseline_asset_id = ?)
                           ORDER BY recorded_at_ns DESC
                           LIMIT 1""",
                        (company_id, candidate_id, baseline_id, baseline_id),
                    ).fetchone()
                    self.assertIsNotNone(row)
                    record = read_review_qualification_record(
                        db, row[0], company_id=company_id
                    )
                    self.assertIsNotNone(record)
                    self.assertTrue(record.passed)
                    self.assertEqual(record.trial_count, PASS_K_TRIALS)
                    return row[0]

            # 1. Self-evaluation by the proposer must be blocked; independent evaluator passes pass^3.
            self_item = schedule_qualification(
                proposer_id, "self-eval-v1", "rule-v1", None, None, V1_CASES
            )
            eval_v1 = schedule_qualification(
                evaluator_id, "eval-v1", "rule-v1", None, None, V1_CASES
            )
            step1 = runtime.step(max_dispatches=4)
            self.assertIn(self_item, step1.blocked_items)
            self.assertEqual(len(step1.executed_runs), 2)
            with closing(open_reader(store)) as db, read_tx(db):
                self.assertEqual(
                    db.execute(
                        "SELECT state FROM work_items WHERE work_item_id = ?",
                        (eval_v1,),
                    ).fetchone(),
                    ("settled",),
                )
            q_v1 = latest_qualification_id("rule-v1", None)

            # Authorize activation, quarantine, and separate read-only inspection from standing_grant.
            deploy_enabled = True

            def authorize(db, cid, operation, _asset_id):
                if operation == "inspect":
                    row = db.execute(
                        "SELECT id FROM standing_grant WHERE agent_id = ? AND scope = 'inspect-work'",
                        (inspector_id,),
                    ).fetchone()
                    return (
                        ReviewToolAuthority(inspector_id, row[0], proposer_id)
                        if row
                        else None
                    )
                if not deploy_enabled:
                    return None
                row = db.execute(
                    "SELECT id FROM standing_grant WHERE agent_id = ? AND scope = 'deploy-change'",
                    (deployer_id,),
                ).fetchone()
                return (
                    ReviewToolAuthority(deployer_id, row[0], proposer_id)
                    if row
                    else None
                )

            def read_work_input(_db, cid, principal_id, result_id):
                self.assertEqual(principal_id, inspector_id)
                if result_id != "work-result-42":
                    return None
                return ReviewWorkInput(cid, result_id, "res-rev-1", (SOURCE,))

            # 2. Activate v1 -> qualify & activate v2 (parent=v1) -> qualify & activate v3 (parent=v2).
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                act_v1 = apply_review_activation(
                    writer,
                    ReviewActivation("req-v1", "activate", "rule-v1", q_v1, 0),
                    authorize=authorize,
                    resolve=resolve_shared_review_rule,
                    cases=V1_CASES,
                )
                # Reply-loss idempotent replay returns the exact same receipt.
                self.assertEqual(
                    apply_review_activation(
                        writer,
                        ReviewActivation("req-v1", "activate", "rule-v1", q_v1, 0),
                        authorize=authorize,
                        resolve=resolve_shared_review_rule,
                        cases=V1_CASES,
                    ),
                    act_v1,
                )
            self.assertEqual(act_v1.revision, 1)

            schedule_qualification(
                evaluator_id, "eval-v2", "rule-v2", "rule-v1", "rule-v1", V2_CASES
            )
            runtime.step(max_dispatches=2)
            q_v2 = latest_qualification_id("rule-v2", "rule-v1")

            # Revoked grant blocks activation without changing effective revision.
            deploy_enabled = False
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                with self.assertRaisesRegex(ReviewRuleRefused, "authority"):
                    apply_review_activation(
                        writer,
                        ReviewActivation(
                            "req-v2", "activate", "rule-v2", q_v2, 1, "rule-v1"
                        ),
                        authorize=authorize,
                        resolve=resolve_shared_review_rule,
                        cases=V2_CASES,
                    )
            deploy_enabled = True

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                act_v2 = apply_review_activation(
                    writer,
                    ReviewActivation(
                        "req-v2", "activate", "rule-v2", q_v2, 1, "rule-v1"
                    ),
                    authorize=authorize,
                    resolve=resolve_shared_review_rule,
                    cases=V2_CASES,
                )
            self.assertEqual(act_v2.revision, 2)

            schedule_qualification(
                evaluator_id, "eval-v3", "rule-v3", "rule-v2", "rule-v2", V3_CASES
            )
            runtime.step(max_dispatches=2)
            q_v3 = latest_qualification_id("rule-v3", "rule-v2")
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                act_v3 = apply_review_activation(
                    writer,
                    ReviewActivation(
                        "req-v3", "activate", "rule-v3", q_v3, 2, "rule-v2"
                    ),
                    authorize=authorize,
                    resolve=resolve_shared_review_rule,
                    cases=V3_CASES,
                )
            self.assertEqual(act_v3.revision, 3)

            # 3. Independent consumer inspects work result under effective revision 3; retired revs refused.
            inspected_v3 = inspect_with_review_tool(
                store,
                company_id,
                3,
                "work-result-42",
                authorize=authorize,
                resolve=resolve_shared_review_rule,
                read_input=read_work_input,
            )
            self.assertEqual(inspected_v3.principal_id, inspector_id)
            self.assertEqual(inspected_v3.missing, (VERIFIED, REPRO))
            with self.assertRaisesRegex(ReviewRuleRefused, "not effective"):
                inspect_with_review_tool(
                    store,
                    company_id,
                    2,
                    "work-result-42",
                    authorize=authorize,
                    resolve=resolve_shared_review_rule,
                    read_input=read_work_input,
                )

            # 4. Fault rule-v2 -> lineage DAG quarantines rule-v2 and descendant rule-v3, rolling back to rule-v1.
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                quarantine = quarantine_review_tool_lineage(
                    writer,
                    request_id="req-quar-v2",
                    faulted_asset_id="rule-v2",
                    reason="defect-found-in-v2",
                    authorize=authorize,
                    resolve=resolve_shared_review_rule,
                )
            self.assertEqual(quarantine.quarantined_assets, ("rule-v2", "rule-v3"))
            self.assertEqual(quarantine.restored_candidate_id, "rule-v1")
            self.assertEqual(quarantine.restored_revision, 4)

            inspected_restored = inspect_with_review_tool(
                store,
                company_id,
                4,
                "work-result-42",
                authorize=authorize,
                resolve=resolve_shared_review_rule,
                read_input=read_work_input,
            )
            self.assertEqual(inspected_restored.missing, ())

            # 5. Qualify & activate a clean candidate on restored rule-v1, then manually revert to rule-v1.
            schedule_qualification(
                evaluator_id, "eval-v4", "rule-v4-clean", "rule-v1", "rule-v1", V4_CASES
            )
            runtime.step(max_dispatches=2)
            q_v4 = latest_qualification_id("rule-v4-clean", "rule-v1")
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                act_v4 = apply_review_activation(
                    writer,
                    ReviewActivation(
                        "req-v4", "activate", "rule-v4-clean", q_v4, 4, "rule-v1"
                    ),
                    authorize=authorize,
                    resolve=resolve_shared_review_rule,
                    cases=V4_CASES,
                )
            self.assertEqual(act_v4.revision, 5)
            self.assertEqual(
                inspect_with_review_tool(
                    store,
                    company_id,
                    5,
                    "work-result-42",
                    authorize=authorize,
                    resolve=resolve_shared_review_rule,
                    read_input=read_work_input,
                ).missing,
                (REPRO,),
            )

            schedule_qualification(
                evaluator_id,
                "eval-revert-v1",
                "rule-v1",
                "rule-v4-clean",
                None,
                V1_CASES,
            )
            runtime.step(max_dispatches=2)
            q_revert = latest_qualification_id("rule-v1", "rule-v4-clean")
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                reverted = apply_review_activation(
                    writer,
                    ReviewActivation("req-revert-v1", "revert", "rule-v1", q_revert, 5),
                    authorize=authorize,
                    resolve=resolve_shared_review_rule,
                    cases=V1_CASES,
                )
            self.assertEqual(reverted.revision, 6)
            self.assertEqual(
                inspect_with_review_tool(
                    store,
                    company_id,
                    6,
                    "work-result-42",
                    authorize=authorize,
                    resolve=resolve_shared_review_rule,
                    read_input=read_work_input,
                ).missing,
                (),
            )

            # 6. Independent SQLite invariants check on canonical v1 core-only store.
            with closing(open_reader(store)) as db, read_tx(db):
                self.assertEqual(
                    db.execute("PRAGMA user_version").fetchone(), (SCHEMA_VERSION,)
                )
                self.assertEqual(db.execute("PRAGMA quick_check").fetchall(), [("ok",)])
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                domains = [
                    r[0]
                    for r in db.execute(
                        "SELECT domain FROM installed_domains ORDER BY domain"
                    ).fetchall()
                ]
                self.assertEqual(domains, ["core"])
                paper_tables = db.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table' AND name LIKE 'paper_%'"
                ).fetchall()
                self.assertEqual(paper_tables, [])
                history = db.execute(
                    """SELECT revision, operation, candidate_id, quarantined
                       FROM review_tool_activation
                       WHERE company_id = ?
                       ORDER BY revision""",
                    (company_id,),
                ).fetchall()
                self.assertEqual(
                    history,
                    [
                        (1, "activate", "rule-v1", 0),
                        (2, "activate", "rule-v2", 1),
                        (3, "activate", "rule-v3", 1),
                        (4, "quarantine", "rule-v1", 0),
                        (5, "activate", "rule-v4-clean", 0),
                        (6, "revert", "rule-v1", 0),
                    ],
                )
                receipts = db.execute(
                    """SELECT COUNT(*) FROM command_receipt
                       WHERE company_id = ? AND command_type LIKE 'review_tool.%' AND state = 'applied'""",
                    (company_id,),
                ).fetchone()
                self.assertEqual(receipts, (6,))
