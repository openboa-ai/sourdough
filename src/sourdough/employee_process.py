"""Bridge a provisioned employee process to scoped company tools."""

import json
import os
import socketserver
import subprocess
import sys
from collections.abc import Callable, Sequence
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Any

from .duty_run import DutyRefused
from .harness import ScopedRun, run_in_desk_sandbox
from .writer_lease import WriterRefused

MAX_TOOL_REQUEST_BYTES = 64 * 1024
LOCAL_MACOS_SANDBOX = "local-macos"


class EmployeeProcessRefused(Exception):
    """Employee process binding or exchange failed."""


class _ReadWorkResult(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(1)
        try:
            result = self.server.read_result()
        except (DutyRefused, EmployeeProcessRefused):
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
            raise EmployeeProcessRefused("Employee runtime binding is unresolved")
        if not all(
            isinstance(value, str) and value.strip() for value in selection.values()
        ):
            raise EmployeeProcessRefused("Employee runtime binding is unresolved")
        if (
            "sandbox_backend" in selection
            and selection["sandbox_backend"] != LOCAL_MACOS_SANDBOX
        ):
            raise EmployeeProcessRefused("Employee sandbox backend is not qualified")
        return selection

    def _profile(self, selection: dict | None = None) -> str:
        if selection is None:
            selection = self._identify()
        with self.executable.open("rb") as stream:
            executable = file_digest(stream, "sha256").hexdigest()
        terms = json.dumps([executable, selection], sort_keys=True).encode()
        return "process-v2:" + sha256(terms).hexdigest()

    def _exchange(self, request: dict, *, timeout: int | None = None) -> dict:
        effective_timeout = (
            timeout
            if timeout is not None
            else (100 if request.get("operation") == "assessment_duty" else 60)
        )
        try:
            result = subprocess.run(
                [str(self.executable)],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                check=True,
                cwd="/",
                env={"PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}"},
            )
            response = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise EmployeeProcessRefused(
                "Employee process did not complete its exchange"
            ) from error
        if not isinstance(response, dict):
            raise EmployeeProcessRefused("Invalid employee response")
        return response

    def check_ready(self) -> None:
        if self._profile() != self.profile:
            raise EmployeeProcessRefused("Employee model binding changed")
        if self._exchange({"operation": "ready"}) != {"ready": True}:
            raise EmployeeProcessRefused("Employee process is unavailable")

    def run_first_duty_exchange(
        self,
        *,
        context: dict[str, Any],
        tools: Sequence[str],
        operation: str = "first_duty",
        timeout: int = 60,
    ) -> dict[str, Any]:
        if self._profile() != self.profile:
            raise EmployeeProcessRefused("Employee model binding changed")
        return self._exchange(
            {
                "operation": operation,
                "context": context,
                "tools": list(tools),
            },
            timeout=timeout,
        )

    def follow_up_exchange(
        self,
        operation: str,
        read_result: Callable[[], dict],
        *,
        timeout: int = 60,
    ) -> dict:
        return self._follow_up(operation, read_result, timeout=timeout)

    def scoped_tool_exchange(
        self,
        operation: str,
        invoke: Callable[[str, dict], dict],
        *,
        timeout: int = 60,
    ) -> dict:
        return self._scoped_exchange(operation, _ToolEndpoint, invoke, timeout=timeout)

    def review(self, read_result: Callable[[], dict]) -> dict:
        return self._follow_up("review_duty", read_result)

    def staffing(self, invoke: Callable[[str, dict], dict]) -> dict:
        return self._scoped_exchange("staffing_duty", _ToolEndpoint, invoke)

    def onboarding(self, invoke: Callable[[str, dict], dict]) -> dict:
        return self._scoped_exchange("onboarding_duty", _ToolEndpoint, invoke)

    def run_scoped(
        self, operation: str, run: ScopedRun, *, timeout: int | None = None
    ) -> dict:
        if run.claim.execution_profile != self.profile:
            raise EmployeeProcessRefused(
                "Scoped run belongs to another execution profile"
            )
        return self._scoped_exchange(
            operation,
            _ToolEndpoint,
            run.call,
            harness=run.manifest(),
            timeout=timeout,
        )

    def run_sandboxed_scoped(
        self,
        operation: str,
        run: ScopedRun,
        *,
        store: Path,
        timeout: int | None = None,
    ) -> dict:
        """Run a scoped employee process inside the macOS Desk sandbox connected to the Outer Broker."""
        if run.claim.execution_profile != self.profile:
            raise EmployeeProcessRefused(
                "Scoped run belongs to another execution profile"
            )
        if self._profile() != self.profile:
            raise EmployeeProcessRefused("Employee model binding changed")
        effective_timeout = (
            timeout
            if timeout is not None
            else (100 if operation == "assessment_duty" else 60)
        )
        manifest = run.manifest()
        with TemporaryDirectory(prefix="sd-sb-") as directory:
            scratch = Path(directory).resolve()
            address = scratch / "broker.sock"
            with _ToolEndpoint(str(address), run.call) as endpoint:
                thread = Thread(
                    target=endpoint.serve_forever, kwargs={"poll_interval": 0.05}
                )
                thread.start()
                try:
                    request = {
                        "operation": operation,
                        "read_endpoint": str(address),
                        "harness": manifest,
                        "desk_path": str(run.session.desk_path),
                    }
                    result = run_in_desk_sandbox(
                        run.session,
                        [str(self.executable)],
                        store=store,
                        broker_socket=address,
                        scratch_dir=scratch,
                        extra_read_files=(self.executable,),
                        input_data=json.dumps(request),
                        timeout=effective_timeout,
                    )
                    if result.returncode != 0:
                        raise EmployeeProcessRefused(
                            "Sandboxed employee process did not complete its exchange"
                        )
                    response = json.loads(result.stdout)
                except (OSError, subprocess.SubprocessError, ValueError) as error:
                    raise EmployeeProcessRefused(
                        "Sandboxed employee process did not complete its exchange"
                    ) from error
                finally:
                    endpoint.shutdown()
                    thread.join()
        if not isinstance(response, dict):
            raise EmployeeProcessRefused("Invalid employee response")
        return response

    def _follow_up(
        self,
        operation: str,
        read_result: Callable[[], dict],
        *,
        timeout: int | None = None,
    ) -> dict:
        return self._scoped_exchange(
            operation, _ResultEndpoint, read_result, timeout=timeout
        )

    def _scoped_exchange(
        self,
        operation: str,
        endpoint_type,
        callback,
        *,
        harness: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        if self._profile() != self.profile:
            raise EmployeeProcessRefused("Employee model binding changed")
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
                    return self._exchange(request, timeout=timeout)
                finally:
                    endpoint.shutdown()
                    thread.join()
