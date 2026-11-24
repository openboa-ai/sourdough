import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.storage.ownership import audit_domain_write_ownership


class StorageDomainOwnership(unittest.TestCase):
    def test_all_product_sql_mutations_respect_domain_table_ownership(self):
        src_root = Path(__file__).resolve().parents[2] / "src" / "sourdough"
        violations = audit_domain_write_ownership(src_root)
        self.assertEqual(violations, [])

    def test_checker_flags_unauthorized_cross_domain_write(self):
        with TemporaryDirectory() as directory:
            fake_root = Path(directory)
            (fake_root / "paper_order.py").write_text(
                "def bad(conn):\n    conn.execute(\"UPDATE company SET charter_revision = 'x'\")\n",
                encoding="utf-8",
            )
            violations = audit_domain_write_ownership(fake_root)
            self.assertEqual(len(violations), 1)
            self.assertIn("paper_order.py", violations[0])
            self.assertIn("company", violations[0])
