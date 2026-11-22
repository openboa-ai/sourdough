"""Validate a sealed quote sequence before any historical activation."""

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from .market_worker import MarketRefused, SpotInstrumentTerms, _positive_decimal
from .paper_execution import VenueRefused
from .spot_cash import SpotCashUnits


@dataclass(frozen=True)
class ReplayQuote:
    event_ns: int
    available_ns: int
    bid: str
    ask: str
    bid_size: str
    ask_size: str

    def __post_init__(self) -> None:
        if (
            type(self.event_ns) is not int
            or type(self.available_ns) is not int
            or not 0 < self.event_ns <= self.available_ns <= 2**63 - 1
            or not all(
                _positive_decimal(value)
                for value in (self.bid, self.ask, self.bid_size, self.ask_size)
            )
            or Decimal(self.ask) < Decimal(self.bid)
        ):
            raise VenueRefused("Historical quote has invalid price or availability")


@dataclass(frozen=True)
class ReplayTape:
    revision: str
    source: str
    terms: SpotInstrumentTerms
    quotes: tuple[ReplayQuote, ...]

    @staticmethod
    def load(path: Path) -> "ReplayTape":
        limit = 4 * 1024 * 1024
        with path.open("rb") as source:
            raw = source.read(limit + 1)
        if not 0 < len(raw) <= limit:
            raise VenueRefused("Historical tape is empty or exceeds its input budget")
        try:
            dataset = json.loads(raw)
            if not isinstance(dataset, dict) or set(dataset) != {
                "source",
                "terms",
                "quotes",
            }:
                raise VenueRefused(
                    "Expected source, instrument terms, and historical quotes"
                )
            source_id = dataset["source"]
            if not isinstance(source_id, str) or not 0 < len(source_id.strip()) <= 128:
                raise VenueRefused("A recorded historical source is required")
            terms = SpotInstrumentTerms(**dataset["terms"])
            quotes = tuple(ReplayQuote(**quote) for quote in dataset["quotes"])
            if not 2 <= len(quotes) <= 10_000 or any(
                right.available_ns <= left.available_ns
                for left, right in zip(quotes, quotes[1:], strict=False)
            ):
                raise VenueRefused(
                    "Historical observations must be strictly ordered by availability"
                )
            units = SpotCashUnits(terms)
            for quote in quotes:
                units.price_per_lot(Decimal(quote.bid))
                units.price_per_lot(Decimal(quote.ask))
                units.lots(Decimal(quote.bid_size))
                units.lots(Decimal(quote.ask_size))
            return ReplayTape(sha256(raw).hexdigest(), source_id, terms, quotes)
        except (TypeError, KeyError, ValueError, MarketRefused) as error:
            raise VenueRefused("Historical tape is malformed") from error
