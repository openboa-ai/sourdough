"""Connected Flow test for three-tier execution accounts and live promotion gate."""

from __future__ import annotations

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    BOARD_VIEWER_ROLE,
    bind_board_principal,
)
from sourdough.company import FoundingTerms, found
from sourdough.execution_tiers import (
    DEMOTE_LIVE_ENVIRONMENT,
    ENV_EXTERNAL_TEST,
    EXECUTION_TIER_COMMAND,
    PROMOTE_EXECUTION_ENVIRONMENT,
    AgentSandboxTools,
    build_promotion_payload_hash,
    compute_trading_core_digest,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
    sync_company_paper_from_legacy_binding,
)
from sourdough.ipc import CompanyDaemon, ControlPlaneClient
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with TemporaryDirectory(prefix="sd-tier-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            sock_dir = root / "s"
            venue = FixedQuoteVenue(root / "v.sqlite")

            founding = found(
                store,
                FoundingTerms("sourdough-fund", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            founder_id = founding.founder_id
            quant_id = "agent-quant"

            # 1. Seed company paper + soak valuation + 2 agents + Board RBAC.
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    db.execute(
                        "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                        (quant_id, company_id, "profile-v1"),
                    )
                    db.execute(
                        "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                        ("grant-quant", quant_id, "paper"),
                    )
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )
                bind_board_principal(
                    writer, principal_id="board-viewer", role_name=BOARD_VIEWER_ROLE
                )

                paper_acct = venue.provision(
                    PaperAccount(
                        account_id="company-paper-us01",
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
                        revision="mandate-paper-v1",
                        company_id=company_id,
                        charter_revision="charter-v1",
                        account=paper_acct,
                        exposure_limit_minor_units=50_000_000,
                    ),
                    venue,
                )
                sync_company_paper_from_legacy_binding(
                    writer, fee_bps=4, slippage_bps=2
                )
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    db.execute(
                        """INSERT INTO paper_valuation (
                               capture_id, binding_revision, policy, evidence,
                               trading_nav_atoms, revision
                           ) VALUES ('v-soak-1', 'mandate-paper-v1', 'mid',
                                     '{"label":"SoakCut1"}', 100450000, 'vrev-soak-1')"""
                    )

                # 2. Both agents independently run personal Tier 1 paper sandboxes via Outer Broker.
                founder_tools = AgentSandboxTools(writer=writer, agent_id=founder_id)
                quant_tools = AgentSandboxTools(writer=writer, agent_id=quant_id)
                founder_tools.invoke(
                    "open_agent_sandbox",
                    {
                        "account_id": "founder-sandbox-01",
                        "starting_minor_units": 10_000_000,
                        "fee_bps": 4,
                        "slippage_bps": 2,
                    },
                )
                quant_tools.invoke(
                    "open_agent_sandbox",
                    {
                        "account_id": "quant-sandbox-01",
                        "starting_minor_units": 8_000_000,
                        "fee_bps": 6,
                        "slippage_bps": 3,
                    },
                )
                f_order = founder_tools.invoke(
                    "submit_agent_sandbox_order",
                    {
                        "action_id": "founder-exp-1",
                        "instrument_id": "BTC-USDT",
                        "side": "buy",
                        "quantity": 1,
                        "limit_minor_units": 6_600_000,
                        "market_price_minor_units": 6_500_000,
                    },
                )
                q_order = quant_tools.invoke(
                    "submit_agent_sandbox_order",
                    {
                        "action_id": "quant-exp-1",
                        "instrument_id": "ETH-USDT",
                        "side": "buy",
                        "quantity": 2,
                        "limit_minor_units": 350_000,
                        "market_price_minor_units": 340_000,
                    },
                )
                self.assertEqual(f_order["status"], "filled")
                self.assertEqual(q_order["status"], "filled")

                # 3. Register dormant Tier 3 live candidate + PASS external_test qualification receipt.
                live_cand = register_live_candidate_binding(
                    writer,
                    binding_id="binance-live-main",
                    account_id="binance-prod-acct-01",
                    destination_id="binance-spot-prod",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=10_000_000,
                    credential_handle_ref="keychain:openboa/binance/prod-01",
                )
                qual = record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-binance-testnet-v1",
                    adapter_revision="binance-spot-v1",
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

            # 4. Boot resident UDS CompanyDaemon and verify projections & fail-closed promotion over IPC!
            with CompanyDaemon(
                store, company_id, socket_dir=sock_dir, lease_directory=leases
            ) as daemon:
                client = ControlPlaneClient(daemon.socket_path)
                meta = client.meta(principal="board-chair")
                self.assertIn("execution-tiers-v1", meta["supported_projections"])
                self.assertIn(PROMOTE_EXECUTION_ENVIRONMENT, meta["supported_commands"])
                self.assertIn(DEMOTE_LIVE_ENVIRONMENT, meta["supported_commands"])

                status, _etag, tiers = client.projection(
                    "execution-tiers", expected_company_id=company_id
                )
                self.assertEqual(status, 200)
                assert tiers is not None
                self.assertTrue(tiers["company_paper_soak_ready"])
                self.assertIsNone(tiers["company_live"])
                self.assertEqual(len(tiers["agent_sandboxes"]), 2)
                self.assertEqual(len(tiers["live_candidates"]), 1)

                # Agent detail projection exposes that agent's private sandbox summary.
                st_a, _, detail_q = client.projection(
                    f"agents/{quant_id}", expected_company_id=company_id
                )
                self.assertEqual(st_a, 200)
                assert detail_q is not None
                self.assertEqual(
                    detail_q["agent_sandbox"]["binding"]["account_id"],
                    "quant-sandbox-01",
                )
                self.assertEqual(detail_q["agent_sandbox"]["fill_count"], 1)

                payload_hash = build_promotion_payload_hash(
                    company_id=company_id,
                    charter_revision="charter-v1",
                    target_binding=live_cand,
                    qualification_receipt_id=qual.receipt_id,
                )

                # Viewer is rejected 403 over UDS.
                code_v, _ = client.submit_command(
                    company_id=company_id,
                    command_id="ipc-promote-viewer",
                    command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                    contract_version=EXECUTION_TIER_COMMAND,
                    preconditions={
                        "charter_revision": "charter-v1",
                        "target_binding_id": live_cand.binding_id,
                        "qualification_receipt_id": qual.receipt_id,
                        "expected_payload_hash": payload_hash,
                    },
                    payload={"reason": "Unauthorized viewer attempt"},
                    principal="board-viewer",
                )
                self.assertEqual(code_v, 403)

                # Board Chair promotes company_live cleanly over UDS!
                code_ok, resp_ok = client.submit_command(
                    company_id=company_id,
                    command_id="ipc-promote-ok",
                    command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                    contract_version=EXECUTION_TIER_COMMAND,
                    preconditions={
                        "charter_revision": "charter-v1",
                        "target_binding_id": live_cand.binding_id,
                        "qualification_receipt_id": qual.receipt_id,
                        "expected_payload_hash": payload_hash,
                    },
                    payload={"reason": "Board approved Prod activation after soak"},
                    principal="board-chair",
                )
                self.assertEqual(code_ok, 200)
                self.assertEqual(resp_ok["state"], "applied")

                # Verify all 3 tiers coexist active simultaneously!
                _, _, tiers_live = client.projection(
                    "execution-tiers", expected_company_id=company_id
                )
                assert tiers_live is not None
                self.assertEqual(
                    tiers_live["company_live"]["binding_id"], live_cand.binding_id
                )
                self.assertEqual(tiers_live["company_live"]["status"], "active")
                self.assertEqual(tiers_live["company_paper"]["status"], "active")
                self.assertEqual(len(tiers_live["agent_sandboxes"]), 2)

                # Emergency demote back to paper-only over UDS!
                code_dem, resp_dem = client.submit_command(
                    company_id=company_id,
                    command_id="ipc-demote-paper",
                    command_type=DEMOTE_LIVE_ENVIRONMENT,
                    contract_version=EXECUTION_TIER_COMMAND,
                    preconditions={
                        "charter_revision": "charter-v1",
                        "target_binding_id": live_cand.binding_id,
                    },
                    payload={
                        "action": "demote_to_paper",
                        "reason": "Returning to paper-only after validation",
                    },
                    principal="board-chair",
                )
                self.assertEqual(code_dem, 200)
                self.assertEqual(resp_dem["state"], "applied")

                _, _, tiers_after = client.projection(
                    "execution-tiers", expected_company_id=company_id
                )
                assert tiers_after is not None
                self.assertIsNone(tiers_after["company_live"])
                self.assertEqual(tiers_after["company_paper"]["status"], "active")
                self.assertEqual(len(tiers_after["agent_sandboxes"]), 2)
                self.assertEqual(len(tiers_after["promotion_history"]), 2)

            # Confirm zero agent sandbox leakage into company investment book!
            with closing(open_reader(store)) as db, read_tx(db):
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM paper_order").fetchone()[0], 0
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM paper_fill").fetchone()[0], 0
                )


if __name__ == "__main__":
    unittest.main()
