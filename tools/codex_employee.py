#!/usr/bin/env python3
"""Run a subscription-backed Codex duty with scoped MCP tools."""

import json
import socket
import subprocess
import sys
from hashlib import sha256
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
PROPOSE_TRADE = {
    "name": "record_paper_decision",
    "description": "Record a later paper decision. Supply order terms only when choosing buy.",
    "inputSchema": PROPOSE_DECISION["inputSchema"]
    | {
        "properties": PROPOSE_DECISION["inputSchema"]["properties"]
        | {
            "action": {"enum": ["hold", "investigate", "buy"]},
            "quantity": {"type": "integer", "minimum": 1},
            "limit_minor_units": {"type": "integer", "minimum": 1},
        }
    },
}
RESEARCH_FIELDS = {
    "snapshot_revision": "string",
    "hypothesis": "string",
    "start_id": "string",
    "end_id": "string",
    "gross_change_minor_units": "integer",
    "round_trip_fee_minor_units": "integer",
    "net_change_minor_units": "integer",
    "uncertainty": "string",
}
RESEARCH_TOOLS = [
    {
        "name": "read_research_inputs",
        "description": "Read the observations and fee terms available to this run at its as-of cut.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    {
        "name": "publish_research_finding",
        "description": "Publish your own two-point comparison. Use the displayed integer fee assumptions.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                key: {"type": value} for key, value in RESEARCH_FIELDS.items()
            },
            "required": list(RESEARCH_FIELDS),
        },
    },
    {
        "name": "record_research_decision",
        "description": "Cite your published finding, justify the decision and set the next check.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                key: value
                for key, value in PROPOSE_TRADE["inputSchema"]["properties"].items()
                if key != "observation_revision"
            }
            | {
                "finding_id": {"type": "string"},
                "finding_revision": {"type": "string"},
            },
            "required": [
                "finding_id",
                "finding_revision",
                "action",
                "rationale",
                "next_check_seconds",
            ],
        },
    },
]


def scoped_tool(name, description, properties):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


STAFFING_TOOLS = [
    scoped_tool(
        "read_staffing_need",
        "Read the assigned workload and remaining hiring allowance.",
        {},
    ),
    scoped_tool(
        "record_staffing_choice",
        "Record your reasoned choice; this does not hire or spend.",
        {
            "evidence_revision": {"type": "string"},
            "action": {"enum": ["hire", "reassign", "defer"]},
            "rationale": {"type": "string", "maxLength": 2000},
            "alternatives": {
                "type": "array",
                "items": {"enum": ["hire", "reassign", "defer"]},
            },
            "next_check_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
        },
    ),
    scoped_tool(
        "request_hire",
        "Apply for one hire against your committed choice and current grant.",
        {
            **{
                field: {"type": "string"}
                for field in (
                    "request_id",
                    "choice_id",
                    "role",
                    "execution_profile",
                    "scope",
                )
            },
            "turns": {"type": "integer", "minimum": 1, "maximum": 64},
        },
    ),
]
ONBOARDING_TOOLS = [
    scoped_tool(
        "read_first_duty", "Read your assignment and authorized private desk.", {}
    ),
    scoped_tool(
        "accept_first_duty",
        "Explicitly accept your first duty.",
        {
            "revision": {"type": "string"},
            "rationale": {"type": "string", "maxLength": 2000},
        },
    ),
]


ASSESSMENT_TOOLS = [
    *ONBOARDING_TOOLS,
    scoped_tool(
        "read_assessment_inputs", "Read company records for your accepted duty.", {}
    ),
    scoped_tool(
        "publish_assessment",
        "Publish one attributed assessment for the permitted audience.",
        {
            "source_revision": {"type": "string"},
            "record_id": {"type": "string"},
            "disposition": {"enum": ["recommend_change", "no_change", "investigate"]},
            "rationale": {"type": "string", "maxLength": 2000},
            "next_check_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
        },
    ),
]
HIRED_RESEARCH_TOOLS = [
    *ONBOARDING_TOOLS,
    *RESEARCH_TOOLS,
    scoped_tool(
        "request_research_order",
        "Ask Trading to admit the exact paper order from a prior buy decision.",
        {"decision_id": {"type": "string"}},
    ),
]
SCOPED_MODES = {
    "--mcp-research": (RESEARCH_TOOLS, "record_research_decision"),
    "--mcp-staffing": (STAFFING_TOOLS, "record_staffing_choice"),
    "--mcp-onboarding": (ONBOARDING_TOOLS, "accept_first_duty"),
    "--mcp-assessment": (ASSESSMENT_TOOLS, "publish_assessment"),
    "--mcp-hired-research": (HIRED_RESEARCH_TOOLS, "record_research_decision"),
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


def scoped_call(address, name, args, outbox, tools, terminal_tool):
    if not isinstance(args, dict) or name not in (tool["name"] for tool in tools):
        return tool_output("Tool is unavailable for this duty.", denied=True)
    if name == "record_research_decision":
        data = json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
        args = args | {"call_id": sha256(data).hexdigest()}
    message = json.dumps({"name": name, "arguments": args}).encode() + b"\n"
    for _ in range(2 if name == "request_hire" else 1):
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5)
                client.connect(address)
                client.sendall(message)
                with client.makefile() as stream:
                    result = json.load(stream)
            if isinstance(result, dict):
                break
        except (OSError, ValueError):
            continue
    else:
        return tool_output("Scoped service is unavailable.", denied=True)
    if "error" in result:
        return tool_output(result["error"], denied=True)
    if name == terminal_tool:
        try:
            with outbox.open("x") as stream:
                json.dump(result, stream)
        except FileExistsError:
            if json.loads(outbox.read_text()) != result:
                return tool_output(
                    "Decision receipt conflicts with the existing answer.", denied=True
                )
    return tool_output(result)


def decision_args(args, proposal, revision):
    if revision is None or not isinstance(args, dict):
        return False
    schema = proposal["inputSchema"]
    has_terms = args.get("action") == "buy"
    fields = set(schema["required"])
    if has_terms:
        fields.update(("quantity", "limit_minor_units"))
    if (
        set(args) != fields
        or args["observation_revision"] != revision
        or args["action"] not in schema["properties"]["action"]["enum"]
        or not isinstance(args["rationale"], str)
        or not 0 < len(args["rationale"].strip()) <= 2000
        or type(args["next_check_seconds"]) is not int
        or not 60 <= args["next_check_seconds"] <= 86400
    ):
        return False
    return not has_terms or all(
        type(args[field]) is int and args[field] > 0
        for field in ("quantity", "limit_minor_units")
    )


def serve(outbox, address=None, *, trading=False, scoped_mode=None):
    revision = None
    proposal = PROPOSE_TRADE if trading else PROPOSE_DECISION
    tools = [PAPER_BUY] if address is None else [READ_RESULT, proposal]
    if scoped_mode is not None:
        tools, terminal_tool = scoped_mode
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
            if scoped_mode is not None:
                result = scoped_call(address, name, args, outbox, tools, terminal_tool)
            elif address is not None and name == "read_work_result" and args == {}:
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
                    and isinstance(args, dict)
                    and set(args) == {"quantity", "limit_minor_units"}
                    and all(type(value) is int and value > 0 for value in args.values())
                )
                valid_decision = (
                    address is not None
                    and name == proposal["name"]
                    and decision_args(args, proposal, revision)
                )
                try:
                    if not (valid_buy or valid_decision):
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
    if len(sys.argv) == 4 and sys.argv[1] in (
        "--mcp-review",
        "--mcp-trade",
        *SCOPED_MODES,
    ):
        serve(
            Path(sys.argv[3]),
            sys.argv[2],
            trading=sys.argv[1] == "--mcp-trade",
            scoped_mode=SCOPED_MODES.get(sys.argv[1]),
        )
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
    if (
        operation
        not in (
            "first_duty",
            "review_duty",
            "trade_duty",
            "research_duty",
            "staffing_duty",
            "onboarding_duty",
            "assessment_duty",
            "assessment_review",
            "hired_research_duty",
        )
        or not ready
    ):
        raise SystemExit("An existing ChatGPT subscription login is required")
    with TemporaryDirectory() as directory:
        outbox = Path(directory) / "proposal.json"
        decision = Path(directory) / "decision.txt"
        tool_args = [str(Path(__file__).resolve())]
        if operation == "hired_research_duty":
            tool_args += ["--mcp-hired-research", request["read_endpoint"], str(outbox)]
            tool_names = [tool["name"] for tool in HIRED_RESEARCH_TOOLS]
            prompt = (
                "You are newly hired for a bounded synthetic paper execution verification, not for live trading or forecasting. "
                "Read your desk and responsibility with read_first_duty and explicitly accept it if authorized. "
                "If the desk shows saved_buy, your original finding and pending decision are already recorded. "
                "Do not replace them. Decide whether to separately request that decision_id and do not act if it is no longer justified. "
                "Then read_research_inputs, choose your own comparison question and evaluate two observed prices. "
                "Publish the signed later-minus-earlier change, the sum of the two fees "
                "(price times fee_bps divided by 10000, rounded half up for each), their difference and uncertainty. "
                "Use record_research_decision to explain how the immutable finding changes your choice. "
                "Here the disclosed quote and cap are synthetic execution terms. A bounded simulation exercise "
                "does not require evidence of live liquidity or profitable persistence; assess whether the disclosed "
                "paper terms and the finding warrant exercising that path. "
                "A feasible bounded verification buy, a hold or investigation is valid. Choose a follow-up of 60 to 120 seconds. "
                "If you choose a buy, separately call request_research_order with the returned decision_id. "
                "A recorded research decision is not an order or a fill. No shell, external account or live funds."
            )
        elif operation == "assessment_duty":
            tool_args += ["--mcp-assessment", request["read_endpoint"], str(outbox)]
            tool_names = [tool["name"] for tool in ASSESSMENT_TOOLS]
            prompt = (
                "You are a newly hired Sourdough Agent. Read your assignment with read_first_duty. "
                "Accept it explicitly if in scope, then read the records with read_assessment_inputs. "
                "Choose a record and compare what was reported with what was observed. "
                "Publish one assessment tied to what you read. Recommend a correction, no change or "
                "investigation as appropriate. Explain the evidence and select a next check in 60 to "
                "120 seconds. You cannot change source records; no shell or external access."
            )
        elif operation == "assessment_review":
            tool_args += ["--mcp-review", request["read_endpoint"], str(outbox)]
            tool_names = ["read_work_result", "record_review_decision"]
            prompt = (
                "Review your previous company assessment. Read that result and current records via "
                "read_work_result. Explain what the current records support and relate it to the "
                "original assessment. Decide whether to hold, investigate or recommend a change. "
                "Record the new decision and next responsibility. No shell or external access."
            )
        elif operation == "onboarding_duty":
            tool_args += ["--mcp-onboarding", request["read_endpoint"], str(outbox)]
            tool_names = [tool["name"] for tool in ONBOARDING_TOOLS]
            prompt = (
                "You are a newly hired Sourdough Agent. Use read_first_duty to read your "
                "company mandate, private desk and assigned first duty. If you accept responsibility, "
                "use accept_first_duty with the returned revision and a reason grounded in that assignment. "
                "Do not perform another Agent's duty or invent a grant. No shell or external access."
            )
        elif operation == "staffing_duty":
            tool_args += ["--mcp-staffing", request["read_endpoint"], str(outbox)]
            tool_names = [tool["name"] for tool in STAFFING_TOOLS]
            prompt = (
                "You are the delegated hiring manager for a local Sourdough company trial. "
                "Use read_staffing_need to compare workload, existing coverage and delegated allowance. "
                "If the result includes saved_choice, keep that original judgment and choice ID. "
                "Recheck current allowance before explicitly deciding whether to request the hire; a saved choice grants nothing. "
                "Choose whether to hire, reassign or defer. Consider both unselected alternatives and "
                "cite the revision, factual rationale and next check in record_staffing_choice. "
                "Do not assume hiring is needed. If justified and affordable, use request_hire with "
                "the committed choice ID, a stable request ID, role, displayed profile and scope, "
                "and a bounded turn allocation sufficient for the hire's independent work and review. "
                "No shell, external account or spending permission is available."
            )
        elif operation == "research_duty":
            tool_args += ["--mcp-research", request["read_endpoint"], str(outbox)]
            tool_names = [tool["name"] for tool in RESEARCH_TOOLS]
            prompt = (
                "You are running a bounded, synthetic execution-verification duty, not forecasting returns. "
                "Both an evidence-supported test order and a decision not to trade are valid. Read the available research inputs "
                "with read_research_inputs. Choose a research question and compare two observed prices. "
                "Report the signed later-minus-earlier change, the sum of the fee on each price "
                "(each per-unit price times fee_bps, divided by 10000 and rounded half up), "
                "their difference after fees, and uncertainty. Publish your finding before deciding. "
                "Use record_research_decision to cite its ID and revision, explain what the result changes, "
                "and decide whether to hold, investigate or propose a bounded paper buy. "
                "If your comparison and the disclosed exposure limit support exercising synthetic execution, "
                "you may choose quantity and limit for a small verification order; uncertainty about profitable "
                "live trading is not the subject of this exercise. Choose a next check interval of 60 to 120 seconds. "
                "No order is required. Do not use the shell or claim a fill."
            )
        elif operation == "trade_duty":
            tool_args += ["--mcp-trade", request["read_endpoint"], str(outbox)]
            tool_names = ["read_work_result", "record_paper_decision"]
            prompt = (
                "You are operating a synthetic Sourdough paper account. "
                "Inspect the committed position and paper terms with read_work_result. "
                "Your operational objective is to assess whether a second small paper execution can exercise "
                "the later duty safely. Decide whether to hold, investigate or buy; do not assume a buy is required. "
                "If buying, choose quantity and limit yourself within the disclosed available exposure. "
                "Use record_paper_decision with the observed revision, your reason and the next check interval. "
                "Do not use the shell. A staged request is not an execution or a fill."
            )
        elif operation == "review_duty":
            tool_args += ["--mcp-review", request["read_endpoint"], str(outbox)]
            tool_names = ["read_work_result", "record_review_decision"]
            prompt = (
                "Review the result of your preceding Sourdough duty. "
                "Use read_work_result to inspect the committed position or the preceding research result. "
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
            timeout=80
            if operation in ("research_duty", "assessment_duty", "hired_research_duty")
            else 50,
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
