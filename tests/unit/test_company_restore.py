import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingRefused, FoundingTerms, _connect, found, reopen
from sourdough.company_checkpoint import CheckpointRefused, checkpoint_company
from sourdough.company_restore import restore_core_checkpoint
from sourdough.first_duty import claim_first_duty
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.writer_lease import WriterRefused, hold_company_writer


class CoreCompanyRestore(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.source = root / "source.sqlite"
        self.archive = root / "checkpoint.sd"
        self.target = root / "replacement.sqlite"
        self.company = found(
            self.source, FoundingTerms("test", "paper", "paper", "profile")
        )

    def restore(self, target=None):
        return restore_core_checkpoint(
            self.source, self.company.company_id, self.archive, target or self.target
        )

    def test_original_writer_is_retired_even_if_publication_is_interrupted(self):
        company_id = self.company.company_id
        checkpoint_company(self.source, company_id, self.archive)
        with hold_company_writer(self.source, company_id):
            with self.assertRaisesRegex(WriterRefused, "already has a writer"):
                self.restore()
        self.assertFalse(self.target.exists())
        crash = subprocess.run(
            [
                sys.executable,
                "-IB",
                "-c",
                """
import os, sys
from pathlib import Path
from sourdough import company_restore
company_restore._publish = lambda *args: os._exit(73)
company_restore.restore_core_checkpoint(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
""",
                str(self.source),
                company_id,
                str(self.archive),
                str(self.target),
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(crash.returncode, 73, crash.stderr)
        self.assertFalse(self.target.exists())
        with closing(
            sqlite3.connect(self.source.as_uri() + "?mode=ro", uri=True)
        ) as original:
            self.assertEqual(
                original.execute("SELECT schema_revision FROM company").fetchone(),
                (-1,),
            )
        self.assertEqual(
            reopen(self.source, company_id).due_work_ids, (self.company.due_work_id,)
        )
        with self.assertRaisesRegex(FoundingRefused, "retired"):
            with hold_company_writer(self.source, company_id):
                self.fail("Original writer was approved after retirement")
        with self.assertRaisesRegex(FoundingRefused, "retired"):
            _connect(self.source, "rw")
        with self.assertRaisesRegex(CheckpointRefused, "another restore request"):
            self.restore(self.target.with_name("other.sqlite"))

        receipt = self.restore()
        self.assertEqual(self.restore(), receipt)
        self.assertEqual(self.target.stat().st_nlink, 1)
        with closing(
            sqlite3.connect(self.target.as_uri() + "?mode=ro", uri=True)
        ) as independent:
            self.assertEqual(
                independent.execute("SELECT id FROM agent").fetchall(),
                [(self.company.founder_id,)],
            )
            self.assertEqual(
                independent.execute(
                    "SELECT id,agent_id FROM standing_grant"
                ).fetchall(),
                [(self.company.grant_id, self.company.founder_id)],
            )
        with hold_company_writer(self.target, company_id):
            claim = claim_first_duty(self.target, company_id, "profile")
        self.assertEqual(
            (claim.duty_id, claim.founder_id),
            (self.company.due_work_id, self.company.founder_id),
        )
        self.assertEqual(self.restore(), receipt)
        self.assertEqual(
            reopen(self.source, company_id).due_work_ids, (self.company.due_work_id,)
        )

    def test_changed_original_and_paper_scope_are_not_retired(self):
        company_id = self.company.company_id
        checkpoint_company(self.source, company_id, self.archive)
        claim_first_duty(self.source, company_id, "profile")
        with self.assertRaisesRegex(CheckpointRefused, "checkpoint cut"):
            self.restore()
        self.assertFalse(self.target.exists())
        venue = FixedQuoteVenue(self.source.with_name("paper.sqlite"))
        account = venue.provision(
            PaperAccount("paper", "local", "rules", "USD", 1000, "paper"), 20
        )
        with hold_company_writer(self.source, company_id):
            bind(
                self.source,
                PaperMandate("paper", company_id, "paper", account, 200),
                venue,
            )
        with self.assertRaisesRegex(
            CheckpointRefused, "independent restore reconciler"
        ):
            self.restore()
        self.assertFalse(self.target.exists())
        with hold_company_writer(self.source, company_id):
            self.assertEqual(reopen(self.source, company_id).company_id, company_id)

    def test_exact_retry_releases_only_its_publication_alias(self):
        company_id = self.company.company_id
        checkpoint_company(self.source, company_id, self.archive)
        cut = subprocess.run(
            [
                sys.executable,
                "-IB",
                "-c",
                """
import os, sys
from pathlib import Path
from sourdough import company_restore

original = company_restore._publish
def interrupted(candidate, target):
    original(candidate, target)
    os._exit(74)
company_restore._publish = interrupted
company_restore.restore_core_checkpoint(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
""",
                str(self.source),
                company_id,
                str(self.archive),
                str(self.target),
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(cut.returncode, 74, cut.stderr)
        self.assertEqual(self.target.stat().st_nlink, 2)
        with self.assertRaisesRegex(FoundingRefused, "retired"):
            with hold_company_writer(self.source, company_id):
                self.fail("Retired source could write after publication")
        unrelated = self.target.with_name("unrecognized-alias.sqlite")
        unrelated.hardlink_to(self.target)
        with self.assertRaisesRegex(CheckpointRefused, "unexpected file alias"):
            self.restore()
        self.assertTrue(unrelated.exists())
        unrelated.unlink()
        receipt = self.restore()
        self.assertEqual(self.restore(), receipt)
        self.assertEqual(self.target.stat().st_nlink, 1)
        with hold_company_writer(self.target, company_id):
            self.assertEqual(
                reopen(self.target, company_id).due_work_ids,
                (self.company.due_work_id,),
            )
