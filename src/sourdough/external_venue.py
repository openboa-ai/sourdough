"""Normalized external venue execution port shared across testnet and live adapters."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class ExternalVenueRefused(Exception):
    """External venue request was refused before dispatch or rejected by the venue."""


class ExternalEnvironmentMismatch(ExternalVenueRefused):
    """Binding environment, endpoint, or credential scope mismatched fail-closed."""


class ExternalOrderReplyUnknown(Exception):
    """Order dispatch reached an indeterminate transport/venue state.

    Callers MUST retain risk reservations and reconcile via query_order(client_order_id)
    rather than assuming rejection or submitting a duplicate order.
    """


@dataclass(frozen=True)
class VenueCapabilities:
    adapter_revision: str
    venue_name: str
    market_kind: str  # "spot" | "usdm_futures"
    environment: str  # "external_test" | "live"
    supports_spot: bool
    supports_usdm_futures: bool
    supports_ioc_limit: bool
    supports_gtc_limit: bool
    seals_absent_orders: bool  # External exchanges generally do NOT seal absent IDs!
    supports_external_reconciliation: bool


@dataclass(frozen=True)
class ExternalOrderRequest:
    action_id: str
    symbol: str
    side: str  # "BUY" | "SELL"
    quantity: Decimal
    limit_price: Decimal
    time_in_force: str = "GTC"  # "GTC" | "IOC"

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ExternalVenueRefused("action_id is required")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ExternalVenueRefused("symbol is required")
        if self.side not in ("BUY", "SELL"):
            raise ExternalVenueRefused("side must be 'BUY' or 'SELL'")
        if not isinstance(self.quantity, Decimal) or self.quantity <= 0:
            raise ExternalVenueRefused("quantity must be a positive Decimal")
        if not isinstance(self.limit_price, Decimal) or self.limit_price <= 0:
            raise ExternalVenueRefused("limit_price must be a positive Decimal")
        if self.time_in_force not in ("GTC", "IOC"):
            raise ExternalVenueRefused("time_in_force must be 'GTC' or 'IOC'")


@dataclass(frozen=True)
class ExternalFillRecord:
    fill_id: str
    venue_order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    commission_asset: str
    transact_time_ms: int


@dataclass(frozen=True)
class ExternalOrderStatus:
    venue_order_id: str | None
    client_order_id: str
    symbol: str
    side: str | None
    status: str  # "open" | "partially_filled" | "filled" | "cancelled" | "expired" | "rejected" | "absent_unsealed"
    original_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    avg_price: Decimal | None
    update_time_ms: int | None
    seals_absence: bool = False


@dataclass(frozen=True)
class ExternalPositionRecord:
    symbol: str
    quantity: Decimal  # signed: >0 long, <0 short
    entry_price: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class ExternalAccountSnapshot:
    account_id: str
    environment: str
    market_kind: str
    destination_id: str
    balances: dict[str, Decimal]
    positions: tuple[ExternalPositionRecord, ...]
    observed_at_ms: int


@dataclass(frozen=True)
class ExternalReconciliationReport:
    account_id: str
    environment: str
    known_order_ids: tuple[str, ...]
    unattributed_fills: tuple[ExternalFillRecord, ...]
    unattributed_positions: tuple[ExternalPositionRecord, ...]
    clean: bool


class ExternalVenueAdapter(Protocol):
    @property
    def capabilities(self) -> VenueCapabilities: ...

    def client_order_id_for(self, action_id: str) -> str: ...

    def observe_account(self) -> ExternalAccountSnapshot: ...

    def submit_order(self, request: ExternalOrderRequest) -> ExternalOrderStatus: ...

    def cancel_order(
        self, symbol: str, client_order_id: str
    ) -> ExternalOrderStatus: ...

    def query_order(self, symbol: str, client_order_id: str) -> ExternalOrderStatus: ...

    def query_fills(
        self, symbol: str, *, client_order_id: str | None = None
    ) -> tuple[ExternalFillRecord, ...]: ...

    def reconcile_external(
        self, symbol: str, known_client_order_ids: frozenset[str]
    ) -> ExternalReconciliationReport: ...
