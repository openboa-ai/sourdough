"""Split company admission from the independently witnessed sandbox execution."""

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, astuple, dataclass
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep, time_ns
from uuid import uuid4

from .market_worker import SpotInstrumentTerms, journal_key
from .paper_account import PaperAccount
from .paper_execution import (
    PaperIntent,
    PaperOrderSizing,
    VenueCancellation,
    VenueExecution,
    VenuePosition,
    VenueRefused,
    require_separate_journal,
)
from .spot_cash import MAX_ATOMS, QUOTE_SCALE, SpotCashUnits


CONNECTOR = "nautilus-spot-sandbox-v1"
REPLAY_CONNECTOR = "nautilus-offline-spot-replay-v1"
_STALE_NS = 15 * 10**9
_CANCELLATION_SCHEMA = """CREATE TABLE IF NOT EXISTS sandbox_cancellation (
    order_id TEXT PRIMARY KEY NOT NULL REFERENCES sandbox_order(order_id),
    generation TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, filled_quantity INTEGER NOT NULL
)"""


@dataclass(frozen=True)
class SandboxPlan:
    company_id: str
    account_id: str
    terms: SpotInstrumentTerms
    starting_atoms: int
    fee_bps: int
    tape_revision: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.company_id
            or not self.account_id
            or type(self.starting_atoms) is not int
            or not 0 < self.starting_atoms <= MAX_ATOMS
            or type(self.fee_bps) is not int
            or self.fee_bps != 0
            or (
                self.tape_revision is not None
                and (
                    not isinstance(self.tape_revision, str)
                    or len(self.tape_revision) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in self.tape_revision
                    )
                )
            )
        ):
            raise VenueRefused(
                "Expected a bounded virtual cash account and zero test fee"
            )
        SpotCashUnits(self.terms)


@dataclass(frozen=True)
class SandboxCommand:
    order_id: str
    action_id: str
    quantity: int
    limit_minor_units: int


@dataclass(frozen=True)
class SandboxBalance:
    position_lots: int
    cash_atoms: int


class NautilusPaperVenue:
    def __init__(self, journal: Path):
        self.journal = journal.resolve()
        if journal.is_symlink():
            raise VenueRefused("Sandbox journal must not be redirected")
        self._key = journal_key(self.journal) if self.journal.exists() else None

    def _connect(self, mode: str):
        key = journal_key(self.journal)
        if self._key is not None and self._key != key:
            raise VenueRefused("Original sandbox journal changed")
        self._key = key
        connection = sqlite3.connect(
            f"{self.journal.as_uri()}?mode={mode}", uri=True, timeout=2
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return closing(connection)

    def require_independent_from(self, company_store: Path) -> None:
        require_separate_journal(company_store, self.journal)

    def provision(self, plan: SandboxPlan) -> str:
        try:
            descriptor = os.open(
                self.journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self._connect("rw") as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sandbox_config (
                       singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                       destination_id TEXT UNIQUE NOT NULL, plan TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sandbox_quote (
                       singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                       generation TEXT NOT NULL, price_atoms_per_lot INTEGER NOT NULL,
                       received_ns INTEGER NOT NULL, exchange_trade_ns INTEGER NOT NULL
                   )"""
            )
            if plan.tape_revision is not None:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS replay_decision_cut (
                           generation TEXT PRIMARY KEY, price_atoms_per_lot INTEGER NOT NULL,
                           event_ns INTEGER NOT NULL, available_ns INTEGER NOT NULL
                       )"""
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sandbox_identity (
                       singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                       venue_account_id TEXT NOT NULL, currency TEXT NOT NULL,
                       initial_atoms INTEGER NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sandbox_order (
                       order_id TEXT PRIMARY KEY NOT NULL, action_id TEXT UNIQUE NOT NULL,
                       quantity INTEGER NOT NULL, limit_atoms INTEGER NOT NULL,
                       state TEXT NOT NULL CHECK (state IN ('queued','dispatching','filled','unresolved')),
                       generation TEXT, fill_id TEXT UNIQUE, price_atoms INTEGER, fee_atoms INTEGER
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sandbox_fill_observation (
                       fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES sandbox_order(order_id),
                       generation TEXT NOT NULL, quantity INTEGER NOT NULL,
                       price_atoms INTEGER NOT NULL, fee_atoms INTEGER NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sandbox_account_observation (
                       order_id TEXT PRIMARY KEY REFERENCES sandbox_order(order_id),
                       position_lots INTEGER NOT NULL, cash_atoms INTEGER NOT NULL
                   )"""
            )
            connection.execute(_CANCELLATION_SCHEMA)
            terms = json.dumps(asdict(plan), sort_keys=True, separators=(",", ":"))
            row = connection.execute(
                "SELECT destination_id,plan FROM sandbox_config WHERE singleton=1"
            ).fetchone()
            if row is not None:
                if self._read_plan(row[1]) != plan:
                    raise VenueRefused(
                        "Sandbox plan does not match the original account"
                    )
                return row[0]
            identity = uuid4().hex
            connection.execute(
                "INSERT INTO sandbox_config VALUES (1, ?, ?)", (identity, terms)
            )
            return identity

    def _configuration(self, connection: sqlite3.Connection) -> tuple[str, SandboxPlan]:
        row = connection.execute(
            "SELECT destination_id,plan FROM sandbox_config WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise VenueRefused("Sandbox account has not been provisioned")
        return row[0], self._read_plan(row[1])

    @staticmethod
    def _read_plan(content: str) -> SandboxPlan:
        terms = json.loads(content)
        terms["terms"] = SpotInstrumentTerms(**terms["terms"])
        return SandboxPlan(**terms)

    def configuration(self) -> tuple[str, SandboxPlan]:
        with self._connect("ro") as connection:
            return self._configuration(connection)

    def _account(self, connection: sqlite3.Connection, account_id: str) -> PaperAccount:
        identity, plan = self._configuration(connection)
        if plan.account_id != account_id:
            raise VenueRefused(
                "Order or lookup is not for the original sandbox account"
            )
        if connection.execute(
            "SELECT venue_account_id,currency,initial_atoms FROM sandbox_identity"
        ).fetchone() != ("BINANCE-001", plan.terms.quote_currency, plan.starting_atoms):
            raise VenueRefused("Virtual account has not been witnessed by the engine")
        return PaperAccount(
            account_id,
            REPLAY_CONNECTOR if plan.tape_revision is not None else CONNECTOR,
            plan.terms.revision,
            plan.terms.quote_currency,
            plan.starting_atoms,
            "paper",
            identity,
        )

    def _fresh_quote(
        self, connection: sqlite3.Connection, generation: str | None = None
    ) -> int:
        row = connection.execute(
            """SELECT price_atoms_per_lot,received_ns,exchange_trade_ns,generation
               FROM sandbox_quote"""
        ).fetchone()
        now = time_ns()
        _, plan = self._configuration(connection)
        if (
            row is None
            or not 0 <= now - row[1] <= _STALE_NS
            or (plan.tape_revision is None and not 0 <= now - row[2] <= _STALE_NS)
            or (generation is not None and row[3] != generation)
        ):
            raise VenueRefused(
                "Sandbox has no fresh quote and supporting exchange trade"
            )
        return row[0]

    def observe_account(self, account_id: str) -> PaperAccount:
        with self._connect("ro") as connection:
            return self._account(connection, account_id)

    def observe_quote(self, account_id: str) -> int:
        with self._connect("ro") as connection:
            self._account(connection, account_id)
            return self._fresh_quote(connection)

    def observe_order_sizing(self, account_id: str) -> PaperOrderSizing:
        with self._connect("ro") as connection:
            self._account(connection, account_id)
            _, plan = self._configuration(connection)
            terms = plan.terms
            units = SpotCashUnits(terms)
            return PaperOrderSizing(
                terms.symbol,
                terms.quantity_increment,
                terms.quote_currency,
                QUOTE_SCALE,
                units.price_per_lot(Decimal(terms.price_increment)),
                units.lots(Decimal(terms.minimum_quantity)),
                units.quote_atoms(Decimal(terms.minimum_notional)),
            )

    def observe_fee_bps(self, account_id: str) -> int:
        with self._connect("ro") as connection:
            _, plan = self._configuration(connection)
        if plan.account_id != account_id:
            raise VenueRefused("Sandbox fee policy belongs to a different account")
        return plan.fee_bps

    def _checkpoint(
        self, connection: sqlite3.Connection, plan: SandboxPlan
    ) -> SandboxBalance:
        orders = connection.execute(
            """SELECT o.state,o.quantity,c.filled_quantity,COALESCE(SUM(f.quantity),0),
                      COALESCE(SUM(f.quantity*f.price_atoms+f.fee_atoms),0),a.position_lots,a.cash_atoms
               FROM sandbox_order o
               LEFT JOIN sandbox_fill_observation f ON f.order_id=o.order_id AND f.generation=o.generation
               LEFT JOIN sandbox_account_observation a ON a.order_id=o.order_id
               LEFT JOIN sandbox_cancellation c ON c.order_id=o.order_id AND c.generation=o.generation
               GROUP BY o.order_id ORDER BY o.rowid"""
        )
        position, cash = 0, plan.starting_atoms
        for state, quantity, cancelled_total, filled, cost, held, available in orders:
            if plan.tape_revision is not None:
                raise VenueRefused("Historical replay cannot reuse a prior activation")
            if state == "queued":
                continue
            expected = quantity if state == "filled" else cancelled_total
            if filled != expected:
                raise VenueRefused("Sandbox original effects remain unresolved")
            position += filled
            cash -= cost
            if cash < 0 or (held, available) != (position, cash):
                raise VenueRefused(
                    "Sandbox restart is missing a corroborated account checkpoint"
                )
        return SandboxBalance(position, cash)

    def start_generation(self) -> SandboxBalance:
        with self._connect("rw") as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_CANCELLATION_SCHEMA)
            connection.execute("DELETE FROM sandbox_quote")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sandbox_activation (
                       generation TEXT PRIMARY KEY, venue_account_id TEXT NOT NULL,
                       position_lots INTEGER NOT NULL, cash_atoms INTEGER NOT NULL
                   )"""
            )
            connection.commit()
            _, plan = self._configuration(connection)
            return self._checkpoint(connection, plan)

    def prove_account(
        self,
        generation: str,
        sdk_account_id: str,
        currency: str,
        observed: SandboxBalance,
    ) -> None:
        with self._connect("rw") as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            _, plan = self._configuration(connection)
            if (sdk_account_id, currency) != (
                "BINANCE-001",
                plan.terms.quote_currency,
            ) or observed != self._checkpoint(connection, plan):
                raise VenueRefused(
                    "SDK virtual account differs from its original verified balances"
                )
            connection.execute(
                "INSERT OR IGNORE INTO sandbox_identity VALUES (1, ?, ?, ?)",
                (sdk_account_id, currency, plan.starting_atoms),
            )
            self._account(connection, plan.account_id)
            connection.execute(
                "INSERT INTO sandbox_activation VALUES (?, ?, ?, ?)",
                (
                    generation,
                    sdk_account_id,
                    observed.position_lots,
                    observed.cash_atoms,
                ),
            )

    def submit(self, order_id: str, account_id: str, intent: PaperIntent) -> None:
        command = SandboxCommand(
            order_id, intent.action_id, intent.quantity, intent.limit_minor_units
        )
        with self._connect("rw") as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            account = self._account(connection, account_id)
            if account.instrument_rules_digest != intent.instrument_rules_digest:
                raise VenueRefused("Paper order uses different instrument terms")
            existing = connection.execute(
                """SELECT order_id,action_id,quantity,limit_atoms FROM sandbox_order
                   WHERE order_id=? OR action_id=?""",
                (order_id, intent.action_id),
            ).fetchall()
            if existing and existing != [astuple(command)]:
                raise VenueRefused("Sandbox order conflicts with its original identity")
            if not existing:
                self._fresh_quote(connection)
                if connection.execute(
                    """SELECT 1 FROM sandbox_order o WHERE state!='filled' AND NOT EXISTS (
                         SELECT 1 FROM sandbox_cancellation c
                         JOIN sandbox_account_observation a USING(order_id)
                         WHERE c.order_id=o.order_id AND c.generation=o.generation
                       ) LIMIT 1"""
                ).fetchone():
                    raise VenueRefused(
                        "Outstanding sandbox action has no verified terminal result"
                    )
                _, plan = self._configuration(connection)
                SpotCashUnits(plan.terms).limit_order(
                    intent.quantity, intent.limit_minor_units
                )
                connection.execute(
                    """INSERT INTO sandbox_order(order_id,action_id,quantity,limit_atoms,state)
                       VALUES (?, ?, ?, ?, 'queued')""",
                    astuple(command),
                )
        deadline = monotonic() + 12
        while monotonic() < deadline:
            with self._connect("ro") as connection:
                (state,) = connection.execute(
                    "SELECT state FROM sandbox_order WHERE order_id=?", (order_id,)
                ).fetchone()
            if state in ("filled", "unresolved"):
                return
            sleep(0.05)
        raise TimeoutError("Sandbox order outcome remains unknown")

    def read_fills(
        self, account_id: str, order_id: str, *, account_sync: bool = False
    ) -> tuple[VenueExecution, ...]:
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            account = self._account(connection, account_id)
            if (
                account_sync
                and not connection.execute(
                    "SELECT 1 FROM sandbox_account_observation WHERE order_id=?",
                    (order_id,),
                ).fetchone()
            ):
                return ()
            return tuple(
                VenueExecution(
                    order_id, account_id, account.instrument_rules_digest, *row
                )
                for row in connection.execute(
                    """SELECT o.quantity,o.limit_atoms,f.fill_id,f.price_atoms,f.fee_atoms,f.quantity
                       FROM sandbox_order o JOIN sandbox_fill_observation f
                         ON f.order_id=o.order_id AND f.generation=o.generation
                       WHERE o.order_id=? ORDER BY f.rowid""",
                    (order_id,),
                )
            )

    def read_cancellation(
        self, account_id: str, order_id: str, *, account_sync: bool = False
    ) -> VenueCancellation | None:
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            account = self._account(connection, account_id)
            if not connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='sandbox_cancellation'"
            ).fetchone():
                return None
            row = connection.execute(
                """SELECT c.event_id,c.filled_quantity FROM sandbox_cancellation c
                   JOIN sandbox_order o ON o.order_id=c.order_id AND o.generation=c.generation
                   WHERE c.order_id=? AND (NOT ? OR EXISTS (
                       SELECT 1 FROM sandbox_account_observation a WHERE a.order_id=c.order_id
                   ))""",
                (order_id, account_sync),
            ).fetchone()
            return (
                VenueCancellation(
                    order_id, account_id, account.instrument_rules_digest, *row
                )
                if row
                else None
            )

    def read_order_position(
        self, account_id: str, order_id: str
    ) -> VenuePosition | None:
        deadline = monotonic() + 2
        while True:
            with self._connect("ro") as connection:
                connection.execute("BEGIN")
                account = self._account(connection, account_id)
                row = connection.execute(
                    """SELECT a.position_lots, o.state
                       FROM sandbox_order o
                       LEFT JOIN sandbox_account_observation a USING(order_id)
                       WHERE o.order_id=?""",
                    (order_id,),
                ).fetchone()
            if row is None:
                return None
            position, state = row
            if position is not None:
                return VenuePosition(
                    order_id, account_id, account.instrument_rules_digest, position
                )
            if state not in ("filled", "unresolved") or monotonic() >= deadline:
                return None
            sleep(0.05)

    def record_account(
        self, generation: str, order_id: str, position_lots: int, cash_atoms: int
    ) -> bool:
        with self._connect("rw") as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT CASE WHEN o.state='filled' THEN o.quantity ELSE c.filled_quantity END,
                          COALESCE(SUM(f.quantity),0)
                   FROM sandbox_order o
                   LEFT JOIN sandbox_cancellation c ON c.order_id=o.order_id AND c.generation=o.generation
                   LEFT JOIN sandbox_fill_observation f ON f.order_id=o.order_id AND f.generation=o.generation
                   WHERE o.order_id=? AND o.generation=? GROUP BY o.order_id""",
                (order_id, generation),
            ).fetchone()
            if row is None or row[0] != row[1]:
                return False
            observed = position_lots, cash_atoms
            prior = connection.execute(
                """SELECT position_lots,cash_atoms FROM sandbox_account_observation
                   WHERE order_id=?""",
                (order_id,),
            ).fetchone()
            if prior is not None:
                return prior == observed
            _, plan = self._configuration(connection)
            quantity, cost = connection.execute(
                """SELECT COALESCE(SUM(f.quantity),0),COALESCE(SUM(f.quantity*f.price_atoms+f.fee_atoms),0)
                   FROM sandbox_order o JOIN sandbox_fill_observation f
                     ON f.order_id=o.order_id AND f.generation=o.generation
                   WHERE o.state='filled' OR EXISTS (
                        SELECT 1 FROM sandbox_cancellation c WHERE c.order_id=o.order_id
                          AND c.generation=o.generation
                   )"""
            ).fetchone()
            if observed != (quantity, plan.starting_atoms - cost):
                return False
            connection.execute(
                "INSERT INTO sandbox_account_observation VALUES (?, ?, ?)",
                (order_id, *observed),
            )
            return True

    def publish_quote(
        self, generation: str, atoms: int, received_ns: int, trade_ns: int
    ) -> None:
        with self._connect("rw") as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO sandbox_quote VALUES (1, ?, ?, ?, ?)",
                (generation, atoms, received_ns, trade_ns),
            )

    def publish_replay_cut(
        self, generation: str, atoms: int, event_ns: int, available_ns: int
    ) -> None:
        with self._connect("rw") as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            _, plan = self._configuration(connection)
            if plan.tape_revision is None or not 0 < event_ns <= available_ns:
                raise VenueRefused("Invalid causal cut for this execution environment")
            connection.execute(
                "INSERT INTO replay_decision_cut VALUES (?, ?, ?, ?)",
                (generation, atoms, event_ns, available_ns),
            )
            connection.execute(
                "INSERT OR REPLACE INTO sandbox_quote VALUES (1, ?, ?, ?, ?)",
                (generation, atoms, time_ns(), event_ns),
            )

    def invalidate_quote(self) -> None:
        with self._connect("rw") as connection, connection:
            connection.execute("DELETE FROM sandbox_quote")

    def claim_command(self, generation: str) -> SandboxCommand | None:
        with self._connect("rw") as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT order_id,action_id,quantity,limit_atoms FROM sandbox_order
                   WHERE state='queued' LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            self._fresh_quote(connection, generation)
            connection.execute(
                "UPDATE sandbox_order SET state='dispatching',generation=? WHERE order_id=?",
                (generation, row[0]),
            )
        return SandboxCommand(*row)

    def record_fill(
        self,
        generation: str,
        order_id: str,
        fill_id: str,
        lots: int,
        price: int,
        fee: int,
    ) -> None:
        with self._connect("rw") as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT quantity,limit_atoms,state,generation
                   FROM sandbox_order WHERE order_id=?""",
                (order_id,),
            ).fetchone()
            if row is None or row[3] != generation:
                raise VenueRefused("Sandbox fill has no original sender")
            observed = order_id, generation, lots, price, fee
            prior = connection.execute(
                """SELECT order_id,generation,quantity,price_atoms,fee_atoms
                   FROM sandbox_fill_observation WHERE fill_id=?""",
                (fill_id,),
            ).fetchone()
            if prior is not None:
                if prior != observed:
                    raise VenueRefused("Sandbox fill replay changed its terms")
                return
            if (
                row[2] not in ("dispatching", "unresolved")
                or not isinstance(fill_id, str)
                or not fill_id
                or type(lots) is not int
                or not 0 < lots <= row[0]
                or type(price) is not int
                or not 0 < price <= row[1]
                or type(fee) is not int
                or not 0 <= fee <= MAX_ATOMS
            ):
                raise VenueRefused("Sandbox fill conflicts with its original order")
            (booked,) = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) FROM sandbox_fill_observation WHERE order_id=?",
                (order_id,),
            ).fetchone()
            terminal = connection.execute(
                "SELECT filled_quantity FROM sandbox_cancellation WHERE order_id=?",
                (order_id,),
            ).fetchone()
            if booked + lots > (terminal[0] if terminal else row[0]):
                raise VenueRefused("Sandbox fills exceed the original order")
            connection.execute(
                "INSERT INTO sandbox_fill_observation VALUES (?, ?, ?, ?, ?, ?)",
                (fill_id, *observed),
            )
            if booked + lots != row[0]:
                return
            connection.execute(
                """UPDATE sandbox_order SET state='filled',fill_id=?,price_atoms=?,fee_atoms=?
                   WHERE order_id=?""",
                (fill_id, price, fee, order_id),
            )

    def record_cancellation(
        self, generation: str, order_id: str, event_id: str, filled_quantity: int | None
    ) -> None:
        with self._connect("rw") as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            order = connection.execute(
                """SELECT o.quantity,COALESCE((SELECT SUM(quantity) FROM sandbox_fill_observation
                         WHERE order_id=o.order_id AND generation=o.generation),0),o.state
                   FROM sandbox_order o WHERE order_id=? AND generation=?""",
                (order_id, generation),
            ).fetchone()
            if order is None or not isinstance(event_id, str) or not event_id:
                raise VenueRefused(
                    "Sandbox cancellation conflicts with the original order"
                )
            requested, booked, state = order
            if (
                state == "filled"
                and requested == booked
                and (
                    filled_quantity is None
                    or (type(filled_quantity) is int and filled_quantity == booked)
                )
            ):
                return
            if state in ("dispatching", "unresolved") and filled_quantity is None:
                self._mark_unresolved(connection, generation, order_id)
                return
            if (
                state not in ("dispatching", "unresolved")
                or type(filled_quantity) is not int
                or not booked <= filled_quantity < requested
            ):
                raise VenueRefused(
                    "Sandbox cancellation conflicts with the original order"
                )
            prior = connection.execute(
                "SELECT * FROM sandbox_cancellation WHERE order_id=? OR event_id=?",
                (order_id, event_id),
            ).fetchall()
            terms = (order_id, generation, event_id, filled_quantity)
            if prior:
                if prior != [terms]:
                    raise VenueRefused("Sandbox cancellation replay changed its terms")
                return
            connection.execute(
                "INSERT INTO sandbox_cancellation VALUES (?,?,?,?)", terms
            )
            self._mark_unresolved(connection, generation, order_id)

    def _mark_unresolved(
        self, connection: sqlite3.Connection, generation: str, order_id: str
    ) -> None:
        connection.execute(
            """UPDATE sandbox_order SET state='unresolved'
               WHERE order_id=? AND generation=? AND state='dispatching'""",
            (order_id, generation),
        )

    def mark_unresolved(self, generation: str, order_id: str) -> None:
        with self._connect("rw") as connection, connection:
            self._mark_unresolved(connection, generation, order_id)
