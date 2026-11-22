"""Bridge a provisioned employee process to the first-duty tool boundary."""

import json
import os
import subprocess
from dataclasses import asdict
from hashlib import file_digest
from pathlib import Path

from .first_trade import OrderTool, TradeRefused, TradingContext


class EmployeeProcess:
    def __init__(self, executable: Path):
        self.executable = executable.resolve()
        with self.executable.open("rb") as stream:
            self.profile = "process-v1:" + file_digest(stream, "sha256").hexdigest()

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
                env={"PATH": os.defpath},
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
        if self._exchange({"operation": "ready"}) != {"ready": True}:
            raise TradeRefused("Employee process is unavailable")

    def run(self, context: TradingContext, order_tool: OrderTool) -> None:
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
