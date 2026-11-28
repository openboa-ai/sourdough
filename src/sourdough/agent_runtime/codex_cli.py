"""Local CLI runtime adapter over a private UDS OuterToolBroker, stdio MCP bridge, and macOS sandbox."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from time import time_ns
from typing import Any, Literal

from ._http import render_context_system_prompt
from ._mcp_bridge import _run_mcp_stdio_bridge
from .protocol import (
    PROVIDER_CODEX_CLI,
    AdapterEvent,
    AdapterManifest,
    AgentProfileBinding,
    AgentProfileMismatch,
    AgentRuntimeRefused,
    AgentTransportInterrupted,
    EnvSecretResolver,
    OuterToolBroker,
    SecretHandleResolver,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
)

_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_ISOLATION_PROFILE = "sourdough-agent"
_VALID_PROTOCOL_MODES = frozenset({"auto", "codex_mcp", "shim"})


def confinement_arguments() -> list[str]:
    """Return Codex CLI workspace-confinement flags matching Sourdough's sandbox profile."""
    profile = _ISOLATION_PROFILE
    return [
        "-c",
        f"default_permissions={json.dumps(profile)}",
        "-c",
        f'permissions.{profile}.filesystem={{":root"="deny",":minimal"="read",":workspace_roots"={{"."="read",".desk"="deny"}}}}',
        "-c",
        f"permissions.{profile}.network.enabled=false",
        "-c",
        'web_search="disabled"',
    ]


def _fatal_broker_exceptions() -> tuple[type[BaseException], ...]:
    from ..company import FoundingRefused
    from ..runtime.registry import RuntimeRefused
    from ..runtime.work_kernel import RunFenced
    from ..storage.checkpoint import CheckpointRefused
    from ..storage.db import StorageRefused
    from ..writer_lease import WriterRefused

    return (
        AgentRuntimeRefused,
        AgentTransportInterrupted,
        RunFenced,
        StorageRefused,
        FoundingRefused,
        WriterRefused,
        CheckpointRefused,
        RuntimeRefused,
    )


def _resolve_cli_path() -> str:
    parts = [
        str(Path(sys.executable).parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        *os.defpath.split(os.pathsep),
    ]
    deduped = list(dict.fromkeys(p for p in parts if p))
    return os.pathsep.join(deduped)


def _resolve_codex_home(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env_home = os.environ.get("CODEX_HOME", "").strip()
    if env_home:
        return Path(env_home)
    app_support = Path.home() / "Library/Application Support/Sourdough/Codex"
    if (app_support / "auth.json").is_file():
        return app_support
    return Path.home() / ".codex"


class _UdsToolBrokerServer:
    """Private 0o600 Unix domain socket server forwarding JSON tool calls to OuterToolBroker."""

    def __init__(self, sock_path: Path, broker: OuterToolBroker) -> None:
        self._sock_path = sock_path
        self._broker = broker
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self.last_refusal: AgentRuntimeRefused | None = None
        self.last_fatal_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._serve,
            name="sourdough-cli-broker",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                self._handle_connection(conn)

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        buffer = b""
        while b"\n" not in buffer and len(buffer) < 262_144:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buffer += chunk
        if not buffer:
            return
        line = buffer.split(b"\n", 1)[0].decode("utf-8", errors="replace")
        fatal_types = _fatal_broker_exceptions()
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise AgentRuntimeRefused("Broker request must be a JSON object")
            call_id = str(req.get("call_id") or "")
            tool_name = str(req.get("tool") or "")
            arguments = req.get("arguments", {})
            result = self._broker.invoke(call_id, tool_name, arguments)
            reply: dict[str, Any] = {"ok": True, "result": result}
        except AgentRuntimeRefused as error:
            self.last_refusal = error
            self.last_fatal_error = error
            reply = {
                "ok": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        except fatal_types as error:
            self.last_fatal_error = error
            reply = {
                "ok": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        except Exception as error:
            reply = {
                "ok": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        encoded = (
            json.dumps(reply, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        conn.sendall(encoded)


class CodexCliAdapter:
    """Subprocess CLI adapter mediating all tools through a private UDS OuterToolBroker."""

    def __init__(
        self,
        *,
        executable: tuple[str, ...] = ("codex", "exec", "--json"),
        use_macos_sandbox: bool = False,
        extra_read_paths: tuple[Path, ...] = (),
        extra_read_files: tuple[Path, ...] = (),
        secret_resolver: SecretHandleResolver | None = None,
        protocol_mode: Literal["auto", "codex_mcp", "shim"] = "auto",
        codex_home: Path | None = None,
        reasoning_effort: str | None = "low",
    ) -> None:
        if not executable or not all(
            isinstance(part, str) and part for part in executable
        ):
            raise AgentRuntimeRefused("CodexCliAdapter requires a non-empty executable")
        if protocol_mode not in _VALID_PROTOCOL_MODES:
            raise AgentRuntimeRefused(
                f"Unsupported CodexCliAdapter protocol_mode: {protocol_mode}"
            )
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
        ):
            raise AgentRuntimeRefused(
                "CodexCliAdapter reasoning_effort must be a non-empty string or None"
            )
        self._executable = tuple(executable)
        self._use_macos_sandbox = use_macos_sandbox
        self._extra_read_paths = tuple(extra_read_paths)
        self._extra_read_files = tuple(extra_read_files)
        self._secret_resolver = secret_resolver or EnvSecretResolver()
        self._protocol_mode = protocol_mode
        self._codex_home = codex_home
        self._reasoning_effort = (
            reasoning_effort.strip() if reasoning_effort is not None else None
        )
        self._manifest = AdapterManifest(
            provider=PROVIDER_CODEX_CLI,
            adapter_revision="codex-cli-uds-v2",
            transport="local_cli_uds",
            supports_tool_mediation=True,
            supports_usage_accounting=True,
            supports_sandbox_desk=True,
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    def _is_codex_mcp_mode(self) -> bool:
        if self._protocol_mode == "codex_mcp":
            return True
        if self._protocol_mode == "shim":
            return False
        return Path(self._executable[0]).name == "codex"

    def run_shift(
        self,
        profile: AgentProfileBinding,
        request: ShiftRequest,
        broker: OuterToolBroker,
    ) -> ShiftResult:
        if profile.provider != PROVIDER_CODEX_CLI:
            raise AgentProfileMismatch(
                f"CodexCliAdapter cannot run provider '{profile.provider}'"
            )

        events: list[AdapterEvent] = [
            AdapterEvent(
                kind="started",
                recorded_at_ns=time_ns(),
                payload={"provider": PROVIDER_CODEX_CLI, "model": profile.model},
            )
        ]

        codex_mcp = self._is_codex_mcp_mode()
        if codex_mcp and self._use_macos_sandbox:
            raise AgentRuntimeRefused(
                "CodexCliAdapter codex_mcp mode uses Codex confinement_arguments() and "
                "cannot run inside outer deny-network sandbox-exec"
            )

        # Short private socket directory under /tmp so AF_UNIX sun_path never exceeds 104 bytes on macOS.
        sock_dir = Path(tempfile.mkdtemp(prefix="sd-cli-", dir="/tmp"))
        os.chmod(sock_dir, 0o700)
        sock_path = sock_dir / "broker.sock"
        server = _UdsToolBrokerServer(sock_path, broker)
        server.start()
        try:
            cwd: str | None = None
            env: dict[str, str] = {
                "PATH": _resolve_cli_path(),
                "SOURDOUGH_BROKER_SOCKET": str(sock_path),
                "SOURDOUGH_MODEL": profile.model,
                "SOURDOUGH_RUN_ID": request.run_id,
                "SOURDOUGH_TURN_ID": request.turn_id,
            }
            py_path = os.environ.get("PYTHONPATH", "").strip()
            if py_path:
                env["PYTHONPATH"] = py_path
            has_isolated_tools = any(
                spec.requires_desk_isolation() for spec in request.tools
            )
            if request.desk_path is not None and not has_isolated_tools:
                request.desk_path.mkdir(parents=True, exist_ok=True)
                os.chmod(request.desk_path, 0o700)
                cwd = str(request.desk_path)
                env["HOME"] = str(request.desk_path)
            else:
                cwd = str(sock_dir)
                env["HOME"] = str(sock_dir)

            # Never inject resolved credential_handle_ref secrets (e.g., SOURDOUGH_CLI_AUTH_TOKEN)
            # into the model-directed CLI child process environment; credentials stay on host-side
            # provider channels or host-managed CODEX_HOME auth outside Agent tool visibility.

            if codex_mcp:
                resolved_codex_home = _resolve_codex_home(self._codex_home)
                env["CODEX_HOME"] = str(resolved_codex_home)
                tools_manifest_path = sock_dir / "mcp_tools.json"
                tools_manifest = [
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "inputSchema": spec.parameters,
                    }
                    for spec in request.tools
                ]
                tools_manifest_path.write_text(
                    json.dumps(tools_manifest, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.chmod(tools_manifest_path, 0o600)

                exe0 = self._executable[0]
                is_explicit_path = "/" in exe0 or os.sep in exe0 or Path(exe0).is_file()
                if Path(exe0).name == "codex":
                    env_codex = os.environ.get("SOURDOUGH_CODEX_EXECUTABLE", "").strip()
                    if is_explicit_path:
                        codex_bin = exe0
                    elif env_codex:
                        codex_bin = env_codex
                    else:
                        found_bin = shutil.which("codex", path=env["PATH"])
                        codex_bin = found_bin or "/opt/homebrew/bin/codex"
                    prefix_cmd = [codex_bin]
                else:
                    prefix_cmd = list(self._executable)

                tool_names = [spec.name for spec in request.tools]
                bridge_entry = str(
                    (Path(__file__).resolve().parent / "_mcp_bridge.py").resolve()
                )
                mcp_args = [
                    "-I",
                    bridge_entry,
                    "--mcp-bridge",
                    str(sock_path),
                    str(tools_manifest_path),
                ]
                command = [
                    *prefix_cmd,
                    "-c",
                    'cli_auth_credentials_store="file"',
                    "-a",
                    "never",
                    "exec",
                    "--json",
                    "--ignore-user-config",
                    "--model",
                    profile.model,
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--cd",
                    str(cwd or sock_dir),
                    *confinement_arguments(),
                ]
                if self._reasoning_effort is not None:
                    command += [
                        "-c",
                        f"model_reasoning_effort={json.dumps(self._reasoning_effort)}",
                    ]
                command += [
                    "-c",
                    f"mcp_servers.sourdough.command={json.dumps(sys.executable)}",
                    "-c",
                    f"mcp_servers.sourdough.args={json.dumps(mcp_args)}",
                    "-c",
                    f"mcp_servers.sourdough.cwd={json.dumps(str(sock_dir))}",
                    "-c",
                    "mcp_servers.sourdough.required=true",
                    "-c",
                    f"mcp_servers.sourdough.enabled_tools={json.dumps(tool_names)}",
                ]
                for tool_name in tool_names:
                    command += [
                        "-c",
                        f'mcp_servers.sourdough.tools.{tool_name}.approval_mode="approve"',
                    ]
                command.append("-")
                stdin_payload = (
                    f"{render_context_system_prompt(request)}\n\n"
                    f"Instruction:\n{request.instruction}\n"
                )
            else:
                command = list(self._executable)
                if self._use_macos_sandbox:
                    if sys.platform != "darwin" or not _SANDBOX_EXEC.is_file():
                        raise AgentRuntimeRefused(
                            "local-macos sandbox backend is unavailable"
                        )
                    if request.store_path is None:
                        raise AgentRuntimeRefused(
                            "store_path is required when use_macos_sandbox=True"
                        )
                    from ..harness import build_desk_sandbox_profile

                    sandbox_desk = (
                        request.desk_path
                        if (request.desk_path is not None and not has_isolated_tools)
                        else None
                    )
                    sbpl = build_desk_sandbox_profile(
                        request.store_path,
                        sandbox_desk,
                        broker_socket=sock_path,
                        scratch_dir=sock_dir if sandbox_desk is None else None,
                        extra_read_paths=self._extra_read_paths,
                        extra_read_files=self._extra_read_files,
                        allow_dotdesk_read=False,
                    )
                    command = [str(_SANDBOX_EXEC), "-p", sbpl, *command]

                stdin_payload = json.dumps(
                    {
                        "model": profile.model,
                        "system": render_context_system_prompt(request),
                        "instruction": request.instruction,
                        "broker_socket": str(sock_path),
                        "tools": [
                            {
                                "name": spec.name,
                                "description": spec.description,
                                "parameters": spec.parameters,
                            }
                            for spec in request.tools
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )

            try:
                proc = subprocess.run(
                    command,
                    input=stdin_payload,
                    capture_output=True,
                    text=True,
                    timeout=request.timeout_seconds,
                    cwd=cwd,
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise AgentTransportInterrupted("Codex CLI shift timed out") from error
            except OSError as error:
                raise AgentTransportInterrupted(
                    f"Codex CLI failed to launch ({type(error).__name__})"
                ) from error

            explicit_output_text = ""
            agent_messages: list[str] = []
            failed_message = ""
            total_usage = TokenUsage(0, 0)
            parsed_any = False

            for raw_line in proc.stdout.splitlines():
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    event_obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event_obj, dict):
                    continue
                parsed_any = True
                event_type = event_obj.get("type")

                usage_obj = event_obj.get("usage")
                if isinstance(usage_obj, dict):
                    round_usage = TokenUsage(
                        input_tokens=int(usage_obj.get("input_tokens") or 0),
                        output_tokens=int(usage_obj.get("output_tokens") or 0),
                        cached_input_tokens=int(
                            usage_obj.get("cached_input_tokens") or 0
                        ),
                    )
                    total_usage = total_usage + round_usage
                    broker.record_usage(round_usage)
                    events.append(
                        AdapterEvent(
                            kind="usage",
                            recorded_at_ns=time_ns(),
                            payload={
                                "input_tokens": round_usage.input_tokens,
                                "cached_input_tokens": round_usage.cached_input_tokens,
                                "output_tokens": round_usage.output_tokens,
                            },
                        )
                    )

                if event_type == "item.completed":
                    item = event_obj.get("item")
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "agent_message"
                        and isinstance(item.get("text"), str)
                    ):
                        agent_messages.append(item["text"])
                elif event_type in ("turn.failed", "error"):
                    err_obj = event_obj.get("error")
                    if isinstance(err_obj, dict) and isinstance(
                        err_obj.get("message"), str
                    ):
                        failed_message = err_obj["message"]
                    elif isinstance(event_obj.get("message"), str):
                        failed_message = event_obj["message"]
                elif isinstance(event_obj.get("output_text"), str):
                    explicit_output_text = event_obj["output_text"]
                elif isinstance(event_obj.get("text"), str) and event_type in (
                    "terminal",
                    "message",
                ):
                    explicit_output_text = event_obj["text"]

            if server.last_fatal_error is not None:
                raise server.last_fatal_error
            if server.last_refusal is not None:
                raise server.last_refusal

            if proc.returncode != 0:
                detail = failed_message or (proc.stderr or "").strip()[:200]
                raise AgentTransportInterrupted(
                    f"Codex CLI exited with status {proc.returncode}: {detail}"
                )

            if failed_message and not explicit_output_text and not agent_messages:
                raise AgentTransportInterrupted(
                    f"Codex CLI turn failed: {failed_message}"
                )

            if explicit_output_text:
                output_text = explicit_output_text
            elif agent_messages:
                output_text = "\n".join(part for part in agent_messages if part).strip()
            elif not parsed_any:
                output_text = proc.stdout.strip()
            else:
                output_text = ""

            events.extend(broker.events)
            events.append(
                AdapterEvent(
                    kind="terminal",
                    recorded_at_ns=time_ns(),
                    payload={"output_chars": len(output_text)},
                )
            )
            return ShiftResult(
                provider=PROVIDER_CODEX_CLI,
                model=profile.model,
                run_id=request.run_id,
                turn_id=request.turn_id,
                output_text=output_text,
                usage=total_usage,
                tool_calls_executed=broker.call_count,
                events=tuple(events),
            )
        finally:
            server.stop()
            shutil.rmtree(sock_dir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--mcp-bridge":
        raise SystemExit(_run_mcp_stdio_bridge(Path(sys.argv[2]), Path(sys.argv[3])))
    raise SystemExit(2)
