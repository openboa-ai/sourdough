"""Typed DutyRegistry mapping duty_kind to handlers without scenario-specific loops."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..writer_lease import WriterLease
from .work_kernel import WorkItem, WorkRun


class RuntimeRefused(Exception):
    """The unified runtime rejected a duty registration or dispatch."""


@dataclass(frozen=True)
class DutyContext:
    writer: WriterLease
    company_id: str
    work_item: WorkItem
    run: WorkRun
    now_ns: int


@dataclass(frozen=True)
class DutyOutcome:
    disposition: Literal["completed", "waiting", "blocked"]
    result: dict[str, Any] = field(default_factory=dict)
    wait_until_ns: int | None = None
    wait_event_key: str | None = None
    wait_reason: str = "waiting"
    next_condition: dict[str, Any] | None = None


@dataclass(frozen=True)
class DutyDescriptor:
    duty_kind: str
    execute: Callable[[DutyContext], DutyOutcome]
    precondition: (
        Callable[[sqlite3.Connection, WorkItem], tuple[bool, str | None]] | None
    ) = None
    required_scope: str | None = None


class DutyRegistry:
    """Registry of duty_kind handlers dispatched by CompanyRuntime."""

    def __init__(self) -> None:
        self._handlers: dict[str, DutyDescriptor] = {}

    def register(self, descriptor: DutyDescriptor) -> None:
        if not descriptor.duty_kind or not descriptor.duty_kind.strip():
            raise RuntimeRefused("duty_kind must be nonempty")
        if descriptor.duty_kind in self._handlers:
            raise RuntimeRefused(
                f"Duplicate duty_kind registration: {descriptor.duty_kind}"
            )
        self._handlers[descriptor.duty_kind] = descriptor

    def get(self, duty_kind: str) -> DutyDescriptor:
        descriptor = self._handlers.get(duty_kind)
        if descriptor is None:
            raise RuntimeRefused(f"Unregistered duty_kind: {duty_kind}")
        return descriptor

    def registered_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))
