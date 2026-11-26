"""Unit tests for the unified TradingBook ledger, environment isolation, and trading-desk projection."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import BOARD_CONTROL_ROLE, bind_board_principal
from sourdough.company import FoundingTerms, found
from sourdough.execution_tiers import (
    ENV_EXTERNAL_TEST,
    ENV_LIVE,
    ENV_PAPER,
    ExecutionTierRefused,
    compute_trading_core_digest,
    execute_paper_to_live_migration,
    open_company_paper_book,
    propose_paper_to_live_migration,
    record_live_book_order_fill,
    record_trading_book_valuation,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
    submit_company_paper_book_order,
)
from sourdough.ipc.projections import read_trading_desk_projection
from sourdough.storage.db import open_writer, write_tx
from sourdough.writer_lease import hold_company_writer


class TestUnifiedTradingDesk(unittest.TestCase):
    def test_composite_fk_blocks_cross_environment_insert(self) -> None:
        with TemporaryDirectory(prefix="sd-desk-fk-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"

            founding = found(
                store,
                FoundingTerms("sourdough-fk", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            ceo_id = founding.founder_id

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                paper_book = open_company_paper_book(
                    writer,
                    account_id="paper-fk-book",
                    book_name="FK Isolation Paper",
                    starting_minor_units=50_000_000,
                    exposure_limit_minor_units=25_000_000,
                )
                live_cand = register_live_candidate_binding(
                    writer,
                    binding_id="live-fk-cand",
                    account_id="binance-fk",
                    destination_id="binance-spot-prod",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=10_000_000,
                    credential_handle_ref="keychain:openboa/binance/fk",
                )

            # Attempting to insert a 'live' order referencing a 'paper' binding_id MUST fail FK.
            with closing(open_writer(store)) as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    with write_tx(db):
                        db.execute(
                            """INSERT INTO trading_book_order (
                                   order_id, binding_id, environment, company_id, actor_id,
                                   action_id, external_order_id, instrument_id, side, quantity,
                                   limit_minor_units, market_price_minor_units, quote_observed_at_ns,
                                   quote_source, fee_bps, slippage_bps, status, filled_quantity,
                                   fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
                               ) VALUES ('bad-1', ?, 'live', ?, ?, 'act-1', NULL, 'BTC-USDT', 'buy', 1,
                                         6500000, 6500000, 100, 'test', 0, 0, 'filled', 1, 6500000, 0, 100, 100)""",
                            (paper_book.binding_id, company_id, ceo_id),
                        )

                # And attempting to insert a 'paper' valuation referencing a 'live' binding_id MUST fail FK.
                with self.assertRaises(sqlite3.IntegrityError):
                    with write_tx(db):
                        db.execute(
                            """INSERT INTO trading_book_valuation (
                                   valuation_id, binding_id, environment, company_id, label,
                                   nav_minor_units, cash_minor_units, gross_exposure_minor_units,
                                   realized_pnl_minor_units, unrealized_pnl_minor_units,
                                   total_fees_minor_units, source, recorded_at_ns
                               ) VALUES ('bad-val', ?, 'paper', ?, 'Sneak',
                                         25000000, 25000000, 0, 0, 0, 0, 'mark', 100)""",
                            (live_cand.binding_id, company_id),
                        )

    def test_unified_live_and_paper_ledger_isolation_and_shadow_benchmark(self) -> None:
        with TemporaryDirectory(prefix="sd-desk-unit-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"

            founding = found(
                store,
                FoundingTerms("sourdough-desk", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            ceo_id = founding.founder_id
            t0 = 1_730_000_000_000_000_000
            hour_ns = 3_600 * 1_000_000_000

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )

                book_btc = open_company_paper_book(
                    writer,
                    account_id="paper-btc-mom",
                    book_name="BTC Spot Momentum",
                    strategy_release_id="rel-btc-v3",
                    starting_minor_units=50_000_000,
                    exposure_limit_minor_units=25_000_000,
                    fee_bps=4,
                    slippage_bps=2,
                )
                book_eth = open_company_paper_book(
                    writer,
                    account_id="paper-eth-basis",
                    book_name="ETH Basis Neutral",
                    strategy_release_id="rel-eth-v2",
                    starting_minor_units=40_000_000,
                    exposure_limit_minor_units=20_000_000,
                    fee_bps=3,
                    slippage_bps=1,
                )

                # Record paper trades and mark valuations on both books.
                submit_company_paper_book_order(
                    writer,
                    binding_id=book_btc.binding_id,
                    actor_id=ceo_id,
                    action_id="p-btc-1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=2,
                    limit_minor_units=6_600_000,
                    market_price_minor_units=6_500_000,
                    valuation_label="Day 1 Entry",
                    now_ns=t0 + hour_ns,
                )
                record_trading_book_valuation(
                    writer,
                    binding_id=book_btc.binding_id,
                    label="Day 2 Mark",
                    mark_prices={"BTC-USDT": 6_750_000},
                    now_ns=t0 + 2 * hour_ns,
                )

                submit_company_paper_book_order(
                    writer,
                    binding_id=book_eth.binding_id,
                    actor_id=ceo_id,
                    action_id="p-eth-1",
                    instrument_id="ETH-USDT",
                    side="buy",
                    quantity=10,
                    limit_minor_units=360_000,
                    market_price_minor_units=350_000,
                    valuation_label="Day 1 ETH",
                    now_ns=t0 + hour_ns,
                )
                record_trading_book_valuation(
                    writer,
                    binding_id=book_eth.binding_id,
                    label="Day 2 ETH Mark",
                    mark_prices={"ETH-USDT": 340_000},
                    now_ns=t0 + 2 * hour_ns,
                )

                live_cand = register_live_candidate_binding(
                    writer,
                    binding_id="binance-live-1",
                    account_id="binance-prod-us01",
                    destination_id="binance-spot-prod",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=15_000_000,
                    credential_handle_ref="keychain:openboa/binance/prod-us01",
                )
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-desk-1",
                    adapter_revision="binance-spot-v1",
                    environment=ENV_EXTERNAL_TEST,
                    destination_id="binance-spot-testnet",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    core_digest=compute_trading_core_digest(),
                    verdict="PASS",
                    cases_passed=6,
                    cases_failed=0,
                    cases_unrun=0,
                    evidence_sha256="a" * 64,
                )

                # Refuses recording live fills before live venue is active.
                with self.assertRaises(ExecutionTierRefused):
                    record_live_book_order_fill(
                        writer,
                        binding_id=live_cand.binding_id,
                        actor_id=ceo_id,
                        action_id="premature-live-1",
                        external_order_id="ex-0",
                        external_fill_id="exf-0",
                        instrument_id="BTC-USDT",
                        side="buy",
                        quantity=1,
                        limit_minor_units=6_600_000,
                        fill_price_minor_units=6_500_000,
                        fee_minor_units=3_000,
                    )

                # Refuses recording live fills against a paper binding.
                with self.assertRaises(ExecutionTierRefused):
                    record_live_book_order_fill(
                        writer,
                        binding_id=book_btc.binding_id,
                        actor_id=ceo_id,
                        action_id="wrong-env-1",
                        external_order_id="ex-bad",
                        external_fill_id="exf-bad",
                        instrument_id="BTC-USDT",
                        side="buy",
                        quantity=1,
                        limit_minor_units=6_600_000,
                        fill_price_minor_units=6_500_000,
                        fee_minor_units=3_000,
                    )

            # Before live activation: Live projection must be clean Standby with zero paper data!
            live_standby, _ = read_trading_desk_projection(
                store, company_id, environment=ENV_LIVE
            )
            self.assertEqual(live_standby["environment"], ENV_LIVE)
            self.assertIsNone(live_standby["selected_book"])
            self.assertEqual(
                live_standby["kpis"]["portfolio_value"]["status"], "standby"
            )
            self.assertEqual(live_standby["cumulative_return_series"], [])
            self.assertEqual(live_standby["open_positions"], [])
            self.assertEqual(live_standby["execution_tape"], [])
            self.assertIsNone(live_standby["shadow_benchmark"])

            # Paper projection switches cleanly between Book #1 and Book #2.
            paper_btc, _ = read_trading_desk_projection(
                store, company_id, environment=ENV_PAPER, book_id=book_btc.binding_id
            )
            paper_eth, _ = read_trading_desk_projection(
                store, company_id, environment=ENV_PAPER, book_id=book_eth.binding_id
            )
            self.assertEqual(paper_btc["selected_book_id"], book_btc.binding_id)
            self.assertEqual(paper_eth["selected_book_id"], book_eth.binding_id)
            self.assertGreater(paper_btc["kpis"]["trading_pnl"]["value_minor"], 0)
            self.assertLess(paper_eth["kpis"]["trading_pnl"]["value_minor"], 0)
            self.assertEqual(
                paper_btc["open_positions"][0]["instrument_id"], "BTC-USDT"
            )
            self.assertEqual(
                paper_eth["open_positions"][0]["instrument_id"], "ETH-USDT"
            )
            self.assertGreaterEqual(len(paper_btc["cumulative_return_series"]), 3)
            self.assertGreaterEqual(len(paper_eth["cumulative_return_series"]), 3)

            # Migrate BTC Spot Momentum -> Live (clean_start + keep_shadow).
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                nom = propose_paper_to_live_migration(
                    writer,
                    question_id="q-desk-mig-1",
                    source_paper_binding_id=book_btc.binding_id,
                    target_live_binding_id=live_cand.binding_id,
                    qualification_receipt_id="qual-desk-1",
                    created_by_agent_id=ceo_id,
                    migration_mode="clean_start",
                    source_paper_disposition="keep_shadow",
                )
                execute_paper_to_live_migration(
                    writer,
                    question_id=nom["question_id"],
                    source_paper_binding_id=book_btc.binding_id,
                    target_live_binding_id=live_cand.binding_id,
                    qualification_receipt_id="qual-desk-1",
                    migration_mode="clean_start",
                    source_paper_disposition="keep_shadow",
                    reason="Board approved live desk launch",
                )

                # Record Live fills and marks + parallel Shadow marks AFTER Live Launch!
                live_launch_ns = read_trading_desk_projection(
                    store, company_id, environment=ENV_LIVE
                )[0]["cumulative_return_series"][0]["observed_at_ns"]
                first_fill = record_live_book_order_fill(
                    writer,
                    binding_id=live_cand.binding_id,
                    actor_id=ceo_id,
                    action_id="live-btc-1",
                    external_order_id="bn-ord-1001",
                    external_fill_id="bn-fill-1001",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=2,
                    limit_minor_units=6_800_000,
                    fill_price_minor_units=6_760_000,
                    fee_minor_units=6_760,
                    valuation_label="T+1h Fill",
                    now_ns=live_launch_ns + hour_ns,
                )
                self.assertFalse(first_fill["replayed"])

                # Idempotent replay returns same fill without duplicating position or valuation!
                replayed = record_live_book_order_fill(
                    writer,
                    binding_id=live_cand.binding_id,
                    actor_id=ceo_id,
                    action_id="live-btc-1",
                    external_order_id="bn-ord-1001",
                    external_fill_id="bn-fill-1001",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=2,
                    limit_minor_units=6_800_000,
                    fill_price_minor_units=6_760_000,
                    fee_minor_units=6_760,
                    now_ns=live_launch_ns + hour_ns,
                )
                self.assertTrue(replayed["replayed"])
                self.assertEqual(replayed["order_id"], first_fill["order_id"])

                # Mark Live position up and mark Shadow book in parallel.
                val_live = record_trading_book_valuation(
                    writer,
                    binding_id=live_cand.binding_id,
                    label="T+4h Mark",
                    mark_prices={"BTC-USDT": 6_920_000},
                    now_ns=live_launch_ns + 4 * hour_ns,
                )
                record_trading_book_valuation(
                    writer,
                    binding_id=book_btc.binding_id,
                    label="T+4h Shadow Mark",
                    mark_prices={"BTC-USDT": 6_950_000},
                    now_ns=live_launch_ns + 4 * hour_ns,
                )

            # Verify Live projection now shows Live KPIs, Live Chart, Shadow Benchmark overlay, Positions & Tape!
            live_active, _ = read_trading_desk_projection(
                store, company_id, environment=ENV_LIVE
            )
            self.assertEqual(live_active["selected_book_id"], live_cand.binding_id)
            self.assertEqual(live_active["kpis"]["portfolio_value"]["status"], "ok")
            # Exact accounting identity: starting 25,000,000 - fee 6,760 + unrl (6,920,000 - 6,760,000)*2 = 25,313,240
            self.assertEqual(val_live["nav_minor_units"], 25_313_240)
            self.assertEqual(
                live_active["kpis"]["portfolio_value"]["value_minor"], 25_313_240
            )
            self.assertEqual(live_active["kpis"]["trading_pnl"]["value_minor"], 313_240)
            self.assertEqual(live_active["kpis"]["trading_pnl"]["fees_minor"], 6_760)
            self.assertEqual(len(live_active["cumulative_return_series"]), 3)
            self.assertEqual(
                live_active["cumulative_return_series"][0]["return_pct"], 0.0
            )
            self.assertEqual(len(live_active["open_positions"]), 1)
            self.assertEqual(live_active["open_positions"][0]["quantity"], 2)
            self.assertEqual(len(live_active["execution_tape"]), 1)
            self.assertEqual(
                live_active["execution_tape"][0]["external_order_id"], "bn-ord-1001"
            )

            # Verify rebased Shadow Benchmark series starts at 0.0% at Live Launch and tracks drift!
            shadow = live_active["shadow_benchmark"]
            self.assertIsNotNone(shadow)
            self.assertEqual(shadow["binding_id"], book_btc.binding_id)
            self.assertEqual(shadow["series"][0]["return_pct"], 0.0)
            self.assertGreaterEqual(len(shadow["series"]), 2)
            self.assertAlmostEqual(
                shadow["execution_drift_pp"],
                round(
                    live_active["selected_book"]["return_pct"]
                    - shadow["post_launch_return_pct"],
                    4,
                ),
                places=4,
            )


if __name__ == "__main__":
    unittest.main()
