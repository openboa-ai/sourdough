"""Connected Flow test for Binance external_test qualification -> Board live promotion."""

from __future__ import annotations

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import BOARD_CONTROL_ROLE, bind_board_principal
from sourdough.company import FoundingTerms, found
from sourdough.ipc import CompanyDaemon, ControlPlaneClient
from sourdough.storage.db import open_reader, read_tx
from sourdough.trading.accounts import (
    open_agent_sandbox,
    register_company_paper_tier,
    register_live_candidate_binding,
)
from sourdough.trading.binance_emulator import BinanceWireEmulator
from sourdough.trading.binance_venue import (
    MARKET_USDM_FUTURES,
    BinanceVenueAdapter,
    BinanceVenueConfig,
)
from sourdough.trading.commands import (
    EXECUTION_TIER_COMMAND,
    PROMOTE_EXECUTION_ENVIRONMENT,
)
from sourdough.trading.migration import build_promotion_payload_hash
from sourdough.trading.credentials import InMemoryTestCredentialProvider
from sourdough.trading.domain import ENV_EXTERNAL_TEST
from sourdough.trading.ledger import record_trading_book_valuation
from sourdough.trading.venue_qualification import run_binance_testnet_qualification
from sourdough.writer_lease import hold_company_writer


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        secret_canary = "canary-never-persist-in-sqlite-9981"
        with TemporaryDirectory(prefix="sd-venue-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            sock_dir = root / "s"

            founding = found(
                store,
                FoundingTerms("venue-qual-fund", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            founder_id = founding.founder_id
            inst_digest = "binance-usdm-btcusdt-v1"

            creds = InMemoryTestCredentialProvider(
                {
                    "envref:BINANCE_TESTNET_USDM": (
                        "emul-usdm-key",
                        secret_canary,
                    )
                }
            )

            with (
                BinanceWireEmulator(
                    api_key="emul-usdm-key",
                    api_secret=secret_canary,
                ) as emul,
                hold_company_writer(
                    store, company_id, lease_directory=leases
                ) as writer,
            ):
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )
                # Tier 1 + Tier 2 + paper soak evidence
                open_agent_sandbox(
                    writer, agent_id=founder_id, account_id="founder-sbx-01"
                )
                paper_binding = register_company_paper_tier(
                    writer,
                    account_id="company-paper-usdm-01",
                    destination_id="nautilus-usdm-paper",
                    connector_revision="nautilus-spot-sandbox-v1",
                    instrument_rules_digest=inst_digest,
                    currency="USD",
                    starting_minor_units=100_000_000,
                    exposure_limit_minor_units=50_000_000,
                    fee_bps=4,
                    slippage_bps=2,
                )
                record_trading_book_valuation(
                    writer,
                    binding_id=paper_binding.binding_id,
                    label="Soak",
                    mark_prices={},
                )

                # Run full 6-case Binance USD-M Futures testnet qualification against wire emulator!
                adapter = BinanceVenueAdapter(
                    BinanceVenueConfig(
                        environment=ENV_EXTERNAL_TEST,
                        market_kind=MARKET_USDM_FUTURES,
                        account_id="binance-usdm-testnet-01",
                        destination_id="binance-usdm-testnet",
                        credential_handle_ref="envref:BINANCE_TESTNET_USDM",
                        base_url_override=emul.base_url,
                        allow_loopback_override=True,
                    ),
                    creds,
                )
                report = run_binance_testnet_qualification(
                    writer,
                    adapter=adapter,
                    emulator=emul,
                    instrument_rules_digest=inst_digest,
                    symbol="BTCUSDT",
                    receipt_id="qual-usdm-pass-01",
                )
                self.assertEqual(report.receipt.verdict, "PASS")
                self.assertEqual(report.receipt.cases_passed, 6)

                # Register dormant Tier 3 live candidate using opaque keychain handle (no live key injected!)
                live_cand = register_live_candidate_binding(
                    writer,
                    binding_id="binance-usdm-live-01",
                    account_id="binance-usdm-prod-01",
                    destination_id="binance-usdm-prod",
                    connector_revision=adapter.capabilities.adapter_revision,
                    instrument_rules_digest=inst_digest,
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=10_000_000,
                    credential_handle_ref="keychain:openboa/binance-usdm-prod",
                )

            # Promote Tier 3 company_live over live UDS Control Plane using the sealed qualification receipt!
            with CompanyDaemon(
                store, company_id, socket_dir=sock_dir, lease_directory=leases
            ) as daemon:
                client = ControlPlaneClient(daemon.socket_path)
                payload_hash = build_promotion_payload_hash(
                    company_id=company_id,
                    charter_revision="charter-v1",
                    target_binding=live_cand,
                    qualification_receipt_id=report.receipt.receipt_id,
                )
                code, resp = client.submit_command(
                    company_id=company_id,
                    command_id="cmd-promote-qualified-live",
                    command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                    contract_version=EXECUTION_TIER_COMMAND,
                    preconditions={
                        "charter_revision": "charter-v1",
                        "target_binding_id": live_cand.binding_id,
                        "qualification_receipt_id": report.receipt.receipt_id,
                        "expected_payload_hash": payload_hash,
                    },
                    payload={
                        "reason": "6/6 Binance USD-M Futures testnet qualification passed"
                    },
                    principal="board-chair",
                )
                self.assertEqual(code, 200)
                self.assertEqual(resp["state"], "applied")

                _, _, desk = client.projection(
                    "trading-desk", expected_company_id=company_id
                )
                assert desk is not None
                gov = desk["governance"]
                self.assertEqual(gov["company_live"]["status"], "active")
                self.assertEqual(gov["qualification_receipts"][0]["verdict"], "PASS")
                _, _, desk_paper = client.projection(
                    "trading-desk?environment=paper", expected_company_id=company_id
                )
                assert desk_paper is not None
                self.assertEqual(desk_paper["selected_book"]["status"], "active")
                _, _, founder_detail = client.projection(
                    f"agents/{founder_id}", expected_company_id=company_id
                )
                assert founder_detail is not None
                self.assertEqual(
                    founder_detail["agent_sandbox"]["binding"]["account_id"],
                    "founder-sbx-01",
                )

            # Verify zero raw secret leakage anywhere in company.sqlite!
            raw_db_bytes = store.read_bytes()
            self.assertNotIn(secret_canary.encode("utf-8"), raw_db_bytes)
            with closing(open_reader(store)) as db, read_tx(db):
                events = db.execute("SELECT payload_json FROM audit_event").fetchall()
                for (payload_json,) in events:
                    self.assertNotIn(secret_canary, str(payload_json))


if __name__ == "__main__":
    unittest.main()
