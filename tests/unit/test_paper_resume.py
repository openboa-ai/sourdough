"""Unit tests for unified Trading restriction resume preconditions (D6)."""

from __future__ import annotations

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns

from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    BoardControlRefused,
    apply_company_restriction,
    bind_board_principal,
    clear_company_restriction,
)
from sourdough.company import FoundingTerms, found
from sourdough.storage.db import open_writer, write_tx
from sourdough.storage.kernel import prepare_external_action
from sourdough.trading.accounts import open_company_paper_book
from sourdough.trading.engine import VERSION
from sourdough.trading.ledger import submit_company_paper_book_order
from sourdough.trading.market_worker import SpotInstrumentTerms
from sourdough.trading.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.trading.risk import (
    SCOPE_EXECUTION_ALL,
    SCOPE_EXECUTION_LIVE,
    SCOPE_EXECUTION_PAPER,
    TRADING_RISK_CAUSE_CATEGORY,
    TradingRestrictionGuard,
)
from sourdough.writer_lease import hold_company_writer


class TradingRestrictionResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="sd-resume-")))
        self.store = self.root / "c.sqlite"
        self.leases = self.root / "l"
        self.engine = self.root / "engine"
        founding = found(
            self.store, FoundingTerms("resume-co", "charter-1", "paper", "runtime")
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
            self.book = open_company_paper_book(
                writer,
                account_id="paper-resume-acct",
                book_name="Resume Paper",
                starting_minor_units=50_000_000,
                exposure_limit_minor_units=25_000_000,
            )

    def _sandbox_db(self) -> Path:
        sandbox_db = self.engine / f"nautilus-{VERSION}" / "sandbox.sqlite"
        sandbox_db.parent.mkdir(parents=True, exist_ok=True)
        return sandbox_db

    def _write_quote(self, *, event_ns: int) -> None:
        venue = NautilusPaperVenue(self._sandbox_db())
        terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        venue.provision(
            SandboxPlan(
                company_id=self.company_id,
                account_id="acct-1",
                terms=terms,
                starting_atoms=50_000_000,
                fee_bps=4,
            )
        )
        with venue._connect("rw") as vconn, vconn:
            vconn.execute(
                """INSERT OR REPLACE INTO sandbox_quote (
                       singleton, generation, price_atoms_per_lot,
                       received_ns, exchange_trade_ns, event_ns, bid_atoms_per_lot
                   ) VALUES (1, 'g-1', 6000000000, ?, ?, ?, 6000000000)""",
                (event_ns + 1_000_000, event_ns, event_ns),
            )

    def test_resume_blocks_when_open_orders_or_unresolved_external_actions_exist(
        self,
    ) -> None:
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            open_ord = submit_company_paper_book_order(
                writer,
                binding_id=self.book.binding_id,
                actor_id=self.founder_id,
                action_id="open-1",
                instrument_id="BTC-USDT",
                side="buy",
                quantity=1,
                limit_minor_units=5_900_000,
                market_price_minor_units=6_000_000,
                fill_mode="resting",
            )
            apply_company_restriction(
                writer,
                command_id="pause-1",
                restriction_id="rest-paper",
                charter_revision="charter-1",
                expected_restriction_revision=0,
                cause_category=TRADING_RISK_CAUSE_CATEGORY,
                scope_ref=SCOPE_EXECUTION_PAPER,
                reason="Risk pause",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
            )
            with self.assertRaisesRegex(BoardControlRefused, "open order"):
                clear_company_restriction(
                    writer,
                    command_id="clear-fail-open",
                    restriction_id="rest-paper",
                    charter_revision="charter-1",
                    expected_restriction_revision=1,
                    reason="Attempt early clear",
                    principal_hint="board-1",
                    restriction_guards=(self.guard,),
                )
            with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
                db.execute(
                    "UPDATE trading_book_order SET status = 'cancelled', cancelled_at_ns = 100 WHERE order_id = ?",
                    (open_ord["order_id"],),
                )
            # Without engine_directory, settled paper order clears cleanly.
            cleared = clear_company_restriction(
                writer,
                command_id="clear-ok",
                restriction_id="rest-paper",
                charter_revision="charter-1",
                expected_restriction_revision=1,
                reason="Settled clear",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
            )
            self.assertEqual(cleared.state, "applied")

            # Live pause blocks while unresolved external_actions exist.
            with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
                prepare_external_action(
                    db,
                    idempotency_key="ext-act-1",
                    company_id=self.company_id,
                    connector="binance",
                    operation="submit_order",
                    request_sha256="a" * 64,
                )

            apply_company_restriction(
                writer,
                command_id="pause-live",
                restriction_id="rest-live",
                charter_revision="charter-1",
                expected_restriction_revision=0,
                cause_category=TRADING_RISK_CAUSE_CATEGORY,
                scope_ref=SCOPE_EXECUTION_LIVE,
                reason="Live pause",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
            )
            with self.assertRaisesRegex(BoardControlRefused, "external action"):
                clear_company_restriction(
                    writer,
                    command_id="clear-live-fail",
                    restriction_id="rest-live",
                    charter_revision="charter-1",
                    expected_restriction_revision=1,
                    reason="Unresolved ext",
                    principal_hint="board-1",
                    restriction_guards=(self.guard,),
                )

    def test_resume_verifies_15s_quote_freshness_when_engine_sandbox_present(
        self,
    ) -> None:
        now_ns = time_ns() + 10_000_000_000
        self._write_quote(event_ns=now_ns - 20_000_000_000)

        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            apply_company_restriction(
                writer,
                command_id="pause-all",
                restriction_id="rest-all",
                charter_revision="charter-1",
                expected_restriction_revision=0,
                cause_category=TRADING_RISK_CAUSE_CATEGORY,
                scope_ref=SCOPE_EXECUTION_ALL,
                reason="Quote check",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
            )
            # Stale quote (>15s) fails closed.
            with self.assertRaisesRegex(BoardControlRefused, "fresh quote"):
                clear_company_restriction(
                    writer,
                    command_id="clear-stale",
                    restriction_id="rest-all",
                    charter_revision="charter-1",
                    expected_restriction_revision=1,
                    reason="Stale resume",
                    principal_hint="board-1",
                    restriction_guards=(self.guard,),
                    engine_directory=self.engine,
                    now_ns=now_ns,
                )

            # Update quote within 15s window -> clear succeeds!
            with NautilusPaperVenue(self._sandbox_db())._connect("rw") as vconn:
                vconn.execute(
                    "UPDATE sandbox_quote SET event_ns = ?, exchange_trade_ns = ?",
                    (now_ns - 5_000_000_000, now_ns - 5_000_000_000),
                )
                vconn.commit()
            cleared = clear_company_restriction(
                writer,
                command_id="clear-fresh",
                restriction_id="rest-all",
                charter_revision="charter-1",
                expected_restriction_revision=1,
                reason="Fresh quote clear",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
                engine_directory=self.engine,
                now_ns=now_ns,
            )
            self.assertEqual(cleared.state, "applied")

    def test_board_pause_on_company_all_clears_without_quotes(self) -> None:
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            apply_company_restriction(
                writer,
                command_id="bp-1",
                restriction_id="rest-bp",
                charter_revision="charter-1",
                expected_restriction_revision=0,
                cause_category="board_pause",
                scope_ref="company:all",
                reason="Manual hold",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
            )
            cleared = clear_company_restriction(
                writer,
                command_id="bp-clear",
                restriction_id="rest-bp",
                charter_revision="charter-1",
                expected_restriction_revision=1,
                reason="Release hold",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
                engine_directory=self.engine,
            )
            self.assertEqual(cleared.state, "applied")

    def test_live_restriction_resume_rejects_stale_sandbox_quote_over_15s_and_allows_fresh_quote(
        self,
    ) -> None:
        from sourdough.trading.accounts import (
            promote_execution_environment,
            record_venue_qualification_receipt,
            register_live_candidate_binding,
        )
        from sourdough.trading.domain import (
            ENV_EXTERNAL_TEST,
            build_promotion_payload_hash,
            compute_trading_core_digest,
        )

        now_ns = time_ns() + 10_000_000_000
        self._write_quote(event_ns=now_ns - 20_000_000_000)

        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            submit_company_paper_book_order(
                writer,
                binding_id=self.book.binding_id,
                actor_id=self.founder_id,
                action_id="live-soak-1",
                instrument_id="BTC-USDT",
                side="buy",
                quantity=1,
                limit_minor_units=6_100_000,
                market_price_minor_units=6_000_000,
                quote_observed_at_ns=now_ns - 2_000_000_000,
                now_ns=now_ns - 2_000_000_000,
            )
            live_cand = register_live_candidate_binding(
                writer,
                binding_id="live-resume-cand",
                account_id="binance-live-resume",
                destination_id="binance-spot-prod",
                connector_revision="binance-spot-v1",
                instrument_rules_digest="btc-usdt-spot-v1",
                currency="USD",
                starting_minor_units=25_000_000,
                exposure_limit_minor_units=10_000_000,
                credential_handle_ref="keychain:openboa/binance/resume",
            )
            record_venue_qualification_receipt(
                writer,
                receipt_id="qual-live-resume",
                adapter_revision="binance-spot-v1",
                environment=ENV_EXTERNAL_TEST,
                destination_id="binance-spot-testnet",
                instrument_rules_digest="btc-usdt-spot-v1",
                core_digest=compute_trading_core_digest(),
                verdict="PASS",
                cases_passed=5,
                cases_failed=0,
                cases_unrun=0,
                evidence_sha256="b" * 64,
            )
            promote_execution_environment(
                writer,
                command_id="prom-live-resume",
                charter_revision="charter-1",
                target_binding_id=live_cand.binding_id,
                qualification_receipt_id="qual-live-resume",
                expected_payload_hash=build_promotion_payload_hash(
                    company_id=self.company_id,
                    charter_revision="charter-1",
                    target_binding=live_cand,
                    qualification_receipt_id="qual-live-resume",
                ),
                reason="Activate live binding",
                principal_hint="board-1",
            )
            apply_company_restriction(
                writer,
                command_id="pause-live-quote",
                restriction_id="rest-live-quote",
                charter_revision="charter-1",
                expected_restriction_revision=0,
                cause_category=TRADING_RISK_CAUSE_CATEGORY,
                scope_ref=SCOPE_EXECUTION_LIVE,
                reason="Live quote freshness gate",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
            )
            # Even with a recent live book_init valuation, stale sandbox_quote (>15s) must fail closed.
            with self.assertRaisesRegex(BoardControlRefused, "fresh quote"):
                clear_company_restriction(
                    writer,
                    command_id="clear-live-stale",
                    restriction_id="rest-live-quote",
                    charter_revision="charter-1",
                    expected_restriction_revision=1,
                    reason="Attempt stale live resume",
                    principal_hint="board-1",
                    restriction_guards=(self.guard,),
                    engine_directory=self.engine,
                    now_ns=now_ns,
                )

            with NautilusPaperVenue(self._sandbox_db())._connect("rw") as vconn:
                vconn.execute(
                    "UPDATE sandbox_quote SET event_ns = ?, exchange_trade_ns = ?",
                    (now_ns - 5_000_000_000, now_ns - 5_000_000_000),
                )
                vconn.commit()
            cleared = clear_company_restriction(
                writer,
                command_id="clear-live-fresh",
                restriction_id="rest-live-quote",
                charter_revision="charter-1",
                expected_restriction_revision=1,
                reason="Fresh quote live resume",
                principal_hint="board-1",
                restriction_guards=(self.guard,),
                engine_directory=self.engine,
                now_ns=now_ns,
            )
            self.assertEqual(cleared.state, "applied")
