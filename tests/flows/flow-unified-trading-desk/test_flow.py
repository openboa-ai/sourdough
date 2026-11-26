"""Connected Flow E2E test for Unified Live & Paper Trading Desk over ResidentCompanyService + ClientWebBridge."""

from __future__ import annotations

import json
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sourdough.agent_runtime.protocol import (
    PROVIDER_CODEX_CLI,
    AdapterManifest,
    AgentProfileBinding,
    ShiftResult,
    TokenUsage,
)
from sourdough.agent_runtime.registry import AgentAdapterRegistry
from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    DECISION_COMMAND,
    SUBMIT_DECISION_ANSWER,
    bind_board_principal,
)
from sourdough.company import FoundingTerms, found
from sourdough.trading.accounts import (
    open_company_paper_book,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
)
from sourdough.trading.domain import (
    ENV_EXTERNAL_TEST,
    compute_trading_core_digest,
)
from sourdough.trading.ledger import (
    record_live_book_order_fill,
    record_trading_book_valuation,
    submit_company_paper_book_order,
)
from sourdough.trading.migration import propose_paper_to_live_migration
from sourdough.ipc import ClientWebBridge
from sourdough.runtime.handlers import build_production_duty_registry
from sourdough.runtime.resident import ResidentCompanyService
from sourdough.trading import TradingDomainPlugin
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
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        return err.code, json.loads(raw) if raw else {}


class _NoopAdapter:
    manifest = AdapterManifest(
        provider=PROVIDER_CODEX_CLI,
        adapter_revision="test-noop-v1",
        transport="local_cli_uds",
        supports_tool_mediation=True,
        supports_usage_accounting=True,
        supports_sandbox_desk=True,
    )

    def run_shift(self, *, profile: Any, request: Any, broker: Any) -> ShiftResult:
        return ShiftResult(
            provider=self.manifest.provider,
            model=profile.model,
            output_text="ok",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            tool_calls_executed=0,
        )


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with (
            TemporaryDirectory(prefix="sd-desk-flow-") as tmp,
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
            ceo_id = founding.founder_id
            t0 = 1_730_000_000_000_000_000
            hour_ns = 3_600 * 1_000_000_000

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )

                book1 = open_company_paper_book(
                    writer,
                    account_id="paper-btc-mom",
                    book_name="BTC Spot Momentum",
                    strategy_release_id="rel-btc-v3",
                    starting_minor_units=50_000_000,
                    exposure_limit_minor_units=25_000_000,
                    fee_bps=4,
                    slippage_bps=2,
                )
                book2 = open_company_paper_book(
                    writer,
                    account_id="paper-eth-basis",
                    book_name="ETH Basis Neutral",
                    strategy_release_id="rel-eth-v2",
                    starting_minor_units=40_000_000,
                    exposure_limit_minor_units=20_000_000,
                    fee_bps=3,
                    slippage_bps=1,
                )

                submit_company_paper_book_order(
                    writer,
                    binding_id=book1.binding_id,
                    actor_id=ceo_id,
                    action_id="flow-p1",
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
                    binding_id=book1.binding_id,
                    label="Day 2 Mark",
                    mark_prices={"BTC-USDT": 6_820_000},
                    now_ns=t0 + 2 * hour_ns,
                )
                submit_company_paper_book_order(
                    writer,
                    binding_id=book2.binding_id,
                    actor_id=ceo_id,
                    action_id="flow-p2",
                    instrument_id="ETH-USDT",
                    side="buy",
                    quantity=8,
                    limit_minor_units=360_000,
                    market_price_minor_units=350_000,
                    valuation_label="Day 1 ETH",
                    now_ns=t0 + hour_ns,
                )

                live_cand = register_live_candidate_binding(
                    writer,
                    binding_id="binance-live-desk-1",
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
                    receipt_id="qual-desk-flow-1",
                    adapter_revision="binance-spot-v1",
                    environment=ENV_EXTERNAL_TEST,
                    destination_id="binance-spot-testnet",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    core_digest=compute_trading_core_digest(),
                    verdict="PASS",
                    cases_passed=6,
                    cases_failed=0,
                    cases_unrun=0,
                    evidence_sha256="b" * 64,
                )
                nom = propose_paper_to_live_migration(
                    writer,
                    question_id="q-desk-flow-1",
                    source_paper_binding_id=book1.binding_id,
                    target_live_binding_id=live_cand.binding_id,
                    qualification_receipt_id="qual-desk-flow-1",
                    created_by_agent_id=ceo_id,
                    migration_mode="clean_start",
                    source_paper_disposition="keep_shadow",
                )

            adapters = AgentAdapterRegistry()
            adapters.register(_NoopAdapter())
            profile = AgentProfileBinding(
                provider=PROVIDER_CODEX_CLI,
                model="gpt-5-codex",
                revision="v1",
            )
            plugins = (TradingDomainPlugin(),)
            duty_registry = build_production_duty_registry(
                adapter_registry=adapters,
                profile_bindings=lambda _aid: profile,
                domain_plugins=plugins,
            )

            # Phase 1: Live Standby + Multi-Book Paper Desk + Board Approval Migration over HTTP.
            with (
                ResidentCompanyService(
                    store=store,
                    company_id=company_id,
                    duty_registry=duty_registry,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                    tick_interval_seconds=0.05,
                    domain_plugins=plugins,
                ) as resident,
                ClientWebBridge(
                    resident.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-chair",
                ) as bridge,
            ):
                base = bridge.info.url.rstrip("/")
                tok = bridge.info.session_token

                # Invalid environment query is rejected fail-closed.
                st_bad, _ = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=invalid", tok
                )
                self.assertEqual(st_bad, 400)

                # Live Desk starts in Standby with zero paper data commingled.
                st_live, live_before = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=live", tok
                )
                self.assertEqual(st_live, 200)
                self.assertEqual(live_before["environment"], "live")
                self.assertIsNone(live_before["selected_book"])
                self.assertEqual(live_before["cumulative_return_series"], [])
                self.assertEqual(live_before["open_positions"], [])
                self.assertEqual(live_before["execution_tape"], [])

                # Paper Desk supports switching between concurrent books via book_id.
                st_p1, paper_default = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=paper", tok
                )
                self.assertEqual(st_p1, 200)
                self.assertEqual(paper_default["selected_book_id"], book1.binding_id)
                self.assertEqual(len(paper_default["books"]), 2)
                self.assertGreaterEqual(
                    len(paper_default["cumulative_return_series"]), 3
                )

                st_p2, paper_b2 = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=paper&book_id={book2.binding_id}",
                    tok,
                )
                self.assertEqual(st_p2, 200)
                self.assertEqual(paper_b2["selected_book_id"], book2.binding_id)
                self.assertEqual(
                    paper_b2["open_positions"][0]["instrument_id"], "ETH-USDT"
                )

                # Board approves Paper-to-Live migration over HTTP command endpoint.
                st_cmd, cmd_res = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-desk-approve-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-desk-approve-1",
                        "command_type": SUBMIT_DECISION_ANSWER,
                        "contract_version": DECISION_COMMAND,
                        "preconditions": {
                            "question_id": nom["question_id"],
                            "expected_revision": 1,
                            "expected_payload_hash": nom["payload_hash"],
                        },
                        "payload": {
                            "choice": "approve",
                            "reason": "Approved unified live desk launch",
                        },
                    },
                )
                self.assertEqual(st_cmd, 200, cmd_res)
                self.assertEqual(cmd_res["state"], "applied")

                deadline = time.monotonic() + 8.0
                live_migrated: dict[str, Any] = {}
                while time.monotonic() < deadline:
                    _, live_migrated = _bridge_json(
                        f"{base}/api/v1/projections/trading-desk?environment=live", tok
                    )
                    if live_migrated.get("selected_book") is not None:
                        break
                    time.sleep(0.05)

                self.assertIsNotNone(live_migrated.get("selected_book"))
                self.assertEqual(
                    live_migrated["selected_book_id"], live_cand.binding_id
                )
                self.assertEqual(len(live_migrated["open_positions"]), 0)
                self.assertEqual(len(live_migrated["cumulative_return_series"]), 1)
                self.assertEqual(
                    live_migrated["cumulative_return_series"][0]["return_pct"], 0.0
                )
                live_launch_ns = live_migrated["cumulative_return_series"][0][
                    "observed_at_ns"
                ]

            # Phase 2: Record reconciled external live fills + parallel shadow marks under WriterLease.
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                record_live_book_order_fill(
                    writer,
                    binding_id=live_cand.binding_id,
                    actor_id=ceo_id,
                    action_id="live-fill-flow-1",
                    external_order_id="bn-flow-ord-901",
                    external_fill_id="bn-flow-fill-901",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=2,
                    limit_minor_units=6_900_000,
                    fill_price_minor_units=6_830_000,
                    fee_minor_units=6_830,
                    valuation_label="T+1h Fill",
                    now_ns=live_launch_ns + hour_ns,
                )
                record_trading_book_valuation(
                    writer,
                    binding_id=live_cand.binding_id,
                    label="T+4h Live Mark",
                    mark_prices={"BTC-USDT": 7_010_000},
                    now_ns=live_launch_ns + 4 * hour_ns,
                )
                record_trading_book_valuation(
                    writer,
                    binding_id=book1.binding_id,
                    label="T+4h Shadow Mark",
                    mark_prices={"BTC-USDT": 7_040_000},
                    now_ns=live_launch_ns + 4 * hour_ns,
                )

            # Phase 3: Reboot ResidentCompanyService + ClientWebBridge and verify Live Desk Chart + Shadow Benchmark over HTTP.
            with (
                ResidentCompanyService(
                    store=store,
                    company_id=company_id,
                    duty_registry=duty_registry,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                    tick_interval_seconds=0.05,
                    domain_plugins=plugins,
                ) as resident,
                ClientWebBridge(
                    resident.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-chair",
                ) as bridge,
            ):
                base = bridge.info.url.rstrip("/")
                tok = bridge.info.session_token

                st_live2, live_after = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=live", tok
                )
                self.assertEqual(st_live2, 200)
                self.assertEqual(len(live_after["cumulative_return_series"]), 3)
                self.assertGreater(live_after["kpis"]["trading_pnl"]["value_minor"], 0)
                self.assertEqual(len(live_after["open_positions"]), 1)
                self.assertEqual(
                    live_after["execution_tape"][0]["external_order_id"],
                    "bn-flow-ord-901",
                )
                self.assertIsNotNone(live_after["shadow_benchmark"])
                self.assertEqual(
                    live_after["shadow_benchmark"]["binding_id"], book1.binding_id
                )
                self.assertEqual(
                    live_after["shadow_benchmark"]["series"][0]["return_pct"], 0.0
                )
                self.assertGreaterEqual(
                    len(live_after["shadow_benchmark"]["series"]), 2
                )


if __name__ == "__main__":
    unittest.main()
