"""Company-facing paper execution contracts, independent of venue storage."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, get_args

from ..company import _connect, _identity
from ..storage.schema import is_domain_installed
from .domain import ENV_PAPER, TIER_COMPANY_PAPER


class BindingRefused(Exception):
    """The execution binding could not be resolved."""


@dataclass(frozen=True)
class PaperAccount:
    account_id: str
    connector_revision: str
    instrument_rules_digest: str
    currency: str
    starting_minor_units: int
    environment: str
    destination_id: str = ""


@dataclass(frozen=True)
class PaperMandate:
    revision: str
    company_id: str
    charter_revision: str
    account: PaperAccount
    exposure_limit_minor_units: int


class AccountObserver(Protocol):
    def observe_account(self, account_id: str) -> PaperAccount: ...


@dataclass(frozen=True)
class BindingChange:
    previous_revision: str
    revision: str
    requested_at_ns: int
    effective_at_ns: int | None
    blocked_by: str | None = None


def read_current_binding(connection, company_id: str) -> PaperMandate:
    """Load the active company_paper execution_environment_binding inside an open connection."""
    if _identity(connection) != company_id:
        raise BindingRefused("Company identity mismatch")
    if not is_domain_installed(connection, "trading"):
        raise BindingRefused("Trading domain is not installed")
    row = connection.execute(
        """SELECT b.binding_revision, b.company_id, c.charter_revision,
                  b.exposure_limit_minor_units, b.account_id,
                  b.connector_revision, b.instrument_rules_digest,
                  b.currency, b.starting_minor_units, b.environment,
                  b.destination_id
           FROM execution_environment_binding b
           JOIN company c ON c.id = b.company_id
           WHERE b.company_id = ? AND b.tier = ? AND b.environment = ?
             AND b.status = 'active'
           ORDER BY b.updated_at_ns DESC, b.binding_id ASC
           LIMIT 1""",
        (company_id, TIER_COMPANY_PAPER, ENV_PAPER),
    ).fetchone()
    if row is None:
        raise BindingRefused("Active company_paper binding not found")
    return PaperMandate(
        revision=str(row[0]),
        company_id=str(row[1]),
        charter_revision=str(row[2]),
        exposure_limit_minor_units=int(row[3]),
        account=PaperAccount(
            account_id=str(row[4]),
            connector_revision=str(row[5]),
            instrument_rules_digest=str(row[6]),
            currency=str(row[7]),
            starting_minor_units=int(row[8]),
            environment=str(row[9]),
            destination_id=str(row[10]),
        ),
    )


def reopen_binding(store: Path, company_id: str) -> PaperMandate:
    """Load the active company_paper execution_environment_binding as a PaperMandate."""
    with closing(_connect(store, "ro")) as connection, connection:
        return read_current_binding(connection, company_id)


TerminalOutcome = Literal["cancelled", "rejected", "denied", "expired"]
TERMINAL_OUTCOMES = get_args(TerminalOutcome)


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
    outcome: TerminalOutcome = "cancelled"


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
