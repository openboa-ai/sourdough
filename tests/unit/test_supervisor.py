import os
import signal
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.supervisor import supervise_worker
from sourdough.writer_lease import WriterRefused, hold_company_writer


class LocalSupervision(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.company_id = found(
            self.store, FoundingTerms("setup", "charter", "paper", "runtime")
        ).company_id
        self.events = []

    def supervise(self, program, *, max_restarts):
        return supervise_worker(
            [
                sys.executable,
                "-B",
                "-c",
                program,
                str(self.store),
                self.company_id,
                str(self.root),
            ],
            self.store,
            self.company_id,
            max_restarts=max_restarts,
            startup_seconds=4,
            max_seconds=8,
            emit=self.events.append,
        )

    def test_restarts_the_child_after_an_observed_exit_on_the_same_store(self):
        result = self.supervise(
            """
import os
from pathlib import Path
import signal
import sys
from sourdough.supervisor import announce_ready
from sourdough.writer_lease import hold_company_writer

store, company_id, root, _, descriptor, _, key = sys.argv[1:]
root = Path(root)
expected = tuple(map(int, key.split(":")))
with hold_company_writer(Path(store), company_id, lease_directory=root/"leases",
                         expected_store_key=expected) as lease:
    announce_ready(int(descriptor), lease)
    marker = root/"prior-attempt"
    if not marker.exists():
        marker.touch()
        os.kill(os.getpid(), signal.SIGKILL)
""",
            max_restarts=1,
        )
        self.assertTrue(result.completed)
        first, second = result.attempts
        self.assertEqual(first.returncode, -signal.SIGKILL)
        self.assertEqual(second.returncode, 0)
        self.assertNotEqual(first.pid, second.pid)
        self.assertLess(first.ready_at_ns, first.exited_at_ns)
        self.assertLess(first.exited_at_ns, second.ready_at_ns)
        self.assertEqual(
            [event["event"] for event in self.events],
            [
                "worker_started",
                "worker_ready",
                "worker_exited",
                "worker_started",
                "worker_ready",
                "worker_exited",
            ],
        )

    def test_successful_exit_without_evidence_is_not_a_ready_worker(self):
        result = self.supervise("pass", max_restarts=1)
        self.assertFalse(result.completed)
        self.assertEqual(len(result.attempts), 2)
        self.assertTrue(all(attempt.ready_at_ns is None for attempt in result.attempts))
        self.assertEqual(
            self.events[-1], {"event": "supervision_exhausted", "attempts": 2}
        )

    def test_worker_checks_the_expected_file_before_acquisition(self):
        other = self.root / "other.sqlite"
        other_id = found(
            other, FoundingTerms("other", "charter", "paper", "runtime")
        ).company_id
        identity = os.stat(self.store)
        with self.assertRaisesRegex(WriterRefused, "configured writer identity"):
            with hold_company_writer(
                other,
                other_id,
                lease_directory=self.root / "leases",
                expected_store_key=(identity.st_dev, identity.st_ino),
            ):
                self.fail("Replaced physical store must not be acquired")
        self.assertFalse((self.root / "leases").exists())
