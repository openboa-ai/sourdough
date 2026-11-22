"""Read a source-backed first-period statement for managed cash-spot paper."""

import json
import sqlite3
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from time import time_ns

from .fund_returns import ReturnMeasurement, ReturnScope, measure
from .nautilus_paper import (
    CONNECTOR,
    SandboxBalance,
    SandboxFillFence,
    current_quote_times,
)
from .nautilus_runtime import paper_venue
from .paper_execution import VenueRefused
from .paper_valuation import ValuationRefused, read_window_samples
from .paper_window import WindowRefused, read_window
from .paper_window_movement import read_paper_movement
from .window_costs import WindowCosts, read_window_costs


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
            ratio = report[name]["time_weighted_return"]
            if ratio is not None:
                report[name]["time_weighted_return"] = {
                    "numerator": ratio.numerator,
                    "denominator": ratio.denominator,
                }
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
        return report | {"revision": sha256(canonical.encode()).hexdigest()}


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

    common = (company_id, window.account.destination_id, window.account.currency)
    policy = plan.valuation_revision
    trading = measure(
        ReturnScope(*common, "trading_net", policy),
        plan.opens_ns,
        plan.closes_ns,
        opening,
        closing,
        (),
        sources=sources,
        missing=tuple(missing),
    )
    costs = (
        WindowCosts(window.revision, None, ("accounting-close",))
        if close_id is None
        else read_window_costs(store, company_id, window_id, close_id)
    )
    cost_atoms = (
        costs.amount_in_units(window.account.currency, scale)
        if scale is not None
        else None
    )
    company_missing = (*missing, *costs.missing)
    if scale is not None and not costs.missing and cost_atoms is None:
        company_missing += ("accounting-units",)
    if costs.accounting is not None:
        receipt = costs.accounting.receipt
        sources += (receipt.request_id, receipt.statement_revision)
    company = measure(
        ReturnScope(*common, "company_net", policy),
        plan.opens_ns,
        plan.closes_ns,
        opening,
        closing - cost_atoms
        if closing is not None and cost_atoms is not None
        else None,
        (),
        sources=sources,
        missing=company_missing,
    )
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
