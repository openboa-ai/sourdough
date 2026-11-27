"""Standalone stdlib-only JSON-RPC 2.0 MCP stdio-to-UDS bridge for CodexCliAdapter.

Launched with ``python -I <abs-path> --mcp-bridge <sock_path> <manifest_path>`` so
the bridge never imports from an Agent's writable Desk directory or user site-packages.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any


def _run_mcp_stdio_bridge(sock_path: Path, manifest_path: Path) -> int:
    """Serve JSON-RPC 2.0 MCP over stdio forwarding tools/call requests to the UDS broker."""
    tools = json.loads(manifest_path.read_text(encoding="utf-8"))
    for raw_line in sys.stdin:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue
        msg_id = message["id"]
        method = message.get("method")
        if method == "initialize":
            params = message.get("params")
            proto = (
                params.get("protocolVersion", "2024-11-05")
                if isinstance(params, dict)
                else "2024-11-05"
            )
            result: dict[str, Any] = {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sourdough-broker", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict):
                result = {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "ok": False,
                                    "error_type": "InvalidParams",
                                    "error": "MCP tools/call params must be an object",
                                }
                            ),
                        }
                    ],
                }
            else:
                name = str(params.get("name") or "")
                args = params.get("arguments")
                if args is None:
                    args = {}
                call_id = f"mcp-{msg_id}"
                req_bytes = (
                    json.dumps(
                        {"call_id": call_id, "tool": name, "arguments": args},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(30.0)
                        client.connect(str(sock_path))
                        client.sendall(req_bytes)
                        buf = b""
                        while b"\n" not in buf and len(buf) < 1_048_576:
                            chunk = client.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                    reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
                except (OSError, ValueError) as exc:
                    result = {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "ok": False,
                                        "error_type": type(exc).__name__,
                                        "error": str(exc),
                                    },
                                    sort_keys=True,
                                ),
                            }
                        ],
                    }
                else:
                    if not isinstance(reply, dict) or not reply.get("ok"):
                        err_payload = {
                            "ok": False,
                            "error_type": (
                                reply.get("error_type", "BrokerError")
                                if isinstance(reply, dict)
                                else "BrokerError"
                            ),
                            "error": (
                                reply.get("error", "Tool call refused")
                                if isinstance(reply, dict)
                                else "Invalid broker reply"
                            ),
                        }
                        result = {
                            "isError": True,
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(err_payload, sort_keys=True),
                                }
                            ],
                        }
                    else:
                        tool_res = reply.get("result", {})
                        is_err = (
                            isinstance(tool_res, dict) and tool_res.get("ok") is False
                        )
                        result = {
                            "isError": is_err,
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(tool_res, sort_keys=True),
                                }
                            ],
                        }
        else:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": "Unknown method"},
                    }
                ),
                flush=True,
            )
            continue
        print(
            json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--mcp-bridge":
        raise SystemExit(_run_mcp_stdio_bridge(Path(sys.argv[2]), Path(sys.argv[3])))
    raise SystemExit(2)
