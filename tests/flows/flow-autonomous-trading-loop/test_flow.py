"""Connected Flow E2E test for Autonomous Trading Loop (Paper -> Migration -> Live -> Reconcile)."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request
from contextlib import closing
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.agent_runtime import (
    PROVIDER_OPENAI,
    AgentProfileBinding,
    InMemorySecretResolver,
    build_default_adapter_registry,
)
from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    DECISION_COMMAND,
    SUBMIT_DECISION_ANSWER,
    bind_board_principal,
)
from sourdough.company import FoundingTerms, found
from sourdough.ipc import ClientWebBridge
from sourdough.runtime import (
    ResidentCompanyService,
    build_production_duty_registry,
    create_work_item,
)
from sourdough.storage.db import open_writer, write_tx
from sourdough.storage.schema import enable_domain_v1
from sourdough.trading import (
    DUTY_QUANT_PAPER_DECISION,
    DUTY_QUANT_RECONCILE,
    TradingDomainPlugin,
)
from sourdough.trading.accounts import (
    open_company_paper_book,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
)
from sourdough.trading.binance_emulator import BinanceWireEmulator
from sourdough.trading.binance_venue import (
    MARKET_SPOT,
    BinanceVenueAdapter,
    BinanceVenueConfig,
)
from sourdough.trading.credentials import ApiCredentialPair
from sourdough.trading.domain import (
    ENV_EXTERNAL_TEST,
    compute_trading_core_digest,
)
from sourdough.trading.quotes import MarketQuote, StaticQuoteProvider
from sourdough.writer_lease import hold_company_writer


def _bridge_json(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    body: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class _StaticCreds:
    def __init__(self, key: str, secret: str) -> None:
        self._key = key
        self._secret = secret

    def resolve(
        self, handle_ref: str, *, expected_environment: str
    ) -> ApiCredentialPair:
        del expected_environment
        return ApiCredentialPair(
            api_key=self._key, api_secret=self._secret, handle_ref=handle_ref
        )


class _AutonomousAgentLlmServer:
    """Loopback OpenAI /v1/responses mock driving real multi-step broker tool calls."""

    def __init__(
        self, *, paper_binding_id: str, live_binding_id: str, qual_receipt_id: str
    ) -> None:
        self.paper_binding_id = paper_binding_id
        self.live_binding_id = live_binding_id
        self.qual_receipt_id = qual_receipt_id
        self.phase = "paper"
        self._step_by_phase: dict[str, int] = {"paper": 0, "live": 0}
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                _ = json.loads(self.rfile.read(length).decode("utf-8"))
                phase = owner.phase
                step = owner._step_by_phase[phase]
                owner._step_by_phase[phase] = step + 1

                if phase == "paper":
                    if step == 0:
                        out = [
                            {
                                "type": "function_call",
                                "call_id": "c-p-quote",
                                "name": "read_market_quote",
                                "arguments": json.dumps({"instrument_id": "BTC-USDT"}),
                            }
                        ]
                    elif step == 1:
                        out = [
                            {
                                "type": "function_call",
                                "call_id": "c-p-order",
                                "name": "submit_company_paper_order",
                                "arguments": json.dumps(
                                    {
                                        "binding_id": owner.paper_binding_id,
                                        "action_id": "auto-paper-btc-1",
                                        "instrument_id": "BTC-USDT",
                                        "side": "buy",
                                        "quantity": 2,
                                        "limit_minor_units": 6_900_000,
                                    }
                                ),
                            }
                        ]
                    elif step == 2:
                        out = [
                            {
                                "type": "function_call",
                                "call_id": "c-p-mig",
                                "name": "propose_live_migration",
                                "arguments": json.dumps(
                                    {
                                        "source_paper_binding_id": owner.paper_binding_id,
                                        "target_live_binding_id": owner.live_binding_id,
                                        "qualification_receipt_id": owner.qual_receipt_id,
                                        "reason": "Autonomous paper soak verified",
                                    }
                                ),
                            }
                        ]
                    elif step == 3:
                        out = [
                            {
                                "type": "function_call",
                                "call_id": "c-p-sched",
                                "name": "schedule_next_shift",
                                "arguments": json.dumps(
                                    {
                                        "delay_seconds": 300,
                                        "duty_kind": "quant.reconcile",
                                    }
                                ),
                            }
                        ]
                    else:
                        out = [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "Paper trade booked, live migration proposed, next shift scheduled.",
                                    }
                                ],
                            }
                        ]
                else:
                    if step == 0:
                        out = [
                            {
                                "type": "function_call",
                                "call_id": "c-l-quote",
                                "name": "read_market_quote",
                                "arguments": json.dumps({"instrument_id": "BTC-USDT"}),
                            }
                        ]
                    elif step == 1:
                        out = [
                            {
                                "type": "function_call",
                                "call_id": "c-l-order",
                                "name": "submit_company_live_order",
                                "arguments": json.dumps(
                                    {
                                        "binding_id": owner.live_binding_id,
                                        "action_id": "auto-live-btc-1",
                                        "instrument_id": "BTC-USDT",
                                        "side": "buy",
                                        "quantity": 1,
                                        "limit_minor_units": 6_850_000,
                                    }
                                ),
                            }
                        ]
                    else:
                        out = [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "Live BTC order executed and confirmed on external exchange.",
                                    }
                                ],
                            }
                        ]

                resp = {
                    "id": f"resp-{phase}-{step}",
                    "output": out,
                    "usage": {"input_tokens": 420, "output_tokens": 90},
                }
                encoded = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with (
            TemporaryDirectory(prefix="sd-auto-flow-") as tmp,
            TemporaryDirectory(prefix="sd-", dir="/tmp") as sock_tmp,
        ):
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            sock_dir = Path(sock_tmp)
            pending_dir = root / "pending"

            resolver = InMemorySecretResolver({"envref:TEST_OPENAI": "sk-local-test"})
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="2026-11",
                credential_handle_ref="envref:TEST_OPENAI",
            )
            founding = found(
                store,
                FoundingTerms(
                    "sourdough-auto-fund", "charter-v1", "paper", profile.profile_id()
                ),
            )
            company_id = founding.company_id
            ceo_id = founding.founder_id

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    enable_domain_v1(db, "trading")
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )
                paper_book = open_company_paper_book(
                    writer,
                    account_id="paper-auto-btc",
                    book_name="BTC Spot Momentum",
                    strategy_release_id="rel-btc-auto-v1",
                    starting_minor_units=50_000_000,
                    exposure_limit_minor_units=30_000_000,
                    fee_bps=4,
                    slippage_bps=2,
                )
                live_cand = register_live_candidate_binding(
                    writer,
                    binding_id="binance-live-auto-1",
                    account_id="binance-prod-us01",
                    destination_id="binance-spot-prod",
                    connector_revision="binance-rest-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=20_000_000,
                    credential_handle_ref="envref:BINANCE_PROD",
                )
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-auto-flow-1",
                    adapter_revision="binance-rest-v1",
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
                # Enqueue autonomous paper trading shift BEFORE Client opens!
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    create_work_item(
                        db,
                        company_id=company_id,
                        owner_agent_id=ceo_id,
                        duty_kind=DUTY_QUANT_PAPER_DECISION,
                        source_kind="schedule",
                        source_id="auto-shift-1",
                        dedupe_key="auto-shift-paper-1",
                    )

            emulator = BinanceWireEmulator(
                api_key="bn-flow-key",
                api_secret="bn-flow-secret",
                starting_usdt=Decimal("500000"),
                fee_bps=4,
                max_order_notional=Decimal("200000"),
            )
            emulator.start()
            llm = _AutonomousAgentLlmServer(
                paper_binding_id=paper_book.binding_id,
                live_binding_id=live_cand.binding_id,
                qual_receipt_id="qual-auto-flow-1",
            )
            try:
                binance_adapter = BinanceVenueAdapter(
                    BinanceVenueConfig(
                        environment=ENV_EXTERNAL_TEST,
                        market_kind=MARKET_SPOT,
                        account_id="binance-prod-us01",
                        destination_id="binance-spot-testnet",
                        credential_handle_ref="envref:BINANCE_TESTNET",
                        base_url_override=emulator.base_url,
                        allow_loopback_override=True,
                    ),
                    _StaticCreds("bn-flow-key", "bn-flow-secret"),
                )
                quotes = StaticQuoteProvider(
                    {
                        "BTC-USDT": MarketQuote(
                            "BTC-USDT", 6_800_000, time_ns(), "live-feed"
                        )
                    }
                )
                trading_plugin = TradingDomainPlugin(
                    quote_provider=quotes,
                    live_venue_resolver=lambda bid: (
                        binance_adapter if bid == live_cand.binding_id else None
                    ),
                )
                adapters = build_default_adapter_registry(
                    secret_resolver=resolver,
                    openai_base_url=llm.base_url,
                    allow_loopback_override=True,
                )
                duty_registry = build_production_duty_registry(
                    adapter_registry=adapters,
                    profile_bindings=lambda _aid: profile,
                    domain_plugins=(trading_plugin,),
                )

                # Phase 1: Autonomous paper shift runs with NO Client open, then Board approves migration via HTTP.
                with (
                    ResidentCompanyService(
                        store=store,
                        company_id=company_id,
                        duty_registry=duty_registry,
                        socket_dir=sock_dir,
                        lease_directory=leases,
                        tick_interval_seconds=0.05,
                        domain_plugins=(trading_plugin,),
                    ) as resident,
                    ClientWebBridge(
                        resident.socket_path,
                        port=0,
                        pending_dir=pending_dir,
                        principal="board-chair",
                    ) as bridge,
                ):
                    resident.wait_for(
                        lambda h: h.runs_executed >= 1, timeout_seconds=10.0
                    )
                    base = bridge.info.url.rstrip("/")
                    tok = bridge.info.session_token

                    # Confirm autonomous Paper order + armed wake condition + migration proposal!
                    st_p, paper_desk = _bridge_json(
                        f"{base}/api/v1/projections/trading-desk?environment=paper", tok
                    )
                    self.assertEqual(st_p, 200)
                    self.assertEqual(paper_desk["open_positions"][0]["quantity"], 2)

                    st_app, approvals = _bridge_json(
                        f"{base}/api/v1/projections/approvals", tok
                    )
                    self.assertEqual(st_app, 200)
                    self.assertEqual(len(approvals["pending"]), 1)
                    mig_q = approvals["pending"][0]
                    self.assertEqual(mig_q["kind"], "live_promotion")

                    # Board approves migration over HTTP!
                    st_cmd, cmd_res = _bridge_json(
                        f"{base}/api/v1/commands",
                        tok,
                        method="POST",
                        idempotency_key="auto-flow-approve-live",
                        payload={
                            "company_id": company_id,
                            "command_id": "auto-flow-approve-live",
                            "command_type": SUBMIT_DECISION_ANSWER,
                            "contract_version": DECISION_COMMAND,
                            "preconditions": {
                                "question_id": mig_q["question_id"],
                                "expected_revision": mig_q["revision"],
                                "expected_payload_hash": mig_q["payload_hash"],
                            },
                            "payload": {
                                "choice": "approve",
                                "reason": "Approved live autonomous launch",
                            },
                        },
                    )
                    self.assertEqual(st_cmd, 200, cmd_res)
                    self.assertEqual(cmd_res["state"], "applied")

                    # Wait for resident board.apply_decision to activate live book.
                    deadline = time.monotonic() + 8.0
                    live_after_mig: dict[str, Any] = {}
                    while time.monotonic() < deadline:
                        _, live_after_mig = _bridge_json(
                            f"{base}/api/v1/projections/trading-desk?environment=live",
                            tok,
                        )
                        if live_after_mig.get("selected_book") is not None:
                            break
                        time.sleep(0.05)
                    self.assertIsNotNone(live_after_mig.get("selected_book"))

                # Phase 2: Enqueue autonomous LIVE trading shift + mark-to-market reconcile shift!
                llm.phase = "live"
                quotes.set_quote(
                    MarketQuote("BTC-USDT", 6_820_000, time_ns(), "live-feed")
                )
                with hold_company_writer(
                    store, company_id, lease_directory=leases
                ) as writer:
                    with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                        create_work_item(
                            db,
                            company_id=company_id,
                            owner_agent_id=ceo_id,
                            duty_kind=DUTY_QUANT_PAPER_DECISION,
                            source_kind="schedule",
                            source_id="auto-shift-live-1",
                            dedupe_key="auto-shift-live-1",
                        )

                with (
                    ResidentCompanyService(
                        store=store,
                        company_id=company_id,
                        duty_registry=duty_registry,
                        socket_dir=sock_dir,
                        lease_directory=leases,
                        tick_interval_seconds=0.05,
                        domain_plugins=(trading_plugin,),
                    ) as resident,
                    ClientWebBridge(
                        resident.socket_path,
                        port=0,
                        pending_dir=pending_dir,
                        principal="board-chair",
                    ) as bridge,
                ):
                    resident.wait_for(
                        lambda h: h.runs_executed >= 1, timeout_seconds=10.0
                    )
                    base = bridge.info.url.rstrip("/")
                    tok = bridge.info.session_token

                    _, live_filled = _bridge_json(
                        f"{base}/api/v1/projections/trading-desk?environment=live", tok
                    )
                    self.assertEqual(len(live_filled["open_positions"]), 1)
                    self.assertEqual(live_filled["open_positions"][0]["quantity"], 1)
                    self.assertIsNotNone(
                        live_filled["execution_tape"][0]["external_order_id"]
                    )
                    self.assertEqual(emulator.submit_count, 1)

                # Phase 3: Price rallies to $70,500 -> autonomous quant.reconcile marks Live & Shadow!
                quotes.set_quote(
                    MarketQuote("BTC-USDT", 7_050_000, time_ns(), "live-feed")
                )
                with hold_company_writer(
                    store, company_id, lease_directory=leases
                ) as writer:
                    with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                        create_work_item(
                            db,
                            company_id=company_id,
                            owner_agent_id=ceo_id,
                            duty_kind=DUTY_QUANT_RECONCILE,
                            source_kind="schedule",
                            source_id="auto-reconcile-1",
                            dedupe_key="auto-reconcile-1",
                        )

                with (
                    ResidentCompanyService(
                        store=store,
                        company_id=company_id,
                        duty_registry=duty_registry,
                        socket_dir=sock_dir,
                        lease_directory=leases,
                        tick_interval_seconds=0.05,
                        domain_plugins=(trading_plugin,),
                    ) as resident,
                    ClientWebBridge(
                        resident.socket_path,
                        port=0,
                        pending_dir=pending_dir,
                        principal="board-chair",
                    ) as bridge,
                ):
                    resident.wait_for(
                        lambda h: h.runs_executed >= 1, timeout_seconds=10.0
                    )
                    base = bridge.info.url.rstrip("/")
                    tok = bridge.info.session_token

                    _, live_marked = _bridge_json(
                        f"{base}/api/v1/projections/trading-desk?environment=live", tok
                    )
                    self.assertGreaterEqual(
                        len(live_marked["cumulative_return_series"]), 3
                    )
                    self.assertGreater(
                        live_marked["kpis"]["trading_pnl"]["value_minor"], 0
                    )
                    self.assertIsNotNone(live_marked["shadow_benchmark"])
                    self.assertGreaterEqual(
                        len(live_marked["shadow_benchmark"]["series"]), 2
                    )
            finally:
                llm.close()
                emulator.stop()


if __name__ == "__main__":
    unittest.main()
