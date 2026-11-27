"""Connected Flow E2E test for Board Client 3-tier execution & live promotion over ClientWebBridge."""

from __future__ import annotations

import json
import unittest
import urllib.request
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    bind_board_principal,
    upsert_agent_profile,
)
from sourdough.company import FoundingTerms, found
from sourdough.ipc import ClientWebBridge, CompanyDaemon
from sourdough.storage.db import open_writer, write_tx
from sourdough.trading.accounts import (
    record_venue_qualification_receipt,
    register_company_paper_tier,
    register_live_candidate_binding,
)
from sourdough.trading.commands import (
    DEMOTE_LIVE_ENVIRONMENT,
    EXECUTION_TIER_COMMAND,
    PROMOTE_EXECUTION_ENVIRONMENT,
)
from sourdough.trading.domain import ENV_EXTERNAL_TEST, compute_trading_core_digest
from sourdough.trading.ledger import record_trading_book_valuation
from sourdough.trading.sandbox_tools import AgentSandboxTools
from sourdough.writer_lease import hold_company_writer


def _bridge_json(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    body: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with (
            TemporaryDirectory(prefix="sd-tui-") as tmp,
            TemporaryDirectory(prefix="sd-", dir="/tmp") as sock_tmp,
        ):
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            sock_dir = Path(sock_tmp)
            pending_dir = root / "pending"

            founding = found(
                store,
                FoundingTerms("sourdough-fund", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            quant_id = "agent-atlas"

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
                        ("grant-atlas", quant_id, "paper"),
                    )
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )
                upsert_agent_profile(
                    writer,
                    agent_id=quant_id,
                    display_name="Atlas Quant",
                    role_label="Quant Researcher",
                    monthly_budget_minor=500_000,
                )

                # Tier 2 company paper + soak valuation.
                paper_binding = register_company_paper_tier(
                    writer,
                    account_id="company-paper-ui",
                    destination_id="fixture-dest",
                    connector_revision="fixed-quote-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=100_000_000,
                    exposure_limit_minor_units=50_000_000,
                    fee_bps=4,
                    slippage_bps=2,
                    binding_revision="mandate-ui-v1",
                )
                record_trading_book_valuation(
                    writer,
                    binding_id=paper_binding.binding_id,
                    label="SoakUI",
                    mark_prices={},
                )

                # Tier 1 per-Agent sandbox trade.
                tools = AgentSandboxTools(writer=writer, agent_id=quant_id)
                tools.invoke(
                    "open_agent_sandbox",
                    {
                        "account_id": "atlas-sbx-ui",
                        "starting_minor_units": 10_000_000,
                        "fee_bps": 4,
                        "slippage_bps": 2,
                    },
                )
                sbx_order = tools.invoke(
                    "submit_agent_sandbox_order",
                    {
                        "action_id": "atlas-act-ui-1",
                        "instrument_id": "BTC-USDT",
                        "side": "buy",
                        "quantity": 1,
                        "limit_minor_units": 6_600_000,
                        "market_price_minor_units": 6_500_000,
                    },
                )
                sandbox_order_id = sbx_order["order_id"]

                # Tier 3 live candidate + passing qualification receipt.
                register_live_candidate_binding(
                    writer,
                    binding_id="binance-live-ui",
                    account_id="binance-prod-ui",
                    destination_id="binance-spot-prod",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=10_000_000,
                    credential_handle_ref="keychain:openboa/binance/prod-ui",
                )
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-ui-pass-1",
                    adapter_revision="binance-spot-v1",
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

            with (
                CompanyDaemon(
                    store,
                    company_id,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                ) as daemon,
                ClientWebBridge(
                    daemon.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-chair",
                ) as bridge,
            ):
                base = bridge.info.url.rstrip("/")
                tok = bridge.info.session_token

                # 1. Retired /execution-tiers returns 404; Board Client reads /trading-desk?environment=live & /agents/{id} & /orders/{id}.
                with self.assertRaises(urllib.error.HTTPError) as retired_err:
                    _bridge_json(f"{base}/api/v1/projections/execution-tiers", tok)
                self.assertEqual(retired_err.exception.code, 404)

                st, desk = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=live", tok
                )
                self.assertEqual(st, 200)
                gov = desk["governance"]
                self.assertTrue(gov["company_paper_soak_ready"])
                self.assertIsNone(gov["company_live"])
                cand = gov["live_candidates"][0]
                self.assertEqual(
                    cand["matching_qualification_receipt_id"], "qual-ui-pass-1"
                )
                self.assertTrue(cand["promotion_payload_hash"])

                st_ag, ag_detail = _bridge_json(
                    f"{base}/api/v1/projections/agents/{quant_id}", tok
                )
                self.assertEqual(st_ag, 200)
                self.assertEqual(ag_detail["agent_sandbox"]["fill_count"], 1)
                self.assertEqual(len(ag_detail["agent_sandbox"]["positions"]), 1)

                st_ord, ord_trace = _bridge_json(
                    f"{base}/api/v1/projections/orders/{sandbox_order_id}", tok
                )
                self.assertEqual(st_ord, 200)
                self.assertEqual(ord_trace["order"]["environment"], "agent_sandbox")
                self.assertNotIn("Paper Order", json.dumps(ord_trace))

                # 2. Board Client promotes candidate via HTTP-to-UDS command using server-provided hash.
                st_p, promo = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="ui-promote-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "ui-promote-1",
                        "command_type": PROMOTE_EXECUTION_ENVIRONMENT,
                        "contract_version": EXECUTION_TIER_COMMAND,
                        "preconditions": {
                            "charter_revision": desk["charter_revision"],
                            "target_binding_id": cand["binding_id"],
                            "qualification_receipt_id": cand[
                                "matching_qualification_receipt_id"
                            ],
                            "expected_payload_hash": cand["promotion_payload_hash"],
                        },
                        "payload": {"reason": "Board approved live promotion from UI"},
                    },
                )
                self.assertEqual(st_p, 200, promo)
                self.assertEqual(promo["state"], "applied")

                _, after_promo = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=live", tok
                )
                self.assertIsNotNone(after_promo["governance"]["company_live"])
                self.assertEqual(
                    after_promo["governance"]["company_live"]["binding_id"],
                    "binance-live-ui",
                )

                # 3. Board Client demotes Tier 3 back to paper-only safely.
                st_d, demote = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="ui-demote-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "ui-demote-1",
                        "command_type": DEMOTE_LIVE_ENVIRONMENT,
                        "contract_version": EXECUTION_TIER_COMMAND,
                        "preconditions": {
                            "charter_revision": desk["charter_revision"],
                            "target_binding_id": "binance-live-ui",
                        },
                        "payload": {
                            "action": "demote_to_paper",
                            "reason": "Returning to paper soak via UI",
                        },
                    },
                )
                self.assertEqual(st_d, 200, demote)
                self.assertEqual(demote["state"], "applied")

                _, after_demote = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=live", tok
                )
                self.assertIsNone(after_demote["governance"]["company_live"])
                self.assertEqual(
                    len(after_demote["governance"]["promotion_history"]), 2
                )


if __name__ == "__main__":
    unittest.main()
