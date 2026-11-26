import os
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough.company import FoundingTerms, found
from sourdough.trading.market_runtime import observe_market
from sourdough.trading.market_worker import (
    MarketRefused,
    MarketScope,
    MarketTrade,
    SpotInstrumentTerms,
    append_trade,
    bind_spot_terms,
    journal_key,
    register,
    reopen,
    spot_trade_time_ns,
)
from sourdough.supervisor import ChildAttempt, SupervisionResult
from sourdough.trading.engine import EngineInstallation


class MarketObservation(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.store = self.root / "company.sqlite"
        self.company = found(
            self.store, FoundingTerms("setup", "charter", "paper", "profile")
        )
        self.scope = MarketScope(self.company.company_id, "1" * 64, "BTCUSDT", 15)
        self.terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        self.trade = MarketTrade("123", "30000.25", "0.1000", 10**10, 11 * 10**9)

    def test_exchange_milliseconds_survive_sdk_float_conversion(self):
        exact = 1_795_331_659_186_000_000
        self.assertEqual(spot_trade_time_ns(exact - 128), exact)
        self.assertEqual(spot_trade_time_ns(exact + 128), exact)
        with self.assertRaisesRegex(MarketRefused, "spot trade millisecond precision"):
            spot_trade_time_ns(exact + 5_000)

    def test_market_tape_pins_scope_and_preserves_earliest_receipt(self):
        journal = self.root / "market.sqlite"
        register(journal, self.scope)
        register(journal, self.scope)
        with self.assertRaisesRegex(MarketRefused, "registered source"):
            register(journal, replace(self.scope, symbol="ETHUSDT"))
        with self.assertRaisesRegex(MarketRefused, "original source"):
            reopen(journal, "another-company")

        key = journal_key(journal)
        self.assertTrue(append_trade(journal, key, self.scope, self.trade, "first", 10))
        duplicate = replace(self.trade, received_ns=12 * 10**9)
        self.assertFalse(
            append_trade(journal, key, self.scope, duplicate, "repeat", 11)
        )
        with self.assertRaisesRegex(MarketRefused, "Conflicting delivery"):
            append_trade(
                journal,
                key,
                self.scope,
                replace(duplicate, price="32000"),
                "repeat",
                11,
            )
        stale = replace(duplicate, trade_id="124", received_ns=30 * 10**9)
        self.assertFalse(append_trade(journal, key, self.scope, stale, "repeat", 11))
        with closing(sqlite3.connect(journal)) as database:
            self.assertEqual(
                database.execute(
                    "SELECT trade_id,received_ns,activation_id FROM market_trade"
                ).fetchall(),
                [("123", self.trade.received_ns, "first")],
            )

    def test_runtime_uses_prepared_scope_and_no_inherited_provider_settings(self):
        home = self.root / "nautilus-1.231.0" / "home"
        home.mkdir(parents=True)
        installation = EngineInstallation(
            self.company.company_id, "1.231.0", "1" * 64, "record", "content"
        )

        def drive(command, journal, company_id, **options):
            self.assertEqual(command[4], self.scope.company_id)
            self.assertEqual(company_id, self.scope.company_id)
            self.assertEqual(reopen(journal, company_id), self.scope)
            self.assertEqual(options["expected_store_key"], journal_key(journal))
            self.assertEqual(options["environment"]["HOME"], str(home))
            self.assertEqual(
                set(options["environment"]), {"HOME", "PATH", "PIP_CONFIG_FILE"}
            )
            key = journal_key(journal)
            bind_spot_terms(journal, key, self.scope, self.terms)
            with self.assertRaisesRegex(MarketRefused, "changed from the registered"):
                bind_spot_terms(
                    journal,
                    key,
                    self.scope,
                    replace(self.terms, price_increment="0.02"),
                )
            append_trade(
                journal, journal_key(journal), self.scope, self.trade, command[5], 10
            )
            return SupervisionResult((ChildAttempt(10, 10, 11, 0),), True)

        with (
            patch(
                "sourdough.trading.market_runtime.prepare_engine",
                return_value=installation,
            ) as prepare,
            patch(
                "sourdough.trading.market_runtime.supervise_worker", side_effect=drive
            ),
            patch.dict(os.environ, {"BINANCE_API_KEY": "fixture-only"}),
        ):
            result = observe_market(
                self.store,
                self.scope.company_id,
                self.root,
                symbol="BTCUSDT",
                max_age_seconds=15,
                samples=1,
                emit=lambda _: None,
            )
        self.assertFalse(prepare.call_args.kwargs["allow_install"])
        self.assertTrue(result.completed)
        self.assertEqual(result.instrument, self.terms)
        self.assertEqual(result.instrument_revision, self.terms.revision)
        self.assertEqual(result.observations, (self.trade,))
