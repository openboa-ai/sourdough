"""Unit tests for multi-provider Agent runtime adapters, OuterToolBroker, and GL cost accounting."""

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
    AgentAdapterRegistry,
    AgentBudgetExceeded,
    AgentProfileBinding,
    AgentProfileMismatch,
    AgentRuntimeRefused,
    AgentTransportInterrupted,
    AnthropicMessagesAdapter,
    CodexCliAdapter,
    EnvSecretResolver,
    GeminiGenerateContentAdapter,
    InMemorySecretResolver,
    MaskedSecret,
    ModelPricing,
    OpenAiResponsesAdapter,
    OuterToolBroker,
    ShiftRequest,
    TokenUsage,
    ToolSpec,
    build_default_adapter_registry,
    compute_usage_cost_minor_units,
    hydrate_shift_context,
    settle_shift_cost_to_ledger,
)
from sourdough.agent_desk import desk_directory
from sourdough.company import FoundingTerms, found
from sourdough.storage.db import open_reader, read_tx
from sourdough.writer_lease import hold_company_writer


class _MultiProviderMockServer:
    """Loopback HTTP server speaking OpenAI Responses, Anthropic Messages, and Gemini APIs."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.fail_next_status: int | None = None
        self.fail_after_tool_status: int | None = None
        self.inject_spoofed_tool_arg = False
        self._lock = threading.Lock()
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(raw.decode("utf-8"))
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                with parent._lock:
                    parent.requests.append((self.path, hdrs, payload))
                    forced = parent.fail_next_status
                    parent.fail_next_status = None
                if forced is not None:
                    self._send_json(forced, {"error": f"simulated-{forced}"})
                    return

                if self.path == "/v1/responses":
                    self._handle_openai(hdrs, payload)
                elif self.path == "/v1/messages":
                    self._handle_anthropic(hdrs, payload)
                elif self.path.startswith("/v1beta/models/") and self.path.endswith(
                    ":generateContent"
                ):
                    self._handle_gemini(hdrs, payload)
                else:
                    self._send_json(404, {"error": "unknown route"})

            def _handle_openai(
                self, hdrs: dict[str, str], payload: dict[str, Any]
            ) -> None:
                if hdrs.get("authorization") != "Bearer canary-openai-token-9911":
                    self._send_json(401, {"error": "unauthorized"})
                    return
                if payload.get("previous_response_id"):
                    with parent._lock:
                        if parent.fail_after_tool_status is not None:
                            code = parent.fail_after_tool_status
                            parent.fail_after_tool_status = None
                            self._send_json(code, {"error": "post-tool outage"})
                            return
                    self._send_json(
                        200,
                        {
                            "id": "resp-2",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": "OpenAI trade recorded.",
                                        }
                                    ],
                                }
                            ],
                            "usage": {"input_tokens": 1200, "output_tokens": 300},
                        },
                    )
                    return
                tool_args: dict[str, Any] = {"symbol": "BTC-USDT", "qty": 2}
                if parent.inject_spoofed_tool_arg:
                    tool_args["agent_id"] = "hijacked-agent"
                self._send_json(
                    200,
                    {
                        "id": "resp-1",
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call-oa-1",
                                "name": "record_note",
                                "arguments": json.dumps(tool_args),
                            }
                        ],
                        "usage": {"input_tokens": 1000, "output_tokens": 200},
                    },
                )

            def _handle_anthropic(
                self, hdrs: dict[str, str], payload: dict[str, Any]
            ) -> None:
                if hdrs.get("x-api-key") != "canary-anthropic-token-9922":
                    self._send_json(401, {"error": "unauthorized"})
                    return
                if hdrs.get("anthropic-version") != "2023-06-01":
                    self._send_json(400, {"error": "missing version"})
                    return
                messages = payload.get("messages", [])
                if len(messages) == 1:
                    self._send_json(
                        200,
                        {
                            "id": "msg-1",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu-ant-1",
                                    "name": "record_note",
                                    "input": {"symbol": "ETH-USDT", "qty": 3},
                                }
                            ],
                            "usage": {"input_tokens": 1500, "output_tokens": 250},
                        },
                    )
                    return
                self._send_json(
                    200,
                    {
                        "id": "msg-2",
                        "content": [
                            {"type": "text", "text": "Anthropic trade recorded."}
                        ],
                        "usage": {"input_tokens": 800, "output_tokens": 150},
                    },
                )

            def _handle_gemini(
                self, hdrs: dict[str, str], payload: dict[str, Any]
            ) -> None:
                if hdrs.get("x-goog-api-key") != "canary-gemini-token-9933":
                    self._send_json(403, {"error": "forbidden"})
                    return
                contents = payload.get("contents", [])
                if len(contents) == 1:
                    self._send_json(
                        200,
                        {
                            "candidates": [
                                {
                                    "content": {
                                        "role": "model",
                                        "parts": [
                                            {
                                                "functionCall": {
                                                    "id": "fc-gem-1",
                                                    "name": "record_note",
                                                    "args": {
                                                        "symbol": "SOL-USDT",
                                                        "qty": 5,
                                                    },
                                                }
                                            }
                                        ],
                                    }
                                }
                            ],
                            "usageMetadata": {
                                "promptTokenCount": 900,
                                "candidatesTokenCount": 100,
                            },
                        },
                    )
                    return
                self._send_json(
                    200,
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": "Gemini trade recorded."}],
                                }
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 600,
                            "candidatesTokenCount": 200,
                        },
                    },
                )

            def _send_json(self, status: int, body: dict[str, Any]) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
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


_CLI_SHIM_SOURCE = """\
import json
import os
import socket
import sys
import time

payload = json.loads(sys.stdin.read())
mode = os.environ.get("SHIM_MODE", "normal")
sock_path = os.environ["SOURDOUGH_BROKER_SOCKET"]

if "OPENAI_API_KEY" in os.environ or "BINANCE_API_SECRET" in os.environ:
    sys.stderr.write("leaked host env var")
    sys.exit(9)

if payload["instruction"] == "hang":
    time.sleep(5)
    sys.exit(0)

def call_broker(call_id, tool, arguments):
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

if payload["instruction"] == "spoof":
    res = call_broker("cli-spoof-1", "record_note", {"symbol": "BTC", "qty": 1, "agent_id": "other"})
    if not res["ok"]:
        sys.exit(2)

res = call_broker("cli-call-1", "record_note", {"symbol": "BTC-USDT", "qty": 4})
if not res["ok"]:
    sys.stderr.write(res.get("error", "broker error"))
    sys.exit(3)

print(json.dumps({
    "type": "terminal",
    "output_text": f"Codex CLI recorded {res['result']['recorded']}",
    "usage": {"input_tokens": 2000, "output_tokens": 500},
}))
"""


class AgentRuntimeAdaptersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = _MultiProviderMockServer()
        self.resolver = InMemorySecretResolver(
            {
                "envref:OPENAI_KEY": "canary-openai-token-9911",
                "envref:ANTHROPIC_KEY": "canary-anthropic-token-9922",
                "envref:GEMINI_KEY": "canary-gemini-token-9933",
            }
        )

    def tearDown(self) -> None:
        self.mock.close()

    def _make_request(
        self,
        *,
        instruction: str = "Record one trade note",
        timeout_seconds: float = 10.0,
        desk_path: Path | None = None,
        store_path: Path | None = None,
    ) -> ShiftRequest:
        ctx = hydrate_shift_context(
            company_id="co-1",
            agent_id="agent-1",
            run_id="run-1",
            fencing_token=7,
            grant_scope="paper",
            mandate_summary="Test trading mandate",
            open_receipts=({"action_id": "open-1", "status": "pending"},),
            history_notes=("note-1", "note-2"),
        )
        return ShiftRequest(
            company_id="co-1",
            agent_id="agent-1",
            run_id="run-1",
            turn_id="turn-1",
            fencing_token=7,
            instruction=instruction,
            context=ctx,
            tools=(
                ToolSpec(
                    name="record_note",
                    description="Record a trade note",
                    parameters={
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "qty": {"type": "integer"},
                        },
                        "required": ["symbol", "qty"],
                    },
                ),
            ),
            budget_ceiling_minor_units=10_000,
            max_tool_calls=4,
            timeout_seconds=timeout_seconds,
            desk_path=desk_path,
            store_path=store_path,
        )

    def test_masked_secret_and_env_resolver(self) -> None:
        secret = MaskedSecret(value="test-dummy-value", handle_ref="envref:MY_KEY")
        self.assertNotIn("test-dummy-value", repr(secret))
        self.assertNotIn("test-dummy-value", str(secret))
        resolver = EnvSecretResolver({"MY_KEY": "test-dummy-value"})
        resolved = resolver.resolve_secret("envref:MY_KEY")
        self.assertEqual(resolved.value, "test-dummy-value")
        with self.assertRaises(AgentRuntimeRefused):
            resolver.resolve_secret("raw-inline-secret")

    def test_all_three_http_adapters_tool_loop_and_usage(self) -> None:
        recorded: list[dict[str, Any]] = []
        registry = build_default_adapter_registry(
            secret_resolver=self.resolver,
            openai_base_url=self.mock.base_url,
            anthropic_base_url=self.mock.base_url,
            gemini_base_url=self.mock.base_url,
            allow_loopback_override=True,
        )
        cases = [
            (
                AgentProfileBinding(
                    provider=PROVIDER_OPENAI,
                    model="gpt-5-mini",
                    revision="2026-11",
                    credential_handle_ref="envref:OPENAI_KEY",
                ),
                "OpenAI trade recorded.",
                TokenUsage(2200, 500),
            ),
            (
                AgentProfileBinding(
                    provider=PROVIDER_ANTHROPIC,
                    model="claude-sonnet-4-5",
                    revision="2026-11",
                    credential_handle_ref="envref:ANTHROPIC_KEY",
                ),
                "Anthropic trade recorded.",
                TokenUsage(2300, 400),
            ),
            (
                AgentProfileBinding(
                    provider=PROVIDER_GEMINI,
                    model="gemini-2.5-pro",
                    revision="2026-11",
                    credential_handle_ref="envref:GEMINI_KEY",
                ),
                "Gemini trade recorded.",
                TokenUsage(1500, 300),
            ),
        ]
        for profile, expected_text, expected_usage in cases:
            broker = OuterToolBroker(
                company_id="co-1",
                agent_id="agent-1",
                run_id="run-1",
                fencing_token=7,
                handlers={
                    "record_note": lambda args: (
                        recorded.append(args) or {"recorded": args["symbol"]}
                    )
                },
            )
            result = registry.execute_shift(
                persisted_profile_id=profile.profile_id(),
                profile=profile,
                request=self._make_request(),
                broker=broker,
            )
            self.assertEqual(result.provider, profile.provider)
            self.assertEqual(result.output_text, expected_text)
            self.assertEqual(result.usage, expected_usage)
            self.assertEqual(result.tool_calls_executed, 1)
        self.assertEqual(
            [r["symbol"] for r in recorded], ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
        )

    def test_codex_cli_adapter_uds_broker_sandbox_and_timeout(self) -> None:
        with TemporaryDirectory(prefix="sd-cli-unit-") as tmp:
            root = Path(tmp)
            shim = root / "shim.py"
            shim.write_text(_CLI_SHIM_SOURCE, encoding="utf-8")
            store = root / "company.sqlite"
            store.write_bytes(b"")
            desk = desk_directory(store, "agent-1")
            desk.mkdir(parents=True, exist_ok=True)

            profile = AgentProfileBinding(
                provider=PROVIDER_CODEX_CLI,
                model="codex-1",
                revision="2026-11",
            )
            adapter = CodexCliAdapter(
                executable=(sys.executable, str(shim)),
                use_macos_sandbox=(sys.platform == "darwin"),
                extra_read_files=(shim,),
            )
            seen: list[dict[str, Any]] = []
            broker = OuterToolBroker(
                company_id="co-1",
                agent_id="agent-1",
                run_id="run-1",
                fencing_token=7,
                handlers={
                    "record_note": lambda args: (
                        seen.append(args) or {"recorded": args["symbol"]}
                    )
                },
            )
            result = adapter.run_shift(
                profile,
                self._make_request(desk_path=desk, store_path=store),
                broker,
            )
            self.assertEqual(result.output_text, "Codex CLI recorded BTC-USDT")
            self.assertEqual(result.usage, TokenUsage(2000, 500))
            self.assertEqual(result.tool_calls_executed, 1)
            self.assertEqual(len(seen), 1)

            # Spoofed identity argument over UDS is refused fail-closed.
            spoof_broker = OuterToolBroker(
                company_id="co-1",
                agent_id="agent-1",
                run_id="run-1",
                fencing_token=7,
                handlers={"record_note": lambda args: {"recorded": "never"}},
            )
            with self.assertRaises(AgentRuntimeRefused):
                adapter.run_shift(
                    profile,
                    self._make_request(
                        instruction="spoof", desk_path=desk, store_path=store
                    ),
                    spoof_broker,
                )
            self.assertEqual(spoof_broker.call_count, 0)

            # Timeout kills hung CLI subprocess cleanly.
            hang_broker = OuterToolBroker(
                company_id="co-1",
                agent_id="agent-1",
                run_id="run-1",
                fencing_token=7,
                handlers={"record_note": lambda args: {"recorded": "never"}},
            )
            with self.assertRaises(AgentTransportInterrupted):
                adapter.run_shift(
                    profile,
                    self._make_request(
                        instruction="hang",
                        timeout_seconds=0.3,
                        desk_path=desk,
                        store_path=store,
                    ),
                    hang_broker,
                )

    def test_profile_mismatch_zero_fallback_and_loopback_guards(self) -> None:
        openai_profile = AgentProfileBinding(
            provider=PROVIDER_OPENAI,
            model="gpt-5-mini",
            revision="2026-11",
            credential_handle_ref="envref:OPENAI_KEY",
        )
        anthropic_profile = AgentProfileBinding(
            provider=PROVIDER_ANTHROPIC,
            model="claude-sonnet-4-5",
            revision="2026-11",
            credential_handle_ref="envref:ANTHROPIC_KEY",
        )
        registry = AgentAdapterRegistry()
        registry.register(
            OpenAiResponsesAdapter(
                secret_resolver=self.resolver,
                base_url_override=self.mock.base_url,
                allow_loopback_override=True,
            )
        )
        broker = OuterToolBroker(
            company_id="co-1",
            agent_id="agent-1",
            run_id="run-1",
            fencing_token=1,
            handlers={},
        )
        # Mismatched persisted profile -> AgentProfileMismatch.
        with self.assertRaises(AgentProfileMismatch):
            registry.execute_shift(
                persisted_profile_id=anthropic_profile.profile_id(),
                profile=openai_profile,
                request=self._make_request(),
                broker=broker,
            )
        # Missing provider adapter -> AgentRuntimeRefused (never silently falls back to OpenAI).
        with self.assertRaises(AgentRuntimeRefused):
            registry.execute_shift(
                persisted_profile_id=anthropic_profile.profile_id(),
                profile=anthropic_profile,
                request=self._make_request(),
                broker=broker,
            )
        # Non-loopback or non-opted-in override refused.
        with self.assertRaises(AgentRuntimeRefused):
            OpenAiResponsesAdapter(base_url_override="http://127.0.0.1:9999")
        with self.assertRaises(AgentRuntimeRefused):
            AnthropicMessagesAdapter(
                base_url_override="https://evil.example.com",
                allow_loopback_override=True,
            )
        with self.assertRaises(AgentRuntimeRefused):
            GeminiGenerateContentAdapter(
                base_url_override="https://192.0.2.1",
                allow_loopback_override=True,
            )

    def test_outer_broker_spoofing_idempotency_and_cap(self) -> None:
        calls = 0

        def handler(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {"count": calls, "symbol": args["symbol"]}

        broker = OuterToolBroker(
            company_id="co-1",
            agent_id="agent-1",
            run_id="run-1",
            fencing_token=2,
            handlers={"trade": handler},
            max_tool_calls=2,
        )
        first = broker.invoke("c1", "trade", {"symbol": "BTC"})
        replay = broker.invoke("c1", "trade", {"symbol": "BTC"})
        self.assertEqual(first, replay)
        self.assertEqual(calls, 1)

        with self.assertRaises(AgentRuntimeRefused):
            broker.invoke("c1", "trade", {"symbol": "ETH"})
        with self.assertRaises(AgentRuntimeRefused):
            broker.invoke("c2", "unknown_tool", {"symbol": "BTC"})
        for forbidden in ("agent_id", "company_id", "run_id", "fencing_token", "tier"):
            with self.assertRaises(AgentRuntimeRefused):
                broker.invoke(f"spoof-{forbidden}", "trade", {forbidden: "x"})

        broker.invoke("c2", "trade", {"symbol": "ETH"})
        with self.assertRaises(AgentRuntimeRefused):
            broker.invoke("c3", "trade", {"symbol": "SOL"})

    def test_context_compaction_preserves_open_receipts(self) -> None:
        receipts = (
            {"action_id": "ord-1", "status": "uncertain"},
            {"action_id": "ord-2", "status": "open"},
        )
        notes = tuple(f"historical-note-{i}-" + ("x" * 120) for i in range(15))
        pack = hydrate_shift_context(
            company_id="co-1",
            agent_id="agent-1",
            run_id="run-1",
            fencing_token=3,
            grant_scope="paper",
            mandate_summary="Preserve open receipts always",
            open_receipts=receipts,
            history_notes=notes,
            max_context_bytes=700,
        )
        self.assertTrue(pack.compacted)
        self.assertEqual(pack.open_receipts, receipts)
        self.assertEqual(pack.grant_scope, "paper")
        self.assertLess(len(pack.history_notes), len(notes))

    def test_transport_interruption_preserves_committed_tool_receipt(self) -> None:
        self.mock.fail_after_tool_status = 503
        committed: list[dict[str, Any]] = []
        profile = AgentProfileBinding(
            provider=PROVIDER_OPENAI,
            model="gpt-5-mini",
            revision="2026-11",
            credential_handle_ref="envref:OPENAI_KEY",
        )
        adapter = OpenAiResponsesAdapter(
            secret_resolver=self.resolver,
            base_url_override=self.mock.base_url,
            allow_loopback_override=True,
        )
        broker = OuterToolBroker(
            company_id="co-1",
            agent_id="agent-1",
            run_id="run-1",
            fencing_token=7,
            handlers={"record_note": lambda a: committed.append(a) or {"ok": True}},
        )
        with self.assertRaises(AgentTransportInterrupted):
            adapter.run_shift(profile, self._make_request(), broker)
        self.assertEqual(len(committed), 1)

        # HTTP 401 -> AgentRuntimeRefused without secret leakage.
        self.mock.fail_next_status = 401
        with self.assertRaises(AgentRuntimeRefused) as ctx:
            adapter.run_shift(profile, self._make_request(), broker)
        self.assertNotIn("canary-openai-token-9911", str(ctx.exception))

    def test_cost_accounting_gl_posting_idempotency_and_budget_cap(self) -> None:
        pricing = ModelPricing(input_micro_per_1k=2_500, output_micro_per_1k=10_000)
        # 4000 * 2500 + 2000 * 10000 = 30,000,000 micro / 10,000,000 = 3 minor units
        self.assertEqual(
            compute_usage_cost_minor_units(TokenUsage(4000, 2000), pricing), 3
        )
        self.assertEqual(compute_usage_cost_minor_units(TokenUsage(0, 0), pricing), 0)

        with TemporaryDirectory(prefix="sd-gl-cost-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms("sourdough-fund", "charter-v1", "paper", "profile-v1"),
            )
            profile = AgentProfileBinding(
                provider=PROVIDER_ANTHROPIC,
                model="claude-sonnet-4-5",
                revision="2026-11",
                credential_handle_ref="envref:ANTHROPIC_KEY",
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as writer:
                s1 = settle_shift_cost_to_ledger(
                    writer,
                    profile=profile,
                    run_id="run-cost-1",
                    turn_id="turn-1",
                    usage=TokenUsage(10_000, 5_000),
                    budget_ceiling_minor_units=500,
                )
                s2 = settle_shift_cost_to_ledger(
                    writer,
                    profile=profile,
                    run_id="run-cost-1",
                    turn_id="turn-1",
                    usage=TokenUsage(10_000, 5_000),
                    budget_ceiling_minor_units=500,
                )
                self.assertEqual(s1.journal_entry_id, s2.journal_entry_id)
                self.assertGreater(s1.cost_minor_units, 0)
                self.assertEqual(
                    s1.released_unused_minor_units, 500 - s1.cost_minor_units
                )

                with self.assertRaises(AgentBudgetExceeded):
                    settle_shift_cost_to_ledger(
                        writer,
                        profile=profile,
                        run_id="run-cost-2",
                        turn_id="turn-1",
                        usage=TokenUsage(1_000_000, 500_000),
                        budget_ceiling_minor_units=5,
                    )

            with closing(open_reader(store)) as db, read_tx(db):
                rows = db.execute(
                    """SELECT account, amount_minor
                       FROM journal_line
                       WHERE entry_id = ?
                       ORDER BY account""",
                    (s1.journal_entry_id,),
                ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        ("expense:agent_llm_compute", s1.cost_minor_units),
                        ("liability:accounts_payable", -s1.cost_minor_units),
                    ],
                )


if __name__ == "__main__":
    unittest.main()
