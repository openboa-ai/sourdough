#!/usr/bin/env python3
"""Run a subscription-backed Codex duty with scoped MCP tools."""

import json
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4


PAPER_BUY = {
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
READ_RESULT = {
    "name": "read_work_result",
    "description": "Read the committed paper-book observation for this duty.",
    "inputSchema": {"type": "object", "additionalProperties": False},
}
PROPOSE_DECISION = {
    "name": "record_review_decision",
    "description": "Propose a decision based on the result that was returned for this duty.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "observation_revision": {"type": "string"},
            "action": {"enum": ["hold", "investigate", "recommend_change"]},
            "rationale": {"type": "string", "minLength": 1, "maxLength": 2000},
            "next_check_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
        },
        "required": [
            "observation_revision",
            "action",
            "rationale",
            "next_check_seconds",
        ],
        "additionalProperties": False,
    },
}


def tool_output(value, *, denied=False):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return {"isError": denied, "content": [{"type": "text", "text": text}]}


def read_result(address):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(address)
        with client.makefile() as stream:
            return json.load(stream)


def serve(outbox, address=None):
    revision = None
    tools = [PAPER_BUY] if address is None else [READ_RESULT, PROPOSE_DECISION]
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
            result = {"tools": tools}
        elif method == "tools/call":
            params = message["params"]
            args = params.get("arguments", {})
            name = params["name"]
            if address is not None and name == "read_work_result" and args == {}:
                try:
                    observation = read_result(address)
                    revision = observation.get("revision")
                    result = tool_output(observation, denied=revision is None)
                except (OSError, ValueError):
                    revision = None
                    result = tool_output("Work result is unavailable.", denied=True)
            else:
                valid_buy = (
                    address is None
                    and name == "paper_buy"
                    and set(args) == {"quantity", "limit_minor_units"}
                    and all(type(value) is int and value > 0 for value in args.values())
                )
                valid_review = (
                    address is not None
                    and name == "record_review_decision"
                    and revision is not None
                    and set(args) == set(PROPOSE_DECISION["inputSchema"]["required"])
                    and args["observation_revision"] == revision
                    and args["action"] in ("hold", "investigate", "recommend_change")
                    and isinstance(args["rationale"], str)
                    and 0 < len(args["rationale"].strip()) <= 2000
                    and type(args["next_check_seconds"]) is int
                    and 60 <= args["next_check_seconds"] <= 86400
                )
                try:
                    if not (valid_buy or valid_review):
                        raise ValueError
                    with outbox.open("x") as stream:
                        json.dump(
                            {"tool": name, "call_id": uuid4().hex, **args}, stream
                        )
                except (FileExistsError, ValueError):
                    result = tool_output("Invalid or repeated proposal.", denied=True)
                else:
                    result = tool_output(
                        "Proposal staged; company settlement is not confirmed."
                    )
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
    if len(sys.argv) == 4 and sys.argv[1] == "--mcp-review":
        serve(Path(sys.argv[3]), sys.argv[2])
        return
    request = json.load(sys.stdin)
    codex = "/opt/homebrew/bin/codex"
    model = "gpt-5.6-sol"
    codex_home = Path.home() / "Library/Application Support/Sourdough/Codex"
    env = {
        "HOME": str(codex_home.parent),
        "CODEX_HOME": str(codex_home),
        "PATH": "/usr/bin:/bin",
    }
    credential_store = ["-c", 'cli_auth_credentials_store="file"']
    if request["operation"] == "identify":
        catalog = subprocess.check_output(
            [codex, *credential_store, "debug", "models"], env=env, timeout=10
        )
        entries = [
            item for item in json.loads(catalog)["models"] if item["slug"] == model
        ]
        if len(entries) != 1 or not entries[0].get("comp_hash"):
            raise SystemExit("Codex model revision is unresolved")
        version = subprocess.check_output([codex, "--version"], env=env, timeout=10)
        print(
            json.dumps(
                {
                    "provider": "openai",
                    "model": model,
                    "revision": f"{version.decode().strip()}:{entries[0]['comp_hash']}",
                }
            )
        )
        return
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
    operation = request["operation"]
    if operation not in ("first_duty", "review_duty") or not ready:
        raise SystemExit("An existing ChatGPT subscription login is required")
    with TemporaryDirectory() as directory:
        outbox = Path(directory) / "proposal.json"
        decision = Path(directory) / "decision.txt"
        tool_args = [str(Path(__file__).resolve())]
        if operation == "review_duty":
            tool_args += ["--mcp-review", request["read_endpoint"], str(outbox)]
            tool_names = ["read_work_result", "record_review_decision"]
            prompt = (
                "Review the result of your preceding Sourdough duty. "
                "Use read_work_result to inspect the committed paper position. "
                "Choose whether to hold, investigate, or recommend a change. "
                "Explain which fact from the result supports your decision and choose a next review interval. "
                "Propose your decision through record_review_decision. "
                "You cannot place an order in this review. Do not use the shell."
            )
        else:
            context = request["context"]
            tool_args += ["--mcp", str(outbox)]
            tool_names = ["paper_buy"]
            prompt = (
                "You are a Sourdough employee running the first duty on a synthetic account. "
                "This is an operational check of the newly authorized paper execution path, not an investment recommendation. "
                "If the quoted terms permit a small order within the cap, decide its size and limit and propose it "
                "using paper_buy. If a safe test is unsuitable, hold and end with exactly HOLD. "
                "If the tool fails, report failure; do not claim a hold or a fill. Do not use the shell. "
                f"Fixed quote per unit: {context['quote_minor_units']} minor units. "
                f"Maximum exposure: {context['exposure_limit_minor_units']} minor units."
            )
        cmd = [
            codex,
            *credential_store,
            "-a",
            "never",
            "exec",
            "--ignore-user-config",
            "--model",
            model,
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--output-last-message",
            str(decision),
            "-c",
            f"mcp_servers.paper.command={json.dumps(sys.executable)}",
            "-c",
            f"mcp_servers.paper.args={json.dumps(tool_args)}",
            "-c",
            "mcp_servers.paper.required=true",
            "-c",
            f"mcp_servers.paper.enabled_tools={json.dumps(tool_names)}",
        ]
        for name in tool_names:
            cmd += ["-c", f'mcp_servers.paper.tools.{name}.approval_mode="approve"']
        cmd.append("-")
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
        elif (
            operation == "first_duty"
            and decision.exists()
            and decision.read_text().strip() == "HOLD"
        ):
            print('{"action":"hold"}')
        else:
            raise SystemExit("Codex did not produce a proposal or an explicit hold")


if __name__ == "__main__":
    main()
