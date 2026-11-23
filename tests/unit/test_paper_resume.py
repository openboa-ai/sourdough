import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from unittest.mock import patch

from sourdough.company import FoundingTerms, found
from sourdough.first_duty import claim_first_duty
from sourdough.market_runtime import SpotInstrumentTerms
from sourdough.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.nautilus_runtime import VERSION, paper_venue
from sourdough.paper_account import PaperMandate, bind
from sourdough.paper_execution import PaperIntent, VenueRefused
from sourdough.paper_order import OrderRefused, OrderReplyUnknown, submit_paper_order
from sourdough.paper_resume import resume_managed_paper_risk
from sourdough.paper_risk import (
    RESUME_NEW_RISK,
    PaperRiskRefused,
    PaperSuspension,
    effective_paper_risk,
    read_paper_risk_receipt,
    suspend_paper_risk,
)
from sourdough.writer_lease import hold_company_writer


class ResumeManagedPaper(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.engine = self.root / "engine"
        self.company_id = found(
            self.store, FoundingTerms("setup", "charter", "paper", "runtime")
        ).company_id
        self.terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        self.plan = SandboxPlan(
            self.company_id, "paper", self.terms, 100_000_000_000, 0
        )
        self.venue = self.prepare_venue(self.engine, self.plan)
        account = self.venue.observe_account("paper")
        bind(
            self.store,
            PaperMandate(
                "mandate", self.company_id, "charter", account, 10_000_000_000
            ),
            self.venue,
        )
        self.claim = claim_first_duty(self.store, self.company_id, "runtime")
        self.intent = PaperIntent("action", self.terms.revision, 20, 59_000_000)
        self.prepared_resume = PaperSuspension(
            "resume", "charter", 1, "Reviewed position"
        )
        self.allowed = True

    def prepare_venue(self, directory, plan):
        (directory / f"nautilus-{VERSION}").mkdir(parents=True)
        venue = paper_venue(self.store, directory)
        venue.provision(plan)
        balance = venue.start_generation()
        venue.prove_account("current", "BINANCE-001", "USDT", balance)
        stamp = time_ns()
        venue.publish_quote("current", 59_000_000, stamp, stamp, event_ns=stamp)
        return venue

    def authorize(self, database, company_id, operation):
        self.assertEqual(company_id, self.company_id)
        self.assertTrue(database.in_transaction)
        self.assertIn(operation, ("suspend_new_risk", RESUME_NEW_RISK))
        return "scoped-board" if self.allowed else None

    def writer(self):
        return hold_company_writer(
            self.store, self.company_id, lease_directory=self.root / "leases"
        )

    def suspend(self, writer, name="suspend", revision=0):
        return suspend_paper_risk(
            writer, PaperSuspension(name, "charter", revision, "Hold"), self.authorize
        )

    def resume(self, writer, command=None, engine=None):
        return resume_managed_paper_risk(
            writer,
            command or self.prepared_resume,
            engine or self.engine,
            self.authorize,
        )

    def state(self):
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            return effective_paper_risk(db)

    def queue(self, writer):
        with (
            patch("sourdough.nautilus_paper.monotonic", side_effect=(0, 13)),
            self.assertRaises(OrderReplyUnknown) as awaiting,
        ):
            submit_paper_order(
                self.store, self.claim, self.intent, self.venue, writer=writer
            )
        return awaiting.exception.order_id

    def test_fresh_resume_allows_one_real_adapter_admission_and_stale_retry_never_resumes(
        self,
    ):
        with self.writer() as writer:
            self.suspend(writer)
            with self.assertRaisesRegex(OrderRefused, "risk is suspended"):
                submit_paper_order(
                    self.store, self.claim, self.intent, self.venue, writer=writer
                )
            resume = self.resume(writer)
            self.assertEqual(
                (resume.disposition, resume.effective_revision), ("applied", 2)
            )
            self.assertFalse(self.state().suspended)
            self.assertEqual(
                read_paper_risk_receipt(
                    writer,
                    self.prepared_resume.request_id,
                    self.authorize,
                    operation=RESUME_NEW_RISK,
                ),
                resume,
            )
            order_id = self.queue(writer)
            original = self.venue.claim_command("current")
            self.assertEqual(original.order_id, order_id)
            self.suspend(writer, "again", 2)
            self.venue.invalidate_quote()
            self.assertEqual(self.resume(writer), resume)
            stale = self.resume(
                writer, replace(self.prepared_resume, request_id="stale")
            )
            self.assertEqual(
                (stale.disposition, stale.effective_revision),
                ("conflicting_revision", 3),
            )
            self.assertTrue(self.state().suspended)

    def test_refuses_expired_binding_mismatch_and_changed_local_lineage(self):
        other = self.root / "other"
        self.prepare_venue(other, replace(self.plan, starting_atoms=90_000_000_000))
        with self.writer() as writer:
            self.suspend(writer)
            with self.assertRaisesRegex(PaperRiskRefused, "original venue cut"):
                self.resume(writer, engine=other)
            cut = self.venue.read_current_risk_cut("paper")
            with (
                patch(
                    "sourdough.paper_resume.time_ns", return_value=cut.expires_ns + 1
                ),
                self.assertRaisesRegex(PaperRiskRefused, "expired before commit"),
            ):
                self.resume(writer)
            original_read = NautilusPaperVenue.read_current_risk_cut

            def change_binding(venue, account_id):
                observed = original_read(venue, account_id)
                with closing(sqlite3.connect(self.store)) as db, db:
                    db.execute("UPDATE paper_binding SET exposure_limit_minor_units=1")
                return observed

            with (
                patch.object(
                    NautilusPaperVenue, "read_current_risk_cut", change_binding
                ),
                self.assertRaisesRegex(PaperRiskRefused, "binding changed"),
            ):
                self.resume(writer)
            self.venue.invalidate_quote()
            with self.assertRaisesRegex(VenueRefused, "no fresh quote"):
                self.resume(writer)
            self.assertTrue(self.state().suspended)
            self.assertEqual(self.state().revision, 1)

    def test_unresolved_original_stays_reserved_and_revoked_authority_does_not_observe(
        self,
    ):
        with self.writer() as writer:
            self.queue(writer)
            self.suspend(writer)
            with self.assertRaisesRegex(PaperRiskRefused, "unresolved original"):
                self.resume(writer)
            self.allowed = False
            with (
                patch.object(
                    NautilusPaperVenue,
                    "read_current_risk_cut",
                    side_effect=AssertionError,
                ),
                self.assertRaisesRegex(PaperRiskRefused, "current board|Current board"),
            ):
                self.resume(writer)
        with closing(sqlite3.connect(self.store.as_uri() + "?mode=ro", uri=True)) as db:
            self.assertEqual(
                db.execute("SELECT reserved_minor_units FROM paper_order").fetchall(),
                [(20 * 59_000_000,)],
            )
        self.assertTrue(self.state().suspended)
