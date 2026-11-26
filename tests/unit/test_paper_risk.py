"""Unit tests for unified scoped Trading risk gating over restriction_cause (D6)."""

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
from sourdough.storage.db import open_reader, read_tx
from sourdough.trading.accounts import (
    open_company_paper_book,
    promote_execution_environment,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
)
from sourdough.trading.domain import (
    ENV_EXTERNAL_TEST,
    ENV_LIVE,
    ENV_PAPER,
    ExecutionTierRefused,
    build_promotion_payload_hash,
    compute_trading_core_digest,
)
from sourdough.trading.ledger import (
    record_live_book_order_fill,
    submit_company_paper_book_order,
)
from sourdough.trading.risk import (
    SCOPE_EXECUTION_ALL,
    SCOPE_EXECUTION_LIVE,
    SCOPE_EXECUTION_PAPER,
    TRADING_RISK_CAUSE_CATEGORY,
    TradingRestrictionGuard,
    is_environment_suspended,
)
from sourdough.writer_lease import hold_company_writer


class UnifiedTradingRiskControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="sd-risk-")))
        self.store = self.root / "c.sqlite"
        self.leases = self.root / "l"
        founding = found(
            self.store, FoundingTerms("risk-co", "charter-1", "paper", "runtime")
        )
        self.company_id = founding.company_id
        self.founder_id = founding.founder_id
        self.guard = TradingRestrictionGuard()

        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            bind_board_principal(
                writer, principal_id="board-1", role_name=BOARD_CONTROL_ROLE
            )
            self.paper_book = open_company_paper_book(
                writer,
                account_id="paper-acct-1",
                book_name="Paper Book",
                starting_minor_units=50_000_000,
                exposure_limit_minor_units=25_000_000,
            )
            submit_company_paper_book_order(
                writer,
                binding_id=self.paper_book.binding_id,
                actor_id=self.founder_id,
                action_id="soak-1",
                instrument_id="BTC-USDT",
                side="buy",
                quantity=1,
                limit_minor_units=6_100_000,
                market_price_minor_units=6_000_000,
                fill_mode="immediate",
            )
            live_cand = register_live_candidate_binding(
                writer,
                binding_id="live-1",
                account_id="binance-live-1",
                destination_id="binance-spot-prod",
                connector_revision="binance-spot-v1",
                instrument_rules_digest="btc-usdt-spot-v1",
                currency="USD",
                starting_minor_units=25_000_000,
                exposure_limit_minor_units=10_000_000,
                credential_handle_ref="keychain:openboa/binance/live-1",
            )
            record_venue_qualification_receipt(
                writer,
                receipt_id="qual-1",
                adapter_revision="binance-spot-v1",
                environment=ENV_EXTERNAL_TEST,
                destination_id="binance-spot-prod",
                instrument_rules_digest="btc-usdt-spot-v1",
                core_digest=compute_trading_core_digest(),
                verdict="PASS",
                cases_passed=5,
                cases_failed=0,
                cases_unrun=0,
                evidence_sha256="a" * 64,
            )
            promote_execution_environment(
                writer,
                command_id="prom-1",
                charter_revision="charter-1",
                target_binding_id=live_cand.binding_id,
                qualification_receipt_id="qual-1",
                expected_payload_hash=build_promotion_payload_hash(
                    company_id=self.company_id,
                    charter_revision="charter-1",
                    target_binding=live_cand,
                    qualification_receipt_id="qual-1",
                ),
                reason="Promote live",
                principal_hint="board-1",
            )
            self.live_binding_id = live_cand.binding_id

    def test_environment_scoped_restrictions_isolate_paper_and_live(self) -> None:
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            # 1. execution:paper pause blocks paper orders but allows live fills!
            apply_company_restriction(
                writer,
                command_id="pause-paper",
                restriction_id="rest-paper",
                charter_revision="charter-1",
                expected_restriction_revision=0,
                cause_category=TRADING_RISK_CAUSE_CATEGORY,
                scope_ref=SCOPE_EXECUTION_PAPER,
                reason="Paper volatility stop",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
            )
            with closing(open_reader(self.store)) as db, read_tx(db):
                self.assertTrue(
                    is_environment_suspended(db, self.company_id, ENV_PAPER)
                )
                self.assertFalse(
                    is_environment_suspended(db, self.company_id, ENV_LIVE)
                )

            with self.assertRaisesRegex(
                ExecutionTierRefused, "blocked by active restriction"
            ):
                submit_company_paper_book_order(
                    writer,
                    binding_id=self.paper_book.binding_id,
                    actor_id=self.founder_id,
                    action_id="blocked-paper-1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=1,
                    limit_minor_units=6_100_000,
                    market_price_minor_units=6_000_000,
                )

            live_res = record_live_book_order_fill(
                writer,
                binding_id=self.live_binding_id,
                actor_id=self.founder_id,
                action_id="allowed-live-1",
                external_order_id="ext-ord-1",
                external_fill_id="ext-fill-1",
                instrument_id="BTC-USDT",
                side="buy",
                quantity=1,
                limit_minor_units=6_100_000,
                fill_price_minor_units=6_050_000,
                fee_minor_units=600,
            )
            self.assertFalse(live_res["replayed"])

            # Clear paper pause and verify paper resumes cleanly.
            clear_company_restriction(
                writer,
                command_id="clear-paper",
                restriction_id="rest-paper",
                charter_revision="charter-1",
                expected_restriction_revision=1,
                reason="Paper clear",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
            )
            paper_ok = submit_company_paper_book_order(
                writer,
                binding_id=self.paper_book.binding_id,
                actor_id=self.founder_id,
                action_id="resumed-paper-1",
                instrument_id="BTC-USDT",
                side="buy",
                quantity=1,
                limit_minor_units=6_100_000,
                market_price_minor_units=6_000_000,
            )
            self.assertEqual(paper_ok["status"], "filled")

            # 2. execution:live pause blocks live fills while allowing paper orders.
            apply_company_restriction(
                writer,
                command_id="pause-live",
                restriction_id="rest-live",
                charter_revision="charter-1",
                expected_restriction_revision=0,
                cause_category=TRADING_RISK_CAUSE_CATEGORY,
                scope_ref=SCOPE_EXECUTION_LIVE,
                reason="Live halt",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
            )
            with self.assertRaisesRegex(
                ExecutionTierRefused, "blocked by active restriction"
            ):
                record_live_book_order_fill(
                    writer,
                    binding_id=self.live_binding_id,
                    actor_id=self.founder_id,
                    action_id="blocked-live-2",
                    external_order_id="ext-ord-2",
                    external_fill_id="ext-fill-2",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=1,
                    limit_minor_units=6_100_000,
                    fill_price_minor_units=6_050_000,
                    fee_minor_units=600,
                )
            paper_ok2 = submit_company_paper_book_order(
                writer,
                binding_id=self.paper_book.binding_id,
                actor_id=self.founder_id,
                action_id="paper-still-ok",
                instrument_id="BTC-USDT",
                side="buy",
                quantity=1,
                limit_minor_units=6_100_000,
                market_price_minor_units=6_000_000,
            )
            self.assertEqual(paper_ok2["status"], "filled")

            # 3. execution:all blocks both paper & live.
            apply_company_restriction(
                writer,
                command_id="pause-all",
                restriction_id="rest-all",
                charter_revision="charter-1",
                expected_restriction_revision=0,
                cause_category=TRADING_RISK_CAUSE_CATEGORY,
                scope_ref=SCOPE_EXECUTION_ALL,
                reason="Emergency global execution halt",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
            )
            with self.assertRaisesRegex(
                ExecutionTierRefused, "blocked by active restriction"
            ):
                submit_company_paper_book_order(
                    writer,
                    binding_id=self.paper_book.binding_id,
                    actor_id=self.founder_id,
                    action_id="blocked-paper-all",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=1,
                    limit_minor_units=6_100_000,
                    market_price_minor_units=6_000_000,
                )

    def test_board_control_fails_closed_on_execution_scope_without_trading_guard(
        self,
    ) -> None:
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            with self.assertRaisesRegex(BoardControlRefused, "guard"):
                apply_company_restriction(
                    writer,
                    command_id="no-guard-pause",
                    restriction_id="rest-noguard",
                    charter_revision="charter-1",
                    expected_restriction_revision=0,
                    cause_category=TRADING_RISK_CAUSE_CATEGORY,
                    scope_ref=SCOPE_EXECUTION_PAPER,
                    reason="Missing guard",
                    principal_hint="board-1",
                    restriction_guards=(),
                )
