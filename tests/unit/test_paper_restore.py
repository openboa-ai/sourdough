import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time_ns

from sourdough.company import FoundingRefused, FoundingTerms, found
from sourdough.company_checkpoint import checkpoint_company
from sourdough.first_duty import claim_first_duty
from sourdough.market_worker import SpotInstrumentTerms
from sourdough.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.paper_account import PaperMandate, bind
from sourdough.paper_execution import PaperIntent, VenueRefused
from sourdough.paper_order import submit_paper_order
from sourdough.paper_restore import restore_managed_paper_checkpoint
from sourdough.paper_sender_lease import hold_sender_lease
from sourdough.trading_engine import VERSION
from sourdough.writer_lease import hold_company_writer


class ManagedPaperRestore(unittest.TestCase):
    def test_post_checkpoint_venue_fill_is_booked_without_another_send(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        store, engine, archive, target = (
            root / name
            for name in ("company.sqlite", "engine", "checkpoint", "replacement.sqlite")
        )
        company = found(store, FoundingTerms("restore", "paper", "paper", "profile"))
        company_id = company.company_id
        runtime = engine / f"nautilus-{VERSION}"
        runtime.mkdir(parents=True)
        venue = NautilusPaperVenue(runtime / "sandbox.sqlite")
        terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        capital, quote, lots = 100_000_000_000, 60_000_000, 20
        venue.provision(SandboxPlan(company_id, "paper", terms, capital, 0))
        generation = "original-sender"
        venue.prove_account(generation, "BINANCE-001", "USDT", venue.start_generation())
        bind(
            store,
            PaperMandate(
                "binding", company_id, "paper", venue.observe_account("paper"), 10**10
            ),
            venue,
        )
        stamp = time_ns()
        venue.publish_quote(generation, quote, stamp, stamp)
        claim = claim_first_duty(store, company_id, "profile")

        with ThreadPoolExecutor(max_workers=1) as pool:
            sent = pool.submit(
                submit_paper_order,
                store,
                claim,
                PaperIntent("original-action", terms.revision, lots, quote),
                venue,
            )
            command = None
            deadline = monotonic() + 4
            while command is None and monotonic() < deadline:
                command = venue.claim_command(generation)
                if command is None:
                    sleep(0.01)
            self.assertIsNotNone(command)
            checkpoint_company(store, company_id, archive)
            with hold_sender_lease(venue.journal):
                with self.assertRaisesRegex(VenueRefused, "active or pending sender"):
                    restore_managed_paper_checkpoint(
                        store, company_id, archive, target, engine
                    )
            with self.assertRaisesRegex(VenueRefused, "unresolved"):
                restore_managed_paper_checkpoint(
                    store, company_id, archive, target, engine
                )
            self.assertFalse(target.exists())
            with hold_company_writer(store, company_id):
                self.assertFalse(target.exists())
            venue.record_fill(
                generation, command.order_id, "original-fill", lots, quote, 0
            )
            self.assertTrue(
                venue.record_account(
                    generation, command.order_id, lots, capital - lots * quote
                )
            )
            sent.result(timeout=2)

        original_venue = venue.journal.read_bytes()
        restored = restore_managed_paper_checkpoint(
            store, company_id, archive, target, engine
        )
        self.assertEqual(
            restore_managed_paper_checkpoint(
                store, company_id, archive, target, engine
            ),
            restored,
        )
        self.assertEqual(venue.journal.read_bytes(), original_venue)
        with self.assertRaisesRegex(FoundingRefused, "retired"):
            with hold_company_writer(store, company_id):
                self.fail("Old company regained its write lease")
        with closing(
            sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
        ) as restored_db:
            self.assertEqual(
                restored_db.execute(
                    "SELECT fill_id,order_id,quantity FROM paper_fill"
                ).fetchall(),
                [("original-fill", command.order_id, lots)],
            )
            self.assertEqual(
                restored_db.execute(
                    "SELECT agent_id FROM duty WHERE state='scheduled'"
                ).fetchall(),
                [(company.founder_id,)],
            )
            self.assertEqual(
                restored_db.execute("SELECT count(*) FROM duty_run").fetchone(), (1,)
            )
