"""Persist a later paper decision before admitting any order for it."""

import sqlite3
from contextlib import closing
from dataclasses import astuple, dataclass
from pathlib import Path
from uuid import uuid4

from .company import _connect
from .duty_run import DutyClaim, DutyRefused
from .paper_account import reopen_binding
from .paper_books import (
    read_book_observation,
    read_no_fill,
    read_position,
    resolved_order_sql,
)
from .storage.schema import is_domain_installed
from .trading.duty import FIRST_DUTY_SCOPE, PAPER_EXECUTION
from .work import (
    FollowUp,
    FollowUpClaim,
    ResultReference,
    _time,
    follow_up_state,
    require_result_read,
    reopen_follow_up,
    schedule_follow_up,
)


@dataclass(frozen=True)
class PaperDecision:
    call_id: str
    observation_revision: str
    rationale: str
    next_check_seconds: int
    action: str
    quantity: int | None = None
    limit_minor_units: int | None = None

    @classmethod
    def from_proposal(cls, proposal: dict) -> "PaperDecision":
        required = {
            "tool",
            "call_id",
            "observation_revision",
            "rationale",
            "next_check_seconds",
            "action",
        }
        if (
            not required <= set(proposal)
            or set(proposal) - required - {"quantity", "limit_minor_units"}
            or proposal["tool"] != "record_paper_decision"
        ):
            raise DutyRefused("Expected a scoped paper decision")
        terms = proposal.copy()
        del terms["tool"]
        return cls(**terms)

    def __post_init__(self) -> None:
        if (
            not all(
                isinstance(value, str) and 0 < len(value.strip()) <= maximum
                for value, maximum in (
                    (self.call_id, 128),
                    (self.observation_revision, 128),
                    (self.rationale, 2000),
                )
            )
            or type(self.next_check_seconds) is not int
            or not 60 <= self.next_check_seconds <= 86400
            or self.action not in ("buy", "hold", "investigate")
        ):
            raise DutyRefused("Invalid paper decision")
        terms = self.quantity, self.limit_minor_units
        if self.action == "buy":
            if not all(
                type(value) is int and 0 < value <= 2**63 - 1 for value in terms
            ):
                raise DutyRefused("Invalid order terms for paper decision")
        elif terms != (None, None):
            raise DutyRefused("A non-order decision cannot specify order terms")


@dataclass(frozen=True)
class PaperDecisionReceipt:
    decision_id: str
    decided_at: int
    decision: PaperDecision


def persist_paper_decision(
    store: Path, follow_up: FollowUpClaim, decision: PaperDecision, *, now: int
) -> PaperDecisionReceipt:
    _time(now)
    _time(now + decision.next_check_seconds)
    claim = follow_up.claim
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        state, scope = follow_up_state(connection, follow_up, PAPER_EXECUTION)
        if scope != FIRST_DUTY_SCOPE or follow_up.result.kind not in (
            "paper_position",
            "paper_no_fill",
            "paper_binding",
        ):
            raise DutyRefused("Duty is not authorized for this paper decision")
        if not is_domain_installed(connection, "paper"):
            raise DutyRefused("Paper domain is not enabled")
        prior = connection.execute(
            """SELECT decision_id, decided_at, call_id, result_revision, rationale,
                      next_check_seconds, action, quantity, limit_minor_units
               FROM paper_decision WHERE duty_id = ?""",
            (claim.duty_id,),
        ).fetchone()
        if prior is not None:
            if prior[2:] != astuple(decision):
                raise DutyRefused("Paper decision conflicts with the original receipt")
            return PaperDecisionReceipt(prior[0], prior[1], decision)
        if state != "running":
            raise DutyRefused("Duty cannot record a paper decision")
        require_result_read(connection, follow_up, decision.observation_revision, now)
        observation = read_book_observation(
            connection,
            claim.company_id,
            follow_up.result.kind,
            follow_up.result.resource_id,
        )
        if observation.revision != decision.observation_revision:
            raise DutyRefused(
                "Paper decision does not match the committed book observation"
            )
        receipt = PaperDecisionReceipt(uuid4().hex, now, decision)
        connection.execute(
            "INSERT INTO paper_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim.duty_id,
                claim.run_id,
                receipt.decision_id,
                now,
                *astuple(decision),
                observation.account_id,
                observation.instrument_rules_digest,
            ),
        )
    return receipt


def has_matching_paper_decision(
    connection: sqlite3.Connection,
    claim: DutyClaim,
    decision_id: str,
    account_id: str,
    instrument_rules_digest: str,
    quantity: int,
    limit_minor_units: int,
) -> bool:
    if not is_domain_installed(connection, "paper"):
        return False
    return connection.execute(
        """SELECT action, account_id, instrument_rules_digest, quantity, limit_minor_units
           FROM paper_decision WHERE duty_id = ? AND decision_id = ?""",
        (claim.duty_id, decision_id),
    ).fetchone() == (
        "buy",
        account_id,
        instrument_rules_digest,
        quantity,
        limit_minor_units,
    )


def reopen_paper_decision(
    store: Path, follow_up: FollowUpClaim
) -> PaperDecisionReceipt:
    with closing(_connect(store, "ro")) as connection:
        connection.execute("BEGIN")
        follow_up_state(connection, follow_up, PAPER_EXECUTION)
        if not is_domain_installed(connection, "paper"):
            raise DutyRefused("No original paper decision to recover")
        row = connection.execute(
            """SELECT decision_id, decided_at, call_id, result_revision, rationale,
                      next_check_seconds, action, quantity, limit_minor_units
               FROM paper_decision WHERE duty_id = ?""",
            (follow_up.claim.duty_id,),
        ).fetchone()
        if row is None:
            raise DutyRefused("No original paper decision to recover")
        return PaperDecisionReceipt(row[0], row[1], PaperDecision(*row[2:]))


def reopen_unsubmitted_decisions(
    store: Path, company_id: str, *, allow_buy: bool
) -> tuple[tuple[FollowUpClaim, PaperDecisionReceipt], ...]:
    with closing(_connect(store, "ro")) as connection:
        connection.execute("BEGIN")
        if not is_domain_installed(connection, "paper"):
            return ()
        account = reopen_binding(store, company_id).account
        rows = connection.execute(
            """SELECT a.id, r.grant_id, d.id, r.run_id, a.execution_profile
               FROM paper_decision decision
               JOIN duty_run r ON r.run_id = decision.run_id AND r.is_current = 1
               JOIN duty d ON d.id = decision.duty_id AND d.id = r.duty_id AND d.state = 'running'
               JOIN agent a ON a.id = d.agent_id AND a.company_id = ?
               WHERE (decision.action != 'buy' OR (? AND decision.account_id=?
                      AND decision.instrument_rules_digest=?))
                 AND NOT EXISTS (
                     SELECT 1 FROM paper_order o JOIN duty_run original ON original.run_id = o.run_id
                     WHERE original.duty_id = d.id
                 )
               ORDER BY decision.decided_at, d.id LIMIT 8""",
            (
                company_id,
                allow_buy,
                account.account_id,
                account.instrument_rules_digest,
            ),
        ).fetchall()
    pending = []
    for row in rows:
        work = reopen_follow_up(store, DutyClaim(company_id, *row))
        pending.append((work, reopen_paper_decision(store, work)))
    return tuple(pending)


def settle_paper_decision(
    store: Path,
    follow_up: FollowUpClaim,
    receipt: PaperDecisionReceipt,
    result: ResultReference,
) -> FollowUp:
    claim = follow_up.claim
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        state, scope = follow_up_state(connection, follow_up, PAPER_EXECUTION)
        recorded = connection.execute(
            """SELECT decision_id, decided_at, call_id, result_revision, rationale,
                      next_check_seconds, action, quantity, limit_minor_units
               FROM paper_decision WHERE duty_id = ?""",
            (claim.duty_id,),
        ).fetchone()
        if recorded != (
            receipt.decision_id,
            receipt.decided_at,
            *astuple(receipt.decision),
        ):
            raise DutyRefused("Unknown or changed paper decision receipt")
        if receipt.decision.action == "buy":
            observation = None
            if result.kind == "paper_no_fill":
                order = connection.execute(
                    """SELECT o.order_id FROM paper_order o JOIN duty_run r ON r.run_id=o.run_id
                       WHERE r.duty_id=? AND o.action_id=?""",
                    (claim.duty_id, receipt.decision_id),
                ).fetchone()
                if order == (result.resource_id,):
                    observation = read_no_fill(
                        connection, claim.company_id, result.resource_id
                    )
            elif result.kind == "paper_position":
                matching_fill = connection.execute(
                    f"""SELECT fill.fill_id FROM paper_fill fill
                       JOIN paper_order o ON o.order_id = fill.order_id
                       JOIN duty_run r ON r.run_id = o.run_id
                       WHERE r.duty_id = ? AND o.action_id = ? AND {resolved_order_sql(connection)}
                       ORDER BY fill.rowid DESC LIMIT 1""",
                    (claim.duty_id, receipt.decision_id),
                ).fetchone()
                if matching_fill == (result.resource_id,):
                    observation = read_position(
                        connection, claim.company_id, result.resource_id
                    )
            if observation is None:
                raise DutyRefused(
                    "The original order has no confirmed book result for settlement"
                )
            if (observation.revision, observation.available_at) != (
                result.revision,
                result.available_at,
            ):
                raise DutyRefused(
                    "The follow-up result does not match the booked order"
                )
        elif result != follow_up.result:
            raise DutyRefused("No new book result was produced by this decision")
        next_due = receipt.decided_at + receipt.decision.next_check_seconds
        if state == "settled":
            replay = connection.execute(
                """SELECT duty_id, due_at, purpose, result_kind, result_id, result_revision, available_at
                   FROM work_follow_up WHERE previous_duty_id = ?""",
                (claim.duty_id,),
            ).fetchone()
            if (
                replay is not None
                and replay[1:3] == (next_due, PAPER_EXECUTION)
                and ResultReference(*replay[3:]) == result
            ):
                return FollowUp(replay[0], next_due, result, PAPER_EXECUTION)
        if state != "running":
            raise DutyRefused("The paper duty cannot be settled")
        return schedule_follow_up(
            connection,
            claim,
            scope,
            result,
            next_due,
            purpose=PAPER_EXECUTION,
            allowed_purposes=(PAPER_EXECUTION,),
        )
