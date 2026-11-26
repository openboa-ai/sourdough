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

    def test_checker_flags_inline_ddl_in_company_store_module(self):
        with TemporaryDirectory() as directory:
            fake_root = Path(directory)
            ledger_dir = fake_root / "trading"
            ledger_dir.mkdir(parents=True, exist_ok=True)
            (ledger_dir / "ledger.py").write_text(
                'def bad(conn):\n    conn.execute("CREATE TABLE IF NOT EXISTS trading_book_order (order_id TEXT)")\n',
                encoding="utf-8",
            )
            violations = audit_domain_write_ownership(fake_root)
            self.assertEqual(len(violations), 1)
            self.assertIn("inline company-store DDL", violations[0])
