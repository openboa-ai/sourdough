import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    BOARD_VIEWER_ROLE,
    BoardControlConflict,
    BoardControlForbidden,
    BoardControlRefused,
    bind_board_principal,
)
from sourdough.company import FoundingTerms, found
from sourdough.execution_tiers import (
    ENV_EXTERNAL_TEST,
    ENV_LIVE,
    ENV_PAPER,
    TIER_AGENT_SANDBOX,
    TIER_COMPANY_LIVE,
    TIER_COMPANY_PAPER,
    AgentSandboxTools,
    ExecutionTierRefused,
    build_promotion_payload_hash,
    cancel_agent_sandbox_order,
    compute_trading_core_digest,
    demote_live_environment,
    open_agent_sandbox,
    promote_execution_environment,
    read_agent_sandbox_portfolio,
    record_venue_qualification_receipt,
    register_company_paper_tier,
    register_live_candidate_binding,
    reset_agent_sandbox,
    submit_agent_sandbox_order,
    sync_company_paper_from_legacy_binding,
)
from sourdough.market_worker import SpotInstrumentTerms
from sourdough.nautilus_paper import SandboxPlan
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_execution import VenueRefused
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer


class ExecutionTiersTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.leases = self.root / "leases"
        self.founding = found(
            self.store,
            FoundingTerms("tiered-fund", "charter-v1", "paper", "profile-v1"),
        )
        self.company_id = self.founding.company_id
        self.founder_id = self.founding.founder_id
        self.second_agent = "agent-quant"
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
                db.execute(
                    "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                    (self.second_agent, self.company_id, "profile-v1"),
                )
                db.execute(
                    "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                    ("grant-quant", self.second_agent, "paper"),
                )
            bind_board_principal(
                writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
            )
            bind_board_principal(
                writer, principal_id="board-auditor", role_name=BOARD_VIEWER_ROLE
            )

    def _seed_paper_soak(self, writer) -> None:
        venue = FixedQuoteVenue(self.root / "venue.sqlite")
        account = venue.provision(
            PaperAccount(
                account_id="company-paper-acct",
                connector_revision="fixed-quote-v1",
                instrument_rules_digest="btc-spot-v1",
                currency="USD",
                starting_minor_units=50_000_000,
                environment="paper",
                destination_id="",
            ),
            quote_minor_units=6_000_000,
            fee_bps=4,
        )
        bind(
            self.store,
            PaperMandate(
                revision="paper-rev-1",
                company_id=self.company_id,
                charter_revision="charter-v1",
                account=account,
                exposure_limit_minor_units=25_000_000,
            ),
            venue,
        )
        sync_company_paper_from_legacy_binding(writer, fee_bps=4, slippage_bps=2)
        with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
            db.execute(
                """INSERT INTO paper_valuation (
                       capture_id, binding_revision, policy, evidence,
                       trading_nav_atoms, revision
                   ) VALUES ('val-1', 'paper-rev-1', 'mid', '{"label":"Day1"}', 50120000, 'vrev-1')"""
            )

    def test_nautilus_sandbox_plan_accepts_realistic_fee_and_slippage_bounds(self):
        terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        zero_plan = SandboxPlan("c1", "paper", terms, 1000 * 10**8, 0)
        self.assertEqual((zero_plan.fee_bps, zero_plan.slippage_bps), (0, 0))
        realistic = SandboxPlan("c1", "paper", terms, 1000 * 10**8, 10, None, 5)
        self.assertEqual((realistic.fee_bps, realistic.slippage_bps), (10, 5))
        with self.assertRaises(VenueRefused):
            SandboxPlan("c1", "paper", terms, 1000 * 10**8, 1001)
        with self.assertRaises(VenueRefused):
            SandboxPlan("c1", "paper", terms, 1000 * 10**8, 10, None, 501)

    def test_two_agents_run_isolated_sandboxes_without_touching_company_books(self):
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            self._seed_paper_soak(writer)
            sbx_a = open_agent_sandbox(
                writer,
                agent_id=self.founder_id,
                account_id="founder-sbx-1",
                starting_minor_units=10_000_000,
                exposure_limit_minor_units=8_000_000,
                fee_bps=10,
                slippage_bps=5,
            )
            sbx_b = open_agent_sandbox(
                writer,
                agent_id=self.second_agent,
                account_id="quant-sbx-1",
                starting_minor_units=5_000_000,
                exposure_limit_minor_units=4_000_000,
                fee_bps=4,
                slippage_bps=2,
            )
            self.assertEqual(sbx_a.tier, TIER_AGENT_SANDBOX)
            self.assertEqual(sbx_b.tier, TIER_AGENT_SANDBOX)
            self.assertNotEqual(sbx_a.binding_id, sbx_b.binding_id)

            # Agent A buys 2 units at market 1,000,000 (5 bps adverse slippage -> 1,000,500)
            buy_a = submit_agent_sandbox_order(
                writer,
                agent_id=self.founder_id,
                action_id="act-a-buy-1",
                instrument_id="BTC-USD",
                side="buy",
                quantity=2,
                limit_minor_units=1_010_000,
                market_price_minor_units=1_000_000,
            )
            self.assertEqual(buy_a.status, "filled")
            self.assertEqual(buy_a.fill_price_minor_units, 1_000_500)
            self.assertEqual(buy_a.fee_minor_units, 2_001)

            # Idempotent replay returns identical receipt; conflicting terms fail.
            replay_a = submit_agent_sandbox_order(
                writer,
                agent_id=self.founder_id,
                action_id="act-a-buy-1",
                instrument_id="BTC-USD",
                side="buy",
                quantity=2,
                limit_minor_units=1_010_000,
                market_price_minor_units=1_000_000,
            )
            self.assertTrue(replay_a.replayed)
            self.assertEqual(replay_a.order_id, buy_a.order_id)
            with self.assertRaises(ExecutionTierRefused):
                submit_agent_sandbox_order(
                    writer,
                    agent_id=self.founder_id,
                    action_id="act-a-buy-1",
                    instrument_id="BTC-USD",
                    side="buy",
                    quantity=3,
                    limit_minor_units=1_010_000,
                    market_price_minor_units=1_000_000,
                )

            # Agent A closes 1 unit at market 1,100,000 (5 bps downward slippage -> 1,099,450)
            sell_a = submit_agent_sandbox_order(
                writer,
                agent_id=self.founder_id,
                action_id="act-a-sell-1",
                instrument_id="BTC-USD",
                side="sell",
                quantity=1,
                limit_minor_units=1_090_000,
                market_price_minor_units=1_100_000,
            )
            self.assertEqual(sell_a.status, "filled")
            self.assertEqual(sell_a.fill_price_minor_units, 1_099_450)
            self.assertGreater(sell_a.realized_pnl_minor_units, 0)

            # Resting limit order & cancellation
            resting = submit_agent_sandbox_order(
                writer,
                agent_id=self.second_agent,
                action_id="act-b-rest-1",
                instrument_id="ETH-USD",
                side="buy",
                quantity=1,
                limit_minor_units=300_000,
                market_price_minor_units=350_000,
            )
            self.assertEqual(resting.status, "open")
            # Cross-agent cancellation must fail!
            with self.assertRaises(ExecutionTierRefused):
                cancel_agent_sandbox_order(
                    writer, agent_id=self.founder_id, order_id=resting.order_id
                )
            cancelled = cancel_agent_sandbox_order(
                writer, agent_id=self.second_agent, order_id=resting.order_id
            )
            self.assertEqual(cancelled["status"], "cancelled")

            # Broker spoofing rejected
            broker = AgentSandboxTools(writer=writer, agent_id=self.second_agent)
            with self.assertRaises(ExecutionTierRefused):
                broker.invoke(
                    "read_agent_sandbox_portfolio", {"agent_id": self.founder_id}
                )
            p_b = broker.invoke("read_agent_sandbox_portfolio", {})["portfolio"]
            self.assertEqual(p_b["binding"]["account_id"], "quant-sbx-1")
            self.assertEqual(p_b["fill_count"], 0)

            # Reset retires prior sandbox binding and opens a clean one.
            sbx_a2 = reset_agent_sandbox(
                writer,
                agent_id=self.founder_id,
                new_account_id="founder-sbx-2",
                starting_minor_units=20_000_000,
            )
            self.assertNotEqual(sbx_a2.binding_id, sbx_a.binding_id)
            p_a2 = read_agent_sandbox_portfolio(
                self.store, self.company_id, self.founder_id
            )
            self.assertEqual(p_a2["cash_minor_units"], 20_000_000)
            self.assertEqual(p_a2["fill_count"], 0)

        # Verify zero contamination of company official books!
        with closing(open_reader(self.store)) as db, read_tx(db):
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM paper_order").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM paper_fill").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM journal_entry").fetchone()[0], 0
            )

    def test_retired_agent_cannot_open_or_trade_sandbox(self):
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
                db.execute(
                    """INSERT INTO agent_archive (
                           company_id, agent_id, manager_id, grant_id,
                           request_id, employment_revision, reason, previous_scope
                       ) VALUES (?, ?, ?, ?, 'retire-1', 'emp-1', 'retired', 'paper')""",
                    (
                        self.company_id,
                        self.second_agent,
                        self.founder_id,
                        "grant-quant",
                    ),
                )
            with self.assertRaisesRegex(ExecutionTierRefused, "Retired agent"):
                open_agent_sandbox(
                    writer,
                    agent_id=self.second_agent,
                    account_id="retired-sbx",
                )

    def test_live_promotion_gate_enforces_all_fail_closed_invariants_and_parallel_tiers(
        self,
    ):
        core_digest = compute_trading_core_digest()
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            # 1. Raw secret and paper/testnet handle names must be rejected at registration.
            with self.assertRaisesRegex(ExecutionTierRefused, "opaque"):
                register_live_candidate_binding(
                    writer,
                    binding_id="live-bad-secret",
                    account_id="binance-live-1",
                    destination_id="binance-spot-live",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-spot-v1",
                    currency="USD",
                    starting_minor_units=10_000_000,
                    exposure_limit_minor_units=5_000_000,
                    credential_handle_ref="sk_live_raw_secret_value_12345",
                )
            with self.assertRaisesRegex(ExecutionTierRefused, "paper, testnet"):
                register_live_candidate_binding(
                    writer,
                    binding_id="live-bad-handle",
                    account_id="binance-live-1",
                    destination_id="binance-spot-live",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-spot-v1",
                    currency="USD",
                    starting_minor_units=10_000_000,
                    exposure_limit_minor_units=5_000_000,
                    credential_handle_ref="envref:BINANCE_TESTNET_KEY",
                )

            # 2. Register company_paper WITHOUT soak evidence yet.
            register_company_paper_tier(
                writer,
                account_id="company-paper-acct",
                destination_id="nautilus-local-1",
                connector_revision="nautilus-spot-sandbox-v1",
                instrument_rules_digest="btc-spot-v1",
                currency="USD",
                starting_minor_units=50_000_000,
                exposure_limit_minor_units=25_000_000,
                fee_bps=4,
                slippage_bps=2,
            )
            # Reusing company_paper account_id on live candidate must fail.
            with self.assertRaisesRegex(ExecutionTierRefused, "cannot reuse"):
                register_live_candidate_binding(
                    writer,
                    binding_id="live-collide",
                    account_id="company-paper-acct",
                    destination_id="binance-spot-live",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-spot-v1",
                    currency="USD",
                    starting_minor_units=10_000_000,
                    exposure_limit_minor_units=5_000_000,
                    credential_handle_ref="keychain:openboa/binance/live-main",
                )

            live_cand = register_live_candidate_binding(
                writer,
                binding_id="live-cand-1",
                account_id="binance-live-main",
                destination_id="binance-spot-live",
                connector_revision="binance-spot-v1",
                instrument_rules_digest="btc-spot-v1",
                currency="USD",
                starting_minor_units=10_000_000,
                exposure_limit_minor_units=5_000_000,
                credential_handle_ref="keychain:openboa/binance/live-main",
            )
            self.assertEqual(live_cand.status, "registered")
            self.assertEqual(live_cand.tier, TIER_COMPANY_LIVE)
            self.assertEqual(live_cand.environment, ENV_LIVE)

            # Qualification receipt on paper (not external_test) must not unlock live!
            paper_qual = record_venue_qualification_receipt(
                writer,
                receipt_id="qual-paper-only",
                adapter_revision="binance-spot-v1",
                environment=ENV_PAPER,
                destination_id="nautilus-local-1",
                instrument_rules_digest="btc-spot-v1",
                core_digest=core_digest,
                verdict="PASS",
                cases_passed=6,
                cases_failed=0,
                cases_unrun=0,
                evidence_sha256="a" * 64,
            )
            # PASS external_test qualification receipt
            ext_qual = record_venue_qualification_receipt(
                writer,
                receipt_id="qual-testnet-pass",
                adapter_revision="binance-spot-v1",
                environment=ENV_EXTERNAL_TEST,
                destination_id="binance-spot-testnet",
                instrument_rules_digest="btc-spot-v1",
                core_digest=core_digest,
                verdict="PASS",
                cases_passed=6,
                cases_failed=0,
                cases_unrun=0,
                evidence_sha256="b" * 64,
            )

            valid_hash = build_promotion_payload_hash(
                company_id=self.company_id,
                charter_revision="charter-v1",
                target_binding=live_cand,
                qualification_receipt_id=ext_qual.receipt_id,
            )

            # board_viewer is forbidden (403)
            with self.assertRaises(BoardControlForbidden):
                promote_execution_environment(
                    writer,
                    command_id="cmd-viewer-deny",
                    charter_revision="charter-v1",
                    target_binding_id=live_cand.binding_id,
                    qualification_receipt_id=ext_qual.receipt_id,
                    expected_payload_hash=valid_hash,
                    reason="Viewer attempting promotion",
                    principal_hint="board-auditor",
                )

            # Missing company_paper soak evidence refuses promotion!
            with self.assertRaisesRegex(BoardControlRefused, "soak evidence"):
                promote_execution_environment(
                    writer,
                    command_id="cmd-no-soak",
                    charter_revision="charter-v1",
                    target_binding_id=live_cand.binding_id,
                    qualification_receipt_id=ext_qual.receipt_id,
                    expected_payload_hash=valid_hash,
                    reason="Promote before paper soak",
                    principal_hint="board-chair",
                )

            # Seed paper soak valuation now!
            with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
                db.execute(
                    """INSERT INTO paper_valuation (
                           capture_id, binding_revision, policy, evidence,
                           trading_nav_atoms, revision
                       ) VALUES ('val-1', 'paper-rev-1', 'mid', '{"label":"Day1"}', 50120000, 'vrev-1')"""
                )

            # Paper-only qualification receipt is refused!
            paper_qual_hash = build_promotion_payload_hash(
                company_id=self.company_id,
                charter_revision="charter-v1",
                target_binding=live_cand,
                qualification_receipt_id=paper_qual.receipt_id,
            )
            with self.assertRaisesRegex(BoardControlRefused, "external_test"):
                promote_execution_environment(
                    writer,
                    command_id="cmd-wrong-qual-env",
                    charter_revision="charter-v1",
                    target_binding_id=live_cand.binding_id,
                    qualification_receipt_id=paper_qual.receipt_id,
                    expected_payload_hash=paper_qual_hash,
                    reason="Paper qual cannot unlock live",
                    principal_hint="board-chair",
                )

            # Tampered/stale payload hash records persisted conflicting_revision denial!
            stale = promote_execution_environment(
                writer,
                command_id="cmd-stale-hash",
                charter_revision="charter-v1",
                target_binding_id=live_cand.binding_id,
                qualification_receipt_id=ext_qual.receipt_id,
                expected_payload_hash="0" * 64,
                reason="Stale hash attempt",
                principal_hint="board-chair",
            )
            self.assertEqual(stale.state, "denied")
            self.assertEqual(stale.denial_code, "conflicting_revision")

            # Open an Agent sandbox too so we verify 3-tier coexistence!
            open_agent_sandbox(
                writer,
                agent_id=self.founder_id,
                account_id="founder-sbx-live-coexist",
            )

            # Valid promotion succeeds!
            applied = promote_execution_environment(
                writer,
                command_id="cmd-promote-1",
                charter_revision="charter-v1",
                target_binding_id=live_cand.binding_id,
                qualification_receipt_id=ext_qual.receipt_id,
                expected_payload_hash=valid_hash,
                reason="Paper soak and Binance testnet qualification passed",
                principal_hint="board-chair",
            )
            self.assertEqual(applied.state, "applied")
            self.assertTrue(applied.result["company_paper_preserved"])
            self.assertTrue(applied.result["agent_sandboxes_preserved"])

            # Idempotent replay succeeds; conflicting command_id raises BoardControlConflict!
            replayed = promote_execution_environment(
                writer,
                command_id="cmd-promote-1",
                charter_revision="charter-v1",
                target_binding_id=live_cand.binding_id,
                qualification_receipt_id=ext_qual.receipt_id,
                expected_payload_hash=valid_hash,
                reason="Paper soak and Binance testnet qualification passed",
                principal_hint="board-chair",
            )
            self.assertEqual(replayed.state, "applied")
            with self.assertRaises(BoardControlConflict):
                promote_execution_environment(
                    writer,
                    command_id="cmd-promote-1",
                    charter_revision="charter-v1",
                    target_binding_id=live_cand.binding_id,
                    qualification_receipt_id=ext_qual.receipt_id,
                    expected_payload_hash=valid_hash,
                    reason="Different reason reusing command_id",
                    principal_hint="board-chair",
                )

            # Verify all 3 tiers active simultaneously!
            with closing(open_reader(self.store)) as db, read_tx(db):
                active_tiers = {
                    str(r[0])
                    for r in db.execute(
                        "SELECT tier FROM execution_environment_binding WHERE status = 'active'"
                    ).fetchall()
                }
            self.assertEqual(
                active_tiers,
                {TIER_AGENT_SANDBOX, TIER_COMPANY_PAPER, TIER_COMPANY_LIVE},
            )

            # Emergency suspend & demote leaves company_paper and agent_sandbox intact!
            suspended = demote_live_environment(
                writer,
                command_id="cmd-suspend-live",
                charter_revision="charter-v1",
                target_binding_id=live_cand.binding_id,
                action="suspend_live",
                reason="Emergency volatility pause on live",
                principal_hint="board-chair",
            )
            self.assertEqual(suspended.state, "applied")
            self.assertEqual(suspended.result["new_status"], "suspended")

            demoted = demote_live_environment(
                writer,
                command_id="cmd-demote-paper",
                charter_revision="charter-v1",
                target_binding_id=live_cand.binding_id,
                action="demote_to_paper",
                reason="Return company to paper-only mode",
                principal_hint="board-chair",
            )
            self.assertEqual(demoted.state, "applied")
            self.assertEqual(demoted.result["new_status"], "retired")

            with closing(open_reader(self.store)) as db, read_tx(db):
                active_after = {
                    str(r[0])
                    for r in db.execute(
                        "SELECT tier FROM execution_environment_binding WHERE status = 'active'"
                    ).fetchall()
                }
            self.assertEqual(active_after, {TIER_AGENT_SANDBOX, TIER_COMPANY_PAPER})


if __name__ == "__main__":
    unittest.main()
