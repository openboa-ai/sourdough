"""Binance Spot and USD-M Futures execution adapter with hard environment pinning.

Supports `external_test` (Binance Spot Testnet / USD-M Futures Testnet) and `live`
(Binance Spot / USD-M Futures Production) through one shared ExternalVenueAdapter
contract. Paper bindings are unconditionally rejected fail-closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from time import time
from typing import Any

from .credentials import (
    CredentialProvider,
    CredentialRefused,
    assert_handle_matches_environment,
)
from .execution_tiers import ENV_EXTERNAL_TEST, ENV_LIVE, ENV_PAPER
from .external_venue import (
    ExternalAccountSnapshot,
    ExternalEnvironmentMismatch,
    ExternalFillRecord,
    ExternalOrderReplyUnknown,
    ExternalOrderRequest,
    ExternalOrderStatus,
    ExternalPositionRecord,
    ExternalReconciliationReport,
    ExternalVenueRefused,
    VenueCapabilities,
)

MARKET_SPOT = "spot"
MARKET_USDM_FUTURES = "usdm_futures"
VALID_MARKETS = frozenset({MARKET_SPOT, MARKET_USDM_FUTURES})

PINNED_BINANCE_ENDPOINTS: dict[tuple[str, str], str] = {
    (ENV_EXTERNAL_TEST, MARKET_SPOT): "https://testnet.binance.vision",
    (ENV_EXTERNAL_TEST, MARKET_USDM_FUTURES): "https://testnet.binancefuture.com",
    (ENV_LIVE, MARKET_SPOT): "https://api.binance.com",
    (ENV_LIVE, MARKET_USDM_FUTURES): "https://fapi.binance.com",
}

BINANCE_ADAPTER_REVISION = "binance-rest-v1"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def _fmt_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _normalize_binance_status(raw: str) -> str:
    mapping = {
        "NEW": "open",
        "PARTIALLY_FILLED": "partially_filled",
        "FILLED": "filled",
        "CANCELED": "cancelled",
        "PENDING_CANCEL": "open",
        "EXPIRED": "expired",
        "EXPIRED_IN_MATCH": "expired",
        "REJECTED": "rejected",
    }
    return mapping.get(raw.upper(), "open")


def _extract_binance_error(body: bytes) -> tuple[int | None, str]:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "Non-JSON venue error response"
    if not isinstance(decoded, dict):
        return None, "Unexpected venue error payload"
    code = decoded.get("code")
    msg = decoded.get("msg", "Venue rejected request")
    return (int(code) if isinstance(code, int) else None), str(msg)


@dataclass(frozen=True)
class BinanceVenueConfig:
    environment: str  # "external_test" | "live"
    market_kind: str  # "spot" | "usdm_futures"
    account_id: str
    destination_id: str
    credential_handle_ref: str
    base_url_override: str | None = None
    allow_loopback_override: bool = False
    timeout_seconds: float = 10.0
    recv_window_ms: int = 5000

    def __post_init__(self) -> None:
        if self.environment == ENV_PAPER:
            raise ExternalEnvironmentMismatch(
                "Paper bindings cannot be bound to BinanceVenueAdapter"
            )
        if self.environment not in (ENV_EXTERNAL_TEST, ENV_LIVE):
            raise ExternalEnvironmentMismatch(
                f"Unsupported Binance environment: {self.environment}"
            )
        if self.market_kind not in VALID_MARKETS:
            raise ExternalVenueRefused(
                f"Unsupported Binance market_kind: {self.market_kind}"
            )
        if not self.account_id.strip() or not self.destination_id.strip():
            raise ExternalVenueRefused("account_id and destination_id are required")
        try:
            assert_handle_matches_environment(
                self.credential_handle_ref, self.environment
            )
        except CredentialRefused as error:
            raise ExternalEnvironmentMismatch(str(error)) from error
        if self.base_url_override is not None:
            if (
                not self.allow_loopback_override
                or self.environment != ENV_EXTERNAL_TEST
            ):
                raise ExternalEnvironmentMismatch(
                    "base_url_override is allowed only for external_test loopback emulators"
                )
            parsed = urllib.parse.urlparse(self.base_url_override)
            if parsed.scheme not in ("http", "https") or (
                parsed.hostname not in _LOOPBACK_HOSTS
            ):
                raise ExternalEnvironmentMismatch(
                    "base_url_override must target loopback (127.0.0.1 or localhost) only"
                )
        if not 0.5 <= self.timeout_seconds <= 60.0:
            raise ExternalVenueRefused("timeout_seconds must be within [0.5, 60.0]")
        if not 1000 <= self.recv_window_ms <= 60_000:
            raise ExternalVenueRefused("recv_window_ms must be within [1000, 60000]")

    @property
    def base_url(self) -> str:
        if self.base_url_override is not None:
            return self.base_url_override.rstrip("/")
        return PINNED_BINANCE_ENDPOINTS[(self.environment, self.market_kind)]


class BinanceVenueAdapter:
    """Shared-core Binance Spot and USD-M Futures REST adapter."""

    def __init__(
        self,
        config: BinanceVenueConfig,
        credentials: CredentialProvider,
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._capabilities = VenueCapabilities(
            adapter_revision=BINANCE_ADAPTER_REVISION,
            venue_name="binance",
            market_kind=config.market_kind,
            environment=config.environment,
            supports_spot=config.market_kind == MARKET_SPOT,
            supports_usdm_futures=config.market_kind == MARKET_USDM_FUTURES,
            supports_ioc_limit=True,
            supports_gtc_limit=True,
            seals_absent_orders=False,
            supports_external_reconciliation=True,
        )

    @property
    def config(self) -> BinanceVenueConfig:
        return self._config

    @property
    def capabilities(self) -> VenueCapabilities:
        return self._capabilities

    def client_order_id_for(self, action_id: str) -> str:
        if not isinstance(action_id, str) or not action_id.strip():
            raise ExternalVenueRefused("action_id is required")
        digest = hashlib.sha256(
            f"{self._config.account_id}:{action_id}".encode("utf-8")
        ).hexdigest()[:28]
        return f"sd_{digest}"

    def _prefix(self) -> str:
        return "/api/v3" if self._config.market_kind == MARKET_SPOT else "/fapi/v1"

    def _signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        *,
        is_submit: bool = False,
    ) -> Any:
        try:
            creds = self._credentials.resolve(
                self._config.credential_handle_ref,
                expected_environment=self._config.environment,
            )
        except CredentialRefused as error:
            raise ExternalEnvironmentMismatch(str(error)) from error

        query_items: list[tuple[str, str]] = sorted((params or {}).items())
        query_items.append(("recvWindow", str(self._config.recv_window_ms)))
        query_items.append(("timestamp", str(int(time() * 1000))))
        query_string = urllib.parse.urlencode(query_items)
        signature = hmac.new(
            creds.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{self._config.base_url}{path}?{query_string}&signature={signature}"
        req = urllib.request.Request(
            url,
            method=method,
            headers={
                "X-MBX-APIKEY": creds.api_key,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self._config.timeout_seconds
            ) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                body = error.read()
            finally:
                error.close()
            code, msg = _extract_binance_error(body)
            if error.code in (401, 403) or code in (-2014, -2015):
                raise ExternalVenueRefused(
                    "Venue authentication rejected (credentials invalid or unauthorized)"
                ) from error
            if error.code in (429, 418) or error.code >= 500:
                if is_submit:
                    raise ExternalOrderReplyUnknown(
                        f"Venue returned indeterminate HTTP {error.code} during submit"
                    ) from error
                raise ExternalOrderReplyUnknown(
                    f"Venue returned transient HTTP {error.code}"
                ) from error
            raise ExternalVenueRefused(
                f"Venue rejected request (code={code}): {msg}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ExternalOrderReplyUnknown(
                "Venue transport timed out or disconnected; outcome unknown"
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExternalOrderReplyUnknown(
                "Venue returned malformed JSON response; outcome unknown"
            ) from error

    def observe_account(self) -> ExternalAccountSnapshot:
        if self._config.market_kind == MARKET_SPOT:
            data = self._signed_request("GET", "/api/v3/account")
            balances: dict[str, Decimal] = {}
            for entry in data.get("balances", ()):
                asset = str(entry["asset"])
                total = Decimal(str(entry.get("free", "0"))) + Decimal(
                    str(entry.get("locked", "0"))
                )
                if total != 0:
                    balances[asset] = total
            positions: list[ExternalPositionRecord] = []
            for asset, qty in balances.items():
                if asset not in ("USDT", "USD", "USDC", "BUSD") and qty != 0:
                    positions.append(
                        ExternalPositionRecord(
                            symbol=f"{asset}USDT",
                            quantity=qty,
                            entry_price=Decimal("0"),
                            unrealized_pnl=Decimal("0"),
                        )
                    )
            return ExternalAccountSnapshot(
                account_id=self._config.account_id,
                environment=self._config.environment,
                market_kind=self._config.market_kind,
                destination_id=self._config.destination_id,
                balances=balances,
                positions=tuple(positions),
                observed_at_ms=int(data.get("updateTime", int(time() * 1000))),
            )

        # USD-M Futures (/fapi/v2/account)
        data = self._signed_request("GET", "/fapi/v2/account")
        balances = {}
        for entry in data.get("assets", ()):
            asset = str(entry["asset"])
            wallet = Decimal(str(entry.get("walletBalance", "0")))
            if wallet != 0:
                balances[asset] = wallet
        positions = []
        for pos in data.get("positions", ()):
            amt = Decimal(str(pos.get("positionAmt", "0")))
            if amt != 0:
                positions.append(
                    ExternalPositionRecord(
                        symbol=str(pos["symbol"]),
                        quantity=amt,
                        entry_price=Decimal(str(pos.get("entryPrice", "0"))),
                        unrealized_pnl=Decimal(str(pos.get("unrealizedProfit", "0"))),
                    )
                )
        return ExternalAccountSnapshot(
            account_id=self._config.account_id,
            environment=self._config.environment,
            market_kind=self._config.market_kind,
            destination_id=self._config.destination_id,
            balances=balances,
            positions=tuple(positions),
            observed_at_ms=int(data.get("updateTime", int(time() * 1000))),
        )

    def _parse_order_status(self, data: dict[str, Any]) -> ExternalOrderStatus:
        orig_qty = Decimal(str(data.get("origQty", "0")))
        filled_qty = Decimal(str(data.get("executedQty", "0")))
        remaining = max(Decimal("0"), orig_qty - filled_qty)
        avg_price: Decimal | None = None
        if data.get("avgPrice") and Decimal(str(data["avgPrice"])) > 0:
            avg_price = Decimal(str(data["avgPrice"]))
        elif filled_qty > 0 and data.get("cummulativeQuoteQty"):
            cum_quote = Decimal(str(data["cummulativeQuoteQty"]))
            if cum_quote > 0:
                avg_price = cum_quote / filled_qty
        elif data.get("price") and Decimal(str(data["price"])) > 0:
            avg_price = Decimal(str(data["price"]))
        return ExternalOrderStatus(
            venue_order_id=str(data["orderId"]) if "orderId" in data else None,
            client_order_id=str(data["clientOrderId"]),
            symbol=str(data["symbol"]),
            side=str(data.get("side")) if data.get("side") else None,
            status=_normalize_binance_status(str(data.get("status", "NEW"))),
            original_quantity=orig_qty,
            filled_quantity=filled_qty,
            remaining_quantity=remaining,
            avg_price=avg_price,
            update_time_ms=int(data["updateTime"]) if "updateTime" in data else None,
            seals_absence=False,
        )

    def submit_order(self, request: ExternalOrderRequest) -> ExternalOrderStatus:
        client_id = self.client_order_id_for(request.action_id)
        params = {
            "symbol": request.symbol,
            "side": request.side,
            "type": "LIMIT",
            "timeInForce": request.time_in_force,
            "quantity": _fmt_decimal(request.quantity),
            "price": _fmt_decimal(request.limit_price),
            "newClientOrderId": client_id,
        }
        data = self._signed_request(
            "POST", f"{self._prefix()}/order", params, is_submit=True
        )
        return self._parse_order_status(data)

    def cancel_order(self, symbol: str, client_order_id: str) -> ExternalOrderStatus:
        data = self._signed_request(
            "DELETE",
            f"{self._prefix()}/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )
        return self._parse_order_status(data)

    def query_order(self, symbol: str, client_order_id: str) -> ExternalOrderStatus:
        try:
            data = self._signed_request(
                "GET",
                f"{self._prefix()}/order",
                {"symbol": symbol, "origClientOrderId": client_order_id},
            )
            return self._parse_order_status(data)
        except ExternalVenueRefused as error:
            if "code=-2013" in str(error):
                # Binance does not seal absent order IDs against delayed arrival.
                return ExternalOrderStatus(
                    venue_order_id=None,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=None,
                    status="absent_unsealed",
                    original_quantity=Decimal("0"),
                    filled_quantity=Decimal("0"),
                    remaining_quantity=Decimal("0"),
                    avg_price=None,
                    update_time_ms=None,
                    seals_absence=False,
                )
            raise

    def query_fills(
        self, symbol: str, *, client_order_id: str | None = None
    ) -> tuple[ExternalFillRecord, ...]:
        path = (
            "/api/v3/myTrades"
            if self._config.market_kind == MARKET_SPOT
            else "/fapi/v1/userTrades"
        )
        rows = self._signed_request("GET", path, {"symbol": symbol})
        fills: list[ExternalFillRecord] = []
        for row in rows:
            cid = str(row.get("clientOrderId", ""))
            if client_order_id is not None and cid != client_order_id:
                continue
            is_buyer = bool(row.get("isBuyer", row.get("side") == "BUY"))
            fills.append(
                ExternalFillRecord(
                    fill_id=str(row["id"]),
                    venue_order_id=str(row["orderId"]),
                    client_order_id=cid,
                    symbol=str(row["symbol"]),
                    side="BUY" if is_buyer else "SELL",
                    quantity=Decimal(str(row["qty"])),
                    price=Decimal(str(row["price"])),
                    commission=Decimal(str(row.get("commission", "0"))),
                    commission_asset=str(row.get("commissionAsset", "USDT")),
                    transact_time_ms=int(row.get("time", 0)),
                )
            )
        return tuple(fills)

    def reconcile_external(
        self, symbol: str, known_client_order_ids: frozenset[str]
    ) -> ExternalReconciliationReport:
        """Surface venue fills or positions not traceable to company-known order IDs."""
        fills = self.query_fills(symbol)
        unattributed_fills = tuple(
            fill for fill in fills if fill.client_order_id not in known_client_order_ids
        )
        account = self.observe_account()
        expected_net = Decimal("0")
        for fill in fills:
            if fill.client_order_id in known_client_order_ids:
                expected_net += fill.quantity if fill.side == "BUY" else -fill.quantity
        unattributed_positions = tuple(
            pos
            for pos in account.positions
            if pos.symbol == symbol and pos.quantity != expected_net
        )
        return ExternalReconciliationReport(
            account_id=self._config.account_id,
            environment=self._config.environment,
            known_order_ids=tuple(sorted(known_client_order_ids)),
            unattributed_fills=unattributed_fills,
            unattributed_positions=unattributed_positions,
            clean=(not unattributed_fills and not unattributed_positions),
        )
