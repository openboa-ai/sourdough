"""Company-facing paper execution contracts, independent of venue storage."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .paper_account import AccountObserver, PaperAccount


class VenueRefused(Exception):
    """The paper destination cannot accept the command."""


@dataclass(frozen=True)
class PaperIntent:
    action_id: str
    instrument_rules_digest: str
    quantity: int
    limit_minor_units: int


@dataclass(frozen=True)
class PaperOrderSizing:
    instrument: str
    base_units_per_lot: str
    quote_currency: str
    quote_units_per_whole: int
    price_step_per_lot: int
    minimum_lots: int
    minimum_notional: int


@dataclass(frozen=True)
class ReplayAvailability:
    tape_revision: str
    event_ns: int
    available_ns: int


@dataclass(frozen=True)
class VenueExecution:
    order_id: str
    account_id: str
    instrument_rules_digest: str
    quantity: int
    limit_minor_units: int
    fill_id: str | None
    fill_price_minor_units: int | None
    fee_minor_units: int = 0
    filled_quantity: int | None = None


@dataclass(frozen=True)
class VenueCancellation:
    order_id: str
    account_id: str
    instrument_rules_digest: str
    event_id: str
    filled_quantity: int


@dataclass(frozen=True)
class VenuePosition:
    order_id: str
    account_id: str
    instrument_rules_digest: str
    position_quantity: int


class PaperDestination(AccountObserver, Protocol):
    def check_order_terms(
        self, account_id: str, intent: PaperIntent
    ) -> PaperAccount: ...

    def submit(self, order_id: str, account_id: str, intent: PaperIntent) -> None: ...


class PaperExecution(PaperDestination, Protocol):
    def require_independent_from(self, company_store: Path) -> None: ...

    def observe_quote(self, account_id: str) -> int: ...

    def observe_research_quote(
        self, account_id: str
    ) -> tuple[int, ReplayAvailability | None]: ...

    def observe_order_sizing(self, account_id: str) -> PaperOrderSizing | None: ...

    def observe_fee_bps(self, account_id: str) -> int: ...

    def read_fills(
        self, account_id: str, order_id: str, *, account_sync: bool = False
    ) -> tuple[VenueExecution, ...]: ...

    def read_cancellation(
        self, account_id: str, order_id: str, *, account_sync: bool = False
    ) -> VenueCancellation | None: ...

    def read_order_position(
        self, account_id: str, order_id: str
    ) -> VenuePosition | None: ...


def observe_bound_position(
    venue: PaperExecution, account: PaperAccount, order_id: str
) -> VenuePosition | None:
    try:
        if venue.observe_account(account.account_id) != account:
            raise VenueRefused("Position lookup requires the bound destination")
        result = venue.read_order_position(account.account_id, order_id)
    except TimeoutError:
        return None
    if result is not None and (
        (result.order_id, result.account_id, result.instrument_rules_digest)
        != (order_id, account.account_id, account.instrument_rules_digest)
        or type(result.position_quantity) is not int
        or not 0 <= result.position_quantity <= 2**63 - 1
    ):
        raise VenueRefused("Position evidence does not match the original order")
    return result


def require_separate_journal(company_store: Path, venue_journal: Path) -> None:
    if company_store.resolve() == venue_journal.resolve() or (
        company_store.exists()
        and venue_journal.exists()
        and company_store.samefile(venue_journal)
    ):
        raise VenueRefused("Company store and venue journal must be separate")
