"""Qualify an immutable, read-only company evidence review tool."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256


REVIEW_FORMAT = "evidence-review-v1"
EVALUATOR_REVISION = "evidence-review-evaluator-v1"
PASS_K_TRIALS = 3
MAX_REVIEW_CASES = 32


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
    present: tuple[EvidenceClass, ...]
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
    gate_passed: bool = True

    @property
    def passed(self) -> bool:
        return (
            bool(self.gate_passed)
            and bool(self.outcomes)
            and all(
                outcome.candidate_missing == outcome.expected_missing
                for outcome in self.outcomes
            )
        )


@dataclass(frozen=True)
class ThreeStageVerdict:
    stage1_structure_and_negative: bool
    stage2_harmlessness: bool
    stage3_pass_k_stability: bool
    trial_count: int
    qualification: ReviewQualification
    failure_stage: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.stage1_structure_and_negative
            and self.stage2_harmlessness
            and self.stage3_pass_k_stability
            and self.qualification.passed
        )


TrialEvaluator = Callable[
    [ReviewRule, ReviewRule | None, tuple[ReviewCase, ...], int],
    ReviewQualification,
]


def review_criteria_digest(cases: tuple[ReviewCase, ...]) -> str:
    if (
        type(cases) is not tuple
        or not 1 <= len(cases) <= MAX_REVIEW_CASES
        or any(not isinstance(case, ReviewCase) for case in cases)
    ):
        raise ReviewRuleRefused("Expected a bounded set of independent cases")
    return _digest(
        [EVALUATOR_REVISION, [(case.present, case.expected_missing) for case in cases]]
    )


def negative_criteria_digest(negative_cases: tuple[ReviewCase, ...]) -> str | None:
    if (
        type(negative_cases) is not tuple
        or len(negative_cases) > MAX_REVIEW_CASES
        or any(not isinstance(case, ReviewCase) for case in negative_cases)
    ):
        raise ReviewRuleRefused("Expected a bounded tuple of negative-trigger cases")
    if not negative_cases:
        return None
    return _digest(
        [
            EVALUATOR_REVISION,
            [(case.present, case.expected_missing) for case in negative_cases],
        ]
    )


def qualify_review_rule(
    candidate: ReviewRule,
    baseline: ReviewRule | None,
    cases: tuple[ReviewCase, ...],
) -> ReviewQualification:
    return ReviewQualification(
        candidate.digest,
        baseline.digest if baseline is not None else None,
        review_criteria_digest(cases),
        tuple(
            CheckOutcome(
                case.present,
                case.expected_missing,
                candidate.missing(case.present),
                baseline.missing(case.present) if baseline is not None else None,
            )
            for case in cases
        ),
    )


def _validate_trial_result(
    trial: ReviewQualification,
    candidate: ReviewRule,
    baseline: ReviewRule | None,
    expected_criteria: str,
) -> None:
    expected_baseline = baseline.digest if baseline is not None else None
    if (
        not isinstance(trial, ReviewQualification)
        or trial.evaluator_revision != EVALUATOR_REVISION
        or trial.candidate_digest != candidate.digest
        or trial.baseline_digest != expected_baseline
        or trial.criteria_digest != expected_criteria
    ):
        raise ReviewRuleRefused(
            "Trial evaluator returned mismatched qualification pins"
        )


def qualify_review_rule_gated(
    candidate: ReviewRule,
    baseline: ReviewRule | None,
    cases: tuple[ReviewCase, ...],
    *,
    negative_cases: tuple[ReviewCase, ...] = (),
    trials: int = PASS_K_TRIALS,
    trial_evaluator: TrialEvaluator | None = None,
) -> ThreeStageVerdict:
    """Evaluate a candidate through Stage 1 (structure/negative), Stage 2 (harmlessness), and Stage 3 (pass^k)."""
    if not isinstance(candidate, ReviewRule):
        raise ReviewRuleRefused("Candidate must be a valid ReviewRule")
    if baseline is not None and not isinstance(baseline, ReviewRule):
        raise ReviewRuleRefused("Baseline must be a valid ReviewRule or None")
    negative_criteria_digest(negative_cases)
    if type(trials) is not int or not 1 <= trials <= 16:
        raise ReviewRuleRefused("Expected a bounded positive trial count")
    expected_criteria = review_criteria_digest(cases)

    # Stage 1: Structure & negative-trigger check (complete evidence never yields false positives).
    complete_present = tuple(EvidenceClass)
    stage1_cases = (
        *negative_cases,
        *(case for case in cases if not case.expected_missing),
    )
    stage1_ok = candidate.missing(complete_present) == () and all(
        candidate.missing(case.present) == case.expected_missing
        for case in stage1_cases
    )

    # Stage 2: Harmlessness (read-only evaluation produces exact expected detections).
    first = (
        trial_evaluator(candidate, baseline, cases, 0)
        if trial_evaluator is not None
        else qualify_review_rule(candidate, baseline, cases)
    )
    _validate_trial_result(first, candidate, baseline, expected_criteria)
    stage2_ok = first.passed

    # Stage 3: pass^k stability across k independent trials with identical digests & outcomes.
    stage3_ok = stage2_ok
    if stage1_ok and stage2_ok:
        for attempt in range(1, trials):
            trial = (
                trial_evaluator(candidate, baseline, cases, attempt)
                if trial_evaluator is not None
                else qualify_review_rule(candidate, baseline, cases)
            )
            if not isinstance(trial, ReviewQualification):
                stage3_ok = False
                break
            if (
                trial.evaluator_revision != EVALUATOR_REVISION
                or trial.candidate_digest != candidate.digest
                or trial.baseline_digest != first.baseline_digest
                or trial.criteria_digest != expected_criteria
                or not trial.passed
                or trial != first
            ):
                stage3_ok = False
                break
    else:
        stage3_ok = False

    failure_stage: str | None = None
    if not stage1_ok:
        failure_stage = "stage1_structure_and_negative"
    elif not stage2_ok:
        failure_stage = "stage2_harmlessness"
    elif not stage3_ok:
        failure_stage = "stage3_pass_k_stability"

    gate_ok = stage1_ok and stage2_ok and stage3_ok
    gated_qualification = ReviewQualification(
        candidate_digest=first.candidate_digest,
        baseline_digest=first.baseline_digest,
        criteria_digest=first.criteria_digest,
        outcomes=first.outcomes,
        evaluator_revision=first.evaluator_revision,
        gate_passed=gate_ok,
    )

    return ThreeStageVerdict(
        stage1_structure_and_negative=stage1_ok,
        stage2_harmlessness=stage2_ok,
        stage3_pass_k_stability=stage3_ok,
        trial_count=trials,
        qualification=gated_qualification,
        failure_stage=failure_stage,
    )
