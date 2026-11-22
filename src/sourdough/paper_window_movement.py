"""Reconcile managed spot-paper cash and lots between two sealed cuts."""

import json
from dataclasses import dataclass
from pathlib import Path

from .nautilus_paper import SandboxBalance, SandboxFillFence
from .nautilus_runtime import paper_venue
from .paper_valuation import ValuationRefused, ValuationRequest, read_window_valuation
from .paper_window import read_window


@dataclass(frozen=True)
class PaperMovement:
    window_revision: str
    opening_revision: str
    closing_revision: str
    currency: str
    units_per_whole: int
    cash_change_atoms: int
    lot_change: int
    fee_atoms: int
    fill_ids: tuple[str, ...]


def read_paper_movement(
    store: Path,
    company_id: str,
    engine_directory: Path,
    window_id: str,
    opening: ValuationRequest,
    closing: ValuationRequest,
) -> PaperMovement:
    window = read_window(store, company_id, window_id)
    if window is None:
        raise ValuationRefused("Movement needs a registered window")
    before = read_window_valuation(store, company_id, window_id, opening).valuation
    after = read_window_valuation(store, company_id, window_id, closing).valuation
    left, right = (
        json.loads(valuation.evidence)["observation"] for valuation in (before, after)
    )
    if (
        left["observed_ns"] >= right["observed_ns"]
        or left["quote_units_per_whole"] != right["quote_units_per_whole"]
        or left["base_units_per_lot"] != right["base_units_per_lot"]
    ):
        raise ValuationRefused("Movement needs two ordered cuts in identical units")
    if "fill_fence" not in left or "fill_fence" not in right:
        raise ValuationRefused("Original fill coverage is missing for a cut")
    venue = paper_venue(store, engine_directory)
    fills = venue.read_fills_between(
        window.account,
        SandboxFillFence(**left["fill_fence"]),
        SandboxFillFence(**right["fill_fence"]),
    )
    if any(
        fill.effective_ns is None
        or not left["observed_ns"] < fill.effective_ns <= right["observed_ns"]
        for fill in fills
    ):
        raise ValuationRefused("Original fill times do not cover the cut interval")
    fees = sum(fill.fee_atoms for fill in fills)
    cash_change = -sum(fill.quantity * fill.price_atoms for fill in fills) - fees
    lot_change = sum(fill.quantity for fill in fills)
    original = SandboxBalance(**left["balance"])
    expected = SandboxBalance(
        original.position_lots + lot_change, original.cash_atoms + cash_change
    )
    if expected != SandboxBalance(**right["balance"]):
        raise ValuationRefused("Original fills do not reconcile the account movements")
    return PaperMovement(
        window.revision,
        before.revision,
        after.revision,
        window.account.currency,
        left["quote_units_per_whole"],
        cash_change,
        lot_change,
        fees,
        tuple(fill.fill_id for fill in fills),
    )
