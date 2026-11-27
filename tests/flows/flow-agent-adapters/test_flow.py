"""Connected Flow E2E test for multi-provider Agent runtime adapters over Two-Plane Harness."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sourdough.agent_runtime import (
    PROVIDER_ANTHROPIC,
    PROVIDER_CODEX_CLI,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    AgentProfileBinding,
    AgentProfileMismatch,
    InMemorySecretResolver,
    OuterToolBroker,
    ShiftRequest,
    ToolSpec,
    build_default_adapter_registry,
    hydrate_shift_context,
    settle_shift_cost_to_ledger,
)
from sourdough.agent_desk import desk_directory
from sourdough.company import FoundingTerms, found
from sourdough.trading.sandbox_tools import AgentSandboxTools
from sourdough.ipc import CompanyDaemon, ControlPlaneClient
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer


_SANDBOX_TOOLS_SPEC: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="open_agent_sandbox",
        description="Open isolated Tier 1 sandbox account for this Agent.",
        parameters={
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "starting_minor_units": {"type": "integer"},
                "fee_bps": {"type": "integer"},
                "slippage_bps": {"type": "integer"},
            },
            "required": ["account_id", "starting_minor_units"],
        },
    ),
    ToolSpec(
        name="submit_agent_sandbox_order",
        description="Submit simulated order in this Agent's Tier 1 sandbox.",
        parameters={
            "type": "object",
            "properties": {
                "action_id": {"type": "string"},
                "instrument_id": {"type": "string"},
                "side": {"type": "string"},
                "quantity": {"type": "integer"},
                "limit_minor_units": {"type": "integer"},
                "market_price_minor_units": {"type": "integer"},
            },
            "required": [
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
                "market_price_minor_units",
            ],
        },
    ),
)

_CLI_TRADER_SHIM = """\
import json
import os
import socket
import sys

_ = json.loads(sys.stdin.read())
sock_path = os.environ["SOURDOUGH_BROKER_SOCKET"]

def invoke(call_id, tool, arguments):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        s.sendall((json.dumps({"call_id": call_id, "tool": tool, "arguments": arguments}) + "\\n").encode())
        buf = b""
        while b"\\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode())

r1 = invoke("cli-open", "open_agent_sandbox", {
    "account_id": "sbx-codex-01",
    "starting_minor_units": 10000000,
    "fee_bps": 4,
    "slippage_bps": 2,
})
assert r1["ok"], r1
r2 = invoke("cli-order", "submit_agent_sandbox_order", {
    "action_id": "act-codex-01",
    "instrument_id": "BTC-USDT",
    "side": "buy",
    "quantity": 1,
    "limit_minor_units": 6600000,
    "market_price_minor_units": 6500000,
})
assert r2["ok"], r2
print(json.dumps({
    "type": "terminal",
    "output_text": "Codex CLI agent completed sandbox buy.",
    "usage": {"input_tokens": 1800, "output_tokens": 420},
}))
"""


class _ConnectedProviderMock:
    """Loopback multi-provider server driving real open + order sandbox tool calls."""

    def __init__(self) -> None:
        self._openai_step = 0
        self._anthropic_step = 0
        self._gemini_step = 0
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                _ = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/v1/responses":
                    parent._openai_step += 1
                    if parent._openai_step == 1:
                        self._send(
                            {
                                "id": "oa-1",
                                "output": [
                                    {
                                        "type": "function_call",
                                        "call_id": "oa-open",
                                        "name": "open_agent_sandbox",
                                        "arguments": json.dumps(
                                            {
                                                "account_id": "sbx-openai-01",
                                                "starting_minor_units": 6_000_000,
                                                "fee_bps": 5,
                                                "slippage_bps": 3,
                                            }
                                        ),
                                    }
                                ],
                                "usage": {"input_tokens": 1200, "output_tokens": 200},
                            }
                        )
                    elif parent._openai_step == 2:
                        self._send(
                            {
                                "id": "oa-2",
                                "output": [
                                    {
                                        "type": "function_call",
                                        "call_id": "oa-ord",
                                        "name": "submit_agent_sandbox_order",
                                        "arguments": json.dumps(
                                            {
                                                "action_id": "act-openai-01",
                                                "instrument_id": "ETH-USDT",
                                                "side": "buy",
                                                "quantity": 2,
                                                "limit_minor_units": 360_000,
                                                "market_price_minor_units": 350_000,
                                            }
                                        ),
                                    }
                                ],
                                "usage": {"input_tokens": 1100, "output_tokens": 180},
                            }
                        )
                    else:
                        self._send(
                            {
                                "id": "oa-3",
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "OpenAI quant executed ETH-USDT sandbox buy.",
                                            }
                                        ],
                                    }
                                ],
                                "usage": {"input_tokens": 900, "output_tokens": 150},
                            }
                        )
                elif self.path == "/v1/messages":
                    parent._anthropic_step += 1
                    if parent._anthropic_step == 1:
                        self._send(
                            {
                                "id": "ant-1",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "ant-open",
                                        "name": "open_agent_sandbox",
                                        "input": {
                                            "account_id": "sbx-anthropic-01",
                                            "starting_minor_units": 7_000_000,
                                            "fee_bps": 4,
                                            "slippage_bps": 2,
                                        },
                                    }
                                ],
                                "usage": {"input_tokens": 1400, "output_tokens": 220},
                            }
                        )
                    elif parent._anthropic_step == 2:
                        self._send(
                            {
                                "id": "ant-2",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "ant-ord",
                                        "name": "submit_agent_sandbox_order",
                                        "input": {
                                            "action_id": "act-anthropic-01",
                                            "instrument_id": "SOL-USDT",
                                            "side": "buy",
                                            "quantity": 5,
                                            "limit_minor_units": 20_000,
                                            "market_price_minor_units": 19_000,
                                        },
                                    }
                                ],
                                "usage": {"input_tokens": 1300, "output_tokens": 210},
                            }
                        )
                    else:
                        self._send(
                            {
                                "id": "ant-3",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Anthropic quant executed SOL-USDT sandbox buy.",
                                    }
                                ],
                                "usage": {"input_tokens": 800, "output_tokens": 140},
                            }
                        )
                elif self.path.endswith(":generateContent"):
                    parent._gemini_step += 1
                    if parent._gemini_step == 1:
                        self._send(
                            {
                                "candidates": [
                                    {
                                        "content": {
                                            "role": "model",
                                            "parts": [
                                                {
                                                    "functionCall": {
                                                        "id": "gem-open",
                                                        "name": "open_agent_sandbox",
                                                        "args": {
                                                            "account_id": "sbx-gemini-01",
                                                            "starting_minor_units": 8_000_000,
                                                            "fee_bps": 3,
                                                            "slippage_bps": 1,
                                                        },
                                                    }
                                                }
                                            ],
                                        }
                                    }
                                ],
                                "usageMetadata": {
                                    "promptTokenCount": 1100,
                                    "candidatesTokenCount": 160,
                                },
                            }
                        )
                    elif parent._gemini_step == 2:
                        self._send(
                            {
                                "candidates": [
                                    {
                                        "content": {
                                            "role": "model",
                                            "parts": [
                                                {
                                                    "functionCall": {
                                                        "id": "gem-ord",
                                                        "name": "submit_agent_sandbox_order",
                                                        "args": {
                                                            "action_id": "act-gemini-01",
                                                            "instrument_id": "ARB-USDT",
                                                            "side": "buy",
                                                            "quantity": 10,
                                                            "limit_minor_units": 15_000,
                                                            "market_price_minor_units": 14_000,
                                                        },
                                                    }
                                                }
                                            ],
                                        }
                                    }
                                ],
                                "usageMetadata": {
                                    "promptTokenCount": 1000,
                                    "candidatesTokenCount": 140,
                                },
                            }
                        )
                    else:
                        self._send(
                            {
                                "candidates": [
                                    {
                                        "content": {
                                            "role": "model",
                                            "parts": [
                                                {
                                                    "text": "Gemini quant executed ARB-USDT sandbox buy."
                                                }
                                            ],
                                        }
                                    }
                                ],
                                "usageMetadata": {
                                    "promptTokenCount": 700,
                                    "candidatesTokenCount": 110,
                                },
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
        mock = _ConnectedProviderMock()
        try:
            with (
                TemporaryDirectory(prefix="sd-a-") as tmp,
                TemporaryDirectory(prefix="sd-", dir="/tmp") as sock_tmp,
            ):
                root = Path(tmp)
                store = root / "c.sqlite"
                leases = root / "l"
                sock_dir = Path(sock_tmp)
                shim = root / "cli_trader.py"
                shim.write_text(_CLI_TRADER_SHIM, encoding="utf-8")

                secret_canaries = {
                    "envref:FLOW_OPENAI_KEY": "canary-flow-openai-secret-4101",
                    "envref:FLOW_ANTHROPIC_KEY": "canary-flow-anthropic-secret-4202",
                    "envref:FLOW_GEMINI_KEY": "canary-flow-gemini-secret-4303",
                }
                resolver = InMemorySecretResolver(secret_canaries)
                registry = build_default_adapter_registry(
                    secret_resolver=resolver,
                    openai_base_url=mock.base_url,
                    anthropic_base_url=mock.base_url,
                    gemini_base_url=mock.base_url,
                    allow_loopback_override=True,
                    codex_executable=(sys.executable, str(shim)),
                    codex_use_macos_sandbox=(sys.platform == "darwin"),
                    codex_extra_read_files=(shim,),
                )

                codex_profile = AgentProfileBinding(
                    provider=PROVIDER_CODEX_CLI,
                    model="codex-1",
                    revision="2026-11",
                )
                openai_profile = AgentProfileBinding(
                    provider=PROVIDER_OPENAI,
                    model="gpt-5-mini",
                    revision="2026-11",
                    credential_handle_ref="envref:FLOW_OPENAI_KEY",
                )
                anthropic_profile = AgentProfileBinding(
                    provider=PROVIDER_ANTHROPIC,
                    model="claude-sonnet-4-5",
                    revision="2026-11",
                    credential_handle_ref="envref:FLOW_ANTHROPIC_KEY",
                )
                gemini_profile = AgentProfileBinding(
                    provider=PROVIDER_GEMINI,
                    model="gemini-2.5-pro",
                    revision="2026-11",
                    credential_handle_ref="envref:FLOW_GEMINI_KEY",
                )

                founding = found(
                    store,
                    FoundingTerms(
                        "sourdough-fund",
                        "charter-v1",
                        "paper",
                        codex_profile.profile_id(),
                    ),
                )
                company_id = founding.company_id
                agents: list[tuple[str, AgentProfileBinding]] = [
                    (founding.founder_id, codex_profile),
                    ("agent-openai", openai_profile),
                    ("agent-anthropic", anthropic_profile),
                    ("agent-gemini", gemini_profile),
                ]

                with hold_company_writer(
                    store, company_id, lease_directory=leases
                ) as writer:
                    with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                        for agent_id, profile in agents[1:]:
                            db.execute(
                                "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                                (agent_id, company_id, profile.profile_id()),
                            )
                            db.execute(
                                "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                                (f"grant-{agent_id}", agent_id, "paper"),
                            )

                    # Execute one real multi-turn sandbox trading shift per provider.
                    settlement_ids: list[str] = []
                    for idx, (agent_id, profile) in enumerate(agents, start=1):
                        with closing(open_reader(store)) as db, read_tx(db):
                            persisted = str(
                                db.execute(
                                    "SELECT execution_profile FROM agent WHERE id = ?",
                                    (agent_id,),
                                ).fetchone()[0]
                            )
                        sandbox_tools = AgentSandboxTools(
                            writer=writer, agent_id=agent_id
                        )
                        broker = OuterToolBroker(
                            company_id=company_id,
                            agent_id=agent_id,
                            run_id=f"run-provider-{idx}",
                            fencing_token=idx,
                            handlers={
                                "open_agent_sandbox": lambda a,
                                t=sandbox_tools: t.invoke("open_agent_sandbox", a),
                                "submit_agent_sandbox_order": lambda a,
                                t=sandbox_tools: t.invoke(
                                    "submit_agent_sandbox_order", a
                                ),
                            },
                        )
                        desk = desk_directory(store, agent_id)
                        desk.mkdir(parents=True, exist_ok=True)
                        ctx = hydrate_shift_context(
                            company_id=company_id,
                            agent_id=agent_id,
                            run_id=f"run-provider-{idx}",
                            fencing_token=idx,
                            grant_scope="paper",
                            mandate_summary="Run isolated Tier 1 strategy sandbox experiment",
                        )
                        req = ShiftRequest(
                            company_id=company_id,
                            agent_id=agent_id,
                            run_id=f"run-provider-{idx}",
                            turn_id="turn-1",
                            fencing_token=idx,
                            instruction="Open personal Tier 1 sandbox and execute one test buy.",
                            context=ctx,
                            tools=_SANDBOX_TOOLS_SPEC,
                            budget_ceiling_minor_units=10_000,
                            desk_path=desk,
                            store_path=store,
                        )
                        result = registry.execute_shift(
                            persisted_profile_id=persisted,
                            profile=profile,
                            request=req,
                            broker=broker,
                        )
                        self.assertEqual(result.provider, profile.provider)
                        self.assertEqual(result.tool_calls_executed, 2)
                        self.assertGreater(result.usage.input_tokens, 0)

                        settlement = settle_shift_cost_to_ledger(
                            writer,
                            profile=profile,
                            run_id=req.run_id,
                            turn_id=req.turn_id,
                            usage=result.usage,
                            budget_ceiling_minor_units=req.budget_ceiling_minor_units,
                        )
                        self.assertIsNotNone(settlement.journal_entry_id)
                        settlement_ids.append(str(settlement.journal_entry_id))

                    # Cross-provider spoofing fails closed (OpenAI agent cannot be run as Gemini).
                    with self.assertRaises(AgentProfileMismatch):
                        registry.execute_shift(
                            persisted_profile_id=openai_profile.profile_id(),
                            profile=gemini_profile,
                            request=req,
                            broker=broker,
                        )

                # Verify zero API secret canary ever touched the company SQLite database.
                raw_sqlite = store.read_bytes()
                for canary in secret_canaries.values():
                    self.assertNotIn(canary.encode("utf-8"), raw_sqlite)

                # Verify 4 distinct balanced GL entries for LLM compute spend.
                self.assertEqual(len(set(settlement_ids)), 4)
                with closing(open_reader(store)) as db, read_tx(db):
                    gl_count = db.execute(
                        "SELECT COUNT(*) FROM journal_entry WHERE mapping_revision = 'agent-llm-cost-v1'"
                    ).fetchone()[0]
                    self.assertEqual(gl_count, 4)

                # Observe all 4 isolated Agent sandboxes over the CompanyDaemon Control Plane.
                with CompanyDaemon(
                    store,
                    company_id,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                ) as daemon:
                    client = ControlPlaneClient(daemon.socket_path)
                    status, _etag, graph = client.projection(
                        "agents-graph", expected_company_id=company_id
                    )
                    self.assertEqual(status, 200)
                    sandboxes = [
                        (node["agent_id"], node["sandbox_activity"])
                        for node in graph["nodes"]
                        if node.get("sandbox_activity") is not None
                    ]
                    self.assertEqual(
                        {aid for aid, _ in sandboxes},
                        {
                            founding.founder_id,
                            "agent-openai",
                            "agent-anthropic",
                            "agent-gemini",
                        },
                    )
                    for _aid, sbx in sandboxes:
                        self.assertEqual(sbx["fill_count"], 1)
                        self.assertEqual(sbx["open_positions_count"], 1)
        finally:
            mock.close()


if __name__ == "__main__":
    unittest.main()
