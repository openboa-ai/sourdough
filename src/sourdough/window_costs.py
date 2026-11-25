"""Join a prospective paper window to its separately reconciled company costs."""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path

from .accounting_close import (
    CloseRequest,
    ClosedPeriod,
    ExpectedCost,
    read_period_close,
)
from .general_ledger import _valid_scale
from .paper_fee_bridge import PAPER_FILL_FEE_SOURCE
from .paper_window import WindowRefused, read_window


_DAY_NS = 86_400 * 10**9
_EPOCH = date(1970, 1, 1)


@dataclass(frozen=True)
class CostInventory:
    window_id: str
    company_id: str
    currency: str
    from_date: date
    through_date: date
    cash_account: str
    mapping_revision: str
    expense_accounts: tuple[str, ...]
    expected_costs: tuple[ExpectedCost, ...]
    units_per_whole: int | None = None

    @classmethod
    def for_close(cls, window_id: str, request: CloseRequest) -> "CostInventory":
        return cls(
            window_id,
            request.company_id,
            request.currency,
            request.from_date,
            request.through_date,
            request.cash_account,
            request.mapping_revision,
            request.expense_accounts,
            request.expected_costs,
            request.units_per_whole,
        )

    @property
    def revision(self) -> str:
        scope = (
            self.window_id,
            self.company_id,
            self.currency,
            self.from_date.isoformat(),
            self.through_date.isoformat(),
            self.cash_account,
            self.mapping_revision,
            sorted(self.expense_accounts),
            sorted((*cost.key, cost.owner) for cost in self.expected_costs),
        )
        terms = (
            ("operating-cost-inventory-v1", *scope)
            if self.units_per_whole is None
            else ("operating-cost-inventory-v2", *scope, self.units_per_whole)
        )
        payload = json.dumps(terms, separators=(",", ":"))
        return sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class WindowCosts:
    window_revision: str
    accounting: ClosedPeriod | None
    missing: tuple[str, ...] = ()

    @property
    def amount_minor(self) -> int | None:
        if self.missing or self.accounting is None:
            return None
        return self.accounting.receipt.verified_cost_minor

    def amount_in_units(self, currency: str, units_per_whole: int) -> int | None:
        amount = self.amount_minor
        if (
            amount is None
            or self.accounting is None
            or units_per_whole is None
            or not _valid_scale(units_per_whole)
            or currency != self.accounting.request.currency
        ):
            return None
        scale = self.accounting.request.units_per_whole
        if scale is None:
            return None
        converted, remainder = divmod(amount * units_per_whole, scale)
        return None if remainder else converted


def read_window_costs(
    store: Path, company_id: str, window_id: str, close_id: str
) -> WindowCosts:
    window = read_window(store, company_id, window_id)
    if window is None:
        raise WindowRefused("Operating costs require a registered window")
    closed = read_period_close(store, company_id, close_id)
    if closed is None:
        return WindowCosts(window.revision, None, ("accounting-close",))
    request = closed.request
    plan = window.plan
    try:
        opens_date = _EPOCH + timedelta(days=plan.opens_ns // _DAY_NS)
        closes_date = _EPOCH + timedelta(days=plan.closes_ns // _DAY_NS)
    except OverflowError:
        return WindowCosts(window.revision, None, ("accounting-cutoff",))
    if (
        plan.opens_ns % _DAY_NS
        or (plan.closes_ns + 1) % _DAY_NS
        or request.from_date != opens_date
        or request.through_date != closes_date
    ):
        return WindowCosts(window.revision, None, ("accounting-cutoff",))
    if type(closed.assessed_ns) is not int or closed.assessed_ns <= plan.closes_ns:
        return WindowCosts(window.revision, None, ("accounting-as-of",))
    if request.currency != window.account.currency:
        return WindowCosts(window.revision, None, ("accounting-currency",))
    if (
        not request.expected_costs
        or any(cost.source == PAPER_FILL_FEE_SOURCE for cost in request.expected_costs)
        or CostInventory.for_close(window_id, request).revision
        != plan.cost_inventory_revision
    ):
        return WindowCosts(window.revision, None, ("operating-cost-inventory",))
    return WindowCosts(
        window.revision,
        closed,
        tuple(dict.fromkeys(issue.reason for issue in closed.receipt.issues)),
    )
