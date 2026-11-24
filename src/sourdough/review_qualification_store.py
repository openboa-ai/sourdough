"""Record independent qualification of published company evidence review rules."""

import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from sqlite3 import Connection
from time import time_ns

from .company import _identity
from .evidence_review import (
    PASS_K_TRIALS,
    CheckOutcome,
    EvidenceClass,
    ReviewCase,
    ReviewQualification,
    ReviewRule,
    ReviewRuleRefused,
    TrialEvaluator,
    qualify_review_rule_gated,
)
from .runtime.registry import DutyContext, DutyDescriptor, DutyOutcome, DutyRegistry
from .storage.db import open_writer, write_tx
from .storage.kernel import append_audit_event
from .writer_lease import WriterLease


_RECEIPT_FORMAT = "evidence-qualification-receipt-v1"
CHANGE_QUALIFY_DUTY_KIND = "ops.change_qualify"


@dataclass(frozen=True)
class PublishedReviewRule:
    company_id: str
    asset_id: str
    rule: ReviewRule


PublishedRuleResolver = Callable[[Connection, str, str], PublishedReviewRule | None]


@dataclass(frozen=True)
class ReviewQualificationReceipt:
    receipt_id: str
    candidate_asset_id: str
    baseline_asset_id: str | None
    qualification: ReviewQualification


@dataclass(frozen=True)
class ReviewQualificationRecord:
    receipt: ReviewQualificationReceipt
    passed: bool
    trial_count: int
    parent_asset_id: str | None


def _valid_asset_id(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 128


def resolve_shared_review_rule(
    database: Connection, company_id: str, asset_id: str
) -> PublishedReviewRule | None:
    """Resolve an immutable ReviewRule published in core shared_publication."""
    row = database.execute(
        """SELECT company_id, asset_id, content
           FROM shared_publication
           WHERE company_id = ? AND asset_id = ?""",
        (company_id, asset_id),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[2])
    except (TypeError, json.JSONDecodeError) as error:
        raise ReviewRuleRefused(
            "Published review asset payload is not valid JSON"
        ) from error
    return PublishedReviewRule(
        str(row[0]), str(row[1]), ReviewRule.from_payload(payload)
    )


def reopen_published_review_rule(
    database: Connection, company_id: str, asset_id: str, resolve: PublishedRuleResolver
) -> ReviewRule:
    if not _valid_asset_id(asset_id):
        raise ReviewRuleRefused("Invalid published review asset identity")
    asset = resolve(database, company_id, asset_id)
    if (
        asset is None
        or (asset.company_id, asset.asset_id) != (company_id, asset_id)
        or not isinstance(asset.rule, ReviewRule)
    ):
        raise ReviewRuleRefused("Published review asset is unavailable in this company")
    return asset.rule


def _receipt_id(document: str) -> str:
    return sha256(document.encode()).hexdigest()


def _record_review_qualification_in_tx(
    database: Connection,
    company_id: str,
    candidate_asset_id: str,
    baseline_asset_id: str | None,
    *,
    resolve: PublishedRuleResolver,
    cases: tuple[ReviewCase, ...],
    parent_asset_id: str | None = None,
    negative_cases: tuple[ReviewCase, ...] = (),
    trials: int = 1,
    trial_evaluator: TrialEvaluator | None = None,
    actor_id: str = "evaluator",
    recorded_at_ns: int | None = None,
) -> ReviewQualificationRecord:
    if parent_asset_id is not None and not _valid_asset_id(parent_asset_id):
        raise ReviewRuleRefused("Invalid parent review asset identity")
    if parent_asset_id is not None and parent_asset_id == candidate_asset_id:
        raise ReviewRuleRefused("Review asset cannot be its own lineage parent")
    candidate = reopen_published_review_rule(
        database, company_id, candidate_asset_id, resolve
    )
    baseline = (
        reopen_published_review_rule(database, company_id, baseline_asset_id, resolve)
        if baseline_asset_id is not None
        else None
    )
    verdict = qualify_review_rule_gated(
        candidate,
        baseline,
        cases,
        negative_cases=negative_cases,
        trials=trials,
        trial_evaluator=trial_evaluator,
    )
    qualified = verdict.qualification
    body: dict[str, object] = {
        "format": _RECEIPT_FORMAT,
        "company_id": company_id,
        "candidate_asset_id": candidate_asset_id,
        "baseline_asset_id": baseline_asset_id,
        "qualification": asdict(qualified),
    }
    if verdict.failure_stage in (
        "stage1_structure_and_negative",
        "stage3_pass_k_stability",
    ):
        body["gate_failure"] = {
            "stage": verdict.failure_stage,
            "trial_count": verdict.trial_count,
        }
    document = json.dumps(body, separators=(",", ":"), sort_keys=True)
    receipt_id = _receipt_id(document)
    now_ns = time_ns() if recorded_at_ns is None else recorded_at_ns
    cursor = database.execute(
        """INSERT INTO review_qualification (
               company_id, receipt_id, candidate_asset_id, baseline_asset_id,
               parent_asset_id, passed, trial_count, evidence, recorded_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (company_id, receipt_id) DO NOTHING""",
        (
            company_id,
            receipt_id,
            candidate_asset_id,
            baseline_asset_id,
            parent_asset_id,
            int(verdict.passed),
            verdict.trial_count,
            document,
            now_ns,
        ),
    )
    stored = read_review_qualification_record(
        database, receipt_id, company_id=company_id
    )
    expected = ReviewQualificationReceipt(
        receipt_id, candidate_asset_id, baseline_asset_id, qualified
    )
    if stored is None or stored.receipt != expected:
        raise ReviewRuleRefused("Recorded review qualification does not match")
    if cursor.rowcount == 1:
        append_audit_event(
            database,
            company_id=company_id,
            domain="ops",
            entity_type="review_qualification",
            entity_id=receipt_id,
            event_type="ops.review_qualified",
            actor_id=actor_id,
            payload={
                "candidate_asset_id": candidate_asset_id,
                "baseline_asset_id": baseline_asset_id,
                "parent_asset_id": parent_asset_id,
                "passed": verdict.passed,
                "trial_count": verdict.trial_count,
                "failure_stage": verdict.failure_stage,
            },
            recorded_at_ns=now_ns,
        )
    return stored


def record_review_qualification(
    writer: WriterLease,
    candidate_asset_id: str,
    baseline_asset_id: str | None,
    *,
    resolve: PublishedRuleResolver,
    cases: tuple[ReviewCase, ...],
    parent_asset_id: str | None = None,
    negative_cases: tuple[ReviewCase, ...] = (),
    trials: int = 1,
    trial_evaluator: TrialEvaluator | None = None,
    actor_id: str = "evaluator",
) -> ReviewQualificationReceipt:
    """Resolve published assets and sealed cases through trusted company services."""
    writer.verify()
    with (
        closing(open_writer(writer.store, lease=writer)) as database,
        write_tx(database),
    ):
        writer.verify()
        company_id = _identity(database)
        if company_id != writer.company_id:
            raise ReviewRuleRefused("Company identity mismatch")
        record = _record_review_qualification_in_tx(
            database,
            company_id,
            candidate_asset_id,
            baseline_asset_id,
            resolve=resolve,
            cases=cases,
            parent_asset_id=parent_asset_id,
            negative_cases=negative_cases,
            trials=trials,
            trial_evaluator=trial_evaluator,
            actor_id=actor_id,
        )
        return record.receipt


def _classes(raw: list[str]) -> tuple[EvidenceClass, ...]:
    return tuple(EvidenceClass(value) for value in raw)


def _outcome(raw: dict) -> CheckOutcome:
    previous = raw["baseline_missing"]
    return CheckOutcome(
        _classes(raw["present"]),
        _classes(raw["expected_missing"]),
        _classes(raw["candidate_missing"]),
        _classes(previous) if previous is not None else None,
    )


def read_review_qualification_record(
    database: Connection,
    receipt_id: str,
    *,
    company_id: str | None = None,
) -> ReviewQualificationRecord | None:
    expected_company_id = _identity(database) if company_id is None else company_id
    row = database.execute(
        """SELECT evidence, passed, trial_count, parent_asset_id
           FROM review_qualification
           WHERE company_id = ? AND receipt_id = ?""",
        (expected_company_id, receipt_id),
    ).fetchone()
    if row is None:
        return None
    document, db_passed, trial_count, parent_asset_id = row
    if not isinstance(document, str) or _receipt_id(document) != receipt_id:
        raise ReviewRuleRefused("Review qualification receipt integrity mismatch")
    stored = json.loads(document)
    if (
        stored["format"] != _RECEIPT_FORMAT
        or stored["company_id"] != expected_company_id
    ):
        raise ReviewRuleRefused("Review qualification is not for this company")
    qualified = stored["qualification"]
    qualification = ReviewQualification(
        qualified["candidate_digest"],
        qualified["baseline_digest"],
        qualified["criteria_digest"],
        tuple(_outcome(outcome) for outcome in qualified["outcomes"]),
        qualified["evaluator_revision"],
    )
    gate_failed = "gate_failure" in stored
    effective_passed = bool(db_passed) and qualification.passed and not gate_failed
    return ReviewQualificationRecord(
        receipt=ReviewQualificationReceipt(
            receipt_id,
            stored["candidate_asset_id"],
            stored["baseline_asset_id"],
            qualification,
        ),
        passed=effective_passed,
        trial_count=int(trial_count),
        parent_asset_id=None if parent_asset_id is None else str(parent_asset_id),
    )


def read_review_qualification(
    database: Connection,
    receipt_id: str,
    *,
    company_id: str | None = None,
) -> ReviewQualificationReceipt | None:
    record = read_review_qualification_record(
        database, receipt_id, company_id=company_id
    )
    return None if record is None else record.receipt


def _parse_cases(raw: object) -> tuple[ReviewCase, ...]:
    if not isinstance(raw, list):
        raise ReviewRuleRefused("Expected a list of review cases in duty payload")
    parsed: list[ReviewCase] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or set(item.keys()) != {"present", "expected_missing"}
            or not isinstance(item["present"], list)
            or not isinstance(item["expected_missing"], list)
        ):
            raise ReviewRuleRefused("Invalid review case entry in duty payload")
        try:
            parsed.append(
                ReviewCase(
                    tuple(EvidenceClass(value) for value in item["present"]),
                    tuple(EvidenceClass(value) for value in item["expected_missing"]),
                )
            )
        except ValueError as error:
            raise ReviewRuleRefused(
                "Unsupported evidence class in duty payload"
            ) from error
    return tuple(parsed)


def register_change_qualification_duty(
    registry: DutyRegistry,
    *,
    resolve: PublishedRuleResolver = resolve_shared_review_rule,
    trial_evaluator: TrialEvaluator | None = None,
    required_scope: str | None = None,
) -> None:
    """Register the ops.change_qualify DutyRegistry handler enforcing 3-stage pass^3 gating."""

    def _execute(context: DutyContext) -> DutyOutcome:
        payload = json.loads(context.work_item.payload_json)
        if not isinstance(payload, dict):
            raise ReviewRuleRefused("Invalid ops.change_qualify payload")
        candidate_asset_id = payload.get("candidate_asset_id")
        baseline_asset_id = payload.get("baseline_asset_id")
        parent_asset_id = payload.get("parent_asset_id")
        if not isinstance(candidate_asset_id, str) or not _valid_asset_id(
            candidate_asset_id
        ):
            raise ReviewRuleRefused("Missing valid candidate_asset_id")
        if baseline_asset_id is not None and (
            not isinstance(baseline_asset_id, str)
            or not _valid_asset_id(baseline_asset_id)
        ):
            raise ReviewRuleRefused("Invalid baseline_asset_id")
        if parent_asset_id is not None and (
            not isinstance(parent_asset_id, str) or not _valid_asset_id(parent_asset_id)
        ):
            raise ReviewRuleRefused("Invalid parent_asset_id")
        cases = _parse_cases(payload.get("cases"))
        raw_negative = payload.get("negative_cases", [])
        negative_cases = _parse_cases(raw_negative) if raw_negative else ()
        trials = payload.get("trials", PASS_K_TRIALS)
        if type(trials) is not int or trials < PASS_K_TRIALS:
            raise ReviewRuleRefused(
                f"ops.change_qualify requires at least {PASS_K_TRIALS} trials (pass^3)"
            )

        now_ns = context.now_ns
        with (
            closing(
                open_writer(context.writer.store, lease=context.writer)
            ) as database,
            write_tx(database),
        ):
            context.writer.verify()
            if _identity(database) != context.company_id:
                raise ReviewRuleRefused("Company identity mismatch")
            pub_row = database.execute(
                """SELECT author_id FROM shared_publication
                   WHERE company_id = ? AND asset_id = ?""",
                (context.company_id, candidate_asset_id),
            ).fetchone()
            if (
                pub_row is not None
                and str(pub_row[0]) == context.work_item.owner_agent_id
            ):
                append_audit_event(
                    database,
                    company_id=context.company_id,
                    domain="ops",
                    entity_type="review_qualification",
                    entity_id=candidate_asset_id,
                    event_type="ops.review_self_evaluation_blocked",
                    actor_id=context.work_item.owner_agent_id,
                    run_id=context.run.run_id,
                    payload={"candidate_asset_id": candidate_asset_id},
                    recorded_at_ns=now_ns,
                )
                return DutyOutcome(
                    disposition="blocked",
                    result={
                        "reason": "self_evaluation_refused",
                        "candidate_asset_id": candidate_asset_id,
                        "author_id": str(pub_row[0]),
                    },
                )
            record = _record_review_qualification_in_tx(
                database,
                context.company_id,
                candidate_asset_id,
                baseline_asset_id,
                resolve=resolve,
                cases=cases,
                parent_asset_id=parent_asset_id,
                negative_cases=negative_cases,
                trials=trials,
                trial_evaluator=trial_evaluator,
                actor_id=context.work_item.owner_agent_id,
                recorded_at_ns=now_ns,
            )
        return DutyOutcome(
            disposition="completed",
            result={
                "receipt_id": record.receipt.receipt_id,
                "candidate_asset_id": record.receipt.candidate_asset_id,
                "baseline_asset_id": record.receipt.baseline_asset_id,
                "parent_asset_id": record.parent_asset_id,
                "passed": record.passed,
                "trial_count": record.trial_count,
            },
        )

    registry.register(
        DutyDescriptor(
            duty_kind=CHANGE_QUALIFY_DUTY_KIND,
            execute=_execute,
            required_scope=required_scope,
        )
    )
