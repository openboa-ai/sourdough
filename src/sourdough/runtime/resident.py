"""ResidentCompanyService unifying CompanyDaemon (UDS) and CompanyRuntime under one WriterLease."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time_ns

from ..board_control import AuthorizeBoard
from ..ipc.server import CompanyDaemon, DaemonInfo
from .domains import DomainPlugin
from .engine import CompanyRuntime, RuntimeStepSummary
from .handlers import sync_board_triggers
from .registry import DutyRegistry, RuntimeRefused


@dataclass(frozen=True)
class ResidentHealth:
    running: bool
    ticks_completed: int
    runs_executed: int
    triggers_enqueued: int
    last_tick_at_ns: int | None
    last_error: str | None


class ResidentCompanyService:
    """Single-process resident Sourdough company: UDS control plane + autonomous Work scheduler."""

    def __init__(
        self,
        store: Path,
        company_id: str,
        duty_registry: DutyRegistry,
        *,
        socket_dir: Path | None = None,
        socket_path: Path | None = None,
        lease_directory: Path | None = None,
        expected_store_key: tuple[int, int] | None = None,
        authorize_board: AuthorizeBoard | None = None,
        engine_directory: Path | None = None,
        evaluator_store: Path | None = None,
        bootstrap_challenge: str | None = None,
        request_timeout: float = 10.0,
        tick_interval_seconds: float = 1.0,
        max_dispatches_per_tick: int = 8,
        enable_board_trigger_intake: bool = True,
        lease_ttl_ns: int = 300_000_000_000,
        clock: Callable[[], int] | None = None,
        domain_plugins: Sequence[DomainPlugin] = (),
    ) -> None:
        if tick_interval_seconds <= 0:
            raise RuntimeRefused("tick_interval_seconds must be positive")
        self._store = store.absolute()
        self._company_id = company_id
        self._tick_interval_seconds = float(tick_interval_seconds)
        self._max_dispatches_per_tick = int(max_dispatches_per_tick)
        self._enable_board_trigger_intake = bool(enable_board_trigger_intake)
        self._clock = clock or time_ns

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._state_cond = threading.Condition()
        self._started = False
        self._ticks_completed = 0
        self._runs_executed = 0
        self._triggers_enqueued = 0
        self._last_tick_at_ns: int | None = None
        self._last_error: str | None = None
        self._last_summary: RuntimeStepSummary | None = None
        self._thread: threading.Thread | None = None

        self._daemon = CompanyDaemon(
            self._store,
            self._company_id,
            socket_dir=socket_dir,
            socket_path=socket_path,
            lease_directory=lease_directory,
            expected_store_key=expected_store_key,
            authorize_board=authorize_board,
            engine_directory=engine_directory,
            evaluator_store=evaluator_store,
            bootstrap_challenge=bootstrap_challenge,
            request_timeout=request_timeout,
            on_commit=self.trigger_now,
            domain_plugins=domain_plugins,
        )
        self._runtime = CompanyRuntime(
            self._store,
            self._company_id,
            duty_registry,
            lease_ttl_ns=lease_ttl_ns,
            lease_directory=lease_directory,
            expected_store_key=expected_store_key,
            clock=self._clock,
        )

    @property
    def daemon(self) -> CompanyDaemon:
        return self._daemon

    @property
    def socket_path(self) -> Path:
        return self._daemon.socket_path

    @property
    def info(self) -> DaemonInfo:
        return self._daemon.info

    @property
    def health(self) -> ResidentHealth:
        with self._state_cond:
            return ResidentHealth(
                running=self._started,
                ticks_completed=self._ticks_completed,
                runs_executed=self._runs_executed,
                triggers_enqueued=self._triggers_enqueued,
                last_tick_at_ns=self._last_tick_at_ns,
                last_error=self._last_error,
            )

    def trigger_now(self) -> None:
        """Wake the autonomous scheduler immediately."""
        self._wake_event.set()

    def start(self) -> DaemonInfo:
        if self._started:
            return self._daemon.info
        info = self._daemon.start()
        try:
            self._stop_event.clear()
            self._wake_event.set()
            with self._state_cond:
                self._started = True
            self._thread = threading.Thread(
                target=self._scheduler_loop,
                name=f"sourdough-resident-{self._company_id[:8]}",
                daemon=True,
            )
            self._thread.start()
            return info
        except BaseException:
            with self._state_cond:
                self._started = False
            self._daemon.stop()
            raise

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        self._daemon.stop()
        with self._state_cond:
            self._started = False
            self._state_cond.notify_all()

    def __enter__(self) -> ResidentCompanyService:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def wait_for(
        self,
        predicate: Callable[[ResidentHealth], bool],
        *,
        timeout_seconds: float = 10.0,
    ) -> ResidentHealth:
        """Block cleanly until predicate(self.health) is True or timeout elapses."""
        deadline = monotonic() + timeout_seconds
        with self._state_cond:
            while True:
                current = ResidentHealth(
                    running=self._started,
                    ticks_completed=self._ticks_completed,
                    runs_executed=self._runs_executed,
                    triggers_enqueued=self._triggers_enqueued,
                    last_tick_at_ns=self._last_tick_at_ns,
                    last_error=self._last_error,
                )
                if predicate(current):
                    return current
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"ResidentCompanyService wait timed out (health={current})"
                    )
                self._state_cond.wait(timeout=min(remaining, 0.25))

    def _run_one_tick(self) -> RuntimeStepSummary:
        writer = self._daemon.writer_lease
        lock = self._daemon.write_lock
        created = 0
        if self._enable_board_trigger_intake:
            with lock:
                created = sync_board_triggers(
                    writer, self._company_id, now_ns=self._clock()
                )
            if created > 0:
                self._daemon.notify_commit(invoke_hook=False)

        summary = self._runtime.step_with_writer(
            writer,
            max_dispatches=self._max_dispatches_per_tick,
            tx_lock=lock,
            on_commit=lambda: self._daemon.notify_commit(invoke_hook=False),
        )
        tick_ts = self._clock()
        with self._state_cond:
            self._ticks_completed += 1
            self._runs_executed += len(summary.executed_runs)
            self._triggers_enqueued += created
            self._last_tick_at_ns = tick_ts
            self._last_summary = summary
            self._state_cond.notify_all()
        return summary

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            try:
                summary = self._run_one_tick()
            except Exception as error:
                with self._state_cond:
                    self._ticks_completed += 1
                    self._last_tick_at_ns = self._clock()
                    self._last_error = f"{type(error).__name__}: {error}"
                    self._state_cond.notify_all()
                self._wake_event.wait(timeout=self._tick_interval_seconds)
                continue
            if summary.executed_runs and not self._stop_event.is_set():
                # Drain queued follow-up items promptly before sleeping.
                continue
            self._wake_event.wait(timeout=self._tick_interval_seconds)
