"""Read-only market quote port and providers for autonomous trading & mark-to-market."""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from time import time_ns
from typing import Protocol

from ..storage.db import connect_sqlite

_INSTRUMENT_RE = re.compile(r"^[A-Z0-9][A-Z0-9._/-]{1,31}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
_MINOR_SCALE = Decimal("100")

BINANCE_PUBLIC_TICKER_ENDPOINTS: dict[str, str] = {
    "spot": "https://api.binance.com/api/v3/ticker/price",
    "usdm_futures": "https://fapi.binance.com/fapi/v1/ticker/price",
}


class QuoteRefused(Exception):
    """A market quote request or observation failed validation or freshness checks."""


def normalize_instrument_id(raw: str) -> str:
    """Validate and uppercase an instrument identifier (e.g. BTC-USDT or BTCUSDT)."""
    if not isinstance(raw, str) or not raw.strip():
        raise QuoteRefused("instrument_id must be a non-empty string")
    cleaned = raw.strip().upper()
    if _INSTRUMENT_RE.fullmatch(cleaned) is None:
        raise QuoteRefused(f"Invalid instrument_id '{raw}'")
    return cleaned


def to_venue_symbol(instrument_id: str) -> str:
    """Convert normalized instrument_id (e.g. 'BTC-USDT') to venue symbol ('BTCUSDT')."""
    norm = normalize_instrument_id(instrument_id)
    symbol = norm.replace("-", "").replace("/", "").replace("_", "")
    if not symbol.isalnum() or len(symbol) < 4:
        raise QuoteRefused(f"Cannot derive venue symbol from '{instrument_id}'")
    return symbol


def decimal_price_to_minor_units(price_str: str | Decimal) -> int:
    """Convert a positive decimal quote price into integer minor units (cents)."""
    try:
        dec = price_str if isinstance(price_str, Decimal) else Decimal(str(price_str))
    except (InvalidOperation, ValueError) as exc:
        raise QuoteRefused(f"Invalid decimal quote price: {price_str!r}") from exc
    if not dec.is_finite() or dec <= 0:
        raise QuoteRefused(f"Quote price must be positive: {price_str!r}")
    scaled = int((dec * _MINOR_SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if scaled <= 0:
        raise QuoteRefused(f"Quote price rounds to zero minor units: {price_str!r}")
    return scaled


def minor_units_to_decimal(minor_units: int) -> Decimal:
    """Convert integer minor units (cents) into a Decimal price."""
    if type(minor_units) is not int or minor_units <= 0:
        raise QuoteRefused("minor_units must be a positive integer")
    return Decimal(minor_units) / _MINOR_SCALE


@dataclass(frozen=True)
class MarketQuote:
    instrument_id: str
    price_minor_units: int
    observed_at_ns: int
    source: str

    def __post_init__(self) -> None:
        norm = normalize_instrument_id(self.instrument_id)
        if norm != self.instrument_id:
            object.__setattr__(self, "instrument_id", norm)
        if type(self.price_minor_units) is not int or self.price_minor_units <= 0:
            raise QuoteRefused("price_minor_units must be a positive integer")
        if type(self.observed_at_ns) is not int or self.observed_at_ns <= 0:
            raise QuoteRefused("observed_at_ns must be a positive integer")
        if not isinstance(self.source, str) or not self.source.strip():
            raise QuoteRefused("quote source must be non-empty")

    def is_fresh(self, *, now_ns: int, max_age_ns: int) -> bool:
        if type(now_ns) is not int or type(max_age_ns) is not int or max_age_ns <= 0:
            return False
        if self.observed_at_ns > now_ns + 5 * 10**9:
            return False
        return (now_ns - self.observed_at_ns) <= max_age_ns


class MarketQuoteProvider(Protocol):
    def latest_quote(self, instrument_id: str) -> MarketQuote | None: ...

    def latest_quotes(
        self, instrument_ids: Sequence[str]
    ) -> dict[str, MarketQuote]: ...


class StaticQuoteProvider:
    """Deterministic in-memory quote provider for offline tests and replays."""

    def __init__(self, quotes: Mapping[str, MarketQuote]) -> None:
        self._by_id: dict[str, MarketQuote] = {}
        self._by_symbol: dict[str, MarketQuote] = {}
        for key, quote in quotes.items():
            norm = normalize_instrument_id(key)
            self._by_id[norm] = quote
            self._by_symbol[to_venue_symbol(norm)] = quote

    def set_quote(self, quote: MarketQuote) -> None:
        self._by_id[quote.instrument_id] = quote
        self._by_symbol[to_venue_symbol(quote.instrument_id)] = quote

    def latest_quote(self, instrument_id: str) -> MarketQuote | None:
        norm = normalize_instrument_id(instrument_id)
        hit = self._by_id.get(norm)
        if hit is not None:
            return hit
        sym_hit = self._by_symbol.get(to_venue_symbol(norm))
        if sym_hit is None:
            return None
        return MarketQuote(
            instrument_id=norm,
            price_minor_units=sym_hit.price_minor_units,
            observed_at_ns=sym_hit.observed_at_ns,
            source=sym_hit.source,
        )

    def latest_quotes(self, instrument_ids: Sequence[str]) -> dict[str, MarketQuote]:
        out: dict[str, MarketQuote] = {}
        for raw in instrument_ids:
            norm = normalize_instrument_id(raw)
            quote = self.latest_quote(norm)
            if quote is not None:
                out[norm] = quote
        return out


class MarketJournalQuoteProvider:
    """Read-only quote provider backed by a company-managed `market_worker.py` journal."""

    def __init__(self, journal_path: Path) -> None:
        self._journal = journal_path.resolve()

    def latest_quote(self, instrument_id: str) -> MarketQuote | None:
        norm = normalize_instrument_id(instrument_id)
        want_sym = to_venue_symbol(norm)
        if not self._journal.is_file():
            return None
        try:
            with closing(
                connect_sqlite(f"{self._journal.as_uri()}?mode=ro", uri=True)
            ) as conn:
                scope_row = conn.execute(
                    "SELECT symbol, source FROM market_scope WHERE singleton = 1"
                ).fetchone()
                if scope_row is None or str(scope_row[0]).upper() != want_sym:
                    return None
                trade_row = conn.execute(
                    """SELECT price, event_ns, received_ns
                       FROM market_trade
                       ORDER BY received_ns DESC, trade_id DESC
                       LIMIT 1"""
                ).fetchone()
                if trade_row is None:
                    return None
                price_minor = decimal_price_to_minor_units(str(trade_row[0]))
                observed_ns = min(int(trade_row[1]), int(trade_row[2]))
                return MarketQuote(
                    instrument_id=norm,
                    price_minor_units=price_minor,
                    observed_at_ns=observed_ns,
                    source=str(scope_row[1]),
                )
        except (sqlite3.Error, QuoteRefused):
            return None

    def latest_quotes(self, instrument_ids: Sequence[str]) -> dict[str, MarketQuote]:
        out: dict[str, MarketQuote] = {}
        for raw in instrument_ids:
            norm = normalize_instrument_id(raw)
            quote = self.latest_quote(norm)
            if quote is not None:
                out[norm] = quote
        return out


class BinancePublicQuoteProvider:
    """Read-only public price ticker provider for Binance Spot / USD-M Futures.

    Requires zero API keys. Only permits URL override for loopback test servers.
    """

    def __init__(
        self,
        *,
        market_kind: str = "spot",
        base_url_override: str | None = None,
        allow_loopback_override: bool = False,
        timeout_seconds: float = 5.0,
    ) -> None:
        if market_kind not in BINANCE_PUBLIC_TICKER_ENDPOINTS:
            raise QuoteRefused(f"Unsupported Binance market_kind: {market_kind}")
        if not 0.5 <= timeout_seconds <= 30.0:
            raise QuoteRefused("timeout_seconds must be within [0.5, 30.0]")
        if base_url_override is not None:
            if not allow_loopback_override:
                raise QuoteRefused(
                    "base_url_override requires allow_loopback_override=True"
                )
            parsed = urllib.parse.urlparse(base_url_override)
            if parsed.scheme not in ("http", "https") or (
                parsed.hostname not in _LOOPBACK_HOSTS
            ):
                raise QuoteRefused(
                    "base_url_override must target loopback (127.0.0.1 or localhost) only"
                )
        self._market_kind = market_kind
        self._base_url_override = (
            base_url_override.rstrip("/") if base_url_override is not None else None
        )
        self._timeout_seconds = timeout_seconds

    @property
    def _ticker_url(self) -> str:
        if self._base_url_override is not None:
            suffix = (
                "/api/v3/ticker/price"
                if self._market_kind == "spot"
                else "/fapi/v1/ticker/price"
            )
            return f"{self._base_url_override}{suffix}"
        return BINANCE_PUBLIC_TICKER_ENDPOINTS[self._market_kind]

    def latest_quote(self, instrument_id: str) -> MarketQuote | None:
        norm = normalize_instrument_id(instrument_id)
        symbol = to_venue_symbol(norm)
        url = f"{self._ticker_url}?{urllib.parse.urlencode({'symbol': symbol})}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "sourdough-quotes/1"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or "price" not in payload:
            return None
        try:
            price_minor = decimal_price_to_minor_units(str(payload["price"]))
        except QuoteRefused:
            return None
        return MarketQuote(
            instrument_id=norm,
            price_minor_units=price_minor,
            observed_at_ns=time_ns(),
            source=f"binance-public-{self._market_kind}",
        )

    def latest_quotes(self, instrument_ids: Sequence[str]) -> dict[str, MarketQuote]:
        out: dict[str, MarketQuote] = {}
        for raw in instrument_ids:
            norm = normalize_instrument_id(raw)
            quote = self.latest_quote(norm)
            if quote is not None:
                out[norm] = quote
        return out


class CompositeQuoteProvider:
    """Ordered fallback quote provider across multiple read-only sources."""

    def __init__(self, providers: Sequence[MarketQuoteProvider]) -> None:
        self._providers = tuple(providers)

    def latest_quote(self, instrument_id: str) -> MarketQuote | None:
        norm = normalize_instrument_id(instrument_id)
        for provider in self._providers:
            quote = provider.latest_quote(norm)
            if quote is not None:
                return quote
        return None

    def latest_quotes(self, instrument_ids: Sequence[str]) -> dict[str, MarketQuote]:
        out: dict[str, MarketQuote] = {}
        for raw in instrument_ids:
            norm = normalize_instrument_id(raw)
            quote = self.latest_quote(norm)
            if quote is not None:
                out[norm] = quote
        return out


def build_quote_provider_from_spec(spec: str) -> MarketQuoteProvider:
    """Build a read-only MarketQuoteProvider from a host configuration specifier.

    Supported formats:
    - 'binance-spot'
    - 'binance-futures'
    - 'journal:<path-to-market.sqlite>'
    """
    if not isinstance(spec, str) or not spec.strip():
        raise QuoteRefused("Quote source specifier must be non-empty")
    cleaned = spec.strip()
    lowered = cleaned.lower()
    if lowered == "binance-spot":
        return BinancePublicQuoteProvider(market_kind="spot")
    if lowered == "binance-futures":
        return BinancePublicQuoteProvider(market_kind="usdm_futures")
    if lowered.startswith("journal:"):
        raw_path = cleaned[len("journal:") :].strip()
        if not raw_path:
            raise QuoteRefused("journal:<path> quote source requires a file path")
        return MarketJournalQuoteProvider(Path(raw_path))
    raise QuoteRefused(
        f"Unsupported quote source '{spec}' (expected binance-spot, binance-futures, or journal:<path>)"
    )
