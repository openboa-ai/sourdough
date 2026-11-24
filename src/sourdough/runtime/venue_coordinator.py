"""Two-process Paper Venue coordination with bounded rotating activation segments."""

from dataclasses import dataclass
from pathlib import Path

from ..paper_execution import PaperExecution, VenueRefused


MIN_SEGMENT_SECONDS = 8
MAX_SEGMENT_SECONDS = 600
DRAIN_MARGIN_SECONDS = 5


@dataclass(frozen=True)
class VenueSegmentPolicy:
    """Enforce bounded rotating activations and drain margins for the separate venue process."""

    segment_seconds: int = 60
    drain_margin_seconds: int = DRAIN_MARGIN_SECONDS

    def __post_init__(self) -> None:
        if (
            type(self.segment_seconds) is not int
            or not MIN_SEGMENT_SECONDS <= self.segment_seconds <= MAX_SEGMENT_SECONDS
        ):
            raise VenueRefused(
                f"Paper venue segment must be between {MIN_SEGMENT_SECONDS} and {MAX_SEGMENT_SECONDS} seconds"
            )
        if (
            type(self.drain_margin_seconds) is not int
            or self.drain_margin_seconds < DRAIN_MARGIN_SECONDS
            or self.drain_margin_seconds >= self.segment_seconds
        ):
            raise VenueRefused(
                "Paper venue drain margin must be at least 5 seconds and strictly below segment length"
            )


@dataclass(frozen=True)
class VenueSegment:
    sequence: int
    active_seconds: int
    drain_margin_seconds: int

    @property
    def work_seconds(self) -> int:
        return self.active_seconds - self.drain_margin_seconds


class BoundedVenueCoordinator:
    """Preserve process and write-authority separation between CompanyRuntime and Paper Venue."""

    def __init__(
        self,
        store: Path,
        venue: PaperExecution,
        *,
        policy: VenueSegmentPolicy | None = None,
    ) -> None:
        venue.require_independent_from(store)
        self._store = store.absolute()
        self._venue = venue
        self._policy = policy or VenueSegmentPolicy()

    @property
    def policy(self) -> VenueSegmentPolicy:
        return self._policy

    @property
    def venue(self) -> PaperExecution:
        return self._venue

    def verify_independence(self) -> None:
        self._venue.require_independent_from(self._store)

    def plan_segments(self, total_seconds: int) -> tuple[VenueSegment, ...]:
        """Partition a bounded operating window into rotating venue activation segments."""
        min_active = max(MIN_SEGMENT_SECONDS, self._policy.drain_margin_seconds + 1)
        cap = self._policy.segment_seconds
        if type(total_seconds) is not int or total_seconds < min_active:
            raise VenueRefused(
                f"Total venue activation window must be at least {min_active} seconds"
            )
        count = (total_seconds + cap - 1) // cap
        if count * min_active > total_seconds:
            raise VenueRefused(
                "Total venue activation window cannot be partitioned within segment bounds"
            )
        segments: list[VenueSegment] = []
        remaining = total_seconds
        for index in range(count):
            remaining_after = count - index - 1
            active = min(cap, remaining - remaining_after * min_active)
            segments.append(
                VenueSegment(
                    sequence=index + 1,
                    active_seconds=active,
                    drain_margin_seconds=self._policy.drain_margin_seconds,
                )
            )
            remaining -= active
        return tuple(segments)
