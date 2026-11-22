"""Translate pinned spot rules into exact company lots and quote atoms."""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from .market_worker import SpotInstrumentTerms
from .paper_execution import VenueRefused


QUOTE_SCALE = 10**8
MAX_ATOMS = 2**63 - 1


def _positive_integer(value: Decimal) -> int:
    if (
        not value.is_finite()
        or not 0 < value <= MAX_ATOMS
        or value != value.to_integral_value()
    ):
        raise VenueRefused("Spot value does not fit its registered integer units")
    return int(value)


@dataclass(frozen=True)
class SpotCashUnits:
    terms: SpotInstrumentTerms

    def __post_init__(self) -> None:
        if self.terms.minimum_quantity is None or self.terms.minimum_notional is None:
            raise VenueRefused("Spot paper requires verified order sizing minima")
        self.price_per_lot(Decimal(self.terms.price_increment))

    def quote_atoms(self, cash: Decimal) -> int:
        with localcontext() as context:
            context.prec = 100
            return 0 if cash.is_zero() else _positive_integer(cash * QUOTE_SCALE)

    def lots(self, quantity: Decimal) -> int:
        with localcontext() as context:
            context.prec = 100
            return _positive_integer(quantity / Decimal(self.terms.quantity_increment))

    def price_per_lot(self, price: Decimal) -> int:
        with localcontext() as context:
            context.prec = 100
            _positive_integer(price / Decimal(self.terms.price_increment))
            return self.quote_atoms(price * Decimal(self.terms.quantity_increment))

    def limit_order(self, lots: int, atoms_per_lot: int) -> tuple[Decimal, Decimal]:
        if (
            type(lots) is not int
            or type(atoms_per_lot) is not int
            or not 0 < lots * atoms_per_lot <= MAX_ATOMS
            or min(lots, atoms_per_lot) <= 0
        ):
            raise VenueRefused("Paper order requires positive integer lots and price")
        with localcontext() as context:
            context.prec = 100
            step = Decimal(self.terms.quantity_increment)
            quantity = lots * step
            step_atoms = self.price_per_lot(Decimal(self.terms.price_increment))
            if atoms_per_lot % step_atoms:
                raise VenueRefused("Paper limit is not aligned to the price increment")
            price = (atoms_per_lot // step_atoms) * Decimal(self.terms.price_increment)
            if quantity < Decimal(
                self.terms.minimum_quantity
            ) or quantity * price < Decimal(self.terms.minimum_notional):
                raise VenueRefused("Paper order is below the verified spot minima")
            return quantity, price
