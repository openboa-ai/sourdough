import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from sourdough.abandoned_work import requeue_uncommitted_work
from sourdough.company import FoundingTerms, found
from sourdough.first_duty import claim_first_duty
from sourdough.storage.checkpoint import compute_logical_watermark
from sourdough.storage.db import open_reader, read_tx
from sourdough.writer_lease import hold_company_writer


class CompanyCheckpoint(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.company = found(
            self.store, FoundingTerms("test", "paper", "paper", "profile")
        )
        self.archive = self.root / "checkpoint.sd"

    def invoke(self, action, *path, company_id=None):
        return subprocess.run(
            [
                sys.executable,
                "-IB",
                "-m",
                "sourdough.cli",
                action,
                *map(str, path),
                "--company-id",
                company_id or self.company.company_id,
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )

    def test_capture_refuses_an_active_writer_then_verifies_original_duty(self):
        claim = claim_first_duty(self.store, self.company.company_id, "profile")
        with hold_company_writer(self.store, self.company.company_id):
            active = self.invoke("checkpoint-company", self.store, self.archive)
        self.assertEqual(active.returncode, 1)
        self.assertIn("already has a writer", active.stderr)
        self.assertFalse(self.archive.exists())

        with closing(open_reader(self.store)) as reader, read_tx(reader):
            before_watermark = compute_logical_watermark(
                reader, self.company.company_id
            )
            before_cuts = reader.execute(
                "SELECT COUNT(*) FROM audit_event WHERE event_type = 'company.checkpoint_cut'"
            ).fetchone()[0]
        created = self.invoke("checkpoint-company", self.store, self.archive)
        self.assertEqual(created.returncode, 0, created.stderr)
        manifest = json.loads(created.stdout)
        self.assertEqual(manifest["company_id"], self.company.company_id)
        with closing(open_reader(self.store)) as reader, read_tx(reader):
            after_watermark = compute_logical_watermark(reader, self.company.company_id)
            after_cuts = reader.execute(
                "SELECT COUNT(*) FROM audit_event WHERE event_type = 'company.checkpoint_cut'"
            ).fetchone()[0]
        self.assertEqual(after_cuts, before_cuts + 1)
        self.assertEqual(
            after_watermark.max_event_seq, before_watermark.max_event_seq + 1
        )
        self.assertEqual(
            manifest["watermark"]["domain_digest"], after_watermark.domain_digest
        )
        snapshot = self.archive.read_bytes()

        with hold_company_writer(self.store, self.company.company_id) as writer:
            self.assertEqual(requeue_uncommitted_work(writer), (claim.run_id,))
        self.assertEqual(self.archive.read_bytes(), snapshot)
        verified = self.invoke("inspect-company-checkpoint", self.archive)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout), manifest)

        with ZipFile(self.archive) as checkpoint:
            contents = checkpoint.read("company.sqlite")
        with closing(sqlite3.connect(":memory:")) as independent:
            independent.deserialize(contents)
            self.assertEqual(
                independent.execute("SELECT id FROM company").fetchall(),
                [(self.company.company_id,)],
            )
            self.assertEqual(
                independent.execute(
                    "SELECT is_current FROM duty_run WHERE run_id=?", (claim.run_id,)
                ).fetchone(),
                (1,),
            )
            self.assertEqual(
                independent.execute(
                    "SELECT state FROM duty WHERE id=?", (claim.duty_id,)
                ).fetchone(),
                ("running",),
            )

        overwrite = self.invoke("checkpoint-company", self.store, self.archive)
        self.assertEqual(overwrite.returncode, 1)
        self.assertIn("destination already exists", overwrite.stderr)
        self.assertEqual(self.archive.read_bytes(), snapshot)

    def test_rejects_wrong_company_and_corrupted_original_without_promoting(self):
        created = self.invoke("checkpoint-company", self.store, self.archive)
        self.assertEqual(created.returncode, 0, created.stderr)
        wrong_scope = self.invoke(
            "inspect-company-checkpoint", self.archive, company_id="other-company"
        )
        self.assertEqual(wrong_scope.returncode, 1)
        self.assertIn("another company", wrong_scope.stderr)
        corrupted = self.root / "corrupted.sd"
        with ZipFile(self.archive) as source, ZipFile(corrupted, "w") as target:
            contents = source.read("company.sqlite")
            target.writestr("manifest.json", source.read("manifest.json"))
            target.writestr("company.sqlite", contents[:-1] + bytes([contents[-1] ^ 1]))
        invalid = self.invoke("inspect-company-checkpoint", corrupted)
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("sha-256", invalid.stderr.lower())
        self.assertEqual(self.invoke("reopen", self.archive).returncode, 1)

    def test_copy_contains_committed_work_even_when_source_wal_is_still_open(self):
        with closing(sqlite3.connect(self.store)) as original:
            self.assertEqual(
                original.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",)
            )
            original.execute("BEGIN")
            original.execute("SELECT id FROM company").fetchone()
            claim = claim_first_duty(self.store, self.company.company_id, "profile")
            source_wal = self.store.with_name(self.store.name + "-wal")
            journal_bytes = source_wal.read_bytes()
            self.assertGreater(len(journal_bytes), 0)
            result = self.invoke("checkpoint-company", self.store, self.archive)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreaterEqual(len(source_wal.read_bytes()), len(journal_bytes))
        restored = self.root / "oracle.sqlite"
        with ZipFile(self.archive) as checkpoint:
            restored.write_bytes(checkpoint.read("company.sqlite"))
        with closing(
            sqlite3.connect(restored.as_uri() + "?mode=ro", uri=True)
        ) as independent:
            self.assertEqual(
                independent.execute("PRAGMA journal_mode").fetchone(), ("delete",)
            )
            self.assertEqual(
                independent.execute("SELECT run_id FROM duty_run").fetchall(),
                [(claim.run_id,)],
            )
