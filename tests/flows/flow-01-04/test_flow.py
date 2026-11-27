"""Connected Flow 01-04: Company founding -> Company Paper Book -> unified trading_book_* order/fill."""

import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.trading.accounts import open_company_paper_book
from sourdough.trading.ledger import submit_company_paper_book_order
from sourdough.writer_lease import hold_company_writer


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        store = folder / "company.sqlite"
        leases = folder / "leases"
        peer = folder / "scripted-peer"
        peer.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "if request['operation'] == 'identify':\n"
            "    print(json.dumps({'provider':'test','model':'scripted','revision':'1'}))\n"
            "else:\n"
            "    print(json.dumps({'ready': True}))\n",
            encoding="utf-8",
        )
        peer.chmod(0o700)

        def cli(*args):
            return json.loads(
                subprocess.run(
                    [sys.executable, "-I", "-m", "sourdough.cli", *map(str, args)],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )

        profile = cli("employee-profile", peer)["execution_profile"]
        company = cli(
            "found",
            store,
            "--setup-identity",
            "setup",
            "--charter-revision",
            "charter",
            "--founder-scope",
            "paper",
            "--execution-profile",
            profile,
        )
        company_id = company["company_id"]
        founder_id = company["founder_id"]

        with closing(sqlite3.connect(f"{store.as_uri()}?mode=ro", uri=True)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT domain FROM installed_domains ORDER BY domain"
                ).fetchall(),
                [("core",)],
            )

        with hold_company_writer(store, company_id, lease_directory=leases) as writer:
            binding = open_company_paper_book(
                writer,
                account_id="account",
                book_name="First Paper Book",
                currency="USD",
                starting_minor_units=5000,
                exposure_limit_minor_units=1000,
                fee_bps=0,
                slippage_bps=0,
                binding_revision="mandate",
            )
            result = submit_company_paper_book_order(
                writer,
                binding_id=binding.binding_id,
                actor_id=founder_id,
                action_id="test-call",
                instrument_id="BTC-USD",
                side="buy",
                quantity=2,
                limit_minor_units=110,
                market_price_minor_units=100,
            )

        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["filled_quantity"], 2)
        order_id = result["order_id"]
        fill_id = result["fill_id"]

        with closing(sqlite3.connect(f"{store.as_uri()}?mode=ro", uri=True)) as book:
            self.assertEqual(
                book.execute(
                    "SELECT domain FROM installed_domains ORDER BY domain"
                ).fetchall(),
                [("core",), ("trading",)],
            )
            self.assertEqual(
                book.execute(
                    "SELECT binding_id, tier, environment, account_id FROM execution_environment_binding WHERE binding_id = ?",
                    (binding.binding_id,),
                ).fetchall(),
                [(binding.binding_id, "company_paper", "paper", "account")],
            )
            self.assertEqual(
                book.execute(
                    "SELECT order_id, binding_id, environment, actor_id, action_id, quantity, status FROM trading_book_order"
                ).fetchall(),
                [
                    (
                        order_id,
                        binding.binding_id,
                        "paper",
                        founder_id,
                        "test-call",
                        2,
                        "filled",
                    )
                ],
            )
            self.assertEqual(
                book.execute(
                    "SELECT order_id, fill_id, environment, quantity, price_minor_units FROM trading_book_fill"
                ).fetchall(),
                [(order_id, fill_id, "paper", 2, 100)],
            )
            self.assertEqual(
                book.execute(
                    "SELECT binding_id, environment, instrument_id, quantity FROM trading_book_position"
                ).fetchall(),
                [(binding.binding_id, "paper", "BTC-USD", 2)],
            )
            self.assertEqual(book.execute("PRAGMA foreign_key_check").fetchall(), [])

        timeline = cli("inspect-work", store, "--company-id", company_id)
        paper_view = timeline["domains"]["trading"]["paper"]
        self.assertEqual(paper_view["booked_position"], 2)
        self.assertEqual(len(paper_view["orders"]), 1)
        self.assertEqual(paper_view["orders"][0]["order_id"], order_id)
        self.assertEqual(paper_view["orders"][0]["fill_id"], fill_id)
