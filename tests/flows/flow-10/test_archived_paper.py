import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import _connect
from sourdough.retirement import (
    Retirement,
    TERMINATION_SCOPE,
    employment_cutoff,
    employment_revision,
    retire_agent,
)
from sourdough.trading.accounts import open_agent_sandbox
from sourdough.trading.domain import ExecutionTierRefused
from sourdough.trading.ledger import submit_agent_sandbox_order
from sourdough.writer_lease import hold_company_writer


def _cli(command, *arguments):
    result = subprocess.run(
        [sys.executable, "-IB", "-m", "sourdough.cli", command, *map(str, arguments)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def _rows(store, sql, parameters=()):
    with closing(sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(sql, parameters).fetchall()


class ArchivedPaper(unittest.TestCase):
    def test_existing_fill_is_retained_without_reopening_the_archived_agent(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        employee, store = (root / name for name in ("employee", "book"))
        employee.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "req = json.load(sys.stdin)\n"
            "print(json.dumps({'provider': 'fixture', 'model': 'm', 'revision': '1'}) "
            "if req['operation'] == 'identify' else {'ready': True})\n"
        )
        employee.chmod(0o700)
        profile = _cli("employee-profile", employee)["execution_profile"]
        company = _cli(
            "found",
            store,
            "--setup-identity",
            "archive",
            "--charter-revision",
            "paper",
            "--founder-scope",
            "manage-company",
            "--execution-profile",
            profile,
        )
        company_id = company["company_id"]
        with hold_company_writer(store, company_id) as writer:
            agent_id = "trader-1"
            with closing(_connect(store, "rw")) as connection, connection:
                connection.execute(
                    "INSERT INTO agent VALUES ('manager',?,?)", (company_id, profile)
                )
                connection.execute(
                    "INSERT INTO standing_grant VALUES ('termination','manager',?)",
                    (TERMINATION_SCOPE,),
                )
                connection.execute(
                    "INSERT INTO agent VALUES (?, ?, ?)",
                    (agent_id, company_id, profile),
                )
                connection.execute(
                    "INSERT INTO standing_grant VALUES ('grant-trader-1', ?, 'paper')",
                    (agent_id,),
                )
            open_agent_sandbox(
                writer,
                agent_id=agent_id,
                account_id="test",
                currency="USD",
                starting_minor_units=50_000,
                exposure_limit_minor_units=25_000,
                fee_bps=4,
                slippage_bps=0,
            )
            order = submit_agent_sandbox_order(
                writer,
                agent_id=agent_id,
                action_id="archive-act-1",
                instrument_id="fixture",
                side="buy",
                quantity=1,
                limit_minor_units=5000,
                market_price_minor_units=5000,
            )
            revision = employment_revision(writer, "manager", "termination", agent_id)
            retire_agent(
                writer,
                "manager",
                "termination",
                Retirement("archive", agent_id, revision, "End employment."),
            )
            with self.assertRaisesRegex(ExecutionTierRefused, "Retired agent"):
                submit_agent_sandbox_order(
                    writer,
                    agent_id=agent_id,
                    action_id="late",
                    instrument_id="fixture",
                    side="buy",
                    quantity=1,
                    limit_minor_units=5000,
                    market_price_minor_units=5000,
                )
        self.assertEqual(
            _rows(store, "SELECT order_id,fill_id FROM trading_book_fill"),
            [(order.order_id, order.fill_id)],
        )
        with closing(_connect(store, "ro")) as connection:
            self.assertIsNotNone(employment_cutoff(connection, agent_id))
        self.assertEqual(_rows(store, "PRAGMA foreign_key_check"), [])
