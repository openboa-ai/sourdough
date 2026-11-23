"""Activate and use the company evidence review tool under scoped service authority."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import astuple, dataclass
from pathlib import Path
from sqlite3 import Connection

from .company import _connect, _identity
from .evidence_review import (
    EVALUATOR_REVISION,
    EvidenceClass,
    ReviewCase,
    ReviewRuleRefused,
    review_criteria_digest,
)
from .review_qualification_store import (
    PublishedRuleResolver,
    ReviewQualificationReceipt,
    read_review_qualification,
    reopen_published_review_rule,
)
from .writer_lease import WriterLease


def _identity_value(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 128


@dataclass(frozen=True)
class ReviewToolAuthority:
    principal_id: str
    grant_id: str
    owner_id: str


AuthorizeReviewTool = Callable[[Connection, str, str, str], ReviewToolAuthority | None]


@dataclass(frozen=True)
class ReviewActivation:
    request_id: str
    operation: str
    candidate_id: str
    qualification_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        if (
            self.operation not in ("activate", "revert")
            or type(self.expected_revision) is not int
            or not 0 <= self.expected_revision < 2**63 - 1
            or not all(
                _identity_value(value)
                for value in (self.request_id, self.candidate_id, self.qualification_id)
            )
        ):
            raise ReviewRuleRefused("Invalid review tool activation")


@dataclass(frozen=True)
class ReviewActivationReceipt:
    request_id: str
    operation: str
    candidate_id: str
    qualification_id: str
    revision: int
    principal_id: str
    grant_id: str
    owner_id: str


def _authorize(
    database: Connection,
    authorize: AuthorizeReviewTool,
    company_id: str,
    operation: str,
    asset_id: str,
) -> ReviewToolAuthority:
    authority = authorize(database, company_id, operation, asset_id)
    if not isinstance(authority, ReviewToolAuthority) or not all(
        _identity_value(value) for value in astuple(authority)
    ):
        raise ReviewRuleRefused("Current scoped review tool authority is required")
    return authority


def _history(
    database: Connection, *, request_id: str | None = None, before: int | None = None
) -> ReviewActivationReceipt | None:
    if not database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='review_tool_activation'"
    ).fetchone():
        return None
    row = database.execute(
        """SELECT request_id,operation,candidate_id,qualification_id,revision,
                  principal_id,grant_id,owner_id
           FROM review_tool_activation
           WHERE (? IS NULL OR request_id=?) AND (? IS NULL OR revision<?)
           ORDER BY revision DESC LIMIT 1""",
        (request_id, request_id, before, before),
    ).fetchone()
    return ReviewActivationReceipt(*row) if row is not None else None


def _qualification(
    database: Connection, qualification_id: str, candidate_id: str
) -> ReviewQualificationReceipt:
    receipt = read_review_qualification(database, qualification_id)
    if (
        receipt is None
        or receipt.candidate_asset_id != candidate_id
        or not receipt.qualification.passed
    ):
        raise ReviewRuleRefused("Matching independent qualification is required")
    return receipt


def _rule(database, company_id, receipt, resolve):
    rule = reopen_published_review_rule(
        database, company_id, receipt.candidate_asset_id, resolve
    )
    if rule.digest != receipt.qualification.candidate_digest:
        raise ReviewRuleRefused("Published review rule changed after qualification")
    return rule


def apply_review_activation(
    writer: WriterLease,
    command: ReviewActivation,
    *,
    authorize: AuthorizeReviewTool,
    resolve: PublishedRuleResolver,
    cases: tuple[ReviewCase, ...],
) -> ReviewActivationReceipt:
    writer.verify()
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        writer.verify()
        if _identity(database) != writer.company_id:
            raise ReviewRuleRefused("Company identity mismatch")
        authority = _authorize(
            database,
            authorize,
            writer.company_id,
            command.operation,
            command.candidate_id,
        )
        if prior := _history(database, request_id=command.request_id):
            if (
                astuple(prior)[:4] != astuple(command)[:4]
                or prior.revision != command.expected_revision + 1
                or prior.principal_id != authority.principal_id
            ):
                raise ReviewRuleRefused("Activation request identity already in use")
            return prior
        current = _history(database)
        if command.expected_revision != (current.revision if current else 0):
            raise ReviewRuleRefused("Effective review tool revision has changed")
        qualified = _qualification(
            database, command.qualification_id, command.candidate_id
        )
        outcome = qualified.qualification
        if (
            outcome.evaluator_revision != EVALUATOR_REVISION
            or outcome.criteria_digest != review_criteria_digest(cases)
        ):
            raise ReviewRuleRefused("Independent qualification is stale")
        _rule(database, writer.company_id, qualified, resolve)
        baseline_id = baseline_digest = None
        if current is not None:
            baseline_id = current.candidate_id
            baseline = _qualification(database, current.qualification_id, baseline_id)
            baseline_digest = _rule(
                database, writer.company_id, baseline, resolve
            ).digest
        if (qualified.baseline_asset_id, outcome.baseline_digest) != (
            baseline_id,
            baseline_digest,
        ):
            raise ReviewRuleRefused(
                "Qualification does not cover the effective baseline"
            )
        if command.operation == "revert":
            previous = _history(database, before=current.revision) if current else None
            if previous is None or previous.candidate_id != command.candidate_id:
                raise ReviewRuleRefused(
                    "Revert must target the prior eligible artifact"
                )
            previous_result = _qualification(
                database, previous.qualification_id, previous.candidate_id
            )
            if (
                previous_result.qualification.candidate_digest
                != outcome.candidate_digest
            ):
                raise ReviewRuleRefused(
                    "Revert artifact differs from the previous version"
                )
        receipt = ReviewActivationReceipt(
            *astuple(command)[:4],
            command.expected_revision + 1,
            *astuple(authority),
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS review_tool_activation (
                   request_id TEXT PRIMARY KEY NOT NULL, operation TEXT NOT NULL,
                   candidate_id TEXT NOT NULL, qualification_id TEXT NOT NULL
                   REFERENCES review_qualification(receipt_id), revision INTEGER NOT NULL UNIQUE,
                   principal_id TEXT NOT NULL, grant_id TEXT NOT NULL, owner_id TEXT NOT NULL
               )"""
        )
        database.execute(
            "INSERT INTO review_tool_activation VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            astuple(receipt),
        )
        return receipt


@dataclass(frozen=True)
class ReviewWorkInput:
    company_id: str
    result_id: str
    revision: str
    present: tuple[EvidenceClass, ...]


ReadReviewInput = Callable[[Connection, str, str, str], ReviewWorkInput | None]


@dataclass(frozen=True)
class ReviewToolInspection:
    effective_revision: int
    principal_id: str
    result_id: str
    result_revision: str
    missing: tuple[EvidenceClass, ...]


def inspect_with_review_tool(
    store: Path,
    company_id: str,
    revision: int,
    result_id: str,
    *,
    authorize: AuthorizeReviewTool,
    resolve: PublishedRuleResolver,
    read_input: ReadReviewInput,
) -> ReviewToolInspection:
    if type(revision) is not int or revision < 1 or not _identity_value(result_id):
        raise ReviewRuleRefused("Invalid review tool inspection")
    with closing(_connect(store, "ro")) as database:
        database.execute("BEGIN")
        if _identity(database) != company_id:
            raise ReviewRuleRefused("Company identity mismatch")
        current = _history(database)
        if current is None or revision != current.revision:
            raise ReviewRuleRefused("Review tool revision is not effective")
        authority = _authorize(
            database, authorize, company_id, "inspect", current.candidate_id
        )
        source = read_input(database, company_id, authority.principal_id, result_id)
        if (
            source is None
            or (source.company_id, source.result_id) != (company_id, result_id)
            or not _identity_value(source.revision)
        ):
            raise ReviewRuleRefused("Work result is unavailable in this scope")
        qualified = _qualification(
            database, current.qualification_id, current.candidate_id
        )
        rule = _rule(database, company_id, qualified, resolve)
        return ReviewToolInspection(
            revision,
            authority.principal_id,
            result_id,
            source.revision,
            rule.missing(source.present),
        )
