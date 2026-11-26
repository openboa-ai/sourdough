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
from sourdough.trading.accounts import register_company_paper_tier
from sourdough.trading.execution import PaperAccount
from sourdough.trading.window import (
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
        self.company = found(
            self.store, FoundingTerms("company", "charter", "paper", "profile")
        ).company_id
        with hold_company_writer(self.store, self.company) as writer:
            binding = register_company_paper_tier(
                writer,
                account_id="paper",
                destination_id="fixture-dest",
                connector_revision="fixture",
                instrument_rules_digest="instrument",
                currency="USD",
                starting_minor_units=1000,
                exposure_limit_minor_units=500,
                fee_bps=0,
                slippage_bps=0,
                binding_revision="binding",
            )
        self.account = PaperAccount(
            binding.account_id,
            binding.connector_revision,
            binding.instrument_rules_digest,
            binding.currency,
            binding.starting_minor_units,
            binding.environment,
            binding.destination_id,
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
            patch("sourdough.trading.window.time_ns", return_value=now),
            hold_company_writer(self.store, self.company) as writer,
        ):
            return register_window(writer, plan)

    def test_same_binding_and_terms_reopen_as_the_exact_pre_window_receipt(self):
        receipt = self.register(self.plan, now=90)
        self.assertEqual(receipt.account, self.account)
        self.assertEqual(receipt.registered_ns, 90)
        self.assertEqual(self.register(self.plan, now=210), receipt)
        self.assertEqual(read_window(self.store, self.company, "first"), receipt)
        self.assertEqual(
            [receipt.admits_availability(stamp) for stamp in (90, 99, 100, 200, 201)],
            [False, False, True, True, False],
        )
        with closing(sqlite3.connect(self.store)) as database:
            [(raw, committed_ns, revision)] = database.execute(
                "SELECT contract,committed_ns,revision FROM trading_operating_window"
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
            patch("sourdough.trading.window.time_ns", side_effect=[99, 100]),
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
            patch(
                "sourdough.trading.window.open_writer", side_effect=traced_open_writer
            ),
            patch("sourdough.trading.window.time_ns", side_effect=clock),
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
                    "SELECT committed_ns,revision FROM trading_operating_window"
                ).fetchone(),
                (None, None),
            )
