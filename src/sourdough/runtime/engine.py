"""Unified CompanyRuntime dispatching registered duties on one SQLite store."""

from collections.abc import Callable
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path
import threading
from time import time_ns
from typing import Any, TypeVar
from uuid import uuid4

from ..duty_run import has_turn_capacity
from ..storage.db import open_writer, wal_checkpoint, write_tx
from ..writer_lease import WriterLease, hold_company_writer
from .domains import IndependentProcessGuard
from .registry import (
    DutyContext,
    DutyDescriptor,
    DutyOutcome,
    DutyRegistry,
    RuntimeRefused,
)
from .work_kernel import (
    RunFenced,
    WorkItem,
    cancel_closed_peer_response_item,
    claim_work_run,
    complete_work_run,
    evaluate_conditions,
    fail_work_run,
    has_active_grant,
    has_authorized_peer_response_item,
    reclaim_expired_runs,
    wake_waiting_runs,
    yield_run_to_wait,
)

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class RuntimeStepSummary:
    fenced_runs: tuple[str, ...]
    woken_items: tuple[str, ...]
    admitted_items: tuple[str, ...]
    executed_runs: tuple[str, ...]
    blocked_items: tuple[str, ...]


class CompanyRuntime:
    """Single-node durable company runtime over the three-entity Work kernel."""

    def __init__(
        self,
        store: Path,
        company_id: str,
        registry: DutyRegistry,
        *,
        lease_owner: str | None = None,
        lease_ttl_ns: int = 300_000_000_000,
        lease_directory: Path | None = None,
        expected_store_key: tuple[int, int] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._store = store.absolute()
        self._company_id = company_id
        self._registry = registry
        self._lease_owner = lease_owner or f"runtime-{uuid4().hex[:12]}"
        self._lease_ttl_ns = lease_ttl_ns
        self._lease_directory = lease_directory
        self._expected_store_key = expected_store_key
        self._clock = clock or time_ns

    def step(
        self,
        *,
        max_dispatches: int = 8,
        now_ns: int | None = None,
        observed_assets: dict[str, str] | None = None,
        event_key: str | None = None,
        triggering_actor_id: str | None = None,
        on_ready: Callable[[WriterLease], None] | None = None,
        truncate_wal_on_exit: bool = False,
    ) -> RuntimeStepSummary:
        """Run one durable recovery, wake, condition-evaluation, and dispatch cycle."""
        with hold_company_writer(
            self._store,
            self._company_id,
            lease_directory=self._lease_directory,
            expected_store_key=self._expected_store_key,
        ) as writer:
            return self.step_with_writer(
                writer,
                max_dispatches=max_dispatches,
                now_ns=now_ns,
                observed_assets=observed_assets,
                event_key=event_key,
                triggering_actor_id=triggering_actor_id,
                on_ready=on_ready,
                truncate_wal_on_exit=truncate_wal_on_exit,
            )

    def step_with_writer(
        self,
        writer: WriterLease,
        *,
        max_dispatches: int = 8,
        now_ns: int | None = None,
        observed_assets: dict[str, str] | None = None,
        event_key: str | None = None,
        triggering_actor_id: str | None = None,
        on_ready: Callable[[WriterLease], None] | None = None,
        truncate_wal_on_exit: bool = False,
        tx_lock: threading.RLock | None = None,
        on_commit: Callable[[], None] | None = None,
    ) -> RuntimeStepSummary:
        """Execute one durable sweep/claim/dispatch cycle using an already-held WriterLease."""
        if max_dispatches < 0:
            raise RuntimeRefused("max_dispatches must be nonnegative")
        read_clock: Callable[[], int] = (
            self._clock if now_ns is None else (lambda: now_ns)
        )
        lock_ctx = tx_lock if tx_lock is not None else nullcontext()

        writer.verify_for(self._store, self._company_id)
        if on_ready is not None:
            on_ready(writer)

        sweep_now = read_clock()
        with (
            lock_ctx,
            closing(open_writer(self._store, lease=writer)) as conn,
            write_tx(conn),
        ):
            fenced = reclaim_expired_runs(conn, self._company_id, now_ns=sweep_now)
            woken = wake_waiting_runs(
                conn,
                self._company_id,
                now_ns=sweep_now,
                event_key=event_key,
                triggering_actor_id=triggering_actor_id,
            )
            admitted_items = evaluate_conditions(
                conn,
                self._company_id,
                now_ns=sweep_now,
                observed_assets=observed_assets,
                triggering_actor_id=triggering_actor_id,
            )
        if on_commit is not None and (fenced or woken or admitted_items):
            on_commit()

        executed: list[str] = []
        blocked: list[str] = []
        for _ in range(max_dispatches):
            writer.verify()
            claim_now = read_clock()
            with lock_ctx:
                claimed = self._claim_next(writer, claim_now, blocked)
            if claimed is None:
                break
            item, run, descriptor = claimed
            if on_commit is not None:
                on_commit()
            # Execute outside any open SQLite write transaction or tx_lock with a live clock.
            try:
                outcome = descriptor.execute(
                    DutyContext(writer, self._company_id, item, run, read_clock)
                )
            except Exception as error:
                try:
                    writer.verify()
                    fail_now = read_clock()
                    with (
                        lock_ctx,
                        closing(open_writer(self._store, lease=writer)) as conn,
                        write_tx(conn),
                    ):
                        fail_work_run(
                            conn,
                            run.run_id,
                            run.fencing_token,
                            error_summary=type(error).__name__,
                            return_to_due=True,
                            now_ns=fail_now,
                        )
                    if on_commit is not None:
                        on_commit()
                except RunFenced:
                    pass
                raise
            writer.verify()
            commit_now = read_clock()
            with (
                lock_ctx,
                closing(open_writer(self._store, lease=writer)) as conn,
                write_tx(conn),
            ):
                if outcome.disposition == "waiting":
                    # Handlers such as open_peer_request() may already have
                    # transitioned this fenced run into durable waiting state.
                    status_row = conn.execute(
                        """SELECT status FROM work_runs
                           WHERE run_id = ? AND fencing_token = ?""",
                        (run.run_id, run.fencing_token),
                    ).fetchone()
                    if status_row is None or status_row[0] != "waiting":
                        yield_run_to_wait(
                            conn,
                            run.run_id,
                            run.fencing_token,
                            wait_until_ns=outcome.wait_until_ns,
                            wait_event_key=outcome.wait_event_key,
                            reason=outcome.wait_reason,
                            now_ns=commit_now,
                        )
                elif outcome.disposition == "blocked":
                    complete_work_run(
                        conn,
                        run.run_id,
                        run.fencing_token,
                        outcome=outcome.result,
                        item_state="blocked",
                        now_ns=commit_now,
                    )
                    blocked.append(item.work_item_id)
                else:
                    complete_work_run(
                        conn,
                        run.run_id,
                        run.fencing_token,
                        outcome=outcome.result,
                        item_state="settled",
                        next_condition=outcome.next_condition,
                        now_ns=commit_now,
                    )
            executed.append(run.run_id)
            if on_commit is not None:
                on_commit()

        with lock_ctx, closing(open_writer(self._store, lease=writer)) as conn:
            wal_checkpoint(conn, "TRUNCATE" if truncate_wal_on_exit else "PASSIVE")

        return RuntimeStepSummary(
            fenced_runs=fenced,
            woken_items=woken,
            admitted_items=tuple(item.work_item_id for item in admitted_items),
            executed_runs=tuple(executed),
            blocked_items=tuple(blocked),
        )

    def run_bound_handler(
        self,
        duty_kind: str,
        execute: Callable[[WriterLease], _ResultT],
        *,
        process_guard: IndependentProcessGuard | None = None,
        now_ns: int | None = None,
        truncate_wal_on_exit: bool = False,
    ) -> _ResultT:
        """Execute a registered company duty under one WriterLease, Work sweep, and process guard."""
        self._registry.get(duty_kind)
        if process_guard is not None:
            process_guard.require_independent_from(self._store)
        timestamp = self._clock() if now_ns is None else now_ns
        with hold_company_writer(
            self._store,
            self._company_id,
            lease_directory=self._lease_directory,
            expected_store_key=self._expected_store_key,
        ) as writer:
            with (
                closing(open_writer(self._store, lease=writer)) as conn,
                write_tx(conn),
            ):
                reclaim_expired_runs(conn, self._company_id, now_ns=timestamp)
                wake_waiting_runs(conn, self._company_id, now_ns=timestamp)
                evaluate_conditions(conn, self._company_id, now_ns=timestamp)
            result = execute(writer)
            with closing(open_writer(self._store, lease=writer)) as conn:
                wal_checkpoint(conn, "TRUNCATE" if truncate_wal_on_exit else "PASSIVE")
            return result

    def _claim_next(
        self,
        writer: WriterLease,
        now_ns: int,
        blocked: list[str],
    ) -> tuple[WorkItem, Any, DutyDescriptor] | None:
        with closing(open_writer(self._store, lease=writer)) as conn, write_tx(conn):
            rows = conn.execute(
                """SELECT work_item_id, company_id, owner_agent_id, duty_kind, state,
                          source_kind, source_id, parent_work_item_id, dedupe_key,
                          payload_json, created_at_ns, updated_at_ns
                   FROM work_items
                   WHERE company_id = ? AND state = 'due'
                     AND owner_agent_id NOT IN (
                         SELECT agent_id FROM work_runs WHERE status = 'running'
                     )
                     AND owner_agent_id NOT IN (
                         SELECT agent_id FROM duty WHERE state = 'running'
                     )
                   ORDER BY created_at_ns, work_item_id""",
                (self._company_id,),
            ).fetchall()
            for row in rows:
                item = WorkItem(*row)
                if item.work_item_id in blocked:
                    continue
                if item.duty_kind == "work.peer_response":
                    if cancel_closed_peer_response_item(
                        conn, self._company_id, item.work_item_id, now_ns=now_ns
                    ):
                        continue
                    if not has_authorized_peer_response_item(
                        conn, self._company_id, item.work_item_id
                    ):
                        blocked.append(item.work_item_id)
                        continue
                descriptor = self._registry.get(item.duty_kind)
                if not has_active_grant(
                    conn,
                    self._company_id,
                    item.owner_agent_id,
                    required_scope=descriptor.required_scope,
                ):
                    blocked.append(item.work_item_id)
                    continue
                if not has_turn_capacity(conn, item.owner_agent_id):
                    blocked.append(item.work_item_id)
                    continue
                if descriptor.precondition is not None:
                    ready, _reason = descriptor.precondition(conn, item)
                    if not ready:
                        blocked.append(item.work_item_id)
                        continue
                run = claim_work_run(
                    conn,
                    company_id=self._company_id,
                    work_item_id=item.work_item_id,
                    lease_owner=self._lease_owner,
                    lease_ttl_ns=self._lease_ttl_ns,
                    required_scope=descriptor.required_scope,
                    now_ns=now_ns,
                )
                return item, run, descriptor
        return None


def build_default_registry(
    scenario_handlers: dict[str, Callable[[DutyContext], DutyOutcome]] | None = None,
) -> DutyRegistry:
    """Create a DutyRegistry with optional scenario handlers bound cleanly."""
    registry = DutyRegistry()
    for duty_kind, fn in (scenario_handlers or {}).items():
        registry.register(DutyDescriptor(duty_kind=duty_kind, execute=fn))
    return registry
