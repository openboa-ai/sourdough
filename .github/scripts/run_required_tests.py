"""Run one mandatory unittest suite. Empty or skipped tests fail CI."""

import sys
import unittest


def case_ids(suite):
    for case in suite:
        if isinstance(case, unittest.TestSuite):
            yield from case_ids(case)
        else:
            yield case.id()


def main(directory, required_case=None):
    suite = unittest.TestLoader().discover(directory, pattern="test_*.py")
    if required_case and required_case not in case_ids(suite):
        print(f"Missing required case: {required_case}", file=sys.stderr)
        return 1
    result = unittest.TextTestRunner().run(suite)
    if result.skipped:
        for case, reason in result.skipped:
            print(f"Required test skipped: {case} ({reason})", file=sys.stderr)
        return 1
    if not result.testsRun:
        print(f"No required tests ran in {directory}.", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit("Usage: run_required_tests.py TEST_DIRECTORY [REQUIRED_CASE]")
    sys.exit(main(*sys.argv[1:]))
