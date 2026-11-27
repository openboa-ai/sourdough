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
from ..episode_index import upsert_episode_entry
from ..memory import update_episode_outcome
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
    WorkEventBatch,
    WorkItem,
    cancel_closed_peer_response_item,
    claim_work_run,
    complete_work_run,
    drain_work_event_cursor,
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
    has_more_events: bool = False


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
        max_transient_attempts: int = 5,
        retry_base_backoff_ns: int = 2_000_000_000,
        retry_max_backoff_ns: int = 60_000_000_000,
    ) -> None:
        if max_transient_attempts < 1:
            raise RuntimeRefused("max_transient_attempts must be at least 1")
        if retry_base_backoff_ns < 0 or retry_max_backoff_ns < 0:
            raise RuntimeRefused("retry backoff bounds must be nonnegative")
        self._store = store.absolute()
        self._company_id = company_id
        self._registry = registry
        self._lease_owner = lease_owner or f"runtime-{uuid4().hex[:12]}"
        self._lease_ttl_ns = lease_ttl_ns
        self._lease_directory = lease_directory
        self._expected_store_key = expected_store_key
        self._clock = clock or time_ns
        self._max_transient_attempts = int(max_transient_attempts)
        self._retry_base_backoff_ns = int(retry_base_backoff_ns)
        self._retry_max_backoff_ns = int(retry_max_backoff_ns)

    def _classify_failure(
        self, error: Exception, item: WorkItem, fail_now: int
    ) -> tuple[bool, bool, int | None]:
        """Return (return_to_due, increment_attempt, next_retry_at_ns) for a failed run."""
        from ..agent_runtime.protocol import (
            AgentRuntimeRefused,
            AgentTransportInterrupted,
        )
        from ..board_control import BoardControlRefused
        from ..company import FoundingRefused
        from ..duty_run import DutyRefused
        from ..general_ledger import JournalRefused
        from ..storage.checkpoint import CheckpointRefused
        from ..storage.db import StorageRefused
        from ..writer_lease import WriterRefused
        from .domains import DomainActionRefused
        from .work_kernel import WorkKernelRefused

        if isinstance(error, RunFenced):
            return False, True, None

        if isinstance(
            error,
            (
                AgentRuntimeRefused,
                RuntimeRefused,
                DutyRefused,
                WorkKernelRefused,
                JournalRefused,
                StorageRefused,
                FoundingRefused,
                BoardControlRefused,
                DomainActionRefused,
                CheckpointRefused,
                WriterRefused,
            ),
        ):
            return False, True, None

        if isinstance(error, (TypeError, ValueError)):
            return False, True, None

        next_attempts = item.attempt_count + 1
        if isinstance(error, AgentTransportInterrupted):
            if next_attempts >= self._max_transient_attempts:
                return False, True, None
            delay_ns = min(
                self._retry_base_backoff_ns * (2 ** (next_attempts - 1)),
                self._retry_max_backoff_ns,
            )
            return True, True, fail_now + delay_ns

        return next_attempts < self._max_transient_attempts, True, None

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
        target_work_item_id: str | None = None,
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
                target_work_item_id=target_work_item_id,
            )

    def _sweep_work_state(
        self,
        writer: WriterLease,
        *,
        now_ns: int,
        tx_lock: threading.RLock | None = None,
        observed_assets: dict[str, str] | None = None,
        event_key: str | None = None,
        triggering_actor_id: str | None = None,
    ) -> tuple[WorkEventBatch, tuple[str, ...], tuple[str, ...], tuple[WorkItem, ...]]:
        lock_ctx = tx_lock if tx_lock is not None else nullcontext()
        with (
            lock_ctx,
            closing(open_writer(self._store, lease=writer)) as conn,
            write_tx(conn),
        ):
            cursor_batch = drain_work_event_cursor(
                conn, self._company_id, now_ns=now_ns
            )
            fenced = reclaim_expired_runs(conn, self._company_id, now_ns=now_ns)
            woken = wake_waiting_runs(
                conn,
                self._company_id,
                now_ns=now_ns,
                event_key=event_key,
                triggering_actor_id=triggering_actor_id,
                event_actors=cursor_batch.peer_events,
            )
            admitted_items = evaluate_conditions(
                conn,
                self._company_id,
                now_ns=now_ns,
                observed_assets=observed_assets,
                triggering_actor_id=triggering_actor_id,
                _cursor_batch=cursor_batch,
            )
        return cursor_batch, fenced, woken, admitted_items

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
        stop_event: threading.Event | None = None,
        target_work_item_id: str | None = None,
    ) -> RuntimeStepSummary:
        """Execute one durable sweep/claim/dispatch cycle using an already-held WriterLease."""
        if max_dispatches < 0:
            raise RuntimeRefused("max_dispatches must be nonnegative")
        if target_work_item_id is not None and (
            not isinstance(target_work_item_id, str) or not target_work_item_id.strip()
        ):
            raise RuntimeRefused("target_work_item_id must be a non-empty string")
        read_clock: Callable[[], int] = (
            self._clock if now_ns is None else (lambda: now_ns)
        )
        lock_ctx = tx_lock if tx_lock is not None else nullcontext()

        writer.verify_for(self._store, self._company_id)
        if on_ready is not None:
            on_ready(writer)

        sweep_now = read_clock()
        if target_work_item_id is None:
            cursor_batch, fenced, woken, admitted_items = self._sweep_work_state(
                writer,
                now_ns=sweep_now,
                tx_lock=tx_lock,
                observed_assets=observed_assets,
                event_key=event_key,
                triggering_actor_id=triggering_actor_id,
            )
        else:
            with (
                lock_ctx,
                closing(open_writer(self._store, lease=writer)) as conn,
                write_tx(conn),
            ):
                fenced = reclaim_expired_runs(conn, self._company_id, now_ns=sweep_now)
            cursor_batch = WorkEventBatch(last_event_seq=0)
            woken = ()
            admitted_items = ()
        if on_commit is not None and (fenced or woken or admitted_items):
            on_commit()

        executed: list[str] = []
        blocked: list[str] = []
        for _ in range(max_dispatches):
            if stop_event is not None and stop_event.is_set():
                break
            writer.verify()
            claim_now = read_clock()
            with lock_ctx:
                claimed = self._claim_next(
                    writer,
                    claim_now,
                    blocked,
                    target_work_item_id=target_work_item_id,
                )
            if claimed is None:
                break
            item, run, descriptor = claimed
            if on_commit is not None:
                on_commit()
            # Execute outside any open SQLite write transaction or tx_lock with a live clock.
            try:
                outcome = descriptor.execute(
                    DutyContext(
                        writer,
                        self._company_id,
                        item,
                        run,
                        read_clock,
                        tx_lock=tx_lock,
                    )
                )
            except Exception as error:
                try:
                    writer.verify()
                    fail_now = read_clock()
                    return_to_due, increment_attempt, next_retry_at_ns = (
                        self._classify_failure(error, item, fail_now)
                    )
                    reconciled_fail_ep = None
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
                            return_to_due=return_to_due,
                            increment_attempt=increment_attempt,
                            next_retry_at_ns=next_retry_at_ns,
                            now_ns=fail_now,
                        )
                        reconciled_fail_ep = update_episode_outcome(
                            conn,
                            self._company_id,
                            run.run_id,
                            outcome_status="failed",
                            reason=type(error).__name__,
                            only_from_statuses=frozenset({"completed"}),
                        )
                    if reconciled_fail_ep is not None:
                        upsert_episode_entry(self._store, reconciled_fail_ep)
                    if on_commit is not None:
                        on_commit()
                except RunFenced:
                    pass
                raise
            writer.verify()
            commit_now = read_clock()
            reconciled_ep = None
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
                    block_reason = (
                        str(outcome.result.get("reason"))
                        if isinstance(outcome.result, dict)
                        and outcome.result.get("reason")
                        else "blocked"
                    )
                    reconciled_ep = update_episode_outcome(
                        conn,
                        self._company_id,
                        run.run_id,
                        outcome_status="blocked",
                        reason=block_reason,
                        only_from_statuses=frozenset({"completed"}),
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
            if reconciled_ep is not None:
                upsert_episode_entry(self._store, reconciled_ep)
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
            has_more_events=cursor_batch.has_more,
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
            self._sweep_work_state(writer, now_ns=timestamp)
            result = execute(writer)
            with closing(open_writer(self._store, lease=writer)) as conn:
                wal_checkpoint(conn, "TRUNCATE" if truncate_wal_on_exit else "PASSIVE")
            return result

    def _claim_next(
        self,
        writer: WriterLease,
        now_ns: int,
        blocked: list[str],
        *,
        target_work_item_id: str | None = None,
    ) -> tuple[WorkItem, Any, DutyDescriptor] | None:
        with closing(open_writer(self._store, lease=writer)) as conn, write_tx(conn):
            target_filter = (
                " AND work_item_id = ?" if target_work_item_id is not None else ""
            )
            params: tuple[Any, ...] = (
                (self._company_id, now_ns, target_work_item_id)
                if target_work_item_id is not None
                else (self._company_id, now_ns)
            )
            rows = conn.execute(
                f"""SELECT work_item_id, company_id, owner_agent_id, duty_kind, state,
                          source_kind, source_id, parent_work_item_id, dedupe_key,
                          payload_json, created_at_ns, updated_at_ns,
                          attempt_count, next_retry_at_ns
                   FROM work_items
                   WHERE company_id = ? AND state = 'due'
                     AND (next_retry_at_ns IS NULL OR next_retry_at_ns <= ?)
                     {target_filter}
                     AND owner_agent_id NOT IN (
                         SELECT agent_id FROM work_runs WHERE status = 'running'
                     )
                     AND owner_agent_id NOT IN (
                         SELECT agent_id FROM duty WHERE state = 'running'
                     )
                   ORDER BY
                       CASE
                           WHEN duty_kind IN ('ceo.respond_message', 'board.apply_decision') THEN 0
                           WHEN duty_kind = 'work.peer_response' THEN 1
                           WHEN parent_work_item_id IS NOT NULL
                                AND duty_kind NOT IN ('work.discovery_review', 'work.dreaming') THEN 2
                           WHEN source_kind = 'condition'
                                AND json_valid(payload_json)
                                AND json_extract(payload_json, '$.source_identity') LIKE 'result_dep:%' THEN 2
                           WHEN duty_kind NOT IN ('work.discovery_review', 'work.dreaming') THEN 2
                           WHEN duty_kind = 'work.discovery_review' THEN 3
                           WHEN duty_kind = 'work.dreaming' THEN 4
                           ELSE 2
                       END ASC,
                       created_at_ns ASC,
                       work_item_id ASC""",
                params,
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
