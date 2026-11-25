import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from sourdough.company import FoundingTerms, found
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.paper_window import (
    WindowPlan,
    WindowRefused,
    read_window,
    register_window,
)
from sourdough.storage.db import open_writer as real_open_writer
from sourdough.writer_lease import hold_company_writer


class ProspectiveWindowTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.store = root / "company.sqlite"
        self.venue = FixedQuoteVenue(root / "venue.sqlite")
        self.company = found(
            self.store, FoundingTerms("company", "charter", "paper", "profile")
        ).company_id
        self.account = self.venue.provision(
            PaperAccount("paper", "fixture", "instrument", "USD", 1000, "paper"), 10
        )
        bind(
            self.store,
            PaperMandate("binding", self.company, "charter", self.account, 500),
            self.venue,
        )
        self.plan = WindowPlan(
            window_id="first",
            binding_revision="binding",
            operating_revision="operating-revision",
            evaluation_mandate_revision="evaluation-mandate",
            source_revision="source-plan",
            valuation_revision="valuation-policy",
            cost_inventory_revision="cost-manifest",
            execution_assumptions_revision="simulated-execution",
            cash_reference_revision="reference",
            contingency_revision="stop-and-correction-rules",
            opens_ns=100,
            closes_ns=200,
            cadence_ns=25,
            minimum_observations=4,
            max_exposure_minor=500,
            max_spend_minor=50,
            max_drawdown_bps=250,
            hurdle_bps=10,
        )

    def register(self, plan, now):
        with (
            patch("sourdough.paper_window.time_ns", return_value=now),
            hold_company_writer(self.store, self.company) as writer,
        ):
            return register_window(writer, plan)

    def test_same_binding_and_terms_reopen_as_the_exact_pre_window_receipt(self):
        original_venue = self.venue.journal.read_bytes()
        receipt = self.register(self.plan, now=90)
        self.assertEqual(receipt.account, self.account)
        self.assertEqual(receipt.registered_ns, 90)
        self.assertEqual(self.register(self.plan, now=210), receipt)
        self.assertEqual(read_window(self.store, self.company, "first"), receipt)
        self.assertEqual(self.venue.journal.read_bytes(), original_venue)
        self.assertEqual(
            [receipt.admits_availability(stamp) for stamp in (90, 99, 100, 200, 201)],
            [False, False, True, True, False],
        )
        with closing(sqlite3.connect(self.store)) as database:
            [(raw, committed_ns, revision)] = database.execute(
                "SELECT contract,committed_ns,revision FROM paper_operating_window"
            ).fetchall()
        self.assertEqual(sha256(f"{committed_ns}:{raw}".encode()).hexdigest(), revision)
        self.assertEqual(json.loads(raw)["account"]["starting_minor_units"], 1000)
        with self.assertRaisesRegex(WindowRefused, "original registration"):
            self.register(
                replace(self.plan, cost_inventory_revision="changed"), now=210
            )

    def test_late_out_of_scope_and_overlapping_windows_are_refused(self):
        with self.assertRaisesRegex(WindowRefused, "before it opens"):
            self.register(self.plan, now=100)
        with self.assertRaisesRegex(WindowRefused, "existing paper mandate"):
            self.register(replace(self.plan, max_exposure_minor=501), now=90)
        self.assertIsNone(read_window(self.store, self.company, "first"))
        self.register(self.plan, now=90)
        overlapping = replace(
            self.plan, window_id="overlap", opens_ns=190, closes_ns=290
        )
        with self.assertRaisesRegex(WindowRefused, "overlapping window"):
            self.register(overlapping, now=95)
        later = replace(overlapping, window_id="later", opens_ns=201)
        self.assertEqual(self.register(later, now=95).plan, later)
        with self.assertRaisesRegex(WindowRefused, "another company"):
            read_window(self.store, "unrelated", "first")

    def test_pending_registration_cannot_cross_opening_at_commit(self):
        with (
            patch("sourdough.paper_window.time_ns", side_effect=[99, 100]),
            hold_company_writer(self.store, self.company) as writer,
        ):
            with self.assertRaisesRegex(
                WindowRefused, "before the registration committed"
            ):
                register_window(writer, self.plan)
        self.assertIsNone(read_window(self.store, self.company, "first"))

    def test_first_commit_crossing_open_cannot_publish_a_seal(self):
        commit_started = Event()
        release_commit = Event()

        def traced_open_writer(*args, **kwargs):
            connection = real_open_writer(*args, **kwargs)

            def on_trace(statement: str) -> None:
                if statement == "COMMIT" and not commit_started.is_set():
                    commit_started.set()
                    release_commit.wait(2)

            connection.set_trace_callback(on_trace)
            return connection

        def clock():
            return self.plan.opens_ns if commit_started.is_set() else 90

        def register():
            with hold_company_writer(self.store, self.company) as writer:
                return register_window(writer, self.plan)

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch("sourdough.paper_window.open_writer", side_effect=traced_open_writer),
            patch("sourdough.paper_window.time_ns", side_effect=clock),
        ):
            attempt = pool.submit(register)
            try:
                self.assertTrue(commit_started.wait(2))
                self.assertFalse(attempt.done())
            finally:
                release_commit.set()
            with self.assertRaisesRegex(
                WindowRefused, "before the registration committed"
            ):
                attempt.result(timeout=2)
        self.assertIsNone(read_window(self.store, self.company, "first"))
        with self.assertRaisesRegex(WindowRefused, "before it opens"):
            self.register(self.plan, now=100)
        with closing(sqlite3.connect(self.store)) as database:
            self.assertEqual(
                database.execute(
                    "SELECT committed_ns,revision FROM paper_operating_window"
                ).fetchone(),
                (None, None),
            )
