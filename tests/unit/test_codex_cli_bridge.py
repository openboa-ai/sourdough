"""Unit tests for dual-mode CodexCliAdapter, stdio MCP->UDS bridge, and JSONL parsing."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sourdough.agent_desk import desk_directory
from sourdough.agent_runtime import (
    PROVIDER_CODEX_CLI,
    AgentProfileBinding,
    AgentRuntimeRefused,
    AgentTransportInterrupted,
    CodexCliAdapter,
    OuterToolBroker,
    ShiftRequest,
    TokenUsage,
    ToolSpec,
    hydrate_shift_context,
)
from sourdough.agent_runtime.codex_cli import confinement_arguments
from sourdough.duty_run import DutyRefused
from sourdough.runtime.work_kernel import RunFenced

_FAKE_CODEX_SCRIPT = """\
import json
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
mode = Path("mode.txt").read_text(encoding="utf-8").strip()
prompt = sys.stdin.read()

# Extract -c mcp_servers.sourdough.command=... and .args=...
mcp_cmd = None
mcp_args = None
for i, tok in enumerate(args):
    if tok == "-c" and i + 1 < len(args):
        cfg = args[i + 1]
        if cfg.startswith("mcp_servers.sourdough.command="):
            mcp_cmd = json.loads(cfg.split("=", 1)[1])
        elif cfg.startswith("mcp_servers.sourdough.args="):
            mcp_args = json.loads(cfg.split("=", 1)[1])

assert mcp_cmd and mcp_args, f"Missing MCP config in args: {args}"
assert "--ignore-user-config" in args
assert 'default_permissions="sourdough-agent"' in args
assert "OPENAI_API_KEY" not in os.environ
assert "/opt/homebrew/bin" in os.environ.get("PATH", "")
assert os.environ.get("CODEX_HOME")

bridge = subprocess.Popen(
    [mcp_cmd, *mcp_args],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
assert bridge.stdin is not None and bridge.stdout is not None

def rpc(obj):
    bridge.stdin.write(json.dumps(obj) + "\\n")
    bridge.stdin.flush()
    if "id" not in obj:
        return None
    line = bridge.stdout.readline()
    return json.loads(line)

init_res = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
assert init_res["result"]["serverInfo"]["name"] == "sourdough-broker"
rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
list_res = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
tool_names = [t["name"] for t in list_res["result"]["tools"]]
assert "desk_save_draft" in tool_names

if mode == "ok":
    ok_call = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desk_save_draft", "arguments": {"name": "memo", "content": "hi"}}})
    assert ok_call["result"]["isError"] is False
    bad_call = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "refuse_me", "arguments": {}}})
    assert bad_call["result"]["isError"] is True
    bridge.stdin.close()
    bridge.wait(timeout=5)
    print(json.dumps({"type": "thread.started", "thread_id": "th-1"}))
    print(json.dumps({"type": "item.completed", "item": {"id": "it-1", "type": "agent_message", "text": "Saved memo and handled refusal."}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 320, "output_tokens": 45}}))
elif mode == "fatal_exit0":
    _ = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "fence_me", "arguments": {}}})
    bridge.stdin.close()
    bridge.wait(timeout=5)
    print(json.dumps({"type": "item.completed", "item": {"id": "it-1", "type": "agent_message", "text": "Pretend success"}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}))
elif mode == "turn_failed":
    bridge.stdin.close()
    bridge.wait(timeout=5)
    print(json.dumps({"type": "turn.failed", "error": {"message": "rate limit exceeded"}}))
elif mode == "spoof_usage_exit0":
    _ = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desk_save_draft", "arguments": {"name": "memo", "content": "hi", "actor_id": "evil"}}})
    bridge.stdin.close()
    bridge.wait(timeout=5)
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5000, "output_tokens": 1000}}))
elif mode == "null_args":
    ping_call = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ping", "arguments": None}})
    assert ping_call["result"]["isError"] is False, ping_call
    bridge.stdin.close()
    bridge.wait(timeout=5)
    print(json.dumps({"type": "item.completed", "item": {"id": "it-1", "type": "agent_message", "text": "Ping ok"}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 10}}))
"""


class CodexCliBridgeTests(unittest.TestCase):
    def _make_request(self, desk: Path, store: Path) -> ShiftRequest:
        ctx = hydrate_shift_context(
            company_id="co-1",
            agent_id="agent-1",
            run_id="run-1",
            fencing_token=4,
            grant_scope="manage",
            mandate_summary="Test codex MCP bridge",
        )
        return ShiftRequest(
            company_id="co-1",
            agent_id="agent-1",
            run_id="run-1",
            turn_id="turn-1",
            fencing_token=4,
            instruction="Write memo draft.",
            context=ctx,
            tools=(
                ToolSpec(
                    name="desk_save_draft",
                    description="Save draft",
                    parameters={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["name", "content"],
                    },
                ),
                ToolSpec(
                    name="refuse_me",
                    description="Raises DutyRefused",
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
                ToolSpec(
                    name="fence_me",
                    description="Raises RunFenced",
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
            ),
            budget_ceiling_minor_units=1000,
            desk_path=desk,
            store_path=store,
        )

    def test_confinement_arguments_and_fail_closed_outer_sandbox(self) -> None:
        args = confinement_arguments()
        self.assertIn('default_permissions="sourdough-agent"', args)
        self.assertIn("permissions.sourdough-agent.network.enabled=false", args)

        with TemporaryDirectory(prefix="sd-codex-fail-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            store.write_bytes(b"")
            desk = desk_directory(store, "agent-1")
            adapter = CodexCliAdapter(
                executable=("codex", "exec", "--json"),
                use_macos_sandbox=True,
            )
            profile = AgentProfileBinding(
                provider=PROVIDER_CODEX_CLI, model="gpt-5-codex", revision="2026-11"
            )
            broker = OuterToolBroker(
                company_id="co-1",
                agent_id="agent-1",
                run_id="run-1",
                fencing_token=4,
                handlers={},
            )
            with self.assertRaises(AgentRuntimeRefused):
                adapter.run_shift(profile, self._make_request(desk, store), broker)

    def test_codex_mcp_stdio_bridge_roundtrip_fatal_and_turn_failed(self) -> None:
        with TemporaryDirectory(prefix="sd-codex-mcp-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            store.write_bytes(b"")
            desk = desk_directory(store, "agent-1")
            codex_home = root / "codex-home"
            codex_home.mkdir()
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(_FAKE_CODEX_SCRIPT, encoding="utf-8")

            profile = AgentProfileBinding(
                provider=PROVIDER_CODEX_CLI, model="gpt-5-codex", revision="2026-11"
            )
            adapter = CodexCliAdapter(
                executable=(sys.executable, str(fake_codex)),
                protocol_mode="codex_mcp",
                codex_home=codex_home,
            )
            self.assertTrue(adapter._is_codex_mcp_mode())
            self.assertFalse(
                CodexCliAdapter(
                    executable=(sys.executable, str(fake_codex)),
                    protocol_mode="auto",
                )._is_codex_mcp_mode()
            )

            saved_calls: list[dict[str, Any]] = []

            def _save(args: dict[str, Any]) -> dict[str, Any]:
                saved_calls.append(args)
                return {"ok": True, "name": args["name"]}

            def _refuse(_args: dict[str, Any]) -> dict[str, Any]:
                raise DutyRefused("Domain refusal through MCP")

            def _fence(_args: dict[str, Any]) -> dict[str, Any]:
                raise RunFenced("Run fenced through MCP")

            old_oa_key = os.environ.get("OPENAI_API_KEY")
            os.environ["OPENAI_API_KEY"] = "must-not-leak-to-codex"
            desk.mkdir(parents=True, exist_ok=True)
            try:
                # 1. Normal MCP roundtrip + recoverable refusal + JSONL usage/message parsing.
                (desk / "mode.txt").write_text("ok", encoding="utf-8")
                broker = OuterToolBroker(
                    company_id="co-1",
                    agent_id="agent-1",
                    run_id="run-1",
                    fencing_token=4,
                    handlers={
                        "desk_save_draft": _save,
                        "refuse_me": _refuse,
                        "fence_me": _fence,
                    },
                )
                result = adapter.run_shift(
                    profile, self._make_request(desk, store), broker
                )
                self.assertEqual(result.output_text, "Saved memo and handled refusal.")
                self.assertEqual(result.usage, TokenUsage(320, 45))
                self.assertEqual(broker.accrued_usage, TokenUsage(320, 45))
                self.assertEqual(saved_calls, [{"name": "memo", "content": "hi"}])

                # 2. Fatal RunFenced captured by _UdsToolBrokerServer raises even when CLI exits 0!
                (desk / "mode.txt").write_text("fatal_exit0", encoding="utf-8")
                fatal_broker = OuterToolBroker(
                    company_id="co-1",
                    agent_id="agent-1",
                    run_id="run-1",
                    fencing_token=4,
                    handlers={
                        "desk_save_draft": _save,
                        "refuse_me": _refuse,
                        "fence_me": _fence,
                    },
                )
                with self.assertRaises(RunFenced):
                    adapter.run_shift(
                        profile, self._make_request(desk, store), fatal_broker
                    )

                # 3. turn.failed JSONL event raises AgentTransportInterrupted.
                (desk / "mode.txt").write_text("turn_failed", encoding="utf-8")
                fail_broker = OuterToolBroker(
                    company_id="co-1",
                    agent_id="agent-1",
                    run_id="run-1",
                    fencing_token=4,
                    handlers={
                        "desk_save_draft": _save,
                        "refuse_me": _refuse,
                        "fence_me": _fence,
                    },
                )
                with self.assertRaisesRegex(
                    AgentTransportInterrupted, "rate limit exceeded"
                ):
                    adapter.run_shift(
                        profile, self._make_request(desk, store), fail_broker
                    )

                # 4. MCP arguments: null normalizes to {} instead of crashing with AgentRuntimeRefused.
                (desk / "mode.txt").write_text("null_args", encoding="utf-8")
                ping_calls: list[dict[str, Any]] = []

                def _ping(args: dict[str, Any]) -> dict[str, Any]:
                    ping_calls.append(args)
                    return {"ok": True}

                base_req = self._make_request(desk, store)
                null_req = ShiftRequest(
                    company_id="co-1",
                    agent_id="agent-1",
                    run_id="run-1",
                    turn_id="turn-1",
                    fencing_token=4,
                    instruction="Ping.",
                    context=base_req.context,
                    tools=(
                        *base_req.tools,
                        ToolSpec(
                            name="ping",
                            description="Ping",
                            parameters={
                                "type": "object",
                                "properties": {},
                                "required": [],
                            },
                        ),
                    ),
                    budget_ceiling_minor_units=1000,
                    desk_path=desk,
                    store_path=store,
                )
                null_broker = OuterToolBroker(
                    company_id="co-1",
                    agent_id="agent-1",
                    run_id="run-1",
                    fencing_token=4,
                    handlers={"ping": _ping},
                )
                null_res = adapter.run_shift(profile, null_req, null_broker)
                self.assertEqual(null_res.output_text, "Ping ok")
                self.assertEqual(ping_calls, [{}])
            finally:
                if old_oa_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_oa_key

    def test_codex_cli_posts_gl_on_fatal_exit0_with_usage(self) -> None:
        from contextlib import closing
        from sourdough.agent_runtime import AgentAdapterRegistry
        from sourdough.company import FoundingTerms, found
        from sourdough.runtime import (
            CompanyRuntime,
            build_production_duty_registry,
            create_work_item,
        )
        from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx

        with TemporaryDirectory(prefix="sd-codex-gl-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            codex_home = root / "codex-home"
            codex_home.mkdir()
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(_FAKE_CODEX_SCRIPT, encoding="utf-8")

            profile = AgentProfileBinding(
                provider=PROVIDER_CODEX_CLI,
                model="gpt-5-codex",
                revision="2026-11",
            )
            founding = found(
                store,
                FoundingTerms(
                    "codex-gl-co", "charter-v1", "manage", profile.profile_id()
                ),
            )
            desk = desk_directory(store, founding.founder_id)
            desk.mkdir(parents=True, exist_ok=True)
            (desk / "mode.txt").write_text("spoof_usage_exit0", encoding="utf-8")

            with closing(open_writer(store)) as db, write_tx(db):
                item, _ = create_work_item(
                    db,
                    company_id=founding.company_id,
                    owner_agent_id=founding.founder_id,
                    duty_kind="work.assessment_review",
                    source_kind="manual",
                    source_id="codex-gl-1",
                    payload={"note": "Check GL settlement"},
                    now_ns=100,
                )

            registry = AgentAdapterRegistry()
            registry.register(
                CodexCliAdapter(
                    executable=(sys.executable, str(fake_codex)),
                    protocol_mode="codex_mcp",
                    codex_home=codex_home,
                )
            )
            duty_registry = build_production_duty_registry(
                adapter_registry=registry,
                profile_bindings={founding.founder_id: profile},
            )
            runtime = CompanyRuntime(
                store, founding.company_id, duty_registry, lease_directory=leases
            )

            with self.assertRaises(AgentRuntimeRefused):
                runtime.step(max_dispatches=1, now_ns=1_000)

            with closing(open_reader(store)) as db, read_tx(db):
                gl_count = db.execute(
                    "SELECT COUNT(*) FROM journal_entry WHERE source = 'agent_llm:codex_cli'"
                ).fetchone()
                self.assertEqual(gl_count, (1,))
                ep = db.execute(
                    "SELECT outcome_status, input_tokens, output_tokens FROM agent_episode WHERE work_item_id = ?",
                    (item.work_item_id,),
                ).fetchone()
                self.assertEqual(ep, ("failed", 5000, 1000))

    def test_codex_cli_explicit_binary_path_precedence(self) -> None:
        with TemporaryDirectory(prefix="sd-codex-bin-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            store.write_bytes(b"")
            desk = desk_directory(store, "agent-1")
            desk.mkdir(parents=True, exist_ok=True)
            (desk / "mode.txt").write_text("ok", encoding="utf-8")
            codex_home = root / "codex-home"
            codex_home.mkdir()

            custom_dir = root / "custom-bin"
            custom_dir.mkdir()
            marker = root / "custom_ran.txt"
            custom_codex = custom_dir / "codex"
            custom_codex.write_text(
                f"#!{sys.executable}\n"
                f"from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('CUSTOM_CODEX_RAN', encoding='utf-8')\n"
                + _FAKE_CODEX_SCRIPT,
                encoding="utf-8",
            )
            os.chmod(custom_codex, 0o755)

            adapter = CodexCliAdapter(
                executable=(str(custom_codex),),
                protocol_mode="codex_mcp",
                codex_home=codex_home,
            )
            profile = AgentProfileBinding(
                provider=PROVIDER_CODEX_CLI, model="gpt-5-codex", revision="2026-11"
            )
            broker = OuterToolBroker(
                company_id="co-1",
                agent_id="agent-1",
                run_id="run-1",
                fencing_token=4,
                handlers={
                    "desk_save_draft": lambda a: {"ok": True, "name": a["name"]},
                    "refuse_me": lambda _a: (_ for _ in ()).throw(
                        DutyRefused("refused")
                    ),
                },
            )
            result = adapter.run_shift(profile, self._make_request(desk, store), broker)
            self.assertEqual(marker.read_text(encoding="utf-8"), "CUSTOM_CODEX_RAN")
            self.assertEqual(result.output_text, "Saved memo and handled refusal.")

    def test_codex_cli_reasoning_effort_cached_tokens_and_env_override(self) -> None:
        with self.assertRaisesRegex(AgentRuntimeRefused, "reasoning_effort"):
            CodexCliAdapter(reasoning_effort="   ")

        cached_script = """\
import json, os, sys
args = sys.argv[1:]
sys.stdin.read()
mode = open("effort.txt", encoding="utf-8").read().strip()
if mode == "default":
    assert 'model_reasoning_effort="low"' in args, args
elif mode == "none":
    assert not any("model_reasoning_effort=" in a for a in args), args
print(json.dumps({"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "ok"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 200, "cached_input_tokens": 128, "output_tokens": 15}}))
"""
        with TemporaryDirectory(prefix="sd-codex-cfg-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            store.write_bytes(b"")
            desk = desk_directory(store, "agent-1")
            desk.mkdir(parents=True, exist_ok=True)
            codex_home = root / "codex-home"
            codex_home.mkdir()

            env_codex = root / "env-codex"
            env_codex.write_text(
                f"#!{sys.executable}\n{cached_script}",
                encoding="utf-8",
            )
            os.chmod(env_codex, 0o755)
            profile = AgentProfileBinding(
                provider=PROVIDER_CODEX_CLI, model="gpt-6-luna", revision="2026-11"
            )
            broker = OuterToolBroker(
                company_id="co-1",
                agent_id="agent-1",
                run_id="run-1",
                fencing_token=4,
                handlers={},
            )

            old_env = os.environ.get("SOURDOUGH_CODEX_EXECUTABLE")
            os.environ["SOURDOUGH_CODEX_EXECUTABLE"] = str(env_codex)
            try:
                (desk / "effort.txt").write_text("default", encoding="utf-8")
                adapter = CodexCliAdapter(
                    executable=("codex", "exec", "--json"),
                    codex_home=codex_home,
                )
                res = adapter.run_shift(
                    profile, self._make_request(desk, store), broker
                )
                self.assertEqual(res.usage, TokenUsage(200, 15, 128))
                usage_events = [ev for ev in res.events if ev.kind == "usage"]
                self.assertEqual(usage_events[0].payload["cached_input_tokens"], 128)

                (desk / "effort.txt").write_text("none", encoding="utf-8")
                adapter_none = CodexCliAdapter(
                    executable=("codex", "exec", "--json"),
                    codex_home=codex_home,
                    reasoning_effort=None,
                )
                res_none = adapter_none.run_shift(
                    profile, self._make_request(desk, store), broker
                )
                self.assertEqual(res_none.usage.cached_input_tokens, 128)
            finally:
                if old_env is None:
                    os.environ.pop("SOURDOUGH_CODEX_EXECUTABLE", None)
                else:
                    os.environ["SOURDOUGH_CODEX_EXECUTABLE"] = old_env


if __name__ == "__main__":
    unittest.main()
