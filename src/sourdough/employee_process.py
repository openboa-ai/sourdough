"""Bridge a provisioned employee process to the first-duty tool boundary."""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from hashlib import file_digest, sha256
from pathlib import Path

from .first_trade import OrderTool, TradeRefused, TradingContext


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
                timeout=60,
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
