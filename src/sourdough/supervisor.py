"""Bound and observe product-owned company worker replacements."""

import json
import os
import select
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep, time_ns

from .writer_lease import WriterLease


class SupervisionRefused(Exception):
    """The configured local worker cannot be supervised safely."""


@dataclass(frozen=True)
class ChildAttempt:
    pid: int
    ready_at_ns: int | None
    exited_at_ns: int
    returncode: int


@dataclass(frozen=True)
class SupervisionResult:
    attempts: tuple[ChildAttempt, ...]
    completed: bool


def announce_ready(descriptor: int, lease: WriterLease) -> None:
    try:
        lease.verify()
        payload = {
            "pid": os.getpid(),
            "company_id": lease.company_id,
            "store_key": lease.store_key,
        }
        os.write(
            descriptor, json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        )
    finally:
        os.close(descriptor)


def _read_ready(
    descriptor: int,
    child_pid: int,
    company_id: str,
    key: tuple[int, int],
    timeout: float,
) -> bool:
    if not select.select([descriptor], [], [], max(0, timeout))[0]:
        raise SupervisionRefused("Worker readiness deadline expired")
    payload = os.read(descriptor, 4096)
    if not payload:
        return False
    try:
        decoded = json.loads(payload)
    except ValueError as error:
        raise SupervisionRefused("Malformed worker readiness evidence") from error
    if decoded != {"pid": child_pid, "company_id": company_id, "store_key": list(key)}:
        raise SupervisionRefused("Worker readiness does not match the original company")
    return True


def supervise_worker(
    command: Sequence[str],
    store: Path,
    company_id: str,
    *,
    max_restarts: int,
    startup_seconds: int,
    max_seconds: int,
    emit: Callable[[dict], None],
    environment: Mapping[str, str] | None = None,
    expected_store_key: tuple[int, int] | None = None,
    inherited_fds: tuple[int, ...] = (),
) -> SupervisionResult:
    if (
        not 0 <= max_restarts <= 4
        or not 1 <= startup_seconds <= 120
        or not 1 <= max_seconds <= 1800
    ):
        raise SupervisionRefused("Invalid worker supervision bounds")
    if not store.is_file() or store.is_symlink():
        raise SupervisionRefused("Original company store is unavailable")
    identity = store.stat(follow_symlinks=False)
    key = identity.st_dev, identity.st_ino
    if expected_store_key is not None and key != expected_store_key:
        raise SupervisionRefused("Original worker store changed before launch")
    deadline = monotonic() + max_seconds
    attempts = []
    for sequence in range(max_restarts + 1):
        if sequence:
            sleep(max(0, min(2 ** (sequence - 1), deadline - monotonic())))
        if monotonic() >= deadline:
            break
        read_end, write_end = os.pipe()
        try:
            try:
                child = subprocess.Popen(
                    [
                        *command,
                        "--ready-fd",
                        str(write_end),
                        "--store-key",
                        f"{key[0]}:{key[1]}",
                    ],
                    pass_fds=(write_end, *inherited_fds),
                    env=environment,
                )
            finally:
                os.close(write_end)
            ready_at = None
            try:
                emit(
                    {
                        "event": "worker_started",
                        "pid": child.pid,
                        "attempt": sequence + 1,
                    }
                )
                if _read_ready(
                    read_end,
                    child.pid,
                    company_id,
                    key,
                    min(startup_seconds, deadline - monotonic()),
                ):
                    ready_at = time_ns()
                    emit(
                        {"event": "worker_ready", "pid": child.pid, "time_ns": ready_at}
                    )
                child.wait(max(0, deadline - monotonic()))
            except subprocess.TimeoutExpired:
                emit({"event": "worker_runtime_bound", "pid": child.pid})
            except SupervisionRefused as error:
                emit(
                    {
                        "event": "worker_not_ready",
                        "pid": child.pid,
                        "reason": str(error),
                    }
                )
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(5)
            attempt = ChildAttempt(child.pid, ready_at, time_ns(), child.returncode)
            attempts.append(attempt)
            emit(
                {
                    "event": "worker_exited",
                    "pid": child.pid,
                    "returncode": child.returncode,
                    "time_ns": attempt.exited_at_ns,
                }
            )
            if ready_at is not None and child.returncode == 0:
                return SupervisionResult(tuple(attempts), True)
        finally:
            os.close(read_end)
    emit({"event": "supervision_exhausted", "attempts": len(attempts)})
    return SupervisionResult(tuple(attempts), False)
