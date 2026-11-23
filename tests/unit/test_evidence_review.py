import unittest

from sourdough.evidence_review import (
    REVIEW_FORMAT,
    EvidenceClass,
    ReviewCase,
    ReviewRule,
    ReviewRuleRefused,
    qualify_review_rule,
)


SOURCE = EvidenceClass.SOURCE_REFERENCE
REPRODUCTION = EvidenceClass.REPRODUCTION
VERIFICATION = EvidenceClass.INDEPENDENT_VERIFICATION


class EvidenceReview(unittest.TestCase):
    def test_candidate_and_independent_expectations_are_pinned_separately(self):
        current = ReviewRule((SOURCE,))
        candidate = ReviewRule((SOURCE, VERIFICATION))
        cases = (
            ReviewCase((SOURCE,), (VERIFICATION,)),
            ReviewCase((SOURCE, VERIFICATION), ()),
        )
        verdict = qualify_review_rule(candidate, current, cases)
        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.candidate_digest, candidate.digest)
        self.assertEqual(verdict.baseline_digest, current.digest)
        self.assertEqual(verdict.outcomes[0].candidate_missing, (VERIFICATION,))
        self.assertEqual(verdict.outcomes[0].baseline_missing, ())
        self.assertEqual(qualify_review_rule(candidate, current, cases), verdict)

        weakened = qualify_review_rule(current, current, cases)
        self.assertFalse(weakened.passed)
        self.assertNotEqual(weakened.candidate_digest, verdict.candidate_digest)
        self.assertEqual(weakened.criteria_digest, verdict.criteria_digest)

        revised_cases = (ReviewCase((SOURCE,), ()), cases[1])
        revised = qualify_review_rule(candidate, current, revised_cases)
        self.assertFalse(revised.passed)
        self.assertNotEqual(revised.criteria_digest, verdict.criteria_digest)
        self.assertEqual(revised.candidate_digest, verdict.candidate_digest)

        changed_baseline = qualify_review_rule(
            candidate, ReviewRule((REPRODUCTION,)), cases
        )
        self.assertNotEqual(changed_baseline.baseline_digest, verdict.baseline_digest)
        self.assertEqual(changed_baseline.criteria_digest, verdict.criteria_digest)
        self.assertEqual(changed_baseline.outcomes[0].baseline_missing, (REPRODUCTION,))

    def test_format_is_canonical_and_does_not_accept_verdict_or_execution_fields(self):
        payload = {
            "format": REVIEW_FORMAT,
            "required": [SOURCE.value, VERIFICATION.value],
        }
        rule = ReviewRule.from_payload(payload)
        self.assertEqual(rule.digest, ReviewRule((VERIFICATION, SOURCE)).digest)
        invalid = (
            payload | {"passed": True},
            payload | {"script": "review.py"},
            payload | {"format": "later"},
            payload | {"required": []},
            payload | {"required": [SOURCE, SOURCE]},
            payload | {"required": ["unknown"]},
            payload | {"required": [{"kind": SOURCE}]},
        )
        for supplied in invalid:
            with self.subTest(supplied=supplied), self.assertRaises(ReviewRuleRefused):
                ReviewRule.from_payload(supplied)

    def test_first_activation_uses_absent_baseline_and_requires_independent_cases(self):
        rule = ReviewRule((SOURCE,))
        case = ReviewCase((), (SOURCE,))
        verdict = qualify_review_rule(rule, None, (case,))
        self.assertTrue(verdict.passed)
        self.assertIsNone(verdict.baseline_digest)
        self.assertIsNone(verdict.outcomes[0].baseline_missing)
        with self.assertRaises(ReviewRuleRefused):
            qualify_review_rule(rule, None, ())
        with self.assertRaises(ReviewRuleRefused):
            ReviewCase((SOURCE,), (SOURCE,))
