import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        store, venue = folder / "company.sqlite", folder / "venue.sqlite"
        peer = folder / "scripted-peer"
        peer.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "if request['operation'] == 'ready':\n"
            "    print(json.dumps({'ready': True}))\n"
            "else:\n"
            "    assert request['context']['quote_minor_units'] == 100\n"
            "    print(json.dumps({'tool': 'paper_buy', 'call_id': 'test-call',"
            " 'quantity': 2, 'limit_minor_units': 110}))\n"
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
        identity = ("--company-id", company["company_id"])
        cli(
            "bind-paper",
            store,
            venue,
            *identity,
            "--charter-revision",
            "charter",
            "--revision",
            "mandate",
            "--account-id",
            "account",
            "--rules",
            "rules",
            "--starting-minor-units",
            5000,
            "--exposure-minor-units",
            1000,
            "--quote-minor-units",
            100,
        )
        result = cli("first-duty", store, venue, *identity, "--employee", peer)
        self.assertEqual(result["position_quantity"], 2)
        self.assertEqual(result["claim"]["founder_id"], company["founder_id"])
        self.assertEqual(result["claim"]["duty_id"], company["due_work_id"])
        with (
            closing(sqlite3.connect(f"{store.as_uri()}?mode=ro", uri=True)) as book,
            closing(sqlite3.connect(f"{venue.as_uri()}?mode=ro", uri=True)) as edge,
        ):
            order = book.execute(
                "SELECT order_id, run_id, action_id, account_id, quantity FROM paper_order"
            ).fetchall()
            order_id = result["order"]["order_id"]
            self.assertEqual(
                order,
                [(order_id, result["claim"]["run_id"], "test-call", "account", 2)],
            )
            observed = edge.execute(
                "SELECT order_id, fill_id, quantity, fill_price_minor_units FROM venue_execution"
            ).fetchall()
            self.assertEqual(
                observed, [(order_id, result["order"]["posting"]["fill_id"], 2, 100)]
            )
            self.assertEqual(
                book.execute(
                    "SELECT order_id, fill_id, quantity, price_minor_units FROM paper_fill"
                ).fetchall(),
                observed,
            )
