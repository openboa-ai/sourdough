"""Reusable private 0o600 Unix domain socket bridge forwarding tool calls to OuterToolBroker."""

from __future__ import annotations

import json
import math
import os
import socket
import threading
from pathlib import Path
from typing import Any

from .protocol import (
    AgentRuntimeRefused,
    AgentTransportInterrupted,
    OuterToolBroker,
)

DEFAULT_MAX_BROKER_REQUEST_BYTES = 262_144
DEFAULT_CLIENT_SOCKET_TIMEOUT_SECONDS = 5.0


def fatal_broker_exceptions() -> tuple[type[BaseException], ...]:
    """Return exception classes that must abort a shift when raised inside a tool handler."""
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


class UdsToolBrokerServer:
    """Private 0o600 Unix domain socket server forwarding JSON-line tool calls to OuterToolBroker.

    Designed for reuse by local CLI adapters, sandboxed subprocess harnesses, and
    non-LLM worker bridges so every out-of-process runtime shares one fail-closed
    tool-mediation contract. Callers should place ``sock_path`` inside a private
    ``0o700`` directory before binding.
    """

    def __init__(
        self,
        sock_path: Path,
        broker: OuterToolBroker,
        *,
        max_request_bytes: int = DEFAULT_MAX_BROKER_REQUEST_BYTES,
        client_timeout_seconds: float = DEFAULT_CLIENT_SOCKET_TIMEOUT_SECONDS,
        thread_name: str = "sourdough-uds-broker",
        unlink_on_stop: bool = False,
    ) -> None:
        if type(max_request_bytes) is not int or max_request_bytes <= 0:
            raise AgentRuntimeRefused("max_request_bytes must be a positive integer")
        if (
            isinstance(client_timeout_seconds, bool)
            or not isinstance(client_timeout_seconds, (int, float))
            or not math.isfinite(float(client_timeout_seconds))
            or float(client_timeout_seconds) <= 0
        ):
            raise AgentRuntimeRefused(
                "client_timeout_seconds must be a positive finite number"
            )
        if sock_path.is_symlink():
            raise AgentRuntimeRefused(
                f"Refused to bind UDS broker socket on symlink path: {sock_path}"
            )
        if not sock_path.parent.is_dir():
            raise AgentRuntimeRefused(
                f"UDS broker socket parent directory does not exist: {sock_path.parent}"
            )
        self._sock_path = sock_path
        self._broker = broker
        self._max_request_bytes = max_request_bytes
        self._client_timeout_seconds = float(client_timeout_seconds)
        self._unlink_on_stop = unlink_on_stop
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.bind(str(sock_path))
            os.chmod(sock_path, 0o600)
            self._sock.listen(8)
            self._sock.settimeout(0.1)
        except Exception:
            try:
                self._sock.close()
            except OSError:
                pass
            raise
        self.last_refusal: AgentRuntimeRefused | None = None
        self.last_fatal_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._serve,
            name=thread_name,
            daemon=True,
        )

    @property
    def socket_path(self) -> Path:
        return self._sock_path

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        try:
            self._sock.close()
        except OSError:
            pass
        if self._unlink_on_stop:
            try:
                self._sock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> UdsToolBrokerServer:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        self.stop()

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
        conn.settimeout(self._client_timeout_seconds)
        buffer = b""
        limit = self._max_request_bytes + 1
        while b"\n" not in buffer and len(buffer) < limit:
            remaining = limit - len(buffer)
            try:
                chunk = conn.recv(min(4096, remaining))
            except OSError:
                return
            if not chunk:
                break
            buffer += chunk
        if not buffer:
            return
        fatal_types = fatal_broker_exceptions()
        try:
            if b"\n" in buffer:
                raw_line = buffer.split(b"\n", 1)[0]
            else:
                raw_line = buffer
            if len(raw_line) > self._max_request_bytes:
                raise AgentRuntimeRefused(
                    f"Broker request exceeded maximum size ({self._max_request_bytes} bytes)"
                )
            line = raw_line.decode("utf-8", errors="strict")
            req = json.loads(line)
            if not isinstance(req, dict):
                raise AgentRuntimeRefused("Broker request must be a JSON object")
            call_id = req.get("call_id")
            tool_name = req.get("tool")
            arguments = req.get("arguments", {})
            if not isinstance(call_id, str) or not call_id.strip():
                raise AgentRuntimeRefused("Broker request requires a nonempty call_id")
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise AgentRuntimeRefused(
                    "Broker request requires a nonempty tool name"
                )
            if not isinstance(arguments, dict):
                raise AgentRuntimeRefused(
                    "Broker request arguments must be a JSON object"
                )
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
        try:
            conn.sendall(encoded)
        except OSError:
            pass


__all__ = [
    "DEFAULT_CLIENT_SOCKET_TIMEOUT_SECONDS",
    "DEFAULT_MAX_BROKER_REQUEST_BYTES",
    "UdsToolBrokerServer",
    "fatal_broker_exceptions",
]
