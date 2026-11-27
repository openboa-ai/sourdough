import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from dataclasses import asdict
from datetime import date
from fractions import Fraction
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch

from sourdough.accounting_close import CloseRequest, ExpectedCost, close_period
from sourdough.cli import main
from sourdough.company import FoundingTerms, found
from sourdough.general_ledger import JournalEntry, JournalLine, post_journal
from sourdough.trading.accounts import register_company_paper_tier
from sourdough.trading.duty import claim_paper_first_duty as claim_first_duty
from sourdough.trading.engine import VERSION
from sourdough.trading.execution import PaperIntent
from sourdough.trading.market_worker import SpotInstrumentTerms
from sourdough.trading.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.trading.nautilus_runtime import VALUATION_SOURCE_REVISION
from sourdough.trading.valuation import (
    BEST_BID_POLICY,
    ValuationRefused,
    capture_window_sample,
)
from sourdough.trading.window import WindowPlan
from sourdough.trading.window_costs import CostInventory
from sourdough.writer_lease import hold_company_writer


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        store, engine = root / "company.sqlite", root / "engine"
        runtime = engine / f"nautilus-{VERSION}"
        runtime.mkdir(parents=True)
        venue = NautilusPaperVenue(runtime / "sandbox.sqlite")
        company_id = found(
            store, FoundingTerms("test", "charter", "paper", "profile")
        ).company_id
        terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        capital, exposure, scale = 100_000_000_000, 10_000_000_000, 100_000_000
        venue.provision(SandboxPlan(company_id, "paper", terms, capital, 0))
        generation = "controlled-venue"
        venue.prove_account(generation, "BINANCE-001", "USDT", venue.start_generation())
        account = venue.observe_account("paper")
        with hold_company_writer(store, company_id) as writer:
            register_company_paper_tier(
                writer,
                account_id="paper",
                destination_id=account.destination_id,
                connector_revision=account.connector_revision,
                instrument_rules_digest=account.instrument_rules_digest,
                currency=account.currency,
                starting_minor_units=account.starting_minor_units,
                exposure_limit_minor_units=exposure,
                fee_bps=0,
                slippage_bps=0,
                binding_revision="bound",
            )
        day = date(2027, 1, 15)
        day_ns = 86_400 * 10**9
        opens = (day - date(1970, 1, 1)).days * day_ns
        closes = opens + day_ns - 1
        window_id = "controlled-window"
        expense = 3
        cost = ExpectedCost(
            "compute", "invoice-1", "expense", "accounting", "bill", expense
        )
        accounting = CloseRequest(
            "cost-close",
            company_id,
            day,
            day,
            "cash",
            "USDT",
            "chart",
            "cash-proof",
            100,
            100 - expense,
            ("expense",),
            (cost,),
            "accounting",
            closes + 10**9,
            units_per_whole=scale,
        )
        plan = WindowPlan(
            window_id,
            "bound",
            "incumbent",
            "test-mandate",
            VALUATION_SOURCE_REVISION,
            BEST_BID_POLICY,
            CostInventory.for_close(window_id, accounting).revision,
            "test-execution",
            "test-cash-reference",
            "test-contingency",
            opens,
            closes,
            day_ns // 2,
            2,
            exposure,
            expense,
            100,
            1,
        )
        plan_file = root / "plan.json"
        plan_file.write_text(json.dumps(asdict(plan)), encoding="utf-8")

        def command(name, *options, now=closes + 3):
            output = StringIO()
            argv = [
                "sourdough",
                name,
                str(store),
                str(engine),
                "--company-id",
                company_id,
                *options,
            ]
            with (
                patch("sys.argv", argv),
                patch("sourdough.trading.statement_store.time_ns", return_value=now),
                patch("sourdough.trading.window_statement.time_ns", return_value=now),
                redirect_stdout(output),
            ):
                self.assertEqual(main(), 0)
            return json.loads(output.getvalue())

        with patch("sourdough.trading.window.time_ns", return_value=opens - 100):
            registered = command("seal-paper-window", "--plan", str(plan_file))
        self.assertLess(registered["registered_ns"], opens)

        def sample(slot, stamp, bid):
            venue.publish_quote(generation, ask, stamp, stamp, bid_atoms_per_lot=bid)
            with (
                patch(
                    "sourdough.trading.nautilus_paper.time_ns", return_value=stamp + 1
                ),
                hold_company_writer(store, company_id) as writer,
            ):
                capture_window_sample(writer, engine, window_id, slot)

        ask, quantity, fee = 60_000_000, 20, 4
        sample(0, opens + 10, ask - 10)
        claim_first_duty(store, company_id, "profile")
        fill_time = opens + 13
        with (
            patch("sourdough.trading.nautilus_paper.time_ns", return_value=fill_time),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            pending = pool.submit(
                venue.submit,
                "order-f08-1",
                "paper",
                PaperIntent("test-buy", terms.revision, quantity, ask),
            )
            local_command = None
            deadline = monotonic() + 4
            while local_command is None and monotonic() < deadline:
                local_command = venue.claim_command(generation)
                if local_command is None:
                    sleep(0.01)
            self.assertIsNotNone(local_command)
            venue.record_fill(
                generation,
                local_command.order_id,
                "original-fill",
                quantity,
                ask,
                fee,
                effective_ns=fill_time,
            )
            pending.result(timeout=2)
        self.assertTrue(
            venue.record_account(
                generation,
                local_command.order_id,
                quantity,
                capital - quantity * ask - fee,
            )
        )
        final_bid = ask - 30
        with self.assertRaisesRegex(ValuationRefused, "registered closing instant"):
            sample(1, opens + day_ns // 2 + 10, final_bid - 100)
        sample(1, closes - 4, final_bid)
        venue.invalidate_quote()

        with closing(
            sqlite3.connect(f"{venue.journal.as_uri()}?mode=ro", uri=True)
        ) as db:
            [(actual_quantity, actual_price, actual_fee, actual_time)] = db.execute(
                """SELECT quantity, price_atoms, fee_atoms, effective_ns
                   FROM sandbox_fill_observation JOIN sandbox_fill_time USING(fill_id)"""
            ).fetchall()
        self.assertEqual(
            (actual_quantity, actual_price, actual_fee, actual_time),
            (quantity, ask, fee, fill_time),
        )
        trading_pnl = actual_quantity * (final_bid - actual_price) - actual_fee
        self.assertLess(trading_pnl, 0)

        window = ("--window-id", window_id)
        initial = ("--request-id", "initial")
        corrected = (
            "--request-id",
            "corrected",
            "--accounting-close",
            accounting.request_id,
            "--supersedes",
            "initial",
        )
        first = command("close-paper-window", *window, *initial, now=closes + 1)
        self.assertEqual(first["statement"]["trading"]["pnl_minor"], trading_pnl)
        self.assertIsNone(first["statement"]["company"]["pnl_minor"])
        self.assertIn("accounting-close", first["statement"]["company"]["missing"])
        with (
            patch("sourdough.accounting_close.time_ns", return_value=closes + 2),
            hold_company_writer(store, company_id) as writer,
        ):
            post_journal(
                writer,
                JournalEntry(
                    company_id,
                    "compute",
                    "invoice-1",
                    "bill",
                    day,
                    "USDT",
                    "chart",
                    (JournalLine("expense", expense), JournalLine("cash", -expense)),
                    units_per_whole=scale,
                ),
            )
            close_period(writer, accounting)
        engine.rename(root / "detached")
        second = command("close-paper-window", *window, *corrected)
        self.assertEqual(second["statement"]["observed_slots"], [0, 1])
        self.assertEqual(second["statement"]["operating_cost_atoms"], expense)
        self.assertEqual(second["statement"]["trading"], first["statement"]["trading"])
        self.assertEqual(second["statement"]["venue_fees_atoms"], actual_fee)
        company = second["statement"]["company"]
        self.assertEqual(company["pnl_minor"], trading_pnl - expense)
        self.assertEqual(
            Fraction(**company["time_weighted_return"]),
            Fraction(trading_pnl - expense, capital),
        )
        self.assertNotEqual(
            first["statement"]["revision"], second["statement"]["revision"]
        )

        self.assertEqual(
            command("close-paper-window", *window, *corrected, now=closes + 4), second
        )
        self.assertEqual(
            command("inspect-paper-window", *window, "--report-id", "initial"), first
        )
        self.assertEqual(
            command("inspect-paper-window", *window, "--report-id", "corrected"), second
        )
