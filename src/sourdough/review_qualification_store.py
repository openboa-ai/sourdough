"""Record independent qualification of published company evidence review rules."""

import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from sqlite3 import Connection

from .company import _connect, _identity
from .evidence_review import (
    CheckOutcome,
    EvidenceClass,
    ReviewCase,
    ReviewQualification,
    ReviewRule,
    ReviewRuleRefused,
    qualify_review_rule,
)
from .writer_lease import WriterLease


_RECEIPT_FORMAT = "evidence-qualification-receipt-v1"


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


def reopen_published_review_rule(
    database: Connection, company_id: str, asset_id: str, resolve: PublishedRuleResolver
) -> ReviewRule:
    if not isinstance(asset_id, str) or not 1 <= len(asset_id) <= 128:
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


def record_review_qualification(
    writer: WriterLease,
    candidate_asset_id: str,
    baseline_asset_id: str | None,
    *,
    resolve: PublishedRuleResolver,
    cases: tuple[ReviewCase, ...],
) -> ReviewQualificationReceipt:
    """Resolve published assets and sealed cases through trusted company services."""
    writer.verify()
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        writer.verify()
        company_id = _identity(database)
        if company_id != writer.company_id:
            raise ReviewRuleRefused("Company identity mismatch")
        candidate = reopen_published_review_rule(
            database, company_id, candidate_asset_id, resolve
        )
        baseline = (
            reopen_published_review_rule(
                database, company_id, baseline_asset_id, resolve
            )
            if baseline_asset_id is not None
            else None
        )
        qualified = qualify_review_rule(candidate, baseline, cases)
        document = json.dumps(
            {
                "format": _RECEIPT_FORMAT,
                "company_id": company_id,
                "candidate_asset_id": candidate_asset_id,
                "baseline_asset_id": baseline_asset_id,
                "qualification": asdict(qualified),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        receipt_id = _receipt_id(document)
        database.execute(
            """CREATE TABLE IF NOT EXISTS review_qualification (
                   receipt_id TEXT PRIMARY KEY NOT NULL, evidence TEXT NOT NULL
               )"""
        )
        database.execute(
            "INSERT INTO review_qualification VALUES (?, ?) ON CONFLICT DO NOTHING",
            (receipt_id, document),
        )
        receipt = ReviewQualificationReceipt(
            receipt_id, candidate_asset_id, baseline_asset_id, qualified
        )
        if read_review_qualification(database, receipt_id) != receipt:
            raise ReviewRuleRefused("Recorded review qualification does not match")
        return receipt


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


def read_review_qualification(
    database: Connection, receipt_id: str
) -> ReviewQualificationReceipt | None:
    if not database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='review_qualification'"
    ).fetchone():
        return None
    row = database.execute(
        "SELECT evidence FROM review_qualification WHERE receipt_id=?", (receipt_id,)
    ).fetchone()
    if row is None:
        return None
    document = row[0]
    if not isinstance(document, str) or _receipt_id(document) != receipt_id:
        raise ReviewRuleRefused("Review qualification receipt integrity mismatch")
    stored = json.loads(document)
    if stored["format"] != _RECEIPT_FORMAT or stored["company_id"] != _identity(
        database
    ):
        raise ReviewRuleRefused("Review qualification is not for this company")
    qualified = stored["qualification"]
    return ReviewQualificationReceipt(
        receipt_id,
        stored["candidate_asset_id"],
        stored["baseline_asset_id"],
        ReviewQualification(
            qualified["candidate_digest"],
            qualified["baseline_digest"],
            qualified["criteria_digest"],
            tuple(_outcome(outcome) for outcome in qualified["outcomes"]),
            qualified["evaluator_revision"],
        ),
    )
