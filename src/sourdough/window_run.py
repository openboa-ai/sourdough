"""Sequence the existing company and managed paper venue supervisors."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from time import sleep, time_ns

from .nautilus_runtime import (
    PAPER_ACTIVATION_LIMIT_SECONDS,
    PAPER_READY_SECONDS,
    paper_venue,
    supervise_paper,
)
from .paper_account import reopen_binding
from .paper_valuation import read_window_samples
from .paper_window import RegisteredWindow, WindowRefused, read_window
from .supervisor import SupervisionResult


_SECOND = 10**9
_DRAIN_MARGIN_SECONDS = 5
_WORK_RUNWAY_SECONDS = 150


@dataclass(frozen=True)
class ObservedSegment:
    venue_pid: int
    observe_through_ns: int
    company: SupervisionResult
    venue: SupervisionResult


@dataclass(frozen=True)
class WindowRun:
    window_revision: str
    segments: tuple[ObservedSegment, ...]
    missing_ranges: tuple[tuple[int, int], ...]
    completed: bool


def _file_key(path: Path) -> tuple[int, int]:
    info = path.stat(follow_symlinks=False)
    if path.is_symlink() or not path.is_file():
        raise WindowRefused(
            "Window requires the original regular store and venue files"
        )
    return info.st_dev, info.st_ino


def _check_window(store: Path, company_id: str, window: RegisteredWindow) -> None:
    binding = reopen_binding(store, company_id)
    if (
        read_window(store, company_id, window.plan.window_id) != window
        or binding.revision != window.plan.binding_revision
        or binding.account != window.account
        or binding.exposure_limit_minor_units != window.plan.max_exposure_minor
    ):
        raise WindowRefused(
            "Window does not match the original paper binding and ceiling"
        )


def run_window(
    store: Path,
    company_id: str,
    directory: Path,
    window_id: str,
    run_company: Callable[[int, int, int], SupervisionResult],
    *,
    segment_seconds: int,
    emit: Callable[[dict], None],
) -> WindowRun:
    window = read_window(store, company_id, window_id)
    if window is None or window.plan.agent_turn_limit is None:
        raise WindowRefused("Automatic operation requires a sealed Agent turn ceiling")
    if (
        type(segment_seconds) is not int
        or not 8 <= segment_seconds <= PAPER_ACTIVATION_LIMIT_SECONDS
    ):
        raise WindowRefused("Venue segment must be bounded to 8 to 600 seconds")
    plan = window.plan
    now = time_ns()
    if not plan.opens_ns - 15 * _SECOND <= now < plan.closes_ns:
        raise WindowRefused(
            "Start the runner within 15 seconds of opening or during the window"
        )
    journal = paper_venue(store, directory).journal
    identity = _file_key(store), _file_key(journal)
    segments = []
    complete = False

    def venue_activation(
        seconds: int, ready: SimpleQueue[tuple[int, int] | None]
    ) -> SupervisionResult:
        def observe(event: dict) -> None:
            if event["event"] == "worker_ready":
                ready.put((event["pid"], event["time_ns"]))
            emit(event | {"actor": "venue"})

        try:
            return supervise_paper(
                store,
                company_id,
                directory,
                seconds=seconds,
                emit=observe,
                max_restarts=0,
            )
        finally:
            ready.put(None)

    sleep(
        max(0, (plan.opens_ns - now - min(15, segment_seconds - 7) * _SECOND) / _SECOND)
    )
    while time_ns() < plan.closes_ns:
        _check_window(store, company_id, window)
        if (_file_key(store), _file_key(journal)) != identity:
            raise WindowRefused("Company or venue file changed between segments")
        horizon = (plan.closes_ns - time_ns() + _SECOND - 1) // _SECOND
        final_duration = horizon + _DRAIN_MARGIN_SECONDS + 1
        duration = (
            max(8, final_duration)
            if final_duration <= segment_seconds
            else min(segment_seconds, max(8, horizon - PAPER_READY_SECONDS))
        )
        ready = SimpleQueue()
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="paper-venue"
        ) as pool:
            venue = pool.submit(venue_activation, duration, ready)
            try:
                readiness = ready.get(timeout=PAPER_READY_SECONDS + 8)
            except Empty:
                readiness = None
            if readiness is None:
                venue.result()
                break
            pid, ready_ns = readiness
            through = min(
                plan.closes_ns, ready_ns + (duration - _DRAIN_MARGIN_SECONDS) * _SECOND
            )
            sleep(max(0, (plan.opens_ns - time_ns()) / _SECOND))
            remaining = through - time_ns()
            if remaining <= 0 or venue.done():
                venue.result()
                break
            _check_window(store, company_id, window)
            if (_file_key(store), _file_key(journal)) != identity:
                raise WindowRefused(
                    "Company or venue file changed before employee work"
                )
            max_seconds = (remaining + _SECOND - 1) // _SECOND + 3
            company = run_company(
                through, through - _WORK_RUNWAY_SECONDS * _SECOND, max_seconds
            )
            segment = ObservedSegment(pid, through, company, venue.result())
        segments.append(segment)
        if not company.completed or not segment.venue.completed or time_ns() <= through:
            break
        if through == plan.closes_ns:
            complete = True
            break

    coverage = read_window_samples(store, company_id, window_id)
    return WindowRun(
        window.revision, tuple(segments), coverage.missing_ranges, complete
    )
