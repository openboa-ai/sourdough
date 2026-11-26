"""Unit tests for multi-book Company Paper Trading and fail-closed Paper-to-Live migration."""

from __future__ import annotations

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    apply_company_restriction,
    bind_board_principal,
    clear_company_restriction,
)
from sourdough.company import FoundingTerms, found
from sourdough.execution_tiers import (
    ENV_EXTERNAL_TEST,
    ExecutionTierRefused,
    compute_trading_core_digest,
    execute_paper_to_live_migration,
    open_company_paper_book,
    propose_paper_to_live_migration,
    read_execution_tiers_snapshot,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
    submit_company_paper_book_order,
)
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer


class TestMultiBookPaperAndLiveMigration(unittest.TestCase):
    def test_concurrent_paper_books_and_safe_live_migration_gates(self) -> None:
        with TemporaryDirectory(prefix="sd-mig-unit-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            venue = FixedQuoteVenue(root / "v.sqlite")

            founding = found(
                store,
                FoundingTerms("sourdough-mig", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            ceo_id = founding.founder_id

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )

                # Legacy paper_binding required only to test paper_order uncertain-order blocking gate.
                paper_acct = venue.provision(
                    PaperAccount(
                        account_id="legacy-paper-base",
                        connector_revision="fixed-quote-v1",
                        instrument_rules_digest="btc-usdt-spot-v1",
                        currency="USD",
                        starting_minor_units=100_000_000,
                        environment="paper",
                        destination_id="",
                    ),
                    quote_minor_units=6_500_000,
                    fee_bps=4,
                )
                bind(
                    store,
                    PaperMandate(
                        revision="mandate-mig-v1",
                        company_id=company_id,
                        charter_revision="charter-v1",
                        account=paper_acct,
                        exposure_limit_minor_units=50_000_000,
                    ),
                    venue,
                )

                # 1. Open 3 concurrent Company Paper Books simultaneously!
                book_btc = open_company_paper_book(
                    writer,
                    account_id="paper-book-btc",
                    book_name="BTC Spot Momentum",
                    strategy_release_id="rel-btc-mom-v3",
                    starting_minor_units=50_000_000,
                    exposure_limit_minor_units=25_000_000,
                    fee_bps=4,
                    slippage_bps=2,
                )
                book_eth = open_company_paper_book(
                    writer,
                    account_id="paper-book-eth",
                    book_name="ETH Basis Neutral",
                    strategy_release_id="rel-eth-basis-v2",
                    starting_minor_units=40_000_000,
                    exposure_limit_minor_units=20_000_000,
                    fee_bps=3,
                    slippage_bps=1,
                )
                book_sol = open_company_paper_book(
                    writer,
                    account_id="paper-book-sol",
                    book_name="SOL Mean Reversion",
                    strategy_release_id="rel-sol-mr-v1",
                    starting_minor_units=30_000_000,
                    exposure_limit_minor_units=15_000_000,
                    fee_bps=5,
                    slippage_bps=3,
                )

                # Register 2 live candidate venues + passing qualification receipt.
                live_a = register_live_candidate_binding(
                    writer,
                    binding_id="live-binance-a",
                    account_id="binance-prod-a",
                    destination_id="binance-spot-prod-a",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=10_000_000,
                    credential_handle_ref="keychain:openboa/binance/prod-a",
                )
                live_b = register_live_candidate_binding(
                    writer,
                    binding_id="live-binance-b",
                    account_id="binance-prod-b",
                    destination_id="binance-spot-prod-b",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=12_000_000,
                    credential_handle_ref="keychain:openboa/binance/prod-b",
                )
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-mig-pass-1",
                    adapter_revision="binance-spot-v1",
                    environment=ENV_EXTERNAL_TEST,
                    destination_id="binance-spot-testnet",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    core_digest=compute_trading_core_digest(),
                    verdict="PASS",
                    cases_passed=6,
                    cases_failed=0,
                    cases_unrun=0,
                    evidence_sha256="e" * 64,
                )

                # Un-soaked paper book cannot be nominated!
                with self.assertRaises(ExecutionTierRefused):
                    propose_paper_to_live_migration(
                        writer,
                        question_id="q-unsoaked",
                        source_paper_binding_id=book_btc.binding_id,
                        target_live_binding_id=live_a.binding_id,
                        qualification_receipt_id="qual-mig-pass-1",
                        created_by_agent_id=ceo_id,
                    )

                # Execute independent simulated trades across Book BTC and Book ETH (Book SOL stays empty).
                r1 = submit_company_paper_book_order(
                    writer,
                    binding_id=book_btc.binding_id,
                    actor_id=ceo_id,
                    action_id="btc-buy-1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=2,
                    limit_minor_units=6_600_000,
                    market_price_minor_units=6_500_000,
                )
                self.assertEqual(r1["status"], "filled")
                r2 = submit_company_paper_book_order(
                    writer,
                    binding_id=book_btc.binding_id,
                    actor_id=ceo_id,
                    action_id="btc-sell-1",
                    instrument_id="BTC-USDT",
                    side="sell",
                    quantity=1,
                    limit_minor_units=6_650_000,
                    market_price_minor_units=6_700_000,
                )
                self.assertEqual(r2["status"], "filled")
                self.assertGreater(r2["realized_pnl_minor_units"], 0)

                r3 = submit_company_paper_book_order(
                    writer,
                    binding_id=book_eth.binding_id,
                    actor_id=ceo_id,
                    action_id="eth-buy-1",
                    instrument_id="ETH-USDT",
                    side="buy",
                    quantity=5,
                    limit_minor_units=360_000,
                    market_price_minor_units=350_000,
                )
                self.assertEqual(r3["status"], "filled")

                # Verify books remain completely isolated from each other.
                with closing(open_reader(store)) as db, read_tx(db):
                    snap = read_execution_tiers_snapshot(db, company_id)
                books_by_id = {b["binding_id"]: b for b in snap["company_paper_books"]}
                self.assertEqual(len(books_by_id), 3)
                self.assertEqual(books_by_id[book_btc.binding_id]["fill_count"], 2)
                self.assertTrue(books_by_id[book_btc.binding_id]["soak_ready"])
                self.assertEqual(books_by_id[book_eth.binding_id]["fill_count"], 1)
                self.assertTrue(books_by_id[book_eth.binding_id]["soak_ready"])
                self.assertEqual(books_by_id[book_sol.binding_id]["fill_count"], 0)
                self.assertFalse(books_by_id[book_sol.binding_id]["soak_ready"])

                # 2. Nominate Book BTC for Live migration.
                nom = propose_paper_to_live_migration(
                    writer,
                    question_id="q-mig-btc-live",
                    source_paper_binding_id=book_btc.binding_id,
                    target_live_binding_id=live_a.binding_id,
                    qualification_receipt_id="qual-mig-pass-1",
                    created_by_agent_id=ceo_id,
                    migration_mode="clean_start",
                    source_paper_disposition="keep_shadow",
                )
                self.assertEqual(nom["question_id"], "q-mig-btc-live")

                # 3. Active risk restriction blocks migration fail-closed!
                apply_company_restriction(
                    writer,
                    command_id="cmd-pause-1",
                    charter_revision="charter-v1",
                    restriction_id="rest-hold-mig",
                    expected_restriction_revision=0,
                    cause_category="board_pause",
                    scope_ref="company:all",
                    reason="Hold live activation during review",
                    principal_hint="board-chair",
                )
                with self.assertRaises(ExecutionTierRefused):
                    execute_paper_to_live_migration(
                        writer,
                        source_paper_binding_id=book_btc.binding_id,
                        target_live_binding_id=live_a.binding_id,
                        qualification_receipt_id="qual-mig-pass-1",
                        migration_mode="clean_start",
                        source_paper_disposition="keep_shadow",
                        question_id="q-mig-btc-live",
                    )
                clear_company_restriction(
                    writer,
                    command_id="cmd-resume-1",
                    charter_revision="charter-v1",
                    restriction_id="rest-hold-mig",
                    expected_restriction_revision=1,
                    reason="Review clear",
                    principal_hint="board-chair",
                )

                # 4. Unresolved legacy paper_order blocks migration fail-closed!
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    db.execute(
                        """INSERT INTO duty_run (
                               run_id, duty_id, grant_id, grant_scope, purpose, is_current
                           ) VALUES ('run-mig-1', ?, ?, 'paper', 'migration-test', 1)""",
                        (founding.due_work_id, founding.grant_id),
                    )
                    db.execute(
                        """INSERT INTO paper_order (
                               order_id, run_id, mandate_revision, account_id,
                               action_id, instrument_rules_digest, quantity,
                               limit_minor_units, reserved_minor_units
                           ) VALUES ('ord-unres-1', 'run-mig-1', 'mandate-mig-v1',
                                     'legacy-paper-base', 'act-unres-1',
                                     'btc-usdt-spot-v1', 2, 6500000, 13000000)"""
                    )
                with self.assertRaises(ExecutionTierRefused):
                    execute_paper_to_live_migration(
                        writer,
                        source_paper_binding_id=book_btc.binding_id,
                        target_live_binding_id=live_a.binding_id,
                        qualification_receipt_id="qual-mig-pass-1",
                        migration_mode="clean_start",
                        source_paper_disposition="keep_shadow",
                        question_id="q-mig-btc-live",
                    )
                # Once filled completely, unresolved order gate clears cleanly!
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    db.execute(
                        "INSERT INTO paper_fill (fill_id, order_id, quantity, price_minor_units) VALUES ('fill-unres-1', 'ord-unres-1', 2, 6500000)"
                    )

                # 5. Safe clean_start + keep_shadow migration succeeds!
                mig1 = execute_paper_to_live_migration(
                    writer,
                    source_paper_binding_id=book_btc.binding_id,
                    target_live_binding_id=live_a.binding_id,
                    qualification_receipt_id="qual-mig-pass-1",
                    migration_mode="clean_start",
                    source_paper_disposition="keep_shadow",
                    question_id="q-mig-btc-live",
                    reason="Board approved clean start migration",
                )
                self.assertEqual(mig1["positions_copied"], 0)
                self.assertEqual(mig1["source_book_final_role"], "shadow")
                self.assertIsNone(mig1["replaced_live_binding_id"])

                with closing(open_reader(store)) as db, read_tx(db):
                    snap1 = read_execution_tiers_snapshot(db, company_id)
                self.assertEqual(snap1["company_live"]["binding_id"], live_a.binding_id)
                books1 = {b["binding_id"]: b for b in snap1["company_paper_books"]}
                self.assertEqual(books1[book_btc.binding_id]["role"], "shadow")
                self.assertEqual(
                    books1[book_btc.binding_id]["shadow_of_live_binding_id"],
                    live_a.binding_id,
                )
                self.assertEqual(books1[book_eth.binding_id]["role"], "active")
                self.assertEqual(len(snap1["migration_history"]), 1)
                self.assertEqual(snap1["migration_history"][0]["positions_copied"], 0)

                # 6. Second migration under clean_start is refused because incumbent live_a is active!
                with self.assertRaises(ExecutionTierRefused):
                    execute_paper_to_live_migration(
                        writer,
                        source_paper_binding_id=book_eth.binding_id,
                        target_live_binding_id=live_b.binding_id,
                        qualification_receipt_id="qual-mig-pass-1",
                        migration_mode="clean_start",
                        source_paper_disposition="retire_source",
                    )

                # 7. Under drain_incumbent + retire_source, an unresolved order blocks swap until cancelled, then swaps cleanly!
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    db.execute(
                        """INSERT INTO paper_order (
                               order_id, run_id, mandate_revision, account_id,
                               action_id, instrument_rules_digest, quantity,
                               limit_minor_units, reserved_minor_units
                           ) VALUES ('ord-work-1', 'run-mig-1', 'mandate-mig-v1',
                                     'legacy-paper-base', 'act-work-1',
                                     'btc-usdt-spot-v1', 1, 6400000, 6400000)"""
                    )
                with self.assertRaises(ExecutionTierRefused):
                    execute_paper_to_live_migration(
                        writer,
                        source_paper_binding_id=book_eth.binding_id,
                        target_live_binding_id=live_b.binding_id,
                        qualification_receipt_id="qual-mig-pass-1",
                        migration_mode="drain_incumbent",
                        source_paper_disposition="retire_source",
                    )
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    db.execute(
                        """INSERT INTO paper_cancellation (
                               order_id, event_id, outcome, released,
                               filled_quantity, position_quantity, available_at
                           ) VALUES ('ord-work-1', 'canc-work-1', 'cancelled_flat', 1, 0, 0, 100)"""
                    )

                mig2 = execute_paper_to_live_migration(
                    writer,
                    source_paper_binding_id=book_eth.binding_id,
                    target_live_binding_id=live_b.binding_id,
                    qualification_receipt_id="qual-mig-pass-1",
                    migration_mode="drain_incumbent",
                    source_paper_disposition="retire_source",
                    reason="Swapped to ETH Basis Neutral via drained incumbent",
                )
                self.assertEqual(mig2["replaced_live_binding_id"], live_a.binding_id)
                self.assertEqual(mig2["source_book_final_role"], "retired")
                self.assertEqual(mig2["positions_copied"], 0)

                with closing(open_reader(store)) as db, read_tx(db):
                    snap2 = read_execution_tiers_snapshot(db, company_id)
                self.assertEqual(snap2["company_live"]["binding_id"], live_b.binding_id)
                books2 = {b["binding_id"]: b for b in snap2["company_paper_books"]}
                # Prior shadow book cleanly returned to active when incumbent live_a retired!
                self.assertEqual(books2[book_btc.binding_id]["role"], "active")
                self.assertIsNone(
                    books2[book_btc.binding_id]["shadow_of_live_binding_id"]
                )
                # Source book_eth retired as requested.
                self.assertEqual(books2[book_eth.binding_id]["role"], "retired")
                self.assertEqual(books2[book_eth.binding_id]["status"], "retired")


if __name__ == "__main__":
    unittest.main()
