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
        )

    @property
    def revision(self) -> str:
        payload = json.dumps(
            (
                "operating-cost-inventory-v1",
                self.window_id,
                self.company_id,
                self.currency,
                self.from_date.isoformat(),
                self.through_date.isoformat(),
                self.cash_account,
                self.mapping_revision,
                sorted(self.expense_accounts),
                sorted((*cost.key, cost.owner) for cost in self.expected_costs),
            ),
            separators=(",", ":"),
        )
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
    if (
        plan.opens_ns % _DAY_NS
        or (plan.closes_ns + 1) % _DAY_NS
        or request.from_date != _EPOCH + timedelta(days=plan.opens_ns // _DAY_NS)
        or request.through_date != _EPOCH + timedelta(days=plan.closes_ns // _DAY_NS)
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
