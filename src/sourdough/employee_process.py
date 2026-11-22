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
from .writer_lease import WriterRefused

MAX_RESEARCH_REQUEST = 64 * 1024


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


class _ResearchTool(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(2)
        try:
            line = self.rfile.readline(MAX_RESEARCH_REQUEST + 1)
            if not line.endswith(b"\n") or len(line) > MAX_RESEARCH_REQUEST:
                raise DutyRefused("Invalid research tool request")
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"name", "arguments"}:
                raise DutyRefused("Invalid research tool envelope")
            result = self.server.invoke(request["name"], request["arguments"])
        except (DutyRefused, WriterRefused) as error:
            result = {"error": str(error)}
        except (OSError, ValueError):
            result = {"error": "Research tool exchange failed"}
        self.wfile.write(json.dumps(result).encode())


class _ResearchEndpoint(socketserver.UnixStreamServer):
    def __init__(self, address: str, invoke: Callable[[str, dict], dict]):
        self.invoke = invoke
        super().__init__(address, _ResearchTool)


class EmployeeProcess:
    def __init__(self, executable: Path):
        self.executable = executable.resolve()
        self.profile = self._profile()

    def _profile(self) -> str:
        selection = self._exchange({"operation": "identify"})
        if set(selection) != {"provider", "model", "revision"} or not all(
            isinstance(value, str) and value.strip() for value in selection.values()
        ):
            raise TradeRefused("Employee model binding is unresolved")
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
                timeout=100 if request["operation"] == "research_duty" else 60,
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
        return self._scoped_exchange("research_duty", _ResearchEndpoint, invoke)

    def _follow_up(self, operation: str, read_result: Callable[[], dict]) -> dict:
        return self._scoped_exchange(operation, _ResultEndpoint, read_result)

    def _scoped_exchange(self, operation: str, endpoint_type, callback) -> dict:
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
                    return self._exchange(
                        {"operation": operation, "read_endpoint": address}
                    )
                finally:
                    endpoint.shutdown()
                    thread.join()
