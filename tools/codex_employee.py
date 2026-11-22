#!/usr/bin/env python3
"""Run one subscription-backed Codex duty with a paper-order MCP tool."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4


def serve(outbox):
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message["method"]
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sourdough-paper", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "paper_buy",
                        "description": "Propose one paper-only limit buy for company admission.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "quantity": {"type": "integer", "minimum": 1},
                                "limit_minor_units": {"type": "integer", "minimum": 1},
                            },
                            "required": ["quantity", "limit_minor_units"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = message["params"]
            args = params.get("arguments", {})
            valid = (
                params["name"] == "paper_buy"
                and set(args) == {"quantity", "limit_minor_units"}
                and all(type(value) is int and value > 0 for value in args.values())
            )
            try:
                if not valid:
                    raise ValueError
                with outbox.open("x") as stream:
                    json.dump(
                        {"tool": "paper_buy", "call_id": uuid4().hex, **args}, stream
                    )
            except (FileExistsError, ValueError):
                result = {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": "Invalid or repeated proposal."}
                    ],
                }
            else:
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": "Proposal staged; no order or fill is confirmed.",
                        }
                    ]
                }
        else:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "Unknown method"},
                    }
                ),
                flush=True,
            )
            continue
        print(
            json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}),
            flush=True,
        )


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--mcp":
        serve(Path(sys.argv[2]))
        return
    request = json.load(sys.stdin)
    codex = "/opt/homebrew/bin/codex"
    codex_home = Path.home() / "Library/Application Support/Sourdough/Codex"
    env = {
        "HOME": str(codex_home.parent),
        "CODEX_HOME": str(codex_home),
        "PATH": "/usr/bin:/bin",
    }
    credential_store = ["-c", 'cli_auth_credentials_store="file"']
    if not (codex_home / "auth.json").is_file():
        if request["operation"] == "ready":
            print('{"ready":false}')
            return
        raise SystemExit("Connect Codex in the Sourdough profile first")
    login = subprocess.run(
        [codex, *credential_store, "login", "status"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    ready = (
        login.returncode == 0
        and "Logged in using ChatGPT" in login.stdout + login.stderr
    )
    if request["operation"] == "ready":
        print(json.dumps({"ready": ready}))
        return
    if request["operation"] != "first_duty" or not ready:
        raise SystemExit("An existing ChatGPT subscription login is required")
    context = request["context"]
    with TemporaryDirectory() as directory:
        outbox = Path(directory) / "proposal.json"
        decision = Path(directory) / "decision.txt"
        cmd = [
            codex,
            *credential_store,
            "-a",
            "never",
            "exec",
            "--ignore-user-config",
            "--model",
            "gpt-5.6-sol",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--output-last-message",
            str(decision),
            "-c",
            f"mcp_servers.paper.command={json.dumps(sys.executable)}",
            "-c",
            f"mcp_servers.paper.args={json.dumps([str(Path(__file__).resolve()), '--mcp', str(outbox)])}",
            "-c",
            "mcp_servers.paper.required=true",
            "-c",
            'mcp_servers.paper.enabled_tools=["paper_buy"]',
            "-c",
            'mcp_servers.paper.tools.paper_buy.approval_mode="approve"',
            "-",
        ]
        prompt = (
            "You are a Sourdough employee running the first duty on a synthetic account. "
            "This is an operational check of the newly authorized paper execution path, not an investment recommendation. "
            "If the quoted terms permit a small order within the cap, decide its size and limit and propose it "
            "using paper_buy. If a safe test is unsuitable, hold and end with exactly HOLD. "
            "If the tool fails, report failure; do not claim a hold or a fill. Do not use the shell. "
            f"Fixed quote per unit: {context['quote_minor_units']} minor units. "
            f"Maximum exposure: {context['exposure_limit_minor_units']} minor units."
        )
        subprocess.run(
            cmd,
            input=prompt,
            text=True,
            env=env,
            cwd=directory,
            stdout=sys.stderr,
            timeout=50,
            check=True,
        )
        if outbox.exists():
            print(outbox.read_text())
        elif decision.exists() and decision.read_text().strip() == "HOLD":
            print('{"action":"hold"}')
        else:
            raise SystemExit("Codex did not produce a proposal or an explicit hold")


if __name__ == "__main__":
    main()
