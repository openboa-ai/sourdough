"""Local CLI runtime adapter over a private UDS OuterToolBroker and optional macOS Desk sandbox."""

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
from typing import Any

from ..harness import build_desk_sandbox_profile
from ._http import render_context_system_prompt
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
    ) -> None:
        if not executable or not all(
            isinstance(part, str) and part for part in executable
        ):
            raise AgentRuntimeRefused("CodexCliAdapter requires a non-empty executable")
        self._executable = tuple(executable)
        self._use_macos_sandbox = use_macos_sandbox
        self._extra_read_paths = tuple(extra_read_paths)
        self._extra_read_files = tuple(extra_read_files)
        self._secret_resolver = secret_resolver or EnvSecretResolver()
        self._manifest = AdapterManifest(
            provider=PROVIDER_CODEX_CLI,
            adapter_revision="codex-cli-uds-v1",
            transport="local_cli_uds",
            supports_tool_mediation=True,
            supports_usage_accounting=True,
            supports_sandbox_desk=True,
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

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

        # Short private socket directory under /tmp so AF_UNIX sun_path never exceeds 104 bytes on macOS.
        sock_dir = Path(tempfile.mkdtemp(prefix="sd-cli-", dir="/tmp"))
        os.chmod(sock_dir, 0o700)
        sock_path = sock_dir / "broker.sock"
        server = _UdsToolBrokerServer(sock_path, broker)
        server.start()
        try:
            command = list(self._executable)
            cwd: str | None = None
            env: dict[str, str] = {
                "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}",
                "SOURDOUGH_BROKER_SOCKET": str(sock_path),
                "SOURDOUGH_MODEL": profile.model,
                "SOURDOUGH_RUN_ID": request.run_id,
                "SOURDOUGH_TURN_ID": request.turn_id,
            }
            if request.desk_path is not None:
                request.desk_path.mkdir(parents=True, exist_ok=True)
                os.chmod(request.desk_path, 0o700)
                cwd = str(request.desk_path)
                env["HOME"] = str(request.desk_path)
            else:
                env["HOME"] = str(sock_dir)

            if profile.credential_handle_ref is not None:
                secret = self._secret_resolver.resolve_secret(
                    profile.credential_handle_ref
                )
                env["SOURDOUGH_CLI_AUTH_TOKEN"] = secret.value

            if self._use_macos_sandbox:
                if sys.platform != "darwin" or not _SANDBOX_EXEC.is_file():
                    raise AgentRuntimeRefused(
                        "local-macos sandbox backend is unavailable"
                    )
                if request.desk_path is None or request.store_path is None:
                    raise AgentRuntimeRefused(
                        "desk_path and store_path are required when use_macos_sandbox=True"
                    )
                sbpl = build_desk_sandbox_profile(
                    request.store_path,
                    request.desk_path,
                    broker_socket=sock_path,
                    extra_read_paths=self._extra_read_paths,
                    extra_read_files=self._extra_read_files,
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

            if proc.returncode != 0:
                if server.last_refusal is not None:
                    raise server.last_refusal
                stderr_excerpt = (proc.stderr or "").strip()[:200]
                raise AgentTransportInterrupted(
                    f"Codex CLI exited with status {proc.returncode}: {stderr_excerpt}"
                )

            output_text = ""
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
                usage_obj = event_obj.get("usage")
                if isinstance(usage_obj, dict):
                    round_usage = TokenUsage(
                        input_tokens=int(usage_obj.get("input_tokens", 0)),
                        output_tokens=int(usage_obj.get("output_tokens", 0)),
                    )
                    total_usage = total_usage + round_usage
                    events.append(
                        AdapterEvent(
                            kind="usage",
                            recorded_at_ns=time_ns(),
                            payload={
                                "input_tokens": round_usage.input_tokens,
                                "output_tokens": round_usage.output_tokens,
                            },
                        )
                    )
                if isinstance(event_obj.get("output_text"), str):
                    output_text = event_obj["output_text"]
                elif isinstance(event_obj.get("text"), str) and event_obj.get(
                    "type"
                ) in (
                    "terminal",
                    "message",
                ):
                    output_text = event_obj["text"]

            if not parsed_any:
                output_text = proc.stdout.strip()

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
