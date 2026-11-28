"""TradingDomainPlugin Seam #9 connector wrapping ExternalVenueAdapter via Core ConnectorProxy."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from ..connectors.protocol import (
    ConnectorEnvironmentMismatch,
    ConnectorIndeterminate,
    ConnectorManifest,
    ConnectorRefused,
    ConnectorRequest,
    ConnectorResponse,
    OperationSpec,
)
from ..secret_vault import MaskedSecret, MaskedSecretBundle
from .external_venue import (
    ExternalEnvironmentMismatch,
    ExternalOrderReplyUnknown,
    ExternalOrderRequest,
    ExternalVenueAdapter,
    ExternalVenueRefused,
)

CONNECTOR_KIND_TRADING_VENUE = "trading_venue"

TRADING_SUBMIT_ORDER_OP = OperationSpec(
    name="submit_order",
    mode="effectful",
    request_schema={
        "type": "object",
        "properties": {
            "action_id": {"type": "string"},
            "symbol": {"type": "string"},
            "side": {"type": "string", "enum": ["BUY", "SELL"]},
            "quantity": {"type": "string"},
            "limit_price": {"type": "string"},
            "time_in_force": {"type": "string", "enum": ["GTC", "IOC"]},
        },
        "required": ["action_id", "symbol", "side", "quantity", "limit_price"],
    },
    supports_lookup=True,
    idempotent=False,
)

TRADING_CANCEL_ORDER_OP = OperationSpec(
    name="cancel_order",
    mode="effectful",
    request_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "client_order_id": {"type": "string"},
        },
        "required": ["symbol", "client_order_id"],
    },
    supports_lookup=True,
    idempotent=False,
)

TRADING_QUERY_ORDER_OP = OperationSpec(
    name="query_order",
    mode="read",
    request_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "client_order_id": {"type": "string"},
        },
        "required": ["symbol", "client_order_id"],
    },
    supports_lookup=True,
    idempotent=True,
)

TRADING_ACCOUNT_SNAPSHOT_OP = OperationSpec(
    name="account_snapshot",
    mode="read",
    request_schema={"type": "object", "properties": {}, "required": []},
    supports_lookup=False,
    idempotent=True,
)


def _to_decimal(raw: Any, field: str) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError) as error:
        raise ConnectorRefused(f"Invalid decimal value for '{field}'") from error


class TradingVenueConnectorAdapter:
    """Bridge ExternalVenueAdapter into Sourdough's shared ConnectorProxy boundary."""

    def __init__(
        self,
        venue_resolver: Callable[[ConnectorRequest], ExternalVenueAdapter]
        | None = None,
    ) -> None:
        self._venue_resolver = venue_resolver
        self._manifest = ConnectorManifest(
            connector_kind=CONNECTOR_KIND_TRADING_VENUE,
            adapter_revision="trading-venue-connector-v1",
            operations=(
                TRADING_SUBMIT_ORDER_OP,
                TRADING_CANCEL_ORDER_OP,
                TRADING_QUERY_ORDER_OP,
                TRADING_ACCOUNT_SNAPSHOT_OP,
            ),
            supports_lookup=True,
            seals_absent=False,
            requires_credential=True,
            credential_fields=("api_key", "api_secret"),
        )

    @property
    def manifest(self) -> ConnectorManifest:
        return self._manifest

    def _resolve_venue(self, request: ConnectorRequest) -> ExternalVenueAdapter:
        if self._venue_resolver is None:
            raise ConnectorRefused(
                "TradingVenueConnectorAdapter requires a configured venue_resolver"
            )
        venue = self._venue_resolver(request)
        if venue.capabilities.environment != request.environment:
            raise ConnectorEnvironmentMismatch(
                f"Resolved venue environment '{venue.capabilities.environment}' "
                f"does not match binding environment '{request.environment}'"
            )
        return venue

    def execute(
        self,
        request: ConnectorRequest,
        *,
        secret: MaskedSecret | MaskedSecretBundle | None,
        timeout_seconds: float,
    ) -> ConnectorResponse:
        del timeout_seconds
        if not isinstance(secret, MaskedSecretBundle):
            raise ConnectorRefused(
                "trading_venue connector requires a resolved api_key/api_secret MaskedSecretBundle"
            )
        venue = self._resolve_venue(request)
        try:
            if request.operation == "submit_order":
                order_req = ExternalOrderRequest(
                    action_id=str(
                        request.payload.get("action_id") or request.request_id
                    ),
                    symbol=str(request.payload.get("symbol") or ""),
                    side=str(request.payload.get("side") or ""),
                    quantity=_to_decimal(request.payload.get("quantity"), "quantity"),
                    limit_price=_to_decimal(
                        request.payload.get("limit_price"), "limit_price"
                    ),
                    time_in_force=str(request.payload.get("time_in_force") or "GTC"),
                )
                status = venue.submit_order(order_req)
                return ConnectorResponse(
                    status="rejected" if status.status == "rejected" else "observed",
                    payload={
                        "venue_order_id": status.venue_order_id,
                        "client_order_id": status.client_order_id,
                        "symbol": status.symbol,
                        "order_status": status.status,
                        "filled_quantity": str(status.filled_quantity),
                        "remaining_quantity": str(status.remaining_quantity),
                    },
                    source_ref=status.venue_order_id or status.client_order_id,
                    indeterminate=False,
                )

            if request.operation == "cancel_order":
                symbol = str(request.payload.get("symbol") or "")
                coid = str(request.payload.get("client_order_id") or "")
                status = venue.cancel_order(symbol=symbol, client_order_id=coid)
                return ConnectorResponse(
                    status="observed",
                    payload={
                        "venue_order_id": status.venue_order_id,
                        "client_order_id": status.client_order_id,
                        "symbol": status.symbol,
                        "order_status": status.status,
                    },
                    source_ref=status.venue_order_id or status.client_order_id,
                    indeterminate=False,
                )

            if request.operation == "query_order":
                return self.lookup(request, secret=secret, timeout_seconds=15.0)

            if request.operation == "account_snapshot":
                snap = venue.fetch_account_snapshot()
                return ConnectorResponse(
                    status="observed",
                    payload={
                        "account_id": snap.account_id,
                        "environment": snap.environment,
                        "market_kind": snap.market_kind,
                        "destination_id": snap.destination_id,
                        "balances": {k: str(v) for k, v in snap.balances.items()},
                        "positions_count": len(snap.positions),
                        "observed_at_ms": snap.observed_at_ms,
                    },
                    source_ref=snap.destination_id,
                    indeterminate=False,
                )
        except ExternalOrderReplyUnknown as error:
            raise ConnectorIndeterminate(str(error)) from error
        except ExternalEnvironmentMismatch as error:
            raise ConnectorEnvironmentMismatch(str(error)) from error
        except ExternalVenueRefused as error:
            raise ConnectorRefused(str(error)) from error

        raise ConnectorRefused(
            f"Unsupported trading_venue operation: {request.operation}"
        )

    def lookup(
        self,
        request: ConnectorRequest,
        *,
        secret: MaskedSecret | MaskedSecretBundle | None,
        timeout_seconds: float,
    ) -> ConnectorResponse:
        del timeout_seconds
        if not isinstance(secret, MaskedSecretBundle):
            raise ConnectorRefused(
                "trading_venue lookup requires a resolved MaskedSecretBundle"
            )
        venue = self._resolve_venue(request)
        symbol = str(request.payload.get("symbol") or "")
        coid = str(
            request.payload.get("client_order_id")
            or request.payload.get("action_id")
            or request.request_id
        )
        try:
            status = venue.query_order(symbol=symbol, client_order_id=coid)
        except ExternalOrderReplyUnknown as error:
            raise ConnectorIndeterminate(str(error)) from error
        except ExternalEnvironmentMismatch as error:
            raise ConnectorEnvironmentMismatch(str(error)) from error
        except ExternalVenueRefused as error:
            raise ConnectorRefused(str(error)) from error

        if status.status == "absent_unsealed" and not status.seals_absence:
            return ConnectorResponse(
                status="unknown",
                payload={
                    "client_order_id": status.client_order_id,
                    "symbol": status.symbol,
                    "order_status": status.status,
                    "seals_absence": False,
                },
                source_ref=status.client_order_id,
                indeterminate=True,
            )
        return ConnectorResponse(
            status="rejected" if status.status == "rejected" else "observed",
            payload={
                "venue_order_id": status.venue_order_id,
                "client_order_id": status.client_order_id,
                "symbol": status.symbol,
                "order_status": status.status,
                "filled_quantity": str(status.filled_quantity),
                "remaining_quantity": str(status.remaining_quantity),
            },
            source_ref=status.venue_order_id or status.client_order_id,
            indeterminate=False,
        )


__all__ = [
    "CONNECTOR_KIND_TRADING_VENUE",
    "TRADING_ACCOUNT_SNAPSHOT_OP",
    "TRADING_CANCEL_ORDER_OP",
    "TRADING_QUERY_ORDER_OP",
    "TRADING_SUBMIT_ORDER_OP",
    "TradingVenueConnectorAdapter",
]
