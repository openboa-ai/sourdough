"""Read a source-backed first-period statement for managed cash-spot paper."""

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path
from time import time_ns

from .execution import VenueRefused
from .fund_returns import ReturnMeasurement, ReturnScope, measure
from .nautilus_paper import (
    CONNECTOR,
    SandboxBalance,
    SandboxFillFence,
    current_quote_times,
)
from .nautilus_runtime import paper_venue
from .valuation import ValuationRefused, read_window_samples
from .window import RegisteredWindow, WindowRefused, read_window
from .window_costs import WindowCosts, read_window_costs
from .window_movement import read_paper_movement


@dataclass(frozen=True)
class PaperStatement:
    window_revision: str
    operating_revision: str
    units_per_whole: int | None
    observed_slots: tuple[int, ...]
    missing_ranges: tuple[tuple[int, int], ...]
    venue_fees_atoms: int | None
    operating_cost_atoms: int | None
    trading: ReturnMeasurement
    company: ReturnMeasurement

    def as_report(self) -> dict:
        report = asdict(self)
        for name in ("trading", "company"):
            _encode_ratio(report[name])
        return _seal_report(report)


def _encode_ratio(report: dict) -> dict:
    ratio = report["time_weighted_return"]
    if ratio is not None:
        report["time_weighted_return"] = {
            "numerator": ratio.numerator,
            "denominator": ratio.denominator,
        }
    return report


def _seal_report(report: dict) -> dict:
    report = {key: value for key, value in report.items() if key != "revision"}
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
    return report | {"revision": sha256(canonical.encode()).hexdigest()}


def _trading_scope(window: RegisteredWindow) -> ReturnScope:
    return ReturnScope(
        window.company_id,
        window.account.destination_id,
        window.account.currency,
        "trading_net",
        window.plan.valuation_revision,
    )


def _company_result(
    store: Path,
    window: RegisteredWindow,
    trading: ReturnMeasurement,
    scale: int | None,
    close_id: str | None,
) -> tuple[ReturnMeasurement, int | None]:
    costs = (
        WindowCosts(window.revision, None, ("accounting-close",))
        if close_id is None
        else read_window_costs(
            store, window.company_id, window.plan.window_id, close_id
        )
    )
    cost_atoms = (
        costs.amount_in_units(window.account.currency, scale)
        if scale is not None
        else None
    )
    missing = (*trading.missing, *costs.missing)
    if scale is not None and not costs.missing and cost_atoms is None:
        missing += ("accounting-units",)
    sources = trading.sources
    if costs.accounting is not None:
        receipt = costs.accounting.receipt
        sources += (receipt.request_id, receipt.statement_revision)
    closing = trading.closing_minor
    company = measure(
        replace(trading.scope, series="company_net"),
        trading.start_ns,
        trading.end_ns,
        trading.opening_minor,
        closing - cost_atoms
        if closing is not None and cost_atoms is not None
        else None,
        (),
        sources=sources,
        missing=missing,
    )
    return company, cost_atoms


def reassess_retained_costs(
    store: Path, window: RegisteredWindow, prior: dict, close_id: str | None
) -> dict | None:
    if prior["window_revision"] != window.revision:
        raise WindowRefused("Predecessor does not belong to the sealed window")
    historical = prior["trading"]
    if historical["missing"] or historical["pnl_minor"] is None:
        return None
    trading = measure(
        _trading_scope(window),
        window.plan.opens_ns,
        window.plan.closes_ns,
        historical["opening_minor"],
        historical["closing_minor"],
        (),
        sources=tuple(historical["sources"]),
    )
    if json.dumps(_encode_ratio(asdict(trading)), sort_keys=True) != json.dumps(
        historical, sort_keys=True
    ):
        raise WindowRefused("Predecessor trading evidence cannot be verified")
    company, cost_atoms = _company_result(
        store, window, trading, prior["units_per_whole"], close_id
    )
    return _seal_report(
        prior
        | {
            "company": _encode_ratio(asdict(company)),
            "operating_cost_atoms": cost_atoms,
        }
    )


def read_paper_statement(
    store: Path,
    company_id: str,
    directory: Path,
    window_id: str,
    *,
    close_id: str | None = None,
) -> PaperStatement:
    window = read_window(store, company_id, window_id)
    if window is None or window.account.connector_revision != CONNECTOR:
        raise WindowRefused("Statement requires a registered managed paper window")
    plan = window.plan
    samples = read_window_samples(store, company_id, window_id)
    sources = (window.revision, *(sample.revision for _, sample in samples.captures))
    missing = []
    if time_ns() <= plan.closes_ns:
        missing.append("window-still-open")
    if samples.missing_ranges or len(samples.captures) < plan.minimum_observations:
        missing.append("valuation-cadence")
    opening = closing = fees = scale = None
    if not missing:
        before, after = samples.captures[0][1], samples.captures[-1][1]
        first, last = (
            json.loads(sample.evidence)["observation"] for sample in (before, after)
        )
        initial = SandboxBalance(**first["balance"])
        if (
            initial != SandboxBalance(0, window.account.starting_minor_units)
            or first.get("fill_fence", {}).get("count") != 0
        ):
            missing.append("opening-cash")
        else:
            opening = initial.cash_atoms
        if not current_quote_times(
            last["quote_event_ns"], last["quote_received_ns"], plan.closes_ns
        ):
            missing.append("closing-mark-stale")
        if not missing:
            try:
                movement = read_paper_movement(
                    store,
                    company_id,
                    directory,
                    window_id,
                    before.request,
                    after.request,
                )
                balance = paper_venue(store, directory).read_fenced_balance(
                    window.account, SandboxFillFence(**last["fill_fence"])
                )
                if balance != SandboxBalance(**last["balance"]):
                    raise VenueRefused("Final mark and settled venue balance differ")
            except (OSError, sqlite3.Error, VenueRefused, ValuationRefused):
                missing.append("venue-close")
            else:
                closing, fees = after.trading_nav_atoms, movement.fee_atoms
                scale = movement.units_per_whole

    trading = measure(
        _trading_scope(window),
        plan.opens_ns,
        plan.closes_ns,
        opening,
        closing,
        (),
        sources=sources,
        missing=tuple(missing),
    )
    company, cost_atoms = _company_result(store, window, trading, scale, close_id)
    return PaperStatement(
        window.revision,
        plan.operating_revision,
        scale,
        tuple(slot for slot, _ in samples.captures),
        samples.missing_ranges,
        fees,
        cost_atoms,
        trading,
        company,
    )
