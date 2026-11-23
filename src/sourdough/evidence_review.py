"""Qualify an immutable, read-only company evidence review tool."""

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256


REVIEW_FORMAT = "evidence-review-v1"
EVALUATOR_REVISION = "evidence-review-evaluator-v1"


class ReviewRuleRefused(ValueError):
    """The artifact or independent check is not supported."""


class EvidenceClass(StrEnum):
    SOURCE_REFERENCE = "source_reference"
    REPRODUCTION = "reproduction"
    INDEPENDENT_VERIFICATION = "independent_verification"


def _canonical(classes: tuple[EvidenceClass, ...]) -> tuple[EvidenceClass, ...]:
    if type(classes) is not tuple or any(
        not isinstance(evidence, EvidenceClass) for evidence in classes
    ):
        raise ReviewRuleRefused("Expected supported evidence classes")
    if len(set(classes)) != len(classes):
        raise ReviewRuleRefused("Repeated evidence class")
    return tuple(sorted(classes))


def _digest(value: object) -> str:
    canonical = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ReviewRule:
    required: tuple[EvidenceClass, ...]

    def __post_init__(self) -> None:
        if not self.required:
            raise ReviewRuleRefused("At least one evidence class is required")
        object.__setattr__(self, "required", _canonical(self.required))

    @classmethod
    def from_payload(cls, payload: object) -> "ReviewRule":
        if (
            type(payload) is not dict
            or payload.keys() != {"format", "required"}
            or payload["format"] != REVIEW_FORMAT
            or type(payload["required"]) is not list
            or not 1 <= len(payload["required"]) <= len(EvidenceClass)
        ):
            raise ReviewRuleRefused("Unsupported review rule payload")
        try:
            required = tuple(EvidenceClass(value) for value in payload["required"])
        except ValueError as error:
            raise ReviewRuleRefused("Unsupported evidence class") from error
        return cls(required)

    @property
    def digest(self) -> str:
        return _digest({"format": REVIEW_FORMAT, "required": self.required})

    def missing(self, present: tuple[EvidenceClass, ...]) -> tuple[EvidenceClass, ...]:
        available = frozenset(_canonical(present))
        return tuple(item for item in self.required if item not in available)


@dataclass(frozen=True)
class ReviewCase:
    present: tuple[EvidenceClass, ...]
    expected_missing: tuple[EvidenceClass, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "present", _canonical(self.present))
        object.__setattr__(self, "expected_missing", _canonical(self.expected_missing))
        if not set(self.present).isdisjoint(self.expected_missing):
            raise ReviewRuleRefused("Case expects existing evidence to be missing")


@dataclass(frozen=True)
class CheckOutcome:
    expected_missing: tuple[EvidenceClass, ...]
    candidate_missing: tuple[EvidenceClass, ...]
    baseline_missing: tuple[EvidenceClass, ...] | None


@dataclass(frozen=True)
class ReviewQualification:
    candidate_digest: str
    baseline_digest: str | None
    criteria_digest: str
    outcomes: tuple[CheckOutcome, ...]
    evaluator_revision: str = EVALUATOR_REVISION

    @property
    def passed(self) -> bool:
        return bool(self.outcomes) and all(
            outcome.candidate_missing == outcome.expected_missing
            for outcome in self.outcomes
        )


def qualify_review_rule(
    candidate: ReviewRule,
    baseline: ReviewRule | None,
    cases: tuple[ReviewCase, ...],
) -> ReviewQualification:
    if type(cases) is not tuple or not cases:
        raise ReviewRuleRefused("Independent qualification requires cases")
    criteria_digest = _digest(
        [EVALUATOR_REVISION, [(case.present, case.expected_missing) for case in cases]]
    )
    return ReviewQualification(
        candidate.digest,
        baseline.digest if baseline is not None else None,
        criteria_digest,
        tuple(
            CheckOutcome(
                case.expected_missing,
                candidate.missing(case.present),
                baseline.missing(case.present) if baseline is not None else None,
            )
            for case in cases
        ),
    )
