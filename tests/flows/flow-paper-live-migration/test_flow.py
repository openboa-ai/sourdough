"""Connected Flow E2E test for Multi-Book Paper Trading -> Board Approval -> Safe Live Migration."""

from __future__ import annotations

import json
import time
import unittest
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
from sourdough.ipc import ClientWebBridge
from sourdough.runtime.handlers import build_production_duty_registry
from sourdough.runtime.resident import ResidentCompanyService
from sourdough.trading import TradingDomainPlugin
from sourdough.trading.accounts import (
    open_company_paper_book,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
)
from sourdough.trading.domain import ENV_EXTERNAL_TEST, compute_trading_core_digest
from sourdough.trading.ledger import submit_company_paper_book_order
from sourdough.trading.migration import propose_paper_to_live_migration
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
            TemporaryDirectory(prefix="sd-mig-flow-") as tmp,
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

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )

                # 1. Open 3 concurrent Company Paper Books and record fills on all 3.
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
                book3 = open_company_paper_book(
                    writer,
                    account_id="paper-sol-mr",
                    book_name="SOL Mean Reversion",
                    strategy_release_id="rel-sol-v1",
                    starting_minor_units=30_000_000,
                    exposure_limit_minor_units=15_000_000,
                    fee_bps=5,
                    slippage_bps=3,
                )

                for b, inst, px in [
                    (book1, "BTC-USDT", 6_500_000),
                    (book2, "ETH-USDT", 350_000),
                    (book3, "SOL-USDT", 18_000),
                ]:
                    submit_company_paper_book_order(
                        writer,
                        binding_id=b.binding_id,
                        actor_id=ceo_id,
                        action_id=f"fill-{b.account_id}-1",
                        instrument_id=inst,
                        side="buy",
                        quantity=2,
                        limit_minor_units=int(px * 1.05),
                        market_price_minor_units=px,
                    )

                live_cand = register_live_candidate_binding(
                    writer,
                    binding_id="binance-live-mig-1",
                    account_id="binance-prod-us01",
                    destination_id="binance-spot-prod",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=10_000_000,
                    credential_handle_ref="keychain:openboa/binance/prod-us01",
                )
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-mig-flow-1",
                    adapter_revision="binance-spot-v1",
                    environment=ENV_EXTERNAL_TEST,
                    destination_id="binance-spot-testnet",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    core_digest=compute_trading_core_digest(),
                    verdict="PASS",
                    cases_passed=6,
                    cases_failed=0,
                    cases_unrun=0,
                    evidence_sha256="f" * 64,
                )

                # Company nominates Book #2 (ETH Basis Neutral) for Live migration.
                nom = propose_paper_to_live_migration(
                    writer,
                    question_id="q-flow-mig-1",
                    source_paper_binding_id=book2.binding_id,
                    target_live_binding_id=live_cand.binding_id,
                    qualification_receipt_id="qual-mig-flow-1",
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

                # 2. Verify 3 concurrent Paper Books and Book #2 nominated via trading-desk projection.
                st_t, desk_live = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=live", tok
                )
                self.assertEqual(st_t, 200)
                self.assertIsNone(desk_live["governance"]["company_live"])
                st_p, desk_paper = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=paper", tok
                )
                self.assertEqual(st_p, 200)
                self.assertEqual(len(desk_paper["books"]), 3)
                books_before = {b["binding_id"]: b for b in desk_paper["books"]}
                self.assertEqual(books_before[book2.binding_id]["role"], "nominated")
                self.assertEqual(
                    books_before[book2.binding_id]["nominated_question_id"],
                    nom["question_id"],
                )

                # 3. Board approves from Paper Trading / Approvals via submit_decision_answer.
                st_cmd, cmd_res = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-approve-mig-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-approve-mig-1",
                        "command_type": SUBMIT_DECISION_ANSWER,
                        "contract_version": DECISION_COMMAND,
                        "preconditions": {
                            "question_id": nom["question_id"],
                            "expected_revision": 1,
                            "expected_payload_hash": nom["payload_hash"],
                        },
                        "payload": {
                            "choice": "approve",
                            "reason": "Board approved Paper-to-Live migration",
                        },
                    },
                )
                self.assertEqual(st_cmd, 200, cmd_res)
                self.assertEqual(cmd_res["state"], "applied")

                # 4. Wait for ResidentCompanyService to execute board.apply_decision -> migrate_paper_to_live.
                deadline = time.monotonic() + 8.0
                desk_after: dict[str, Any] = {}
                while time.monotonic() < deadline:
                    _, desk_after = _bridge_json(
                        f"{base}/api/v1/projections/trading-desk?environment=live", tok
                    )
                    if desk_after.get("governance", {}).get("company_live") is not None:
                        break
                    time.sleep(0.05)

                self.assertIsNotNone(desk_after["governance"].get("company_live"))
                self.assertEqual(
                    desk_after["governance"]["company_live"]["binding_id"],
                    live_cand.binding_id,
                )
                _, desk_paper_after = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=paper", tok
                )
                books_after = {b["binding_id"]: b for b in desk_paper_after["books"]}
                self.assertEqual(books_after[book2.binding_id]["role"], "shadow")
                self.assertEqual(
                    books_after[book2.binding_id]["shadow_of_live_binding_id"],
                    live_cand.binding_id,
                )
                self.assertEqual(books_after[book1.binding_id]["role"], "active")
                self.assertEqual(books_after[book3.binding_id]["role"], "active")
                self.assertEqual(len(desk_after["governance"]["migration_history"]), 1)
                self.assertEqual(
                    desk_after["governance"]["migration_history"][0][
                        "positions_copied"
                    ],
                    0,
                )

                # Confirm decision question application status is applied & delivered.
                _, approvals_after = _bridge_json(
                    f"{base}/api/v1/projections/approvals", tok
                )
                self.assertEqual(len(approvals_after["pending"]), 0)
                self.assertEqual(
                    approvals_after["history"][0]["application_status"], "applied"
                )


if __name__ == "__main__":
    unittest.main()
