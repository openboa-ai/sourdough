"""Shared fenced tool execution base and argument validation helpers for OuterToolBroker suites."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from time import time_ns
from typing import Any

from ..agent_desk import require_desk
from ..duty_run import DutyRefused, WorkRunClaim
from ..storage.db import ManagedConnection, scoped_reader, write_uow
from ..writer_lease import WriterLease
from .work_kernel import assert_run_fencing


def require_object(
    args: Any, *, allowed: set[str], required: set[str], label: str
) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise DutyRefused(f"{label} arguments must be a JSON object")
    keys = set(args.keys())
    if not required <= keys or not keys <= allowed:
        raise DutyRefused(f"{label} arguments do not match schema")
    return args


def require_str(
    value: Any,
    label: str,
    *,
    max_len: int | None = 128,
    strip_check: bool = True,
) -> str:
    if not isinstance(value, str):
        raise DutyRefused(f"{label} must be a nonempty string")
    if strip_check:
        if not value.strip():
            raise DutyRefused(f"{label} must be a nonempty string")
    elif not value:
        raise DutyRefused(f"{label} must be a nonempty string")
    if max_len is not None and len(value) > max_len:
        raise DutyRefused(f"{label} must be a nonempty string")
    return value


def optional_str(value: Any, label: str, *, max_len: int | None = 128) -> str | None:
    if value is None:
        return None
    return require_str(value, label, max_len=max_len)


def require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value >= 2**63:
        raise DutyRefused(f"{label} must be a valid integer")
    return value


def optional_int(value: Any, label: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return require_int(value, label, minimum=minimum)


class FencedToolBase:
    """Shared fencing, clock, Desk check, and SQLite UoW helper for Core tool suites."""

    def __init__(
        self,
        *,
        writer: WriterLease,
        claim: WorkRunClaim,
        purpose: str | None = None,
        tx_lock: threading.RLock | None = None,
    ) -> None:
        self.writer = writer
        self.claim = claim
        self.purpose = claim.purpose if purpose is None else purpose
        self.tx_lock = tx_lock

    def lock_ctx(self) -> Any:
        return self.tx_lock if self.tx_lock is not None else nullcontext()

    def now_ns(self) -> int:
        ts = self.claim.current_now_ns()
        return time_ns() if ts is None else int(ts)

    def now_sec(self) -> int:
        return max(0, self.now_ns() // 1_000_000_000)

    def verify_fencing_on(self, db: Any, *, verify_desk: bool = False) -> None:
        assert_run_fencing(
            db,
            self.claim.run_id,
            self.claim.fencing_token,
            now_ns=self.claim.current_now_ns(),
        )
        if verify_desk:
            require_desk(db, self.claim, self.purpose)

    def verify_fencing(self, *, verify_desk: bool = False) -> None:
        with scoped_reader(self.writer.store) as db:
            self.verify_fencing_on(db, verify_desk=verify_desk)

    @contextmanager
    def fenced_reader(
        self, *, verify_desk: bool = False, verify_writer: bool = False
    ) -> Iterator[ManagedConnection]:
        if verify_writer:
            self.writer.verify_for(self.writer.store, self.claim.company_id)
        with scoped_reader(self.writer.store) as db:
            self.verify_fencing_on(db, verify_desk=verify_desk)
            yield db

    @contextmanager
    def fenced_write_uow(
        self,
        *,
        verify_desk: bool = False,
        verify_fencing: bool = True,
        verify_writer: bool = True,
    ) -> Iterator[ManagedConnection]:
        if verify_writer:
            self.writer.verify_for(self.writer.store, self.claim.company_id)
        with write_uow(self.writer, tx_lock=self.tx_lock) as db:
            if verify_fencing:
                self.verify_fencing_on(db, verify_desk=verify_desk)
            elif verify_desk:
                require_desk(db, self.claim, self.purpose)
            yield db
