import os
import select
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingRefused, FoundingTerms, found
from sourdough.writer_lease import WriterRefused, hold_company_writer


class WriterLeaseTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.leases = self.root / "leases"
        self.terms = FoundingTerms("setup", "charter", "paper", "profile")
        self.company_id = found(self.store, self.terms).company_id

    def test_same_store_through_hard_link_is_one_writer_even_across_processes(self):
        alias = self.root / "alias.sqlite"
        code = """
from pathlib import Path
import sys
import time
from sourdough.writer_lease import hold_company_writer

with hold_company_writer(Path(sys.argv[1]), sys.argv[2], lease_directory=Path(sys.argv[3])) as lease:
    lease.verify()
    print("ready", flush=True)
    time.sleep(30)
"""
        with subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                code,
                str(self.store),
                self.company_id,
                str(self.leases),
            ],
            stdout=subprocess.PIPE,
            text=True,
        ) as first:
            try:
                self.assertTrue(select.select([first.stdout], [], [], 5)[0])
                self.assertEqual(first.stdout.readline().strip(), "ready")
                self.assertIsNone(first.poll())
                os.link(self.store, alias)
                with self.assertRaisesRegex(WriterRefused, "already has a writer"):
                    with hold_company_writer(
                        alias, self.company_id, lease_directory=self.leases
                    ):
                        self.fail("A second process acquired the same file")
            finally:
                if first.poll() is None:
                    first.kill()
                first.wait(5)
            self.assertEqual(first.returncode, -9)
        with self.assertRaisesRegex(WriterRefused, "Hard-link aliases"):
            with hold_company_writer(
                alias, self.company_id, lease_directory=self.leases
            ):
                self.fail("An alias must not open a different SQLite journal path")
        alias.unlink()
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as replacement:
            replacement.verify()

    def test_replaced_file_is_refused_before_further_mutation(self):
        substitute = self.root / "substitute.sqlite"
        other_id = found(substitute, self.terms).company_id
        self.assertNotEqual(other_id, self.company_id)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as lease:
            self.assertFalse(os.get_inheritable(lease._lock_descriptor))
            os.replace(substitute, self.store)
            with self.assertRaisesRegex(WriterRefused, "identity changed"):
                lease.verify()
        with self.assertRaisesRegex(FoundingRefused, "identity mismatch"):
            with hold_company_writer(
                self.store, self.company_id, lease_directory=self.leases
            ):
                self.fail("The wrong company acquired a writer lease")

    def test_missing_or_symbolic_store_never_creates_or_substitutes_database(self):
        absent = self.root / "absent.sqlite"
        alias = self.root / "alias.sqlite"
        alias.symlink_to(self.store)
        for candidate in (absent, alias):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(WriterRefused, "original company store"):
                    with hold_company_writer(
                        candidate, self.company_id, lease_directory=self.leases
                    ):
                        self.fail("An unsupported path acquired a lease")
        self.assertFalse(absent.exists())
        self.assertTrue(alias.is_symlink())

    def test_writer_can_commit_a_separate_sqlite_transaction_while_leased(self):
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as lease:
            with closing(sqlite3.connect(self.store)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE duty SET state = 'running'")
            lease.verify()
            with closing(sqlite3.connect(self.store)) as db:
                self.assertEqual(
                    db.execute("SELECT state FROM duty").fetchall(), [("running",)]
                )
