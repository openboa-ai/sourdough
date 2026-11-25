"""Calculate exact performance from separately reconciled NAV and flow series."""

from dataclasses import dataclass
from fractions import Fraction


class ReturnRefused(Exception):
    """The source records cannot be interpreted as one coherent valuation series."""


@dataclass(frozen=True)
class ReturnScope:
    company_id: str
    perimeter: str
    currency: str
    series: str
    policy_revision: str

    def __post_init__(self) -> None:
        if self.series not in ("trading_net", "company_net") or not all(
            isinstance(token, str) and bool(token.strip())
            for token in (
                self.company_id,
                self.perimeter,
                self.currency,
                self.policy_revision,
            )
        ):
            raise ReturnRefused("A recognized valuation series and scope are required")


@dataclass(frozen=True)
class ExternalFlow:
    source_id: str
    at_ns: int | None
    sequence: int
    signed_minor: int
    before_minor: int | None
    after_minor: int | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_id, str)
            or not self.source_id.strip()
            or (
                self.at_ns is not None
                and (type(self.at_ns) is not int or self.at_ns < 0)
            )
            or type(self.sequence) is not int
            or self.sequence < 0
            or type(self.signed_minor) is not int
            or not self.signed_minor
            or any(
                value is not None and type(value) is not int
                for value in (self.before_minor, self.after_minor)
            )
        ):
            raise ReturnRefused(
                "External flow must retain its signed units and event identity"
            )
        if (
            self.before_minor is not None
            and self.after_minor is not None
            and (self.after_minor - self.before_minor != self.signed_minor)
        ):
            raise ReturnRefused("Flow and its adjoining NAV cuts do not reconcile")


@dataclass(frozen=True)
class ReturnMeasurement:
    scope: ReturnScope
    start_ns: int
    end_ns: int
    opening_minor: int | None
    closing_minor: int | None
    gross_in_minor: int
    gross_out_minor: int
    net_flow_minor: int
    pnl_minor: int | None
    time_weighted_return: Fraction | None
    sources: tuple[str, ...]
    missing: tuple[str, ...]
    method: str = "exact-time-weighted-v1"


def measure(
    scope: ReturnScope,
    start_ns: int,
    end_ns: int,
    opening_minor: int | None,
    closing_minor: int | None,
    flows: tuple[ExternalFlow, ...],
    *,
    sources: tuple[str, ...],
    missing: tuple[str, ...] = (),
) -> ReturnMeasurement:
    if (
        type(start_ns) is not int
        or type(end_ns) is not int
        or not 0 <= start_ns < end_ns
        or any(
            value is not None and type(value) is not int
            for value in (opening_minor, closing_minor)
        )
        or not sources
        or any(
            not isinstance(item, str) or not item.strip()
            for item in (*sources, *missing)
        )
    ):
        raise ReturnRefused(
            "Return calculation requires a dated and source-backed window"
        )
    if len({flow.source_id for flow in flows}) != len(flows):
        raise ReturnRefused("Duplicate external flow in reconciled valuation inputs")
    known_order = [
        (flow.at_ns, flow.sequence) for flow in flows if flow.at_ns is not None
    ]
    if any(not start_ns <= key[0] <= end_ns for key in known_order) or any(
        left >= right for left, right in zip(known_order, known_order[1:], strict=False)
    ):
        raise ReturnRefused(
            "External flows must be sequenced within the valuation window"
        )
    gross_in = sum(flow.signed_minor for flow in flows if flow.signed_minor > 0)
    gross_out = -sum(flow.signed_minor for flow in flows if flow.signed_minor < 0)
    net_flow = gross_in - gross_out
    gaps = list(missing)
    if opening_minor is None:
        gaps.append("opening-nav")
    if closing_minor is None:
        gaps.append("closing-nav")
    pnl = None if gaps else closing_minor - opening_minor - net_flow
    interval_start = opening_minor
    factor = Fraction(1)
    for flow in flows:
        if flow.at_ns is None:
            gaps.append(f"flow-time:{flow.source_id}")
        if flow.before_minor is None or flow.after_minor is None:
            gaps.append(f"flow-mark:{flow.source_id}")
        if interval_start is not None and interval_start <= 0:
            gaps.append(f"nonpositive-start:{flow.source_id}")
        elif interval_start is not None and flow.before_minor is not None:
            factor *= Fraction(flow.before_minor, interval_start)
        interval_start = flow.after_minor
    if interval_start is not None and interval_start <= 0:
        gaps.append("nonpositive-start:closing-interval")
    twr = None if gaps else factor * Fraction(closing_minor, interval_start) - 1
    return ReturnMeasurement(
        scope,
        start_ns,
        end_ns,
        opening_minor,
        closing_minor,
        gross_in,
        gross_out,
        net_flow,
        pnl,
        twr,
        sources,
        tuple(dict.fromkeys(gaps)),
    )


def link(periods: tuple[ReturnMeasurement, ...]) -> Fraction | None:
    """Geometrically link only adjacent, comparable, fully measured periods."""
    if not periods:
        return None
    factor = Fraction(1)
    previous = None
    for period in periods:
        if previous is not None and (
            previous.end_ns != period.start_ns
            or previous.scope != period.scope
            or previous.method != period.method
            or previous.closing_minor != period.opening_minor
        ):
            return None
        if period.time_weighted_return is None:
            return None
        factor *= 1 + period.time_weighted_return
        previous = period
    return factor - 1
