"""Connected Flow E2E test for ResidentCompanyService running autonomously and with Board Client."""

from __future__ import annotations

import json
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sourdough.agent_runtime import (
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    AgentProfileBinding,
    InMemorySecretResolver,
    build_default_adapter_registry,
)
from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    CEO_CHAT_COMMAND,
    DECISION_COMMAND,
    SEND_CEO_MESSAGE,
    SUBMIT_DECISION_ANSWER,
    bind_board_principal,
    record_decision_question,
    upsert_agent_profile,
)
from sourdough.company import FoundingTerms, found
from sourdough.ipc import ControlPlaneClient
from sourdough.runtime import (
    ResidentCompanyService,
    build_production_duty_registry,
    create_work_item,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.trading import DUTY_QUANT_AGENT_SANDBOX, TradingDomainPlugin
from sourdough.writer_lease import hold_company_writer


class _LiveResidentMockServer:
    """Loopback provider server powering Founder-CEO (OpenAI) and Atlas Quant (Anthropic)."""

    def __init__(self) -> None:
        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/v1/responses":
                    if payload.get("previous_response_id"):
                        self._send(
                            {
                                "id": "flow-ceo-2",
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "CEO live reply: Tier 1 sandbox is active and Tier 3 live remains gated.",
                                            }
                                        ],
                                    }
                                ],
                                "usage": {"input_tokens": 1100, "output_tokens": 210},
                            }
                        )
                    else:
                        self._send(
                            {
                                "id": "flow-ceo-1",
                                "output": [
                                    {
                                        "type": "function_call",
                                        "call_id": "ceo-tiers-1",
                                        "name": "read_execution_tiers",
                                        "arguments": "{}",
                                    }
                                ],
                                "usage": {"input_tokens": 900, "output_tokens": 140},
                            }
                        )
                elif self.path == "/v1/messages":
                    messages = payload.get("messages", [])
                    if len(messages) == 1:
                        self._send(
                            {
                                "id": "flow-q-1",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "flow-q-open",
                                        "name": "open_agent_sandbox",
                                        "input": {
                                            "account_id": "resident-quant-sbx",
                                            "starting_minor_units": 10_000_000,
                                            "fee_bps": 4,
                                            "slippage_bps": 2,
                                        },
                                    }
                                ],
                                "usage": {"input_tokens": 1200, "output_tokens": 220},
                            }
                        )
                    elif len(messages) == 3:
                        self._send(
                            {
                                "id": "flow-q-2",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "flow-q-ord",
                                        "name": "submit_agent_sandbox_order",
                                        "input": {
                                            "action_id": "resident-act-01",
                                            "instrument_id": "BTC-USDT",
                                            "side": "buy",
                                            "quantity": 1,
                                            "limit_minor_units": 6_600_000,
                                            "market_price_minor_units": 6_500_000,
                                        },
                                    }
                                ],
                                "usage": {"input_tokens": 1100, "output_tokens": 190},
                            }
                        )
                    else:
                        self._send(
                            {
                                "id": "flow-q-3",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Autonomous Tier 1 BTC-USDT sandbox buy completed.",
                                    }
                                ],
                                "usage": {"input_tokens": 700, "output_tokens": 130},
                            }
                        )

            def _send(self, body: dict[str, Any]) -> None:
                encoded = json.dumps(body).encode("utf-8")
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
        self._thread.join(timeout=2.0)


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        mock = _LiveResidentMockServer()
        try:
            with (
                TemporaryDirectory(prefix="sd-res-") as tmp,
                TemporaryDirectory(prefix="sd-", dir="/tmp") as sock_tmp,
            ):
                root = Path(tmp)
                store = root / "c.sqlite"
                leases = root / "l"
                sock_dir = Path(sock_tmp)

                resolver = InMemorySecretResolver(
                    {
                        "envref:FLOW_CEO_KEY": "local-flow-ceo-val",
                        "envref:FLOW_QUANT_KEY": "local-flow-quant-val",
                    }
                )
                ceo_profile = AgentProfileBinding(
                    provider=PROVIDER_OPENAI,
                    model="gpt-5-mini",
                    revision="2026-11",
                    credential_handle_ref="envref:FLOW_CEO_KEY",
                )
                quant_profile = AgentProfileBinding(
                    provider=PROVIDER_ANTHROPIC,
                    model="claude-sonnet-4-5",
                    revision="2026-11",
                    credential_handle_ref="envref:FLOW_QUANT_KEY",
                )

                founding = found(
                    store,
                    FoundingTerms(
                        "sourdough-fund",
                        "charter-v1",
                        "paper",
                        ceo_profile.profile_id(),
                    ),
                )
                company_id = founding.company_id
                founder_id = founding.founder_id
                quant_id = "agent-quant"

                # 1. Seed company with Founder-CEO, Atlas Quant, Board Chair, one due sandbox duty, and one open decision.
                with hold_company_writer(
                    store, company_id, lease_directory=leases
                ) as writer:
                    with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                        db.execute(
                            "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                            (quant_id, company_id, quant_profile.profile_id()),
                        )
                        db.execute(
                            "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                            ("grant-quant", quant_id, "paper"),
                        )
                    bind_board_principal(
                        writer,
                        principal_id="board-chair",
                        role_name=BOARD_CONTROL_ROLE,
                    )
                    upsert_agent_profile(
                        writer,
                        agent_id=quant_id,
                        display_name="Atlas Quant",
                        role_label="Quant Researcher",
                        monthly_budget_minor=250_000,
                    )
                    with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                        create_work_item(
                            db,
                            company_id=company_id,
                            owner_agent_id=quant_id,
                            duty_kind=DUTY_QUANT_AGENT_SANDBOX,
                            source_kind="schedule",
                            source_id="auto-sbx-01",
                            dedupe_key="flow-auto-sbx-01",
                        )
                    decision_hash = record_decision_question(
                        writer,
                        question_id="q-raise-budget-1",
                        kind="budget_adjustment",
                        title="Expand Atlas Quant sandbox budget",
                        summary="Raise monthly compute budget to 750000 minor units.",
                        impact={
                            "action": "update_agent_budget",
                            "agent_id": quant_id,
                            "monthly_budget_minor": 750_000,
                        },
                        created_by_agent_id=founder_id,
                    )

                def _make_service() -> ResidentCompanyService:
                    adapters = build_default_adapter_registry(
                        secret_resolver=resolver,
                        openai_base_url=mock.base_url,
                        anthropic_base_url=mock.base_url,
                        allow_loopback_override=True,
                    )
                    plugins = (TradingDomainPlugin(),)
                    duties = build_production_duty_registry(
                        adapter_registry=adapters,
                        profile_bindings={
                            founder_id: ceo_profile,
                            quant_id: quant_profile,
                        },
                        domain_plugins=plugins,
                    )
                    return ResidentCompanyService(
                        store,
                        company_id,
                        duties,
                        socket_dir=sock_dir,
                        lease_directory=leases,
                        tick_interval_seconds=0.15,
                        domain_plugins=plugins,
                    )

                # 2. Boot ResidentCompanyService with ZERO connected Board Clients.
                # Verify autonomous Tier 1 sandbox execution + GL cost settlement + clean stop.
                with _make_service() as service:
                    health = service.wait_for(
                        lambda h: h.runs_executed >= 1, timeout_seconds=10.0
                    )
                    self.assertIsNone(health.last_error)

                # Verify durable autonomous results survived stop before restart.
                with closing(open_reader(store)) as db, read_tx(db):
                    fill_count = db.execute(
                        "SELECT COUNT(*) FROM agent_sandbox_fill"
                    ).fetchone()[0]
                    self.assertEqual(fill_count, 1)
                    gl_count = db.execute(
                        "SELECT COUNT(*) FROM journal_entry WHERE mapping_revision = 'agent-llm-cost-v1'"
                    ).fetchone()[0]
                    self.assertEqual(gl_count, 1)

                # 3. Restart ResidentCompanyService cleanly, connect Board Client, and exercise live IPC + events.
                with _make_service() as service:
                    client = ControlPlaneClient(service.socket_path)
                    initial_events = client.events(
                        after_seq=0, limit=500, expected_company_id=company_id
                    )
                    last_seq = max(
                        (int(ev["event_seq"]) for ev in initial_events), default=0
                    )

                    # Send Board -> CEO message over UDS control plane.
                    msg_status, msg_resp = client.submit_command(
                        company_id=company_id,
                        command_id="flow-cmd-ceo-msg-1",
                        command_type=SEND_CEO_MESSAGE,
                        contract_version=CEO_CHAT_COMMAND,
                        preconditions={
                            "ceo_agent_id": founder_id,
                            "charter_revision": "charter-v1",
                        },
                        payload={
                            "target_agent_id": founder_id,
                            "body": "Status check on Atlas Quant sandbox and live gate.",
                        },
                        principal="board-chair",
                    )
                    self.assertEqual(msg_status, 200, msg_resp)

                    # Wait for CEO reply shift to run automatically via on_commit wakeup!
                    service.wait_for(
                        lambda h: h.runs_executed >= 1, timeout_seconds=10.0
                    )
                    pushed = client.events(
                        after_seq=last_seq,
                        limit=100,
                        wait_ms=2000,
                        expected_company_id=company_id,
                    )
                    event_types = {str(ev["event_type"]) for ev in pushed}
                    self.assertIn("ceo.message_replied", event_types)

                    status, _etag, messages = client.projection(
                        "messages", expected_company_id=company_id
                    )
                    self.assertEqual(status, 200)
                    ceo_replies = [
                        m
                        for m in messages["ceo_thread"]["messages"]
                        if m["sender_kind"] == "ceo"
                    ]
                    self.assertEqual(len(ceo_replies), 1)
                    self.assertIn("CEO live reply", ceo_replies[0]["body"])

                    # Approve inline Board decision over UDS -> triggers board.apply_decision automatically!
                    dec_status, dec_resp = client.submit_command(
                        company_id=company_id,
                        command_id="flow-cmd-dec-1",
                        command_type=SUBMIT_DECISION_ANSWER,
                        contract_version=DECISION_COMMAND,
                        preconditions={
                            "question_id": "q-raise-budget-1",
                            "expected_revision": 1,
                            "expected_payload_hash": decision_hash,
                        },
                        payload={
                            "choice": "approve",
                            "reason": "Approved for sandbox expansion",
                        },
                        principal="board-chair",
                    )
                    self.assertEqual(dec_status, 200, dec_resp)

                    service.wait_for(
                        lambda h: h.runs_executed >= 2, timeout_seconds=10.0
                    )
                    self.assertIsNone(service.health.last_error)

                    # Verify concrete domain effect applied on agent_profile projection!
                    ag_status, _etag, agent_detail = client.projection(
                        f"agents/{quant_id}", expected_company_id=company_id
                    )
                    self.assertEqual(ag_status, 200)
                    self.assertEqual(
                        agent_detail["authority_budget"]["monthly_budget_minor"],
                        750_000,
                    )

                    # Verify execution-tiers projection shows autonomous Tier 1 fill and zero live promotion.
                    tier_status, _etag, tiers = client.projection(
                        "execution-tiers", expected_company_id=company_id
                    )
                    self.assertEqual(tier_status, 200)
                    self.assertIsNone(tiers["company_live"])
                    self.assertEqual(len(tiers["agent_sandboxes"]), 1)
                    self.assertEqual(tiers["agent_sandboxes"][0]["fill_count"], 1)
        finally:
            mock.close()


if __name__ == "__main__":
    unittest.main()
