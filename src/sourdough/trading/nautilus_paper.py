"""Split company admission from the independently witnessed sandbox execution."""

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, astuple, dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep, time_ns
from uuid import uuid4

from ..storage.db import connect_sqlite
from .execution import (
    PaperAccount,
    PaperIntent,
    PaperOrderSizing,
    ReplayAvailability,
    TERMINAL_OUTCOMES,
    TerminalOutcome,
    VenueCancellation,
    VenueExecution,
    VenuePosition,
    VenueRefused,
    require_separate_journal,
)
from .market_worker import SpotInstrumentTerms, journal_key
from .spot_cash import MAX_ATOMS, QUOTE_SCALE, SpotCashUnits


CONNECTOR = "nautilus-spot-sandbox-v1"
REPLAY_CONNECTOR = "nautilus-offline-spot-replay-v1"
QUOTE_MAX_AGE_NS = 15 * 10**9
_CANCELLATION_SCHEMA = """CREATE TABLE IF NOT EXISTS sandbox_cancellation (
    order_id TEXT PRIMARY KEY NOT NULL REFERENCES sandbox_order(order_id),
    generation TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, filled_quantity INTEGER NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'cancelled'
)"""
_FILL_TIME_SCHEMA = """CREATE TABLE IF NOT EXISTS sandbox_fill_time (
    fill_id TEXT PRIMARY KEY NOT NULL REFERENCES sandbox_fill_observation(fill_id),
    effective_ns INTEGER NOT NULL CHECK(effective_ns>0)
)"""
_STORE_QUOTE = """INSERT OR REPLACE INTO sandbox_quote
    (singleton,generation,price_atoms_per_lot,received_ns,exchange_trade_ns,event_ns,bid_atoms_per_lot)
    VALUES (1, ?, ?, ?, ?, ?, ?)"""


def current_quote_times(event_ns: int, received_ns: int, now_ns: int) -> bool:
    return (
        type(event_ns) is int
        and type(received_ns) is int
        and 0 < event_ns <= received_ns <= now_ns
        and now_ns - event_ns <= QUOTE_MAX_AGE_NS
    )


def _ensure_quote_witness(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(sandbox_quote)")}
    for column in ("event_ns", "bid_atoms_per_lot"):
        if column not in columns:
            connection.execute(f"ALTER TABLE sandbox_quote ADD COLUMN {column} INTEGER")


def _ensure_terminal_outcome(connection: sqlite3.Connection) -> None:
    connection.execute(_CANCELLATION_SCHEMA)
    if not _has_terminal_outcome(connection):
        connection.execute(
            "ALTER TABLE sandbox_cancellation ADD COLUMN outcome TEXT NOT NULL DEFAULT 'cancelled'"
        )


def _has_terminal_outcome(connection: sqlite3.Connection) -> bool:
    return any(
        row[1] == "outcome"
        for row in connection.execute("PRAGMA table_info(sandbox_cancellation)")
    )


@dataclass(frozen=True)
class SandboxPlan:
    company_id: str
    account_id: str
    terms: SpotInstrumentTerms
    starting_atoms: int
    fee_bps: int
    tape_revision: str | None = None
    slippage_bps: int = 0

    def __post_init__(self) -> None:
        if (
            not self.company_id
            or not self.account_id
            or type(self.starting_atoms) is not int
            or not 0 < self.starting_atoms <= MAX_ATOMS
            or type(self.fee_bps) is not int
            or not 0 <= self.fee_bps <= 1000
            or type(self.slippage_bps) is not int
            or not 0 <= self.slippage_bps <= 500
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
                "Expected a bounded virtual cash account with valid fee/slippage policy"
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


@dataclass(frozen=True)
class SandboxRestoreCut:
    balance: SandboxBalance
    order_ids: tuple[str, ...]
    fills: tuple[tuple[str, str, int, int, int], ...]


@dataclass(frozen=True)
class SandboxRiskCut:
    account: PaperAccount
    settled: SandboxRestoreCut
    generation: str
    observed_ns: int
    expires_ns: int


@dataclass(frozen=True)
class SandboxFillFact:
    fill_id: str
    order_id: str
    generation: str
    quantity: int
    price_atoms: int
    fee_atoms: int
    effective_ns: int | None


@dataclass(frozen=True)
class SandboxFillFence:
    count: int
    revision: str


@dataclass(frozen=True)
class SandboxValuationCut:
    account: PaperAccount
    balance: SandboxBalance
    quote_generation: str
    observed_ns: int
    quote_event_ns: int
    quote_received_ns: int
    supporting_trade_ns: int
    quote_units_per_whole: int
    base_units_per_lot: str
    bid_atoms_per_lot: int
    ask_atoms_per_lot: int
    fill_fence: SandboxFillFence


@dataclass(frozen=True)
class SandboxFeeEvidence:
    destination_id: str
    account_id: str
    order_id: str
    fill_id: str
    effective_ns: int
    currency: str
    units_per_whole: int
    fee_atoms: int


def _has_fill_times(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='sandbox_fill_time'"
        ).fetchone()
        is not None
    )


def _fill_event_time(connection: sqlite3.Connection, fill_id: str) -> int | None:
    if not _has_fill_times(connection):
        return None
    row = connection.execute(
        "SELECT effective_ns FROM sandbox_fill_time WHERE fill_id=?", (fill_id,)
    ).fetchone()
    return row[0] if row is not None else None


def _fill_facts(
    connection: sqlite3.Connection, maximum: int
) -> tuple[SandboxFillFact, ...]:
    if _has_fill_times(connection):
        rows = connection.execute(
            """SELECT fill.fill_id,order_id,generation,quantity,price_atoms,fee_atoms,effective_ns
               FROM sandbox_fill_observation fill LEFT JOIN sandbox_fill_time time
               ON fill.fill_id=time.fill_id ORDER BY fill.rowid LIMIT ?""",
            (maximum,),
        )
    else:
        rows = connection.execute(
            """SELECT fill_id,order_id,generation,quantity,price_atoms,fee_atoms,NULL
               FROM sandbox_fill_observation ORDER BY rowid LIMIT ?""",
            (maximum,),
        )
    return tuple(SandboxFillFact(*row) for row in rows)


def _fill_fence(fills: tuple[SandboxFillFact, ...]) -> SandboxFillFence:
    digest = sha256()
    for fill in fills:
        digest.update(json.dumps(astuple(fill), separators=(",", ":")).encode())
        digest.update(b"\n")
    return SandboxFillFence(len(fills), digest.hexdigest())


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
        connection = connect_sqlite(
            f"{self.journal.as_uri()}?mode={mode}",
            uri=True,
            timeout=2,
            autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,
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
                       received_ns INTEGER NOT NULL, exchange_trade_ns INTEGER NOT NULL,
                       event_ns INTEGER, bid_atoms_per_lot INTEGER
                   )"""
            )
            _ensure_quote_witness(connection)
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
            _ensure_terminal_outcome(connection)
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

    def _quote_observation(
        self,
        connection: sqlite3.Connection,
        generation: str | None = None,
        *,
        now_ns: int | None = None,
    ) -> tuple[dict, ReplayAvailability | None]:
        cursor = connection.execute("SELECT * FROM sandbox_quote")
        values = cursor.fetchone()
        row = (
            dict(zip((column[0] for column in cursor.description), values, strict=True))
            if values is not None
            else {}
        )
        now = time_ns() if now_ns is None else now_ns
        _, plan = self._configuration(connection)
        if (
            not row
            or (generation is not None and row["generation"] != generation)
            or (
                plan.tape_revision is None
                and not (
                    current_quote_times(row.get("event_ns"), row["received_ns"], now)
                    and 0 <= now - row["exchange_trade_ns"] <= QUOTE_MAX_AGE_NS
                )
            )
        ):
            raise VenueRefused(
                "Sandbox has no fresh quote and supporting exchange trade"
            )
        if plan.tape_revision is None:
            return row, None
        cut = connection.execute(
            """SELECT event_ns,available_ns FROM replay_decision_cut
               WHERE generation=? AND price_atoms_per_lot=? AND event_ns=?""",
            (row["generation"], row["price_atoms_per_lot"], row["exchange_trade_ns"]),
        ).fetchone()
        if cut is None or not 0 < cut[0] <= cut[1]:
            raise VenueRefused("Replay requires the current sealed availability cut")
        return row, ReplayAvailability(plan.tape_revision, *cut)

    def observe_account(self, account_id: str) -> PaperAccount:
        with self._connect("ro") as connection:
            return self._account(connection, account_id)

    def observe_quote(self, account_id: str) -> int:
        return self.observe_research_quote(account_id)[0]

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

    def observe_research_quote(
        self, account_id: str
    ) -> tuple[int, ReplayAvailability | None]:
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            self._account(connection, account_id)
            quote, cut = self._quote_observation(connection)
            return quote["price_atoms_per_lot"], cut

    def observe_valuation_cut(self, account_id: str) -> SandboxValuationCut:
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            account = self._account(connection, account_id)
            if account.connector_revision != CONNECTOR:
                raise VenueRefused("Historical replay is not a current valuation cut")
            _, plan = self._configuration(connection)
            observed_ns = time_ns()
            quote, _ = self._quote_observation(connection, now_ns=observed_ns)
            bid, ask = quote.get("bid_atoms_per_lot"), quote["price_atoms_per_lot"]
            if type(bid) is not int or not 0 < bid <= ask:
                raise VenueRefused("Current valuation requires an observed bid and ask")
            if not connection.execute(
                "SELECT 1 FROM sandbox_activation WHERE generation=?",
                (quote["generation"],),
            ).fetchone():
                raise VenueRefused("Valuation has no corroborated account activation")
            return SandboxValuationCut(
                account,
                self._checkpoint(connection, plan),
                quote["generation"],
                observed_ns,
                quote["event_ns"],
                quote["received_ns"],
                quote["exchange_trade_ns"],
                QUOTE_SCALE,
                plan.terms.quantity_increment,
                bid,
                ask,
                _fill_fence(_fill_facts(connection, -1)),
            )

    def read_fills_between(
        self, account: PaperAccount, start: SandboxFillFence, end: SandboxFillFence
    ) -> tuple[SandboxFillFact, ...]:
        if (
            type(start.count) is not int
            or type(end.count) is not int
            or not 0 <= start.count <= end.count < 2**63
        ):
            raise VenueRefused("Sandbox fill fences do not identify an ordered range")
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            if (
                self._account(connection, account.account_id) != account
                or account.connector_revision != CONNECTOR
            ):
                raise VenueRefused("Fill evidence did not come from the bound account")
            observed = _fill_facts(connection, end.count)
            if (
                _fill_fence(observed[: start.count]) != start
                or _fill_fence(observed) != end
            ):
                raise VenueRefused(
                    "Original venue fill evidence no longer matches the cut"
                )
            return observed[start.count :]

    def read_fenced_balance(
        self, account: PaperAccount, fence: SandboxFillFence
    ) -> SandboxBalance:
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            if (
                self._account(connection, account.account_id) != account
                or account.connector_revision != CONNECTOR
                or _fill_fence(_fill_facts(connection, -1)) != fence
            ):
                raise VenueRefused("Venue balance is not at the final valuation fence")
            _, plan = self._configuration(connection)
            return self._checkpoint(connection, plan)

    def observe_drained_account(self, account_id: str) -> PaperAccount | None:
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            account = self._account(connection, account_id)
            baseline = connection.execute(
                "SELECT 1 FROM sandbox_activation WHERE position_lots=0 AND cash_atoms=? LIMIT 1",
                (account.starting_minor_units,),
            ).fetchone()
            if (
                baseline is None
                or connection.execute(
                    """SELECT 1 FROM sandbox_order orders WHERE NOT EXISTS (
                       SELECT 1 FROM sandbox_account_observation observed
                       WHERE observed.order_id=orders.order_id
                         AND observed.position_lots=0 AND observed.cash_atoms=?
                   ) LIMIT 1""",
                    (account.starting_minor_units,),
                ).fetchone()
            ):
                return None
        return account

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
            _ensure_terminal_outcome(connection)
            _ensure_quote_witness(connection)
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

    def read_restore_cut(self, account_id: str) -> SandboxRestoreCut:
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            self._account(connection, account_id)
            return self._settled_cut(connection)

    def _settled_cut(self, connection: sqlite3.Connection) -> SandboxRestoreCut:
        _, plan = self._configuration(connection)
        if (
            plan.tape_revision is not None
            or connection.execute(
                "SELECT 1 FROM sandbox_order WHERE state='queued' LIMIT 1"
            ).fetchone()
        ):
            raise VenueRefused("Expected settled original paper orders")
        return SandboxRestoreCut(
            self._checkpoint(connection, plan),
            tuple(
                row[0]
                for row in connection.execute(
                    "SELECT order_id FROM sandbox_order ORDER BY order_id"
                )
            ),
            tuple(
                connection.execute(
                    """SELECT fill_id,order_id,quantity,price_atoms,fee_atoms
                       FROM sandbox_fill_observation ORDER BY fill_id"""
                )
            ),
        )

    def read_current_risk_cut(self, account_id: str) -> SandboxRiskCut:
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            account = self._account(connection, account_id)
            if account.connector_revision != CONNECTOR:
                raise VenueRefused(
                    "Risk resumption requires current paper market evidence"
                )
            self._quote_observation(connection)
            generation, event, trade = connection.execute(
                "SELECT generation,event_ns,exchange_trade_ns FROM sandbox_quote"
            ).fetchone()
            if not connection.execute(
                """SELECT 1 FROM sandbox_activation
                   WHERE generation=? AND venue_account_id='BINANCE-001'""",
                (generation,),
            ).fetchone():
                raise VenueRefused(
                    "Current quote generation has no witnessed SDK account"
                )
            return SandboxRiskCut(
                account,
                self._settled_cut(connection),
                generation,
                time_ns(),
                min(event, trade) + QUOTE_MAX_AGE_NS,
            )

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

    def _check_size(self, connection: sqlite3.Connection, intent: PaperIntent) -> None:
        _, plan = self._configuration(connection)
        SpotCashUnits(plan.terms).limit_order(intent.quantity, intent.limit_minor_units)

    def check_order_terms(self, account_id: str, intent: PaperIntent) -> PaperAccount:
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            account = self._account(connection, account_id)
            if account.instrument_rules_digest != intent.instrument_rules_digest:
                raise VenueRefused("Paper order uses different instrument terms")
            self._check_size(connection, intent)
        return account

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
                self._check_size(connection, intent)
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

    def read_fill_fee(self, account_id: str, fill_id: str) -> SandboxFeeEvidence | None:
        with self._connect("ro") as connection:
            connection.execute("BEGIN")
            account = self._account(connection, account_id)
            if account.connector_revision != CONNECTOR:
                return None
            effective_ns = _fill_event_time(connection, fill_id)
            if effective_ns is None:
                return None
            row = connection.execute(
                """SELECT f.order_id,f.fee_atoms
                   FROM sandbox_fill_observation f JOIN sandbox_order o USING(order_id)
                   WHERE f.fill_id=? AND f.generation=o.generation""",
                (fill_id,),
            ).fetchone()
            if row is None:
                raise VenueRefused("Fee evidence has no original sandbox fill")
            order_id, fee = row
            return SandboxFeeEvidence(
                account.destination_id,
                account_id,
                order_id,
                fill_id,
                effective_ns,
                account.currency,
                QUOTE_SCALE,
                fee,
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
            outcome = (
                "c.outcome" if _has_terminal_outcome(connection) else "'cancelled'"
            )
            row = connection.execute(
                f"""SELECT c.event_id,c.filled_quantity,{outcome}
                   FROM sandbox_cancellation c
                   JOIN sandbox_order o ON o.order_id=c.order_id AND o.generation=c.generation
                   WHERE c.order_id=? AND ((NOT ? AND {outcome}='cancelled') OR EXISTS (
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
        self,
        generation: str,
        atoms: int,
        received_ns: int,
        trade_ns: int,
        *,
        event_ns: int | None = None,
        bid_atoms_per_lot: int | None = None,
    ) -> None:
        if bid_atoms_per_lot is not None and (
            type(bid_atoms_per_lot) is not int or not 0 < bid_atoms_per_lot <= atoms
        ):
            raise VenueRefused("Sandbox quote has an invalid bid")
        with self._connect("rw") as connection, connection:
            connection.execute(
                _STORE_QUOTE,
                (
                    generation,
                    atoms,
                    received_ns,
                    trade_ns,
                    received_ns if event_ns is None else event_ns,
                    bid_atoms_per_lot,
                ),
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
                _STORE_QUOTE,
                (generation, atoms, available_ns, event_ns, event_ns, None),
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
            self._quote_observation(connection, generation)
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
        *,
        effective_ns: int | None = None,
    ) -> None:
        if effective_ns is not None and (
            type(effective_ns) is not int or not 0 < effective_ns < 2**63
        ):
            raise VenueRefused("Sandbox fill event time is invalid")
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
                if (
                    prior != observed
                    or _fill_event_time(connection, fill_id) != effective_ns
                ):
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
            if effective_ns is not None:
                connection.execute(_FILL_TIME_SCHEMA)
                connection.execute(
                    "INSERT INTO sandbox_fill_time VALUES (?,?)",
                    (fill_id, effective_ns),
                )
            if booked + lots != row[0]:
                return
            connection.execute(
                """UPDATE sandbox_order SET state='filled',fill_id=?,price_atoms=?,fee_atoms=?
                   WHERE order_id=?""",
                (fill_id, price, fee, order_id),
            )

    def record_cancellation(
        self,
        generation: str,
        order_id: str,
        event_id: str,
        filled_quantity: int | None,
        *,
        outcome: TerminalOutcome = "cancelled",
    ) -> None:
        with self._connect("rw") as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            order = connection.execute(
                """SELECT o.quantity,COALESCE((SELECT SUM(quantity) FROM sandbox_fill_observation
                         WHERE order_id=o.order_id AND generation=o.generation),0),o.state
                   FROM sandbox_order o WHERE order_id=? AND generation=?""",
                (order_id, generation),
            ).fetchone()
            if (
                order is None
                or not isinstance(event_id, str)
                or not event_id
                or outcome not in TERMINAL_OUTCOMES
            ):
                raise VenueRefused(
                    "Sandbox cancellation conflicts with the original order"
                )
            requested, booked, state = order
            if (
                outcome == "cancelled"
                and state == "filled"
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
                or (outcome in ("rejected", "denied") and filled_quantity != 0)
            ):
                raise VenueRefused(
                    "Sandbox cancellation conflicts with the original order"
                )
            prior = connection.execute(
                """SELECT order_id,generation,event_id,filled_quantity,outcome
                   FROM sandbox_cancellation WHERE order_id=? OR event_id=?""",
                (order_id, event_id),
            ).fetchall()
            terms = (order_id, generation, event_id, filled_quantity, outcome)
            if prior:
                if prior != [terms]:
                    raise VenueRefused("Sandbox cancellation replay changed its terms")
                return
            connection.execute(
                """INSERT INTO sandbox_cancellation
                   (order_id,generation,event_id,filled_quantity,outcome) VALUES (?,?,?,?,?)""",
                terms,
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
