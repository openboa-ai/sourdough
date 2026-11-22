import unittest

from product_scope import scope


class ProductScopeTest(unittest.TestCase):
    def test_unit_pr_runs_fast_suite_even_for_setup(self):
        for paths in [[], ["src/sourdough/company.py"], ["docs/spec.md"]]:
            self.assertEqual(scope("feat/flow-01-04", "codex/found-company", False, paths), "unit")

    def test_aggregate_requires_its_own_suite_even_for_empty_diff(self):
        for branch in ["flow-01-04", "flow-hired-agent"]:
            self.assertEqual(scope("main", f"feat/{branch}", True, []), branch)

    def test_direct_maintenance_does_not_claim_a_product_run(self):
        paths = ["README.md", "docs/engineering/plan.md", ".github/workflows/repository.yml", ".editorconfig"]
        self.assertEqual(scope("main", "ci/flow-routing", True, paths), "none")

    def test_product_cannot_take_maintenance_route(self):
        for path in ["src/a.py", "tests/unit/test_work.py", "pyproject.toml", "requirements-dev.lock",
                     ".python-version", "new-service/worker", "docs/tool.py"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                scope("main", "docs/change", True, ["docs/change.md", path])

    def test_fork_nested_and_malformed_aggregates_are_not_flow_evidence(self):
        for base, head, same_repo in [("main", "feat/flow-01-04", False), ("main", "feat/flow-../04", True),
                                      ("feat/flow-01-04", "feat/flow-other", True), ("feat/flow-", "fix/unit", True)]:
            with self.subTest(base=base, head=head), self.assertRaises(ValueError):
                scope(base, head, same_repo, [])
