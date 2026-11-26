"""Faithful local HTTP Binance Spot & USD-M Futures wire-protocol emulator.

Verifies HMAC-SHA256 signatures, models balances/positions/fills/fees, and supports
deterministic fault injection (over-limit rejection, partial fill + cancel leaving
residual exposure, lost reply after accept, unsealed delayed absence, and external
venue-originated fills) for qualification and parity tests without live credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.parse
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time
from typing import Any


class BinanceWireEmulator:
    """In-process loopback HTTP server speaking Binance Spot & USD-M Futures REST."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        starting_usdt: Decimal = Decimal("100000"),
        fee_bps: int = 4,
        max_order_notional: Decimal = Decimal("50000"),
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._fee_rate = Decimal(fee_bps) / Decimal(10_000)
        self._max_notional = max_order_notional
        self._lock = threading.Lock()
        self._next_order_id = 1000
        self._next_trade_id = 5000
        self._balances: dict[str, Decimal] = {"USDT": starting_usdt}
        self._positions: dict[str, tuple[Decimal, Decimal]] = {}  # symbol -> (qty, avg)
        self._orders: dict[str, dict[str, Any]] = {}  # clientOrderId -> order
        self._trades: list[dict[str, Any]] = []
        self._fault_rules: dict[str, str] = {}  # clientOrderId -> fault_mode
        self._delayed_orders: dict[str, dict[str, Any]] = {}
        self._submit_count = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("BinanceWireEmulator is not started")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def submit_count(self) -> int:
        with self._lock:
            return self._submit_count

    def set_fault(self, client_order_id: str, mode: str) -> None:
        """Configure deterministic fault for a specific clientOrderId.

        Supported modes:
        - 'partial_then_cancel': fills 40% immediately and cancels remaining 60%
        - 'lost_reply_after_accept': fills order in ledger then replies HTTP 504
        - 'delayed_unsealed': rejects submit with HTTP 504 and withholds from first query
        """
        with self._lock:
            self._fault_rules[client_order_id] = mode

    def inject_external_fill(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        external_client_id: str = "ext_manual_venue_fill",
    ) -> dict[str, Any]:
        """Inject a venue-originated fill outside company-known orders."""
        with self._lock:
            return self._record_fill_locked(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                client_order_id=external_client_id,
                orig_qty=quantity,
                status="FILLED",
            )

    def start(self) -> str:
        if self._server is not None:
            return self.base_url
        emulator = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                emulator._handle("GET", self)

            def do_POST(self) -> None:  # noqa: N802
                emulator._handle("POST", self)

            def do_DELETE(self) -> None:  # noqa: N802
                emulator._handle("DELETE", self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="binance-wire-emulator",
            daemon=True,
        )
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def __enter__(self) -> BinanceWireEmulator:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def _send_json(
        self, handler: BaseHTTPRequestHandler, status: int, payload: Any
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _verify_auth(
        self, handler: BaseHTTPRequestHandler, raw_query: str
    ) -> dict[str, str] | None:
        api_key = handler.headers.get("X-MBX-APIKEY", "")
        if not hmac.compare_digest(api_key, self._api_key):
            self._send_json(
                handler, 401, {"code": -2015, "msg": "Invalid API-key or permissions"}
            )
            return None
        parts = raw_query.split("&signature=")
        if len(parts) != 2:
            self._send_json(handler, 401, {"code": -1022, "msg": "Signature missing"})
            return None
        payload_qs, provided_sig = parts
        expected_sig = hmac.new(
            self._api_secret.encode("utf-8"),
            payload_qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(provided_sig, expected_sig):
            self._send_json(
                handler,
                401,
                {"code": -1022, "msg": "Signature for this request is not valid"},
            )
            return None
        parsed = urllib.parse.parse_qs(payload_qs, keep_blank_values=False)
        return {k: v[0] for k, v in parsed.items()}

    def _record_fill_locked(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
        orig_qty: Decimal,
        status: str,
    ) -> dict[str, Any]:
        now_ms = int(time() * 1000)
        self._next_order_id += 1
        self._next_trade_id += 1
        oid = self._next_order_id
        tid = self._next_trade_id
        notional = quantity * price
        commission = (notional * self._fee_rate).quantize(Decimal("0.00000001"))

        if side == "BUY":
            self._balances["USDT"] = (
                self._balances.get("USDT", Decimal("0")) - notional - commission
            )
            base = symbol.replace("USDT", "")
            self._balances[base] = self._balances.get(base, Decimal("0")) + quantity
            cur_qty, cur_avg = self._positions.get(symbol, (Decimal("0"), Decimal("0")))
            new_qty = cur_qty + quantity
            new_avg = (
                ((cur_qty * cur_avg) + notional) / new_qty
                if new_qty != 0
                else Decimal("0")
            )
            self._positions[symbol] = (new_qty, new_avg)
        else:
            self._balances["USDT"] = (
                self._balances.get("USDT", Decimal("0")) + notional - commission
            )
            base = symbol.replace("USDT", "")
            self._balances[base] = self._balances.get(base, Decimal("0")) - quantity
            cur_qty, cur_avg = self._positions.get(symbol, (Decimal("0"), Decimal("0")))
            new_qty = cur_qty - quantity
            self._positions[symbol] = (
                new_qty,
                cur_avg if new_qty != 0 else Decimal("0"),
            )

        order = {
            "symbol": symbol,
            "orderId": oid,
            "clientOrderId": client_order_id,
            "side": side,
            "price": str(price),
            "avgPrice": str(price),
            "origQty": str(orig_qty),
            "executedQty": str(quantity),
            "cummulativeQuoteQty": str(notional),
            "status": status,
            "updateTime": now_ms,
        }
        self._orders[client_order_id] = order
        self._trades.append(
            {
                "id": tid,
                "orderId": oid,
                "clientOrderId": client_order_id,
                "symbol": symbol,
                "side": side,
                "isBuyer": side == "BUY",
                "qty": str(quantity),
                "price": str(price),
                "commission": str(commission),
                "commissionAsset": "USDT",
                "time": now_ms,
            }
        )
        return order

    def _handle(self, method: str, handler: BaseHTTPRequestHandler) -> None:
        split = urllib.parse.urlsplit(handler.path)
        params = self._verify_auth(handler, split.query)
        if params is None:
            return
        path = split.path

        with self._lock:
            now_ms = int(time() * 1000)
            if method == "GET" and path == "/api/v3/account":
                self._send_json(
                    handler,
                    200,
                    {
                        "updateTime": now_ms,
                        "balances": [
                            {"asset": k, "free": str(v), "locked": "0"}
                            for k, v in sorted(self._balances.items())
                        ],
                    },
                )
                return

            if method == "GET" and path == "/fapi/v2/account":
                self._send_json(
                    handler,
                    200,
                    {
                        "updateTime": now_ms,
                        "assets": [
                            {
                                "asset": "USDT",
                                "walletBalance": str(self._balances["USDT"]),
                            }
                        ],
                        "positions": [
                            {
                                "symbol": sym,
                                "positionAmt": str(qty),
                                "entryPrice": str(avg),
                                "unrealizedProfit": "0",
                            }
                            for sym, (qty, avg) in sorted(self._positions.items())
                        ],
                    },
                )
                return

            if method == "POST" and path in ("/api/v3/order", "/fapi/v1/order"):
                self._submit_count += 1
                symbol = params["symbol"]
                side = params["side"]
                qty = Decimal(params["quantity"])
                price = Decimal(params["price"])
                cid = params["newClientOrderId"]
                if cid in self._orders:
                    self._send_json(handler, 200, self._orders[cid])
                    return
                if qty * price > self._max_notional:
                    self._send_json(
                        handler,
                        400,
                        {
                            "code": -2010,
                            "msg": "Order notional exceeds account risk limit",
                        },
                    )
                    return

                fault = self._fault_rules.get(cid)
                if fault == "delayed_unsealed":
                    # Withhold visibility until after first absence lookup.
                    self._delayed_orders[cid] = {
                        "symbol": symbol,
                        "side": side,
                        "qty": qty,
                        "price": price,
                        "queries_seen": 0,
                    }
                    self._send_json(
                        handler,
                        504,
                        {"code": -1007, "msg": "Timeout waiting for response"},
                    )
                    return

                if fault == "partial_then_cancel":
                    filled = (qty * Decimal("0.4")).quantize(Decimal("0.0001"))
                    order = self._record_fill_locked(
                        symbol=symbol,
                        side=side,
                        quantity=filled,
                        price=price,
                        client_order_id=cid,
                        orig_qty=qty,
                        status="CANCELED",
                    )
                    self._send_json(handler, 200, order)
                    return

                order = self._record_fill_locked(
                    symbol=symbol,
                    side=side,
                    quantity=qty,
                    price=price,
                    client_order_id=cid,
                    orig_qty=qty,
                    status="FILLED",
                )
                if fault == "lost_reply_after_accept":
                    self._send_json(
                        handler,
                        504,
                        {"code": -1007, "msg": "Gateway timeout after accept"},
                    )
                    return
                self._send_json(handler, 200, order)
                return

            if method == "GET" and path in ("/api/v3/order", "/fapi/v1/order"):
                cid = params.get("origClientOrderId", "")
                if cid in self._orders:
                    self._send_json(handler, 200, self._orders[cid])
                    return
                if cid in self._delayed_orders:
                    pending = self._delayed_orders[cid]
                    pending["queries_seen"] += 1
                    if pending["queries_seen"] == 1:
                        self._send_json(
                            handler,
                            400,
                            {"code": -2013, "msg": "Order does not exist."},
                        )
                        return
                    # Second lookup sees the delayed fill arrive!
                    order = self._record_fill_locked(
                        symbol=pending["symbol"],
                        side=pending["side"],
                        quantity=pending["qty"],
                        price=pending["price"],
                        client_order_id=cid,
                        orig_qty=pending["qty"],
                        status="FILLED",
                    )
                    del self._delayed_orders[cid]
                    self._send_json(handler, 200, order)
                    return
                self._send_json(
                    handler, 400, {"code": -2013, "msg": "Order does not exist."}
                )
                return

            if method == "DELETE" and path in ("/api/v3/order", "/fapi/v1/order"):
                cid = params.get("origClientOrderId", "")
                order = self._orders.get(cid)
                if order is None:
                    self._send_json(
                        handler, 400, {"code": -2011, "msg": "Unknown order sent."}
                    )
                    return
                if order["status"] == "NEW":
                    order["status"] = "CANCELED"
                    order["updateTime"] = now_ms
                self._send_json(handler, 200, order)
                return

            if method == "GET" and path in ("/api/v3/myTrades", "/fapi/v1/userTrades"):
                symbol = params["symbol"]
                matching = [t for t in self._trades if t["symbol"] == symbol]
                self._send_json(handler, 200, matching)
                return

            self._send_json(
                handler, 404, {"code": -1000, "msg": f"Unsupported path {path}"}
            )
