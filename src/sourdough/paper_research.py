"""Own the opening Agent's as-of inputs, finding and cited paper decision."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from time import time
from uuid import uuid4

from .abandoned_work import has_uncommitted_work
from .company import _connect, _identity
from .duty_run import PAPER_EXECUTION, DutyClaim, DutyRefused
from .first_duty import require_historical_claim, require_paper_authority
from .onboarding import has_hired_acceptance
from .paper_account import reopen_binding
from .paper_decision import PaperDecision
from .paper_execution import PaperOrderSizing
from .writer_lease import WriterLease


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(body: str) -> str:
    return sha256(body.encode()).hexdigest()


def _table(connection: Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


@dataclass(frozen=True)
class ResearchObservation:
    observation_id: str
    price_minor_units: int
    observed_at: int
    available_at: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.observation_id, str)
            or not 0 < len(self.observation_id.strip()) <= 128
            or not all(
                type(value) is int
                for value in (
                    self.price_minor_units,
                    self.observed_at,
                    self.available_at,
                )
            )
            or not 0 < self.price_minor_units <= 2**63 - 1
            or not 0 <= self.observed_at <= self.available_at <= 2**63 - 1
        ):
            raise DutyRefused("Invalid research observation")


def stage_research_inputs(store: Path, company_id: str, inputs: list[dict]) -> dict:
    if not isinstance(inputs, list) or not 2 <= len(inputs) <= 16:
        raise DutyRefused("Expected two to sixteen sealed observations")
    try:
        observations = [ResearchObservation(**item) for item in inputs]
    except TypeError as error:
        raise DutyRefused("Invalid research input fields") from error
    if len({item.observation_id for item in observations}) != len(observations):
        raise DutyRefused("Observation identities must be unique")
    account = reopen_binding(store, company_id).account
    body = _json(
        {
            "account_id": account.account_id,
            "instrument_rules_digest": account.instrument_rules_digest,
            "observations": [
                asdict(item)
                for item in sorted(
                    observations, key=lambda row: (row.observed_at, row.observation_id)
                )
            ],
        }
    )
    revision = _digest(body)
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if _identity(connection) != company_id:
            raise DutyRefused("Company identity mismatch")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS paper_research_plan (
                company_id TEXT PRIMARY KEY NOT NULL REFERENCES company(id),
                revision TEXT NOT NULL, inputs TEXT NOT NULL
            )"""
        )
        existing = connection.execute(
            "SELECT revision, inputs FROM paper_research_plan WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        if existing is not None and existing != (revision, body):
            raise DutyRefused("Research plan cannot be replaced")
        if existing is None:
            opening = connection.execute(
                """SELECT d.state FROM duty d JOIN founding_receipt f ON f.due_work_id = d.id
                   WHERE f.company_id = ?""",
                (company_id,),
            ).fetchone()
            if opening != ("due",):
                raise DutyRefused(
                    "Research inputs must be sealed before the opening duty"
                )
            connection.execute(
                "INSERT INTO paper_research_plan VALUES (?, ?, ?)",
                (company_id, revision, body),
            )
    return {"company_id": company_id, "revision": revision}


def requires_research(connection: Connection, company_id: str) -> bool:
    return (
        _table(connection, "paper_research_plan")
        and connection.execute(
            "SELECT 1 FROM paper_research_plan WHERE company_id = ?", (company_id,)
        ).fetchone()
        is not None
    )


@dataclass(frozen=True)
class ResearchDecisionReceipt:
    decision_id: str
    finding_id: str
    decided_at: int
    decision: PaperDecision

    @property
    def next_due_at(self) -> int:
        return self.decided_at + self.decision.next_check_seconds


@dataclass(frozen=True)
class ResearchTools:
    store: Path
    claim: DutyClaim
    quote_minor_units: int
    fee_bps: int
    exposure_limit_minor_units: int
    writer: WriterLease | None = None
    sizing: PaperOrderSizing | None = None

    def invoke(self, name: str, arguments: dict, *, now: int | None = None) -> dict:
        if self.writer is not None:
            self.writer.verify_for(self.store, self.claim.company_id)
        if not isinstance(arguments, dict):
            raise DutyRefused("Expected research tool arguments")
        clock = int(time()) if now is None else now
        with closing(_connect(self.store, "rw")) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            require_paper_authority(connection, self.claim)
            if name == "read_research_inputs" and not arguments:
                return self._snapshot(connection, clock)
            if name == "publish_research_finding":
                return self._publish(connection, arguments, clock)
            if name == "record_research_decision":
                return self._decide(connection, arguments, clock)
            raise DutyRefused("Tool is unavailable for research")

    def _snapshot(self, connection: Connection, now: int) -> dict:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS paper_research_snapshot (
                run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
                revision TEXT NOT NULL, inputs TEXT NOT NULL
            )"""
        )
        row = connection.execute(
            "SELECT revision, inputs FROM paper_research_snapshot WHERE run_id = ?",
            (self.claim.run_id,),
        ).fetchone()
        if row is not None:
            return json.loads(row[1]) | {"revision": row[0]}
        plan = connection.execute(
            "SELECT revision, inputs FROM paper_research_plan WHERE company_id = ?",
            (self.claim.company_id,),
        ).fetchone()
        if plan is None or _digest(plan[1]) != plan[0]:
            raise DutyRefused("Research plan is unavailable")
        inputs = json.loads(plan[1])
        inputs["observations"] = [
            item for item in inputs["observations"] if item["available_at"] <= now
        ]
        if len(inputs["observations"]) < 2:
            raise DutyRefused("Insufficient observations available at this cut")
        inputs.update(
            cut_at=now,
            source_plan_revision=plan[0],
            quote_minor_units=self.quote_minor_units,
            fee_bps=self.fee_bps,
            exposure_limit_minor_units=self.exposure_limit_minor_units,
        )
        if self.sizing is not None:
            inputs["order_sizing"] = asdict(self.sizing)
        body = _json(inputs)
        revision = _digest(body)
        connection.execute(
            "INSERT INTO paper_research_snapshot VALUES (?, ?, ?)",
            (self.claim.run_id, revision, body),
        )
        return inputs | {"revision": revision}

    def _publish(self, connection: Connection, finding: dict, now: int) -> dict:
        text_fields = (
            "snapshot_revision",
            "hypothesis",
            "start_id",
            "end_id",
            "uncertainty",
        )
        amount_fields = (
            "gross_change_minor_units",
            "round_trip_fee_minor_units",
            "net_change_minor_units",
        )
        if (
            set(finding) != set((*text_fields, *amount_fields))
            or not all(
                isinstance(finding[key], str) and 0 < len(finding[key].strip()) <= 2000
                for key in text_fields
            )
            or not all(type(finding[key]) is int for key in amount_fields)
        ):
            raise DutyRefused("Invalid structured research finding")
        if not _table(connection, "paper_research_snapshot"):
            raise DutyRefused("Read the sealed observations before publishing")
        snapshot = connection.execute(
            "SELECT revision, inputs FROM paper_research_snapshot WHERE run_id = ?",
            (self.claim.run_id,),
        ).fetchone()
        if (
            snapshot is None
            or snapshot[0] != finding["snapshot_revision"]
            or _digest(snapshot[1]) != snapshot[0]
        ):
            raise DutyRefused("Finding must cite this run's research input revision")
        inputs = json.loads(snapshot[1])
        observations = {item["observation_id"]: item for item in inputs["observations"]}
        start, end = (
            observations.get(finding["start_id"]),
            observations.get(finding["end_id"]),
        )
        if (
            start is None
            or end is None
            or start["observed_at"] >= end["observed_at"]
            or inputs["cut_at"] > now
        ):
            raise DutyRefused(
                "Finding requires two ordered observations available at its cut"
            )
        start_price, end_price = start["price_minor_units"], end["price_minor_units"]
        gross = end_price - start_price
        fee = sum(
            (price * inputs["fee_bps"] + 5000) // 10000
            for price in (start_price, end_price)
        )
        if tuple(finding[key] for key in amount_fields) != (gross, fee, gross - fee):
            raise DutyRefused("Finding arithmetic does not match the sealed inputs")
        body = _json(finding)
        revision = _digest(body)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS paper_research_finding (
                run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
                finding_id TEXT NOT NULL UNIQUE, revision TEXT NOT NULL,
                published_at INTEGER NOT NULL, content TEXT NOT NULL
            )"""
        )
        saved = connection.execute(
            "SELECT finding_id, revision, content FROM paper_research_finding WHERE run_id = ?",
            (self.claim.run_id,),
        ).fetchone()
        if saved is not None:
            if saved[1:] != (revision, body):
                raise DutyRefused("Finding conflicts with the published revision")
            return {"finding_id": saved[0], "revision": revision}
        finding_id = uuid4().hex
        connection.execute(
            "INSERT INTO paper_research_finding VALUES (?, ?, ?, ?, ?)",
            (self.claim.run_id, finding_id, revision, now, body),
        )
        return {"finding_id": finding_id, "revision": revision}

    def _decide(self, connection: Connection, proposal: dict, now: int) -> dict:
        if not _table(connection, "paper_research_finding"):
            raise DutyRefused("Publish a finding before recording the decision")
        published = connection.execute(
            "SELECT finding_id, revision, published_at, content FROM paper_research_finding WHERE run_id = ?",
            (self.claim.run_id,),
        ).fetchone()
        if (
            published is None
            or published[:2]
            != (proposal.get("finding_id"), proposal.get("finding_revision"))
            or published[2] > now
            or _digest(published[3]) != published[1]
        ):
            raise DutyRefused("Decision must cite the earlier finding from this run")
        fields = {
            key: value
            for key, value in proposal.items()
            if key not in ("finding_id", "finding_revision")
        }
        try:
            decision = PaperDecision(observation_revision=published[1], **fields)
        except TypeError as error:
            raise DutyRefused("Invalid research decision fields") from error
        body = _json(asdict(decision))
        connection.execute(
            """CREATE TABLE IF NOT EXISTS paper_research_decision (
                duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
                run_id TEXT NOT NULL REFERENCES duty_run(run_id),
                finding_id TEXT NOT NULL REFERENCES paper_research_finding(finding_id),
                decision_id TEXT NOT NULL UNIQUE, decided_at INTEGER NOT NULL, content TEXT NOT NULL
            )"""
        )
        saved = connection.execute(
            "SELECT run_id, finding_id, decision_id, content FROM paper_research_decision WHERE duty_id = ?",
            (self.claim.duty_id,),
        ).fetchone()
        if saved is not None:
            if (saved[0], saved[1], saved[3]) != (
                self.claim.run_id,
                published[0],
                body,
            ):
                raise DutyRefused(
                    "Decision conflicts with the original research decision"
                )
            return {"decision_id": saved[2]}
        decision_id = uuid4().hex
        connection.execute(
            "INSERT INTO paper_research_decision VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.claim.duty_id,
                self.claim.run_id,
                published[0],
                decision_id,
                now,
                body,
            ),
        )
        return {"decision_id": decision_id}


def _research_decision(
    connection: Connection, claim: DutyClaim
) -> ResearchDecisionReceipt | None:
    if not _table(connection, "paper_research_decision"):
        return None
    row = connection.execute(
        """SELECT d.decision_id, d.finding_id, d.decided_at, d.content, f.revision, f.content
           FROM paper_research_decision d
           JOIN paper_research_finding f ON f.finding_id=d.finding_id AND f.run_id=d.run_id
           JOIN duty_run original ON original.run_id=d.run_id AND original.duty_id=d.duty_id
           WHERE d.duty_id=? AND f.published_at<=d.decided_at
             AND (d.run_id=? OR (? AND original.grant_id=? AND original.purpose=?
                                 AND original.grant_scope='paper'))""",
        (
            claim.duty_id,
            claim.run_id,
            has_hired_acceptance(connection, claim),
            claim.grant_id,
            PAPER_EXECUTION,
        ),
    ).fetchone()
    if row is None:
        return None
    decision = PaperDecision(**json.loads(row[3]))
    if decision.observation_revision != row[4] or _digest(row[5]) != row[4]:
        raise DutyRefused("Research receipt does not match the original finding")
    return ResearchDecisionReceipt(*row[:3], decision)


def _pending_buy(connection: Connection, duty_id: str) -> bool:
    if not _table(connection, "paper_research_decision"):
        return False
    for table, sql in (
        (
            "hired_research_order_request",
            "SELECT 1 FROM hired_research_order_request WHERE duty_id=?",
        ),
        (
            "paper_order",
            """SELECT 1 FROM paper_order o JOIN duty_run r ON r.run_id=o.run_id
               WHERE r.duty_id=?""",
        ),
    ):
        if _table(connection, table) and connection.execute(sql, (duty_id,)).fetchone():
            return False
    return (
        connection.execute(
            """SELECT 1 FROM paper_research_decision p
               JOIN duty d ON d.id=p.duty_id AND d.state='running'
               WHERE d.id=? AND json_extract(p.content,'$.action')='buy'""",
            (duty_id,),
        ).fetchone()
        is not None
    )


def recoverable_hired_research(connection: Connection, duty_id: str) -> bool:
    return has_uncommitted_work(connection, duty_id) or _pending_buy(
        connection, duty_id
    )


def read_pending_hired_buy(
    writer: WriterLease, claim: DutyClaim
) -> ResearchDecisionReceipt | None:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as connection:
        connection.execute("BEGIN")
        require_paper_authority(connection, claim)
        if not has_hired_acceptance(connection, claim) or not _pending_buy(
            connection, claim.duty_id
        ):
            return None
        receipt = _research_decision(connection, claim)
        if receipt is None:
            raise DutyRefused("Saved buy does not match the original research")
        return receipt


def reopen_research_decision(
    store: Path, claim: DutyClaim
) -> ResearchDecisionReceipt | None:
    with closing(_connect(store, "ro")) as connection:
        connection.execute("BEGIN")
        if not requires_research(connection, claim.company_id):
            return None
        require_historical_claim(connection, claim)
        receipt = _research_decision(connection, claim)
        if receipt is None:
            raise DutyRefused("Opening duty has no committed research decision")
        return receipt


def find_unsubmitted_opening(
    store: Path, company_id: str, *, hired: bool = False
) -> tuple[DutyClaim, ResearchDecisionReceipt] | None:
    with closing(_connect(store, "ro")) as connection:
        connection.execute("BEGIN")
        if not _table(connection, "paper_research_decision"):
            return None
        unsubmitted = (
            "AND NOT EXISTS (SELECT 1 FROM paper_order o WHERE o.run_id = r.run_id)"
            if _table(connection, "paper_order")
            else ""
        )
        if hired and not _table(connection, "hire_onboarding"):
            return None
        ownership = (
            """JOIN hire_onboarding opening ON opening.first_duty_id = d.id
               AND opening.agent_id = f.id AND opening.state = 'accepted'"""
            if hired
            else """JOIN founding_receipt opening ON opening.due_work_id = d.id
               AND opening.company_id = f.company_id AND opening.founder_id = f.id
               AND opening.grant_id = r.grant_id"""
        )
        candidates = connection.execute(
            f"""SELECT f.id, r.grant_id, d.id, r.run_id, f.execution_profile
                FROM duty d
                JOIN agent f ON f.id = d.agent_id AND f.company_id = ?
                JOIN duty_run r ON r.duty_id = d.id AND r.is_current = 1
                JOIN paper_research_decision decision
                    ON decision.duty_id = d.id AND (decision.run_id = r.run_id OR ?)
                {ownership}
                WHERE d.state = 'running' {unsubmitted}
                ORDER BY d.rowid""",
            (company_id, hired),
        )
        for row in candidates:
            claim = DutyClaim(company_id, *row)
            require_paper_authority(connection, claim)
            receipt = _research_decision(connection, claim)
            if receipt is None:
                raise DutyRefused(
                    "Interrupted research decision has no matching finding"
                )
            decision = receipt.decision
            if (
                not hired
                or decision.action != "buy"
                or allows_hired_research_order(
                    connection,
                    claim,
                    receipt.decision_id,
                    decision.quantity,
                    decision.limit_minor_units,
                )
            ):
                return claim, receipt
    return None


def allows_research_order(
    connection: Connection, claim: DutyClaim, action_id: str, quantity: int, limit: int
) -> bool:
    if not requires_research(connection, claim.company_id):
        return True
    receipt = _research_decision(connection, claim)
    return receipt is not None and (
        receipt.decision_id,
        receipt.decision.action,
        receipt.decision.quantity,
        receipt.decision.limit_minor_units,
    ) == (action_id, "buy", quantity, limit)


def authorize_hired_research_order(
    writer: WriterLease, claim: DutyClaim, decision_id: str
) -> dict:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        require_paper_authority(connection, claim)
        receipt = _research_decision(connection, claim)
        if (
            not has_hired_acceptance(connection, claim)
            or not connection.execute(
                "SELECT 1 FROM onboarding_read WHERE run_id=?", (claim.run_id,)
            ).fetchone()
            or receipt is None
            or receipt.decision_id != decision_id
            or receipt.decision.action != "buy"
        ):
            raise DutyRefused(
                "A hired order must request the original authorized research decision"
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS hired_research_order_request (
                duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
                run_id TEXT NOT NULL REFERENCES duty_run(run_id),
                decision_id TEXT NOT NULL UNIQUE REFERENCES paper_research_decision(decision_id)
            )"""
        )
        terms = (claim.duty_id, claim.run_id, decision_id)
        connection.execute(
            "INSERT OR IGNORE INTO hired_research_order_request VALUES (?, ?, ?)", terms
        )
        saved = connection.execute(
            "SELECT duty_id,run_id,decision_id FROM hired_research_order_request WHERE duty_id=?",
            (claim.duty_id,),
        ).fetchone()
        if saved != terms:
            raise DutyRefused("Order request conflicts with its original receipt")
        return {"decision_id": decision_id, "disposition": "requested"}


def allows_hired_research_order(
    connection: Connection,
    claim: DutyClaim,
    decision_id: str,
    quantity: int,
    limit: int,
) -> bool:
    return (
        has_hired_acceptance(connection, claim)
        and _table(connection, "hired_research_order_request")
        and connection.execute(
            """SELECT 1 FROM hired_research_order_request
               WHERE duty_id=? AND run_id=? AND decision_id=?""",
            (claim.duty_id, claim.run_id, decision_id),
        ).fetchone()
        is not None
        and requires_research(connection, claim.company_id)
        and allows_research_order(connection, claim, decision_id, quantity, limit)
    )


def read_research_result(
    connection: Connection, company_id: str, finding_id: str
) -> dict:
    row = connection.execute(
        """SELECT f.revision, f.content, j.content, j.decided_at
           FROM paper_research_finding f
           JOIN paper_research_decision j ON j.finding_id = f.finding_id AND j.run_id = f.run_id
           JOIN duty_run r ON r.run_id = f.run_id JOIN duty d ON d.id = r.duty_id
           JOIN agent a ON a.id = d.agent_id
           WHERE f.finding_id = ? AND a.company_id = ?""",
        (finding_id, company_id),
    ).fetchone()
    if row is None or _digest(row[1]) != row[0]:
        raise DutyRefused("Committed research result is unavailable")
    decision = json.loads(row[2])
    if decision["observation_revision"] != row[0]:
        raise DutyRefused("Committed research decision has a different finding")
    return {
        "revision": row[0],
        "kind": "research_finding",
        "finding": json.loads(row[1]),
        "decision": decision,
        "decided_at": row[3],
    }
