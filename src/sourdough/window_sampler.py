"""Record prospective marks during a bounded managed company activation."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from time import time_ns

from .nautilus_paper import CONNECTOR, NautilusPaperVenue
from .nautilus_runtime import (
    PAPER_ACTIVATION_LIMIT_SECONDS,
    VALUATION_SOURCE_REVISION,
    paper_venue,
)
from .paper_account import reopen_binding
from .paper_execution import PaperExecution, VenueRefused
from .paper_valuation import (
    BEST_BID_POLICY,
    ValuationRefused,
    capture_window_sample,
    earliest_sample_ns,
)
from .paper_window import (
    RegisteredWindow,
    WindowPlan,
    WindowRefused,
    read_window,
    register_window,
)
from .writer_lease import WriterLease


_MAX_SEGMENT_NS = PAPER_ACTIVATION_LIMIT_SECONDS * 10**9


def _verify_scope(
    writer: WriterLease, venue: NautilusPaperVenue, plan: WindowPlan
) -> None:
    writer.verify()
    binding = reopen_binding(writer.store, writer.company_id)
    _, configured = venue.configuration()
    if (
        binding.revision != plan.binding_revision
        or plan.source_revision != VALUATION_SOURCE_REVISION
        or plan.valuation_revision != BEST_BID_POLICY
        or binding.account.connector_revision != CONNECTOR
        or configured.company_id != writer.company_id
        or venue.observe_account(binding.account.account_id) != binding.account
    ):
        raise WindowRefused("Window requires the original managed paper account")


def register_managed_window(
    writer: WriterLease, directory: Path, plan: WindowPlan
) -> RegisteredWindow:
    writer.verify()
    previous = read_window(writer.store, writer.company_id, plan.window_id)
    if previous is not None:
        if previous.plan != plan:
            raise WindowRefused(
                "Window request conflicts with its original registration"
            )
        return previous
    _verify_scope(writer, paper_venue(writer.store, directory), plan)
    return register_window(writer, plan)


@dataclass(frozen=True)
class WindowSampler:
    directory: Path
    window_id: str
    observe_through_ns: int | None = None

    @contextmanager
    def activate(self, writer: WriterLease, venue: PaperExecution):
        window = read_window(writer.store, writer.company_id, self.window_id)
        if window is None:
            raise WindowRefused("Sampling requires an existing sealed window")
        cutoff = self.observe_through_ns
        if cutoff is not None and (
            type(cutoff) is not int
            or window.plan.agent_turn_limit is None
            or not window.plan.opens_ns <= cutoff <= window.plan.closes_ns
            or not 0 < cutoff - time_ns() <= _MAX_SEGMENT_NS
        ):
            raise WindowRefused(
                "Observation cutoff is outside the bounded window segment"
            )
        expected = paper_venue(writer.store, self.directory)
        if (
            not isinstance(venue, NautilusPaperVenue)
            or venue.journal != expected.journal
        ):
            raise WindowRefused("Sampling cannot use a separate execution destination")
        _verify_scope(writer, venue, window.plan)
        stop = Event()
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="paper-samples"
        ) as pool:
            sampler = pool.submit(self._collect, writer, window, stop)
            body_failed = False
            try:
                yield window
                if cutoff is not None:
                    sampler.result()
            except BaseException:
                body_failed = True
                raise
            finally:
                stop.set()
                try:
                    sampler.result()
                except Exception:
                    if not body_failed:
                        raise

    def _collect(
        self, writer: WriterLease, window: RegisteredWindow, stop: Event
    ) -> None:
        plan = window.plan
        cutoff = (
            self.observe_through_ns
            if self.observe_through_ns is not None
            else plan.closes_ns
        )
        captured = None
        while not stop.is_set():
            now = time_ns()
            if now > cutoff:
                return
            delay_seconds = 0.2
            if now < plan.opens_ns:
                delay_seconds = min(1, (plan.opens_ns - now) / 10**9)
            else:
                slot = (now - plan.opens_ns) // plan.cadence_ns
                eligible = earliest_sample_ns(window, slot)
                if now < eligible:
                    stop.wait(min(1, (min(eligible, cutoff + 1) - now) / 10**9))
                    continue
                if slot != captured:
                    try:
                        capture_window_sample(
                            writer,
                            self.directory,
                            self.window_id,
                            slot,
                            through_ns=cutoff,
                        )
                    except (VenueRefused, ValuationRefused, sqlite3.OperationalError):
                        pass
                    else:
                        captured = slot
                if slot == captured:
                    next_boundary = min(
                        plan.opens_ns + (slot + 1) * plan.cadence_ns, cutoff + 1
                    )
                    delay_seconds = min(1, (next_boundary - now) / 10**9)
            stop.wait(min(delay_seconds, (cutoff + 1 - now) / 10**9))
