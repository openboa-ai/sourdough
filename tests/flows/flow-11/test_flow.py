import json
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time_ns

from sourdough.company import FoundingRefused, FoundingTerms, found
from sourdough.company_checkpoint import checkpoint_company
from sourdough.market_worker import SpotInstrumentTerms
from sourdough.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.paper_account import PaperMandate, bind
from sourdough.paper_execution import PaperIntent, VenueRefused
from sourdough.paper_order import OrderRefused, submit_paper_order
from sourdough.paper_restore import restore_managed_paper_checkpoint
from sourdough.paper_risk import SUSPEND_NEW_RISK, PaperSuspension, suspend_paper_risk
from sourdough.paper_sender_lease import hold_sender_lease
from sourdough.trading.duty import claim_paper_first_duty as claim_first_duty
from sourdough.trading_engine import VERSION
from sourdough.writer_lease import hold_company_writer


def inspect_work(store, company_id):
    response = subprocess.run(
        [
            sys.executable,
            "-IB",
            "-m",
            "sourdough.cli",
            "inspect-work",
            str(store),
            "--company-id",
            company_id,
            "--contract-revision",
            "work-timeline-v1",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(response.stdout)


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        source = root / "company.sqlite"
        checkpoint = root / "checkpoint"
        replacement = root / "replacement.sqlite"
        engine = root / "engine"
        company = found(source, FoundingTerms("board", "charter", "paper", "profile"))
        company_id = company.company_id
        runtime = engine / f"nautilus-{VERSION}"
        runtime.mkdir(parents=True)
        venue = NautilusPaperVenue(runtime / "sandbox.sqlite")
        terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        capital, price, quantity = 100_000_000_000, 60_000_000, 20
        venue.provision(SandboxPlan(company_id, "paper", terms, capital, 0))
        generation = "fixture-sender"
        venue.prove_account(generation, "BINANCE-001", "USDT", venue.start_generation())
        bind(
            source,
            PaperMandate(
                "binding", company_id, "charter", venue.observe_account("paper"), 10**10
            ),
            venue,
        )
        stamp = time_ns()
        venue.publish_quote(generation, price, stamp, stamp)
        claim = claim_first_duty(source, company_id, "profile")
        original = PaperIntent("original", terms.revision, quantity, price)

        def authorize_fixture(database, requested_company, operation):
            persisted = database.execute("SELECT id FROM company").fetchone()[0]
            if (
                persisted == requested_company == company_id
                and operation == SUSPEND_NEW_RISK
            ):
                return "fixture-board"
            return None

        with ThreadPoolExecutor(max_workers=1) as pool:
            admission = pool.submit(submit_paper_order, source, claim, original, venue)
            command = None
            deadline = monotonic() + 4
            while command is None and monotonic() < deadline:
                command = venue.claim_command(generation)
                if command is None:
                    sleep(0.01)
            self.assertIsNotNone(command)

            suspension = PaperSuspension(
                "suspend", "charter", 0, "Review open exposure"
            )
            with hold_company_writer(source, company_id) as writer:
                receipt = suspend_paper_risk(writer, suspension, authorize_fixture)
                self.assertEqual(receipt.disposition, "applied")
                with self.assertRaisesRegex(OrderRefused, "risk is suspended"):
                    submit_paper_order(
                        source,
                        claim,
                        PaperIntent("new-risk", terms.revision, quantity, price),
                        venue,
                        writer=writer,
                    )
            checkpoint_company(source, company_id, checkpoint)
            company_cut, venue_cut = source.read_bytes(), venue.journal.read_bytes()
            pending = inspect_work(source, company_id)
            self.assertEqual(
                pending["domains"]["trading"]["risk_control"]["last_applied"][
                    "request_id"
                ],
                "suspend",
            )
            self.assertEqual(
                pending["domains"]["trading"]["paper"]["unresolved_orders"], 1
            )
            self.assertEqual(
                pending["domains"]["trading"]["paper"]["booked_position"], 0
            )
            self.assertEqual(source.read_bytes(), company_cut)
            self.assertEqual(venue.journal.read_bytes(), venue_cut)
            with hold_sender_lease(venue.journal):
                with self.assertRaisesRegex(VenueRefused, "active or pending sender"):
                    restore_managed_paper_checkpoint(
                        source, company_id, checkpoint, replacement, engine
                    )
            self.assertFalse(replacement.exists())
            venue.record_fill(
                generation, command.order_id, "original-fill", quantity, price, 0
            )
            self.assertTrue(
                venue.record_account(
                    generation, command.order_id, quantity, capital - quantity * price
                )
            )
            self.assertEqual(admission.result(timeout=2).order_id, command.order_id)

        venue_cut = venue.journal.read_bytes()
        restore = restore_managed_paper_checkpoint(
            source, company_id, checkpoint, replacement, engine
        )
        self.assertEqual(
            restore_managed_paper_checkpoint(
                source, company_id, checkpoint, replacement, engine
            ),
            restore,
        )
        with self.assertRaisesRegex(FoundingRefused, "retired"):
            with hold_company_writer(source, company_id):
                self.fail("The source regained writer authority")
        restored_cut = replacement.read_bytes()
        settled = inspect_work(replacement, company_id)
        self.assertEqual(
            settled["domains"]["trading"]["risk_control"],
            pending["domains"]["trading"]["risk_control"],
        )
        self.assertEqual(
            settled["domains"]["trading"]["risk_control"]["state"], "suspended"
        )
        self.assertEqual(settled["domains"]["trading"]["paper"]["unresolved_orders"], 0)
        self.assertEqual(
            settled["domains"]["trading"]["paper"]["booked_position"], quantity
        )
        with closing(
            sqlite3.connect(replacement.as_uri() + "?mode=ro", uri=True)
        ) as database:
            self.assertEqual(
                database.execute(
                    "SELECT order_id,fill_id,quantity FROM paper_fill"
                ).fetchall(),
                [(command.order_id, "original-fill", quantity)],
            )
            self.assertEqual(
                database.execute(
                    "SELECT agent_id FROM duty WHERE state='scheduled'"
                ).fetchall(),
                [(company.founder_id,)],
            )
        self.assertEqual(replacement.read_bytes(), restored_cut)
        self.assertEqual(venue.journal.read_bytes(), venue_cut)
