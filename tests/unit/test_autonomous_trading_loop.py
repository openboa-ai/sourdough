"""Unit tests for autonomous trading tools, market quotes, two-phase live execution, and reconciliation."""

from __future__ import annotations

import json
import threading
import unittest
from contextlib import closing
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.board_control import BOARD_CONTROL_ROLE, bind_board_principal
from sourdough.company import FoundingTerms, found
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.storage.schema import enable_domain_v1
from sourdough.trading.accounts import (
    open_company_paper_book,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
)
from sourdough.trading.binance_emulator import BinanceWireEmulator
from sourdough.trading.binance_venue import (
    MARKET_SPOT,
    BinanceVenueAdapter,
    BinanceVenueConfig,
)
from sourdough.trading.company_tools import CompanyTradingTools
from sourdough.trading.credentials import ApiCredentialPair
from sourdough.trading.domain import (
    ENV_EXTERNAL_TEST,
    ENV_LIVE,
    ENV_PAPER,
    ExecutionTierRefused,
    compute_trading_core_digest,
)
from sourdough.trading.market_worker import (
    MarketScope,
    MarketTrade,
    append_trade,
    journal_key,
    register as register_market_journal,
)
from sourdough.trading.migration import execute_paper_to_live_migration
from sourdough.trading.projections import read_trading_desk_projection
from sourdough.trading.quotes import (
    BinancePublicQuoteProvider,
    CompositeQuoteProvider,
    MarketJournalQuoteProvider,
    MarketQuote,
    QuoteRefused,
    StaticQuoteProvider,
)
from sourdough.trading.reconcile import (
    reconcile_trading_books,
    submit_company_live_book_order,
)
from sourdough.trading.risk import active_trading_risk_restrictions
from sourdough.writer_lease import hold_company_writer


class _StaticCreds:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    def resolve(
        self, handle_ref: str, *, expected_environment: str
    ) -> ApiCredentialPair:
        del expected_environment
        return ApiCredentialPair(
            api_key=self._api_key,
            api_secret=self._api_secret,
            handle_ref=handle_ref,
        )


class _TickerServer:
    def __init__(self, prices: dict[str, str]) -> None:
        self.prices = dict(prices)
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                if "?" not in self.path:
                    self.send_response(400)
                    self.end_headers()
                    return
                query = self.path.split("?", 1)[1]
                symbol = query.split("symbol=", 1)[-1].split("&", 1)[0]
                if symbol not in owner.prices:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(
                    {"symbol": symbol, "price": owner.prices[symbol]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class TestAutonomousTradingLoop(unittest.TestCase):
    def test_quote_providers_journal_binance_public_and_freshness(self) -> None:
        now = time_ns()
        stale_q = MarketQuote("BTC-USDT", 6_500_000, now - 120 * 10**9, "test")
        fresh_q = MarketQuote("BTC-USDT", 6_625_050, now - 2 * 10**9, "test")
        self.assertFalse(stale_q.is_fresh(now_ns=now, max_age_ns=60 * 10**9))
        self.assertTrue(fresh_q.is_fresh(now_ns=now, max_age_ns=60 * 10**9))

        static = StaticQuoteProvider({"BTC-USDT": fresh_q})
        self.assertEqual(static.latest_quote("btcusdt").price_minor_units, 6_625_050)

        with TemporaryDirectory(prefix="sd-quotes-") as tmp:
            journal = Path(tmp) / "market.sqlite"
            scope = MarketScope("co-1", "a" * 64, "ETHUSDT", 30)
            register_market_journal(journal, scope)
            key = journal_key(journal)
            append_trade(
                journal,
                key,
                scope,
                MarketTrade("101", "3512.45", "1.5", now - 10**9, now),
                "act-1",
                1234,
            )
            j_provider = MarketJournalQuoteProvider(journal)
            eth_q = j_provider.latest_quote("ETH-USDT")
            self.assertIsNotNone(eth_q)
            self.assertEqual(eth_q.price_minor_units, 351_245)
            self.assertIsNone(j_provider.latest_quote("SOL-USDT"))

            # Refuse non-loopback override for BinancePublicQuoteProvider.
            with self.assertRaises(QuoteRefused):
                BinancePublicQuoteProvider(
                    market_kind="spot",
                    base_url_override="https://evil.example.com",
                    allow_loopback_override=True,
                )

            ticker = _TickerServer({"SOLUSDT": "184.75"})
            try:
                pub = BinancePublicQuoteProvider(
                    market_kind="spot",
                    base_url_override=ticker.base_url,
                    allow_loopback_override=True,
                )
                comp = CompositeQuoteProvider([j_provider, pub])
                sol_q = comp.latest_quote("SOL-USDT")
                self.assertIsNotNone(sol_q)
                self.assertEqual(sol_q.price_minor_units, 18_475)
                self.assertEqual(sol_q.source, "binance-public-spot")
            finally:
                ticker.close()

    def test_company_trading_tools_paper_cancel_migration_and_schedule(self) -> None:
        with TemporaryDirectory(prefix="sd-tools-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            now = time_ns()

            founding = found(
                store,
                FoundingTerms("sourdough-auto", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            ceo_id = founding.founder_id

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    enable_domain_v1(db, "trading")
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )
                book = open_company_paper_book(
                    writer,
                    account_id="paper-auto-1",
                    book_name="Autonomous BTC Book",
                    starting_minor_units=50_000_000,
                    exposure_limit_minor_units=25_000_000,
                    fee_bps=4,
                    slippage_bps=2,
                )
                live_cand = register_live_candidate_binding(
                    writer,
                    binding_id="live-auto-cand-1",
                    account_id="binance-auto-01",
                    destination_id="binance-spot-prod",
                    connector_revision="binance-rest-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=15_000_000,
                    credential_handle_ref="envref:BINANCE_PROD",
                )
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-auto-1",
                    adapter_revision="binance-rest-v1",
                    environment=ENV_EXTERNAL_TEST,
                    destination_id="binance-spot-testnet",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    core_digest=compute_trading_core_digest(),
                    verdict="PASS",
                    cases_passed=6,
                    cases_failed=0,
                    cases_unrun=0,
                    evidence_sha256="c" * 64,
                )

                quotes = StaticQuoteProvider(
                    {
                        "BTC-USDT": MarketQuote(
                            "BTC-USDT", 6_500_000, now - 2 * 10**9, "test-feed"
                        ),
                        "ETH-USDT": MarketQuote(
                            "ETH-USDT", 350_000, now - 300 * 10**9, "stale-feed"
                        ),
                    }
                )
                tools = CompanyTradingTools(
                    writer=writer,
                    agent_id=ceo_id,
                    run_id=None,
                    quote_provider=quotes,
                    clock=lambda: now,
                )

                # Reject spoofed server-bound identity fields.
                with self.assertRaises(ExecutionTierRefused):
                    tools.invoke(
                        "read_trading_books",
                        {"environment": "paper", "agent_id": "someone-else"},
                    )

                # Reject stale quote before paper execution.
                with self.assertRaises(ExecutionTierRefused):
                    tools.invoke(
                        "submit_company_paper_order",
                        {
                            "binding_id": book.binding_id,
                            "action_id": "stale-eth-1",
                            "instrument_id": "ETH-USDT",
                            "side": "buy",
                            "quantity": 1,
                            "limit_minor_units": 360_000,
                        },
                    )

                # Submit filled immediate order using authoritative host quote.
                fill_res = tools.invoke(
                    "submit_company_paper_order",
                    {
                        "binding_id": book.binding_id,
                        "action_id": "auto-btc-buy-1",
                        "instrument_id": "BTC-USDT",
                        "side": "buy",
                        "quantity": 2,
                        "limit_minor_units": 6_600_000,
                    },
                )
                self.assertEqual(fill_res["status"], "filled")
                self.assertEqual(fill_res["filled_quantity"], 2)

                # Submit resting order and cancel it cleanly.
                rest_res = tools.invoke(
                    "submit_company_paper_order",
                    {
                        "binding_id": book.binding_id,
                        "action_id": "auto-btc-rest-1",
                        "instrument_id": "BTC-USDT",
                        "side": "buy",
                        "quantity": 1,
                        "limit_minor_units": 6_000_000,
                        "fill_mode": "resting",
                    },
                )
                self.assertEqual(rest_res["status"], "open")
                cancel_res = tools.invoke(
                    "cancel_company_paper_order",
                    {
                        "binding_id": book.binding_id,
                        "order_id": rest_res["order_id"],
                    },
                )
                self.assertEqual(cancel_res["status"], "cancelled")
                self.assertFalse(cancel_res["replayed"])
                cancel_replay = tools.invoke(
                    "cancel_company_paper_order",
                    {
                        "binding_id": book.binding_id,
                        "order_id": rest_res["order_id"],
                    },
                )
                self.assertTrue(cancel_replay["replayed"])

                # Propose migration (creates Board approval, does NOT activate live).
                prop = tools.invoke(
                    "propose_live_migration",
                    {
                        "source_paper_binding_id": book.binding_id,
                        "target_live_binding_id": live_cand.binding_id,
                        "qualification_receipt_id": "qual-auto-1",
                        "reason": "BTC momentum passed soak threshold",
                    },
                )
                self.assertTrue(prop["question_id"].startswith("q-mig-"))
                live_books = tools.invoke(
                    "read_trading_books", {"environment": ENV_LIVE}
                )
                self.assertEqual(live_books["books"][0]["status"], "registered")

                # Schedule next autonomous shift cleanly and enforce bounds.
                with self.assertRaises(ExecutionTierRefused):
                    tools.invoke(
                        "schedule_next_shift",
                        {"delay_seconds": 5, "duty_kind": "quant.reconcile"},
                    )
                sched = tools.invoke(
                    "schedule_next_shift",
                    {"delay_seconds": 300, "duty_kind": "quant.reconcile"},
                )
                self.assertEqual(sched["status"], "armed")
                self.assertEqual(sched["duty_kind"], "quant.reconcile")

    def test_two_phase_live_execution_timeout_recovery_and_drift_pause(self) -> None:
        with TemporaryDirectory(prefix="sd-live-loop-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            now = time_ns()

            founding = found(
                store,
                FoundingTerms("sourdough-live", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            ceo_id = founding.founder_id

            emulator = BinanceWireEmulator(
                api_key="test-key-1234",
                api_secret="test-secret-5678",
                starting_usdt=Decimal("500000"),
                fee_bps=4,
                max_order_notional=Decimal("200000"),
            )
            emulator.start()
            try:
                adapter = BinanceVenueAdapter(
                    BinanceVenueConfig(
                        environment=ENV_EXTERNAL_TEST,
                        market_kind=MARKET_SPOT,
                        account_id="binance-prod-us01",
                        destination_id="binance-spot-testnet",
                        credential_handle_ref="envref:BINANCE_TESTNET",
                        base_url_override=emulator.base_url,
                        allow_loopback_override=True,
                    ),
                    _StaticCreds("test-key-1234", "test-secret-5678"),
                )

                quotes = StaticQuoteProvider(
                    {
                        "BTC-USDT": MarketQuote(
                            "BTC-USDT", 6_500_000, now - 10**9, "live-ticker"
                        )
                    }
                )

                with hold_company_writer(
                    store, company_id, lease_directory=leases
                ) as writer:
                    with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                        enable_domain_v1(db, "trading")
                    bind_board_principal(
                        writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                    )
                    paper_book = open_company_paper_book(
                        writer,
                        account_id="paper-shadow-src",
                        book_name="BTC Spot Momentum",
                        starting_minor_units=50_000_000,
                        exposure_limit_minor_units=30_000_000,
                    )
                    # Give paper book a position so Shadow Benchmark marks alongside Live!
                    paper_tools = CompanyTradingTools(
                        writer=writer,
                        agent_id=ceo_id,
                        run_id=None,
                        quote_provider=quotes,
                        clock=lambda: now,
                    )
                    paper_tools.invoke(
                        "submit_company_paper_order",
                        {
                            "binding_id": paper_book.binding_id,
                            "action_id": "paper-seed-1",
                            "instrument_id": "BTC-USDT",
                            "side": "buy",
                            "quantity": 1,
                            "limit_minor_units": 6_600_000,
                        },
                    )

                    live_cand = register_live_candidate_binding(
                        writer,
                        binding_id="live-bn-1",
                        account_id="binance-prod-us01",
                        destination_id="binance-spot-prod",
                        connector_revision="binance-rest-v1",
                        instrument_rules_digest="btc-usdt-spot-v1",
                        currency="USD",
                        starting_minor_units=25_000_000,
                        exposure_limit_minor_units=20_000_000,
                        credential_handle_ref="envref:BINANCE_PROD",
                    )
                    record_venue_qualification_receipt(
                        writer,
                        receipt_id="qual-live-loop-1",
                        adapter_revision="binance-rest-v1",
                        environment=ENV_EXTERNAL_TEST,
                        destination_id="binance-spot-testnet",
                        instrument_rules_digest="btc-usdt-spot-v1",
                        core_digest=compute_trading_core_digest(),
                        verdict="PASS",
                        cases_passed=6,
                        cases_failed=0,
                        cases_unrun=0,
                        evidence_sha256="d" * 64,
                    )
                    execute_paper_to_live_migration(
                        writer,
                        source_paper_binding_id=paper_book.binding_id,
                        target_live_binding_id=live_cand.binding_id,
                        qualification_receipt_id="qual-live-loop-1",
                        migration_mode="clean_start",
                        source_paper_disposition="keep_shadow",
                        reason="Board approved live test",
                    )

                    # 1. Normal live order execution through 2-phase outbox + HMAC Binance HTTP!
                    res1 = submit_company_live_book_order(
                        writer,
                        binding_id=live_cand.binding_id,
                        actor_id=ceo_id,
                        action_id="live-act-1",
                        instrument_id="BTC-USDT",
                        side="buy",
                        quantity=1,
                        limit_minor_units=6_500_000,
                        adapter=adapter,
                        quote_provider=quotes,
                        now_ns=now,
                    )
                    self.assertEqual(res1["status"], "filled")
                    self.assertEqual(res1["outbox_status"], "confirmed")
                    self.assertEqual(res1["fills_booked"], 1)
                    self.assertEqual(emulator.submit_count, 1)

                    # Idempotent replay does NOT resend to exchange!
                    res1_replay = submit_company_live_book_order(
                        writer,
                        binding_id=live_cand.binding_id,
                        actor_id=ceo_id,
                        action_id="live-act-1",
                        instrument_id="BTC-USDT",
                        side="buy",
                        quantity=1,
                        limit_minor_units=6_500_000,
                        adapter=adapter,
                        quote_provider=quotes,
                        now_ns=now,
                    )
                    self.assertTrue(res1_replay["replayed"])
                    self.assertEqual(emulator.submit_count, 1)

                    # 2. Lost reply timeout (HTTP 504 after fill!) -> settles to 'unknown' fail-closed!
                    coid_2 = adapter.client_order_id_for("live-act-2")
                    emulator.set_fault(coid_2, "lost_reply_after_accept")
                    res2 = submit_company_live_book_order(
                        writer,
                        binding_id=live_cand.binding_id,
                        actor_id=ceo_id,
                        action_id="live-act-2",
                        instrument_id="BTC-USDT",
                        side="buy",
                        quantity=1,
                        limit_minor_units=6_500_000,
                        adapter=adapter,
                        quote_provider=quotes,
                        now_ns=now + 10**9,
                    )
                    self.assertEqual(res2["status"], "unknown")
                    self.assertEqual(res2["fills_booked"], 0)

                    # Retrying an unknown order OR submitting a new live order while unresolved is REFUSED fail-closed!
                    with self.assertRaises(ExecutionTierRefused):
                        submit_company_live_book_order(
                            writer,
                            binding_id=live_cand.binding_id,
                            actor_id=ceo_id,
                            action_id="live-act-2",
                            instrument_id="BTC-USDT",
                            side="buy",
                            quantity=1,
                            limit_minor_units=6_500_000,
                            adapter=adapter,
                            quote_provider=quotes,
                            now_ns=now + 2 * 10**9,
                        )
                    with self.assertRaises(ExecutionTierRefused):
                        submit_company_live_book_order(
                            writer,
                            binding_id=live_cand.binding_id,
                            actor_id=ceo_id,
                            action_id="live-act-blocked-by-unknown",
                            instrument_id="BTC-USDT",
                            side="buy",
                            quantity=1,
                            limit_minor_units=6_500_000,
                            adapter=adapter,
                            quote_provider=quotes,
                            now_ns=now + 2 * 10**9,
                        )

                    # Move quote up and run reconcile_trading_books: recovers unknown fill + marks Live & Shadow!
                    t_rec = now + 5 * 10**9
                    quotes.set_quote(
                        MarketQuote("BTC-USDT", 6_750_000, t_rec - 10**9, "live-ticker")
                    )
                    rec_summary = reconcile_trading_books(
                        writer,
                        quote_provider=quotes,
                        live_venue_resolver=lambda bid: (
                            adapter if bid == live_cand.binding_id else None
                        ),
                        now_ns=t_rec,
                    )
                    self.assertEqual(rec_summary["live_actions_reconciled"], 1)
                    self.assertEqual(rec_summary["live_fills_booked"], 1)
                    self.assertFalse(rec_summary["drift_detected"])
                    marked_envs = {
                        b["environment"] for b in rec_summary["books_marked"]
                    }
                    self.assertEqual(marked_envs, {ENV_PAPER, ENV_LIVE})

                    # Verify Live projection shows 2 BTC position, positive P&L, and Shadow Benchmark!
                    live_proj, _ = read_trading_desk_projection(
                        store, company_id, environment=ENV_LIVE
                    )
                    self.assertEqual(live_proj["open_positions"][0]["quantity"], 2)
                    self.assertGreater(
                        live_proj["kpis"]["trading_pnl"]["value_minor"], 0
                    )
                    self.assertIsNotNone(live_proj["shadow_benchmark"])

                    # 3. Unattributed external venue fill triggers automatic execution:live safety pause!
                    emulator.inject_external_fill(
                        symbol="BTCUSDT",
                        side="BUY",
                        quantity=Decimal("1"),
                        price=Decimal("67500"),
                    )
                    rec_drift = reconcile_trading_books(
                        writer,
                        quote_provider=quotes,
                        live_venue_resolver=lambda bid: (
                            adapter if bid == live_cand.binding_id else None
                        ),
                        now_ns=t_rec + 2 * 10**9,
                    )
                    self.assertTrue(rec_drift["drift_detected"])
                    self.assertEqual(len(rec_drift["restrictions_applied"]), 1)

                    with closing(open_reader(store)) as db, read_tx(db):
                        active_live_restr = active_trading_risk_restrictions(
                            db, company_id, ENV_LIVE
                        )
                    self.assertEqual(len(active_live_restr), 1)
                    self.assertEqual(
                        active_live_restr[0]["scope_ref"], "execution:live"
                    )

                    # Subsequent live orders are now REFUSED fail-closed!
                    with self.assertRaises(ExecutionTierRefused):
                        submit_company_live_book_order(
                            writer,
                            binding_id=live_cand.binding_id,
                            actor_id=ceo_id,
                            action_id="live-act-blocked-3",
                            instrument_id="BTC-USDT",
                            side="buy",
                            quantity=1,
                            limit_minor_units=6_750_000,
                            adapter=adapter,
                            quote_provider=quotes,
                            now_ns=t_rec + 3 * 10**9,
                        )
            finally:
                emulator.stop()


if __name__ == "__main__":
    unittest.main()
