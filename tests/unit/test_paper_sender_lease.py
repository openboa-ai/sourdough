import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.paper_execution import VenueRefused
from sourdough.paper_sender_lease import hold_sender_lease, verify_sender_lease


_VERIFY = """
import sys
from pathlib import Path
from sourdough.paper_sender_lease import verify_worker_sender
verify_worker_sender(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3])
"""


class PaperSenderLease(unittest.TestCase):
    def test_child_uses_inherited_lease_without_opening_a_second_sender(self):
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "venue.sqlite"
            journal.touch(mode=0o600)
            with hold_sender_lease(journal) as (descriptor, key):
                with self.assertRaisesRegex(VenueRefused, "active or pending sender"):
                    with hold_sender_lease(journal):
                        self.fail("A separate sender acquired the original lock")
                result = subprocess.run(
                    [
                        sys.executable,
                        "-IB",
                        "-c",
                        _VERIFY,
                        str(journal),
                        str(descriptor),
                        f"{key[0]}:{key[1]}",
                    ],
                    pass_fds=(descriptor,),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                with self.assertRaisesRegex(VenueRefused, "active or pending sender"):
                    with hold_sender_lease(journal):
                        self.fail("Child exit released its live supervisor's lock")
                journal.rename(journal.with_name("original.sqlite"))
                journal.touch(mode=0o600)
                with self.assertRaisesRegex(VenueRefused, "journal changed"):
                    verify_sender_lease(journal, descriptor, key)
            with hold_sender_lease(journal):
                self.assertTrue(journal.is_file())
