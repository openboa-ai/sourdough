"""Company-facing paper execution contracts, independent of venue storage."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .paper_account import AccountObserver


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
class VenueExecution:
    order_id: str
    account_id: str
    instrument_rules_digest: str
    quantity: int
    limit_minor_units: int
    fill_id: str | None
    fill_price_minor_units: int | None
    fee_minor_units: int = 0


class PaperDestination(AccountObserver, Protocol):
    def submit(self, order_id: str, account_id: str, intent: PaperIntent) -> None: ...


class PaperExecution(PaperDestination, Protocol):
    def require_independent_from(self, company_store: Path) -> None: ...

    def observe_quote(self, account_id: str) -> int: ...

    def observe_order_sizing(self, account_id: str) -> PaperOrderSizing | None: ...

    def observe_fee_bps(self, account_id: str) -> int: ...

    def observe_execution(self, order_id: str) -> VenueExecution | None: ...

    def sync_account_execution(
        self, account_id: str, order_id: str
    ) -> VenueExecution | None: ...


def require_separate_journal(company_store: Path, venue_journal: Path) -> None:
    if company_store.resolve() == venue_journal.resolve() or (
        company_store.exists()
        and venue_journal.exists()
        and company_store.samefile(venue_journal)
    ):
        raise VenueRefused("Company store and venue journal must be separate")
