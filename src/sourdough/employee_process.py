"""Bridge a provisioned employee process to scoped company tools."""

import json
import os
import socketserver
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from .first_duty import DutyRefused
from .first_trade import OrderTool, TradeRefused, TradingContext
from .harness import ScopedRun
from .writer_lease import WriterRefused

MAX_TOOL_REQUEST_BYTES = 64 * 1024
LOCAL_MACOS_SANDBOX = "local-macos"


class _ReadWorkResult(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(1)
        try:
            result = self.server.read_result()
        except (DutyRefused, TradeRefused):
            result = {"error": "Work result is not available to this claim"}
        self.request.sendall(json.dumps(result).encode())


class _ResultEndpoint(socketserver.UnixStreamServer):
    def __init__(self, address: str, read_result: Callable[[], dict]):
        self.read_result = read_result
        super().__init__(address, _ReadWorkResult)


class _ToolRequest(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(2)
        try:
            line = self.rfile.readline(MAX_TOOL_REQUEST_BYTES + 1)
            if not line.endswith(b"\n") or len(line) > MAX_TOOL_REQUEST_BYTES:
                raise DutyRefused("Invalid scoped tool request")
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"name", "arguments"}:
                raise DutyRefused("Invalid scoped tool envelope")
            result = self.server.invoke(request["name"], request["arguments"])
        except (DutyRefused, WriterRefused) as error:
            result = {"error": str(error)}
        except (OSError, ValueError):
            result = {"error": "Scoped tool exchange failed"}
        self.wfile.write(json.dumps(result).encode())


class _ToolEndpoint(socketserver.UnixStreamServer):
    def __init__(self, address: str, invoke: Callable[[str, dict], dict]):
        self.invoke = invoke
        super().__init__(address, _ToolRequest)


class EmployeeProcess:
    def __init__(self, executable: Path):
        self.executable = executable.resolve()
        selection = self._identify()
        self.sandbox_backend = selection.get("sandbox_backend")
        self.profile = self._profile(selection)

    def _identify(self) -> dict:
        selection = self._exchange({"operation": "identify"})
        runtime_fields = {"provider", "model", "revision"}
        if set(selection) not in (runtime_fields, runtime_fields | {"sandbox_backend"}):
            raise TradeRefused("Employee runtime binding is unresolved")
        if not all(
            isinstance(value, str) and value.strip() for value in selection.values()
        ):
            raise TradeRefused("Employee runtime binding is unresolved")
        if (
            "sandbox_backend" in selection
            and selection["sandbox_backend"] != LOCAL_MACOS_SANDBOX
        ):
            raise TradeRefused("Employee sandbox backend is not qualified")
        return selection

    def _profile(self, selection: dict | None = None) -> str:
        if selection is None:
            selection = self._identify()
        with self.executable.open("rb") as stream:
            executable = file_digest(stream, "sha256").hexdigest()
        terms = json.dumps([executable, selection], sort_keys=True).encode()
        return "process-v2:" + sha256(terms).hexdigest()

    def _exchange(self, request: dict) -> dict:
        try:
            result = subprocess.run(
                [str(self.executable)],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=100
                if request["operation"]
                in ("research_duty", "assessment_duty", "hired_research_duty")
                else 60,
                check=True,
                cwd="/",
                env={"PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}"},
            )
            response = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise TradeRefused(
                "Employee process did not complete its exchange"
            ) from error
        if not isinstance(response, dict):
            raise TradeRefused("Invalid employee response")
        return response

    def check_ready(self) -> None:
        if self._profile() != self.profile:
            raise TradeRefused("Employee model binding changed")
        if self._exchange({"operation": "ready"}) != {"ready": True}:
            raise TradeRefused("Employee process is unavailable")

    def run(self, context: TradingContext, order_tool: OrderTool) -> None:
        if self._profile() != self.profile:
            raise TradeRefused("Employee model binding changed")
        response = self._exchange(
            {
                "operation": "first_duty",
                "context": asdict(context),
                "tools": ["paper_buy"],
            }
        )
        if response == {"action": "hold"}:
            return
        if set(response) != {"tool", "call_id", "quantity", "limit_minor_units"} or (
            response["tool"] != "paper_buy"
        ):
            raise TradeRefused("Unexpected employee tool request")
        order_tool(
            response["call_id"], response["quantity"], response["limit_minor_units"]
        )

    def review(self, read_result: Callable[[], dict]) -> dict:
        return self._follow_up("review_duty", read_result)

    def trade(self, read_result: Callable[[], dict]) -> dict:
        return self._follow_up("trade_duty", read_result)

    def research(self, invoke: Callable[[str, dict], dict]) -> dict:
        return self._scoped_exchange("research_duty", _ToolEndpoint, invoke)

    def staffing(self, invoke: Callable[[str, dict], dict]) -> dict:
        return self._scoped_exchange("staffing_duty", _ToolEndpoint, invoke)

    def onboarding(self, invoke: Callable[[str, dict], dict]) -> dict:
        return self._scoped_exchange("onboarding_duty", _ToolEndpoint, invoke)

    def run_scoped(self, operation: str, run: ScopedRun) -> dict:
        if run.claim.execution_profile != self.profile:
            raise TradeRefused("Scoped run belongs to another execution profile")
        return self._scoped_exchange(
            operation, _ToolEndpoint, run.call, harness=run.manifest()
        )

    def research_hired(self, invoke: Callable[[str, dict], dict]) -> dict:
        return self._scoped_exchange("hired_research_duty", _ToolEndpoint, invoke)

    def _follow_up(self, operation: str, read_result: Callable[[], dict]) -> dict:
        return self._scoped_exchange(operation, _ResultEndpoint, read_result)

    def _scoped_exchange(
        self, operation: str, endpoint_type, callback, *, harness: dict | None = None
    ) -> dict:
        if self._profile() != self.profile:
            raise TradeRefused("Employee model binding changed")
        with TemporaryDirectory(prefix="sd-") as directory:
            address = str(Path(directory) / "result.sock")
            with endpoint_type(address, callback) as endpoint:
                thread = Thread(
                    target=endpoint.serve_forever, kwargs={"poll_interval": 0.05}
                )
                thread.start()
                try:
                    request = {"operation": operation, "read_endpoint": address}
                    if harness is not None:
                        request["harness"] = harness
                    return self._exchange(request)
                finally:
                    endpoint.shutdown()
                    thread.join()
