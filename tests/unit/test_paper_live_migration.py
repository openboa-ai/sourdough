"""Unit tests for multi-book Company Paper Trading and fail-closed Paper-to-Live migration."""

from __future__ import annotations

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    BoardControlRefused,
    apply_company_restriction,
    bind_board_principal,
    clear_company_restriction,
)
from sourdough.company import FoundingTerms, found
from sourdough.trading.accounts import (
    open_company_paper_book,
    promote_execution_environment,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
)
from sourdough.trading.domain import (
    ENV_EXTERNAL_TEST,
    ExecutionTierRefused,
    build_promotion_payload_hash,
    compute_trading_core_digest,
)
from sourdough.trading.ledger import submit_company_paper_book_order
from sourdough.trading.migration import (
    execute_paper_to_live_migration,
    propose_paper_to_live_migration,
    read_execution_tiers_snapshot,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer


class TestMultiBookPaperAndLiveMigration(unittest.TestCase):
    def test_concurrent_paper_books_and_safe_live_migration_gates(self) -> None:
        with TemporaryDirectory(prefix="sd-mig-unit-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"

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

                # 4. Open trading_book_order on source binding blocks migration fail-closed!
                open_ord = submit_company_paper_book_order(
                    writer,
                    binding_id=book_btc.binding_id,
                    actor_id=ceo_id,
                    action_id="act-unres-1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=2,
                    limit_minor_units=6_000_000,
                    market_price_minor_units=6_500_000,
                    fill_mode="resting",
                )
                with self.assertRaisesRegex(ExecutionTierRefused, "unresolved order"):
                    execute_paper_to_live_migration(
                        writer,
                        source_paper_binding_id=book_btc.binding_id,
                        target_live_binding_id=live_a.binding_id,
                        qualification_receipt_id="qual-mig-pass-1",
                        migration_mode="clean_start",
                        source_paper_disposition="keep_shadow",
                        question_id="q-mig-btc-live",
                    )
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    db.execute(
                        "UPDATE trading_book_order SET status = 'cancelled', cancelled_at_ns = 100 WHERE order_id = ?",
                        (open_ord["order_id"],),
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

                # 7. Under drain_incumbent + retire_source, an open order on source/incumbent blocks swap until cancelled, then swaps cleanly!
                open_ord2 = submit_company_paper_book_order(
                    writer,
                    binding_id=book_eth.binding_id,
                    actor_id=ceo_id,
                    action_id="act-work-1",
                    instrument_id="ETH-USDT",
                    side="buy",
                    quantity=1,
                    limit_minor_units=3_000_000,
                    market_price_minor_units=3_200_000,
                    fill_mode="resting",
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
                        "UPDATE trading_book_order SET status = 'cancelled', cancelled_at_ns = 100 WHERE order_id = ?",
                        (open_ord2["order_id"],),
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

    def test_qualification_rejects_unrun_cases_and_adapter_revision_mismatch(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-mig-qual-strict-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms("sourdough-qual", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            ceo_id = founding.founder_id

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )
                paper_book = open_company_paper_book(
                    writer,
                    account_id="paper-q-1",
                    book_name="Soak Paper",
                    starting_minor_units=20_000_000,
                    exposure_limit_minor_units=10_000_000,
                )
                submit_company_paper_book_order(
                    writer,
                    binding_id=paper_book.binding_id,
                    actor_id=ceo_id,
                    action_id="soak-q-1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=1,
                    limit_minor_units=1_010_000,
                    market_price_minor_units=1_000_000,
                )
                live_cand = register_live_candidate_binding(
                    writer,
                    binding_id="live-q-cand",
                    account_id="binance-q",
                    destination_id="binance-spot-prod",
                    connector_revision="binance-spot-v2",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=20_000_000,
                    exposure_limit_minor_units=10_000_000,
                    credential_handle_ref="keychain:openboa/binance/q",
                )
                core_digest = compute_trading_core_digest()
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-unrun",
                    adapter_revision="binance-spot-v2",
                    environment=ENV_EXTERNAL_TEST,
                    destination_id="binance-spot-testnet",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    core_digest=core_digest,
                    verdict="PASS",
                    cases_passed=5,
                    cases_failed=0,
                    cases_unrun=3,
                    evidence_sha256="a" * 64,
                )
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-wrong-adapter",
                    adapter_revision="binance-spot-v1",
                    environment=ENV_EXTERNAL_TEST,
                    destination_id="binance-spot-testnet",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    core_digest=core_digest,
                    verdict="PASS",
                    cases_passed=5,
                    cases_failed=0,
                    cases_unrun=0,
                    evidence_sha256="b" * 64,
                )

                with self.assertRaisesRegex(BoardControlRefused, "unrun"):
                    promote_execution_environment(
                        writer,
                        command_id="prom-unrun",
                        charter_revision="charter-v1",
                        target_binding_id=live_cand.binding_id,
                        qualification_receipt_id="qual-unrun",
                        expected_payload_hash=build_promotion_payload_hash(
                            company_id=company_id,
                            charter_revision="charter-v1",
                            target_binding=live_cand,
                            qualification_receipt_id="qual-unrun",
                        ),
                        reason="Must fail on unrun cases",
                        principal_hint="board-chair",
                    )

                with self.assertRaisesRegex(BoardControlRefused, "adapter_revision"):
                    promote_execution_environment(
                        writer,
                        command_id="prom-wrong-adapter",
                        charter_revision="charter-v1",
                        target_binding_id=live_cand.binding_id,
                        qualification_receipt_id="qual-wrong-adapter",
                        expected_payload_hash=build_promotion_payload_hash(
                            company_id=company_id,
                            charter_revision="charter-v1",
                            target_binding=live_cand,
                            qualification_receipt_id="qual-wrong-adapter",
                        ),
                        reason="Must fail on adapter mismatch",
                        principal_hint="board-chair",
                    )

                with self.assertRaisesRegex(ExecutionTierRefused, "adapter_revision"):
                    propose_paper_to_live_migration(
                        writer,
                        question_id="q-wrong-adapter",
                        source_paper_binding_id=paper_book.binding_id,
                        target_live_binding_id=live_cand.binding_id,
                        qualification_receipt_id="qual-wrong-adapter",
                        created_by_agent_id=ceo_id,
                    )

                with self.assertRaisesRegex(ExecutionTierRefused, "adapter_revision"):
                    execute_paper_to_live_migration(
                        writer,
                        source_paper_binding_id=paper_book.binding_id,
                        target_live_binding_id=live_cand.binding_id,
                        qualification_receipt_id="qual-wrong-adapter",
                    )


if __name__ == "__main__":
    unittest.main()
