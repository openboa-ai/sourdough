"""Three-tier execution binding management, qualification receipts, and promotion/demotion."""

from __future__ import annotations

import json
from contextlib import closing
from sqlite3 import Connection
from time import time_ns
from typing import Any
from uuid import uuid4

from ..board_control import (
    BOARD_CONTROL_ROLE,
    AuthorizeBoard,
    BoardCommandOutcome,
    BoardControlConflict,
    BoardControlForbidden,
    BoardControlRefused,
    resolve_board_access,
)
from ..company import _identity
from ..storage.db import open_writer, write_tx
from ..storage.kernel import (
    CommandConflict,
    admit_command_receipt,
    append_audit_event,
)
from ..storage.schema import enable_domain_v1, is_domain_installed
from ..writer_lease import WriterLease
from .domain import (
    _SELECT_BINDING_COLS,
    DEMOTE_LIVE_ENVIRONMENT,
    ENV_AGENT_SANDBOX,
    ENV_EXTERNAL_TEST,
    ENV_LIVE,
    ENV_PAPER,
    EXECUTION_TIER_COMMAND,
    PROMOTE_EXECUTION_ENVIRONMENT,
    TIER_AGENT_SANDBOX,
    TIER_COMPANY_LIVE,
    TIER_COMPANY_PAPER,
    ExecutionBinding,
    ExecutionTierRefused,
    VenueQualificationReceipt,
    _require_bps,
    _require_positive_int,
    _require_safe_id,
    _require_sha256,
    _require_text,
    _row_to_binding,
    _sha256_json,
    _validate_credential_handle,
    build_promotion_payload_hash,
    compute_trading_core_digest,
)
from .ledger import (
    _assert_active_agent,
    _insert_book_valuation_in_tx,
    has_company_paper_soak_evidence,
)


def _require_board_control(
    db: Connection,
    company_id: str,
    *,
    operation: str,
    authorize: AuthorizeBoard | None,
    principal_hint: str | None,
) -> str:
    access = resolve_board_access(
        db,
        company_id,
        authorize_board=authorize,
        operation=operation,
        principal_hint=principal_hint,
    )
    if access.bootstrap_locked or access.principal_id is None:
        raise BoardControlForbidden("Board control authority is not bound")
    if access.role != BOARD_CONTROL_ROLE:
        raise BoardControlForbidden(
            "board_control role is required for execution environment promotion/demotion"
        )
    return access.principal_id


def open_agent_sandbox(
    writer: WriterLease,
    *,
    agent_id: str,
    account_id: str,
    currency: str = "USD",
    starting_minor_units: int = 10_000_000,
    exposure_limit_minor_units: int | None = None,
    fee_bps: int = 4,
    slippage_bps: int = 2,
    quote_source: str = "live_read_only",
    destination_id: str | None = None,
    connector_revision: str = "sourdough-agent-sandbox-v1",
    instrument_rules_digest: str = "spot-default-v1",
    binding_id: str | None = None,
) -> ExecutionBinding:
    """Open an isolated Tier 1 sandbox account for one active Agent."""
    writer.verify()
    _require_safe_id(agent_id, "agent_id")
    _require_safe_id(account_id, "account_id")
    _require_text(currency, "currency", max_len=32)
    _require_positive_int(starting_minor_units, "starting_minor_units")
    limit = (
        starting_minor_units
        if exposure_limit_minor_units is None
        else _require_positive_int(
            exposure_limit_minor_units, "exposure_limit_minor_units"
        )
    )
    if limit > starting_minor_units:
        raise ExecutionTierRefused(
            "exposure_limit_minor_units cannot exceed starting_minor_units"
        )
    _require_bps(fee_bps, "fee_bps", upper=1000)
    _require_bps(slippage_bps, "slippage_bps", upper=500)
    _require_text(quote_source, "quote_source", max_len=64)
    resolved_dest = (
        f"sandbox:{agent_id}:{account_id}"
        if destination_id is None
        else _require_safe_id(destination_id, "destination_id")
    )
    _require_text(connector_revision, "connector_revision", max_len=128)
    _require_text(instrument_rules_digest, "instrument_rules_digest", max_len=128)
    resolved_id = (
        f"sbx-{uuid4().hex[:16]}"
        if binding_id is None
        else _require_safe_id(binding_id, "binding_id")
    )

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        enable_domain_v1(db, "trading", company_id=writer.company_id, actor_id=agent_id)
        _assert_active_agent(db, writer.company_id, agent_id)

        existing = db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ? AND tier = ? AND owner_agent_id = ? AND status = 'active'""",
            (writer.company_id, TIER_AGENT_SANDBOX, agent_id),
        ).fetchone()
        if existing is not None:
            prior = _row_to_binding(existing)
            if (
                prior.account_id == account_id
                and prior.destination_id == resolved_dest
                and prior.currency == currency
                and prior.starting_minor_units == starting_minor_units
                and prior.exposure_limit_minor_units == limit
                and prior.fee_bps == fee_bps
                and prior.slippage_bps == slippage_bps
                and prior.quote_source == quote_source
            ):
                return prior
            raise ExecutionTierRefused(
                "Agent already has an active sandbox account; call reset_agent_sandbox first"
            )

        now = time_ns()
        db.execute(
            """INSERT INTO execution_environment_binding (
                   binding_id, company_id, tier, environment, owner_agent_id,
                   account_id, destination_id, connector_revision,
                   instrument_rules_digest, currency, starting_minor_units,
                   exposure_limit_minor_units, fee_bps, slippage_bps,
                   quote_source, credential_handle_ref, binding_revision,
                   book_name, book_role, status, created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '1', ?, 'active', 'active', ?, ?)""",
            (
                resolved_id,
                writer.company_id,
                TIER_AGENT_SANDBOX,
                ENV_AGENT_SANDBOX,
                agent_id,
                account_id,
                resolved_dest,
                connector_revision,
                instrument_rules_digest,
                currency,
                starting_minor_units,
                limit,
                fee_bps,
                slippage_bps,
                quote_source,
                account_id,
                now,
                now,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="execution_environment_binding",
            entity_id=resolved_id,
            event_type="agent_sandbox.opened",
            actor_id=agent_id,
            payload={
                "binding_id": resolved_id,
                "agent_id": agent_id,
                "account_id": account_id,
                "environment": ENV_AGENT_SANDBOX,
                "starting_minor_units": starting_minor_units,
                "fee_bps": fee_bps,
                "slippage_bps": slippage_bps,
                "quote_source": quote_source,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return ExecutionBinding(
            binding_id=resolved_id,
            company_id=writer.company_id,
            tier=TIER_AGENT_SANDBOX,
            environment=ENV_AGENT_SANDBOX,
            owner_agent_id=agent_id,
            account_id=account_id,
            destination_id=resolved_dest,
            connector_revision=connector_revision,
            instrument_rules_digest=instrument_rules_digest,
            currency=currency,
            starting_minor_units=starting_minor_units,
            exposure_limit_minor_units=limit,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            quote_source=quote_source,
            credential_handle_ref=None,
            status="active",
            created_at_ns=now,
            updated_at_ns=now,
            binding_revision="1",
            book_name=account_id,
            book_role="active",
        )


def reset_agent_sandbox(
    writer: WriterLease,
    *,
    agent_id: str,
    new_account_id: str,
    currency: str = "USD",
    starting_minor_units: int = 10_000_000,
    exposure_limit_minor_units: int | None = None,
    fee_bps: int = 4,
    slippage_bps: int = 2,
    quote_source: str = "live_read_only",
) -> ExecutionBinding:
    """Retire the Agent's active sandbox binding (preserving immutable history) and open a fresh one."""
    writer.verify()
    _require_safe_id(agent_id, "agent_id")
    _require_safe_id(new_account_id, "new_account_id")
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if is_domain_installed(db, "trading"):
            _assert_active_agent(db, writer.company_id, agent_id)
            now = time_ns()
            active = db.execute(
                """SELECT binding_id, account_id FROM execution_environment_binding
                   WHERE company_id = ? AND tier = ? AND owner_agent_id = ? AND status = 'active'""",
                (writer.company_id, TIER_AGENT_SANDBOX, agent_id),
            ).fetchone()
            if active is not None:
                if str(active[1]) == new_account_id:
                    raise ExecutionTierRefused(
                        "Resetting an agent sandbox requires a new account_id"
                    )
                db.execute(
                    """UPDATE execution_environment_binding
                       SET status = 'retired', book_role = 'retired', updated_at_ns = ?
                       WHERE binding_id = ?""",
                    (now, str(active[0])),
                )
                append_audit_event(
                    db,
                    company_id=writer.company_id,
                    domain="trading",
                    entity_type="execution_environment_binding",
                    entity_id=str(active[0]),
                    event_type="agent_sandbox.retired",
                    actor_id=agent_id,
                    payload={"retired_binding_id": str(active[0])},
                    recorded_at_ns=now,
                )
    return open_agent_sandbox(
        writer,
        agent_id=agent_id,
        account_id=new_account_id,
        currency=currency,
        starting_minor_units=starting_minor_units,
        exposure_limit_minor_units=exposure_limit_minor_units,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        quote_source=quote_source,
    )


def register_company_paper_tier(
    writer: WriterLease,
    *,
    account_id: str,
    destination_id: str,
    connector_revision: str,
    instrument_rules_digest: str,
    currency: str,
    starting_minor_units: int,
    exposure_limit_minor_units: int,
    fee_bps: int = 4,
    slippage_bps: int = 2,
    quote_source: str = "live_read_only",
    binding_id: str | None = None,
    book_name: str | None = None,
    strategy_release_id: str | None = None,
    binding_revision: str = "1",
) -> ExecutionBinding:
    """Register or idempotently verify an active Tier 2 company_paper book binding."""
    writer.verify()
    _require_safe_id(account_id, "account_id")
    _require_safe_id(destination_id, "destination_id")
    _require_text(connector_revision, "connector_revision", max_len=128)
    _require_text(instrument_rules_digest, "instrument_rules_digest", max_len=128)
    _require_text(currency, "currency", max_len=32)
    _require_positive_int(starting_minor_units, "starting_minor_units")
    _require_positive_int(exposure_limit_minor_units, "exposure_limit_minor_units")
    if exposure_limit_minor_units > starting_minor_units:
        raise ExecutionTierRefused(
            "exposure_limit_minor_units cannot exceed starting_minor_units"
        )
    _require_bps(fee_bps, "fee_bps", upper=1000)
    _require_bps(slippage_bps, "slippage_bps", upper=500)
    _require_text(quote_source, "quote_source", max_len=64)
    resolved_rev = _require_text(binding_revision, "binding_revision", max_len=128)
    resolved_book_name = _require_text(
        book_name if book_name is not None else account_id, "book_name", max_len=128
    )
    resolved_strategy = (
        None
        if strategy_release_id is None
        else _require_safe_id(strategy_release_id, "strategy_release_id")
    )
    resolved_id = (
        f"cpap-{uuid4().hex[:16]}"
        if binding_id is None
        else _require_safe_id(binding_id, "binding_id")
    )

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        enable_domain_v1(
            db, "trading", company_id=writer.company_id, actor_id="trading_accounts"
        )
        existing = db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ? AND tier = ? AND account_id = ? AND status = 'active'""",
            (writer.company_id, TIER_COMPANY_PAPER, account_id),
        ).fetchone()
        if existing is not None:
            prior = _row_to_binding(existing)
            if (
                prior.account_id == account_id
                and prior.destination_id == destination_id
                and prior.connector_revision == connector_revision
                and prior.instrument_rules_digest == instrument_rules_digest
                and prior.currency == currency
                and prior.starting_minor_units == starting_minor_units
                and prior.exposure_limit_minor_units == exposure_limit_minor_units
                and prior.fee_bps == fee_bps
                and prior.slippage_bps == slippage_bps
            ):
                if prior.book_name is None or prior.book_role is None:
                    now = time_ns()
                    db.execute(
                        """UPDATE execution_environment_binding
                           SET book_name = COALESCE(book_name, ?),
                               strategy_release_id = COALESCE(strategy_release_id, ?),
                               book_role = COALESCE(book_role, 'active'),
                               updated_at_ns = ?
                           WHERE binding_id = ?""",
                        (resolved_book_name, resolved_strategy, now, prior.binding_id),
                    )
                return prior
            raise ExecutionTierRefused(
                "An active company_paper book binding already exists for this account_id with different terms"
            )
        now = time_ns()
        db.execute(
            """INSERT INTO execution_environment_binding (
                   binding_id, company_id, tier, environment, owner_agent_id,
                   account_id, destination_id, connector_revision,
                   instrument_rules_digest, currency, starting_minor_units,
                   exposure_limit_minor_units, fee_bps, slippage_bps,
                   quote_source, credential_handle_ref, binding_revision,
                   book_name, strategy_release_id, book_role,
                   shadow_of_live_binding_id, nominated_question_id,
                   status, created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'active', NULL, NULL, 'active', ?, ?)""",
            (
                resolved_id,
                writer.company_id,
                TIER_COMPANY_PAPER,
                ENV_PAPER,
                account_id,
                destination_id,
                connector_revision,
                instrument_rules_digest,
                currency,
                starting_minor_units,
                exposure_limit_minor_units,
                fee_bps,
                slippage_bps,
                quote_source,
                resolved_rev,
                resolved_book_name,
                resolved_strategy,
                now,
                now,
            ),
        )
        new_binding = ExecutionBinding(
            binding_id=resolved_id,
            company_id=writer.company_id,
            tier=TIER_COMPANY_PAPER,
            environment=ENV_PAPER,
            owner_agent_id=None,
            account_id=account_id,
            destination_id=destination_id,
            connector_revision=connector_revision,
            instrument_rules_digest=instrument_rules_digest,
            currency=currency,
            starting_minor_units=starting_minor_units,
            exposure_limit_minor_units=exposure_limit_minor_units,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            quote_source=quote_source,
            credential_handle_ref=None,
            status="active",
            created_at_ns=now,
            updated_at_ns=now,
            binding_revision=resolved_rev,
            book_name=resolved_book_name,
            strategy_release_id=resolved_strategy,
            book_role="active",
        )
        _insert_book_valuation_in_tx(
            db,
            new_binding,
            label="Start",
            source="book_open",
            recorded_at_ns=now,
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="execution_environment_binding",
            entity_id=resolved_id,
            event_type="execution_tier.company_paper_registered",
            actor_id="trading_accounts",
            payload={
                "binding_id": resolved_id,
                "account_id": account_id,
                "book_name": resolved_book_name,
                "strategy_release_id": resolved_strategy,
                "destination_id": destination_id,
                "fee_bps": fee_bps,
                "slippage_bps": slippage_bps,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return new_binding


def open_company_paper_book(
    writer: WriterLease,
    *,
    account_id: str,
    book_name: str,
    strategy_release_id: str | None = None,
    destination_id: str = "company-paper-sim",
    connector_revision: str = "company-paper-sim-v1",
    instrument_rules_digest: str = "btc-usdt-spot-v1",
    currency: str = "USD",
    starting_minor_units: int = 100_000_000,
    exposure_limit_minor_units: int | None = None,
    fee_bps: int = 4,
    slippage_bps: int = 2,
    quote_source: str = "live_read_only",
    binding_id: str | None = None,
    binding_revision: str = "1",
) -> ExecutionBinding:
    """Open an isolated concurrent Company Paper Book with its own simulated ledger."""
    exp_limit = (
        starting_minor_units // 2
        if exposure_limit_minor_units is None
        else exposure_limit_minor_units
    )
    return register_company_paper_tier(
        writer,
        account_id=account_id,
        destination_id=destination_id,
        connector_revision=connector_revision,
        instrument_rules_digest=instrument_rules_digest,
        currency=currency,
        starting_minor_units=starting_minor_units,
        exposure_limit_minor_units=exp_limit,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        quote_source=quote_source,
        binding_id=binding_id,
        book_name=book_name,
        strategy_release_id=strategy_release_id,
        binding_revision=binding_revision,
    )


def record_venue_qualification_receipt(
    writer: WriterLease,
    *,
    receipt_id: str,
    adapter_revision: str,
    environment: str,
    destination_id: str,
    instrument_rules_digest: str,
    core_digest: str,
    verdict: str,
    cases_passed: int,
    cases_failed: int,
    cases_unrun: int,
    evidence_sha256: str,
) -> VenueQualificationReceipt:
    """Seal an immutable venue qualification receipt (required for external_test -> live promotion)."""
    writer.verify()
    _require_safe_id(receipt_id, "receipt_id")
    _require_text(adapter_revision, "adapter_revision", max_len=128)
    if environment not in (ENV_PAPER, ENV_EXTERNAL_TEST):
        raise ExecutionTierRefused(
            "Qualification receipt environment must be 'paper' or 'external_test'"
        )
    _require_safe_id(destination_id, "destination_id")
    _require_text(instrument_rules_digest, "instrument_rules_digest", max_len=128)
    _require_sha256(core_digest, "core_digest")
    if verdict not in ("PASS", "FAIL", "INCOMPLETE", "BLOCKED"):
        raise ExecutionTierRefused("Invalid qualification verdict")
    for count, label in (
        (cases_passed, "cases_passed"),
        (cases_failed, "cases_failed"),
        (cases_unrun, "cases_unrun"),
    ):
        if type(count) is not int or count < 0:
            raise ExecutionTierRefused(f"{label} must be a non-negative integer")
    if verdict == "PASS" and (cases_passed <= 0 or cases_failed != 0):
        raise ExecutionTierRefused(
            "PASS qualification verdict requires cases_passed > 0 and cases_failed == 0"
        )
    _require_sha256(evidence_sha256, "evidence_sha256")

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        enable_domain_v1(
            db, "trading", company_id=writer.company_id, actor_id="venue_qualification"
        )
        existing = db.execute(
            """SELECT receipt_id, company_id, adapter_revision, environment,
                      destination_id, instrument_rules_digest, core_digest,
                      verdict, cases_passed, cases_failed, cases_unrun,
                      evidence_sha256, recorded_at_ns
               FROM venue_qualification_receipt
               WHERE receipt_id = ?""",
            (receipt_id,),
        ).fetchone()
        if existing is not None:
            prior = VenueQualificationReceipt(
                receipt_id=str(existing[0]),
                company_id=str(existing[1]),
                adapter_revision=str(existing[2]),
                environment=str(existing[3]),
                destination_id=str(existing[4]),
                instrument_rules_digest=str(existing[5]),
                core_digest=str(existing[6]),
                verdict=str(existing[7]),
                cases_passed=int(existing[8]),
                cases_failed=int(existing[9]),
                cases_unrun=int(existing[10]),
                evidence_sha256=str(existing[11]),
                recorded_at_ns=int(existing[12]),
            )
            if (
                prior.company_id != writer.company_id
                or prior.adapter_revision != adapter_revision
                or prior.environment != environment
                or prior.destination_id != destination_id
                or prior.instrument_rules_digest != instrument_rules_digest
                or prior.core_digest != core_digest
                or prior.verdict != verdict
                or prior.cases_passed != cases_passed
                or prior.cases_failed != cases_failed
                or prior.cases_unrun != cases_unrun
                or prior.evidence_sha256 != evidence_sha256
            ):
                raise ExecutionTierRefused(
                    "Qualification receipt ID conflicts with previously sealed record"
                )
            return prior

        now = time_ns()
        db.execute(
            """INSERT INTO venue_qualification_receipt (
                   receipt_id, company_id, adapter_revision, environment,
                   destination_id, instrument_rules_digest, core_digest,
                   verdict, cases_passed, cases_failed, cases_unrun,
                   evidence_sha256, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id,
                writer.company_id,
                adapter_revision,
                environment,
                destination_id,
                instrument_rules_digest,
                core_digest,
                verdict,
                cases_passed,
                cases_failed,
                cases_unrun,
                evidence_sha256,
                now,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="venue_qualification_receipt",
            entity_id=receipt_id,
            event_type="venue_qualification.recorded",
            actor_id="venue_qualification",
            payload={
                "receipt_id": receipt_id,
                "adapter_revision": adapter_revision,
                "environment": environment,
                "verdict": verdict,
                "cases_passed": cases_passed,
                "cases_failed": cases_failed,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return VenueQualificationReceipt(
            receipt_id=receipt_id,
            company_id=writer.company_id,
            adapter_revision=adapter_revision,
            environment=environment,
            destination_id=destination_id,
            instrument_rules_digest=instrument_rules_digest,
            core_digest=core_digest,
            verdict=verdict,
            cases_passed=cases_passed,
            cases_failed=cases_failed,
            cases_unrun=cases_unrun,
            evidence_sha256=evidence_sha256,
            recorded_at_ns=now,
        )


def register_live_candidate_binding(
    writer: WriterLease,
    *,
    binding_id: str,
    account_id: str,
    destination_id: str,
    connector_revision: str,
    instrument_rules_digest: str,
    currency: str,
    starting_minor_units: int,
    exposure_limit_minor_units: int,
    credential_handle_ref: str,
    fee_bps: int = 4,
    slippage_bps: int = 0,
    quote_source: str = "live_exchange",
    binding_revision: str = "1",
) -> ExecutionBinding:
    """Register a dormant (status='registered') Tier 3 company_live candidate binding."""
    writer.verify()
    _require_safe_id(binding_id, "binding_id")
    _require_safe_id(account_id, "account_id")
    _require_safe_id(destination_id, "destination_id")
    _require_text(connector_revision, "connector_revision", max_len=128)
    _require_text(instrument_rules_digest, "instrument_rules_digest", max_len=128)
    _require_text(currency, "currency", max_len=32)
    _require_positive_int(starting_minor_units, "starting_minor_units")
    _require_positive_int(exposure_limit_minor_units, "exposure_limit_minor_units")
    if exposure_limit_minor_units > starting_minor_units:
        raise ExecutionTierRefused(
            "exposure_limit_minor_units cannot exceed starting_minor_units"
        )
    _require_bps(fee_bps, "fee_bps", upper=1000)
    _require_bps(slippage_bps, "slippage_bps", upper=500)
    _require_text(quote_source, "quote_source", max_len=64)
    resolved_rev = _require_text(binding_revision, "binding_revision", max_len=128)
    handle = _validate_credential_handle(
        credential_handle_ref, environment=ENV_LIVE, required=True
    )

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        enable_domain_v1(
            db, "trading", company_id=writer.company_id, actor_id="trading_accounts"
        )

        collision = db.execute(
            """SELECT 1 FROM execution_environment_binding
               WHERE company_id = ? AND environment != 'live'
                 AND (account_id = ? OR destination_id = ? OR credential_handle_ref = ?)
               LIMIT 1""",
            (writer.company_id, account_id, destination_id, handle),
        ).fetchone()
        if collision is not None:
            raise ExecutionTierRefused(
                "Live candidate binding cannot reuse a paper/sandbox account, destination, or credential handle"
            )

        existing = db.execute(
            f"{_SELECT_BINDING_COLS} WHERE binding_id = ?",
            (binding_id,),
        ).fetchone()
        if existing is not None:
            prior = _row_to_binding(existing)
            if (
                prior.company_id == writer.company_id
                and prior.tier == TIER_COMPANY_LIVE
                and prior.account_id == account_id
                and prior.destination_id == destination_id
                and prior.connector_revision == connector_revision
                and prior.instrument_rules_digest == instrument_rules_digest
                and prior.credential_handle_ref == handle
                and prior.exposure_limit_minor_units == exposure_limit_minor_units
            ):
                return prior
            raise ExecutionTierRefused(
                "Live candidate binding_id conflicts with existing binding"
            )

        now = time_ns()
        db.execute(
            """INSERT INTO execution_environment_binding (
                   binding_id, company_id, tier, environment, owner_agent_id,
                   account_id, destination_id, connector_revision,
                   instrument_rules_digest, currency, starting_minor_units,
                   exposure_limit_minor_units, fee_bps, slippage_bps,
                   quote_source, credential_handle_ref, binding_revision,
                   status, created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?)""",
            (
                binding_id,
                writer.company_id,
                TIER_COMPANY_LIVE,
                ENV_LIVE,
                account_id,
                destination_id,
                connector_revision,
                instrument_rules_digest,
                currency,
                starting_minor_units,
                exposure_limit_minor_units,
                fee_bps,
                slippage_bps,
                quote_source,
                handle,
                resolved_rev,
                now,
                now,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="execution_environment_binding",
            entity_id=binding_id,
            event_type="execution_tier.live_candidate_registered",
            actor_id="trading_accounts",
            payload={
                "binding_id": binding_id,
                "account_id": account_id,
                "destination_id": destination_id,
                "connector_revision": connector_revision,
                "credential_handle_ref": handle,
                "exposure_limit_minor_units": exposure_limit_minor_units,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return ExecutionBinding(
            binding_id=binding_id,
            company_id=writer.company_id,
            tier=TIER_COMPANY_LIVE,
            environment=ENV_LIVE,
            owner_agent_id=None,
            account_id=account_id,
            destination_id=destination_id,
            connector_revision=connector_revision,
            instrument_rules_digest=instrument_rules_digest,
            currency=currency,
            starting_minor_units=starting_minor_units,
            exposure_limit_minor_units=exposure_limit_minor_units,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            quote_source=quote_source,
            credential_handle_ref=handle,
            status="registered",
            created_at_ns=now,
            updated_at_ns=now,
            binding_revision=resolved_rev,
        )


def promote_execution_environment(
    writer: WriterLease,
    *,
    command_id: str,
    charter_revision: Any,
    target_binding_id: Any,
    qualification_receipt_id: Any,
    expected_payload_hash: Any,
    reason: Any,
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
    expected_core_digest: str | None = None,
) -> BoardCommandOutcome:
    """Server-side fail-closed Board promotion gate activating Tier 3 company_live."""
    writer.verify()
    try:
        cmd_id = _require_safe_id(command_id, "command_id")
        charter = _require_text(charter_revision, "charter_revision", max_len=128)
        target_id = _require_safe_id(target_binding_id, "target_binding_id")
        qual_id = _require_safe_id(qualification_receipt_id, "qualification_receipt_id")
        exp_hash = _require_sha256(expected_payload_hash, "expected_payload_hash")
        clean_reason = _require_text(reason, "reason", max_len=1000)
    except ExecutionTierRefused as error:
        raise BoardControlRefused(str(error)) from error

    core_digest = (
        compute_trading_core_digest()
        if expected_core_digest is None
        else expected_core_digest
    )
    preconditions = {
        "charter_revision": charter,
        "target_binding_id": target_id,
        "qualification_receipt_id": qual_id,
        "expected_payload_hash": exp_hash,
    }
    payload_sha = _sha256_json(
        {
            "command_type": PROMOTE_EXECUTION_ENVIRONMENT,
            "preconditions": preconditions,
            "reason": clean_reason,
        }
    )

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        actor_id = _require_board_control(
            db,
            writer.company_id,
            operation=PROMOTE_EXECUTION_ENVIRONMENT,
            authorize=authorize,
            principal_hint=principal_hint,
        )

        existing_receipt = db.execute(
            """SELECT company_id, command_id, actor_id, peer_pid, command_type,
                      contract_version, payload_sha256, preconditions_json, state,
                      denial_code, result_json, applied_seq, created_at_ns, finalized_at_ns
               FROM command_receipt
               WHERE company_id = ? AND command_id = ?""",
            (writer.company_id, cmd_id),
        ).fetchone()
        if existing_receipt is not None:
            try:
                replayed_receipt, _ = admit_command_receipt(
                    db,
                    company_id=writer.company_id,
                    command_id=cmd_id,
                    actor_id=actor_id,
                    peer_pid=peer_pid,
                    command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                    contract_version=EXECUTION_TIER_COMMAND,
                    payload_sha256=payload_sha,
                    preconditions=preconditions,
                    state=str(existing_receipt[8]),
                )
            except CommandConflict as error:
                raise BoardControlConflict(str(error)) from error
            saved_result = (
                json.loads(replayed_receipt.result_json)
                if replayed_receipt.result_json
                else {}
            )
            return BoardCommandOutcome(
                command_id=replayed_receipt.command_id,
                command_type=replayed_receipt.command_type,
                state=replayed_receipt.state,
                disposition="applied"
                if replayed_receipt.state == "applied"
                else "denied",
                denial_code=replayed_receipt.denial_code,
                effective_revision=int(replayed_receipt.applied_seq or 0),
                recorded_at_ns=replayed_receipt.created_at_ns,
                peer_pid=replayed_receipt.peer_pid,
                result=saved_result,
            )

        if not is_domain_installed(db, "trading"):
            raise BoardControlRefused(
                "Trading domain must be active with paper soak evidence before live promotion"
            )

        current_charter_row = db.execute(
            "SELECT charter_revision FROM company WHERE id = ?", (writer.company_id,)
        ).fetchone()
        current_charter = str(current_charter_row[0]) if current_charter_row else ""

        target_row = db.execute(
            f"{_SELECT_BINDING_COLS} WHERE company_id = ? AND binding_id = ?",
            (writer.company_id, target_id),
        ).fetchone()
        if target_row is None:
            raise BoardControlRefused("Target live candidate binding was not found")
        target = _row_to_binding(target_row)
        if target.tier != TIER_COMPANY_LIVE or target.environment != ENV_LIVE:
            raise BoardControlRefused(
                "Promotion target must be a company_live binding with environment='live'"
            )
        if target.status not in ("registered", "suspended"):
            raise BoardControlRefused(
                f"Cannot promote target live binding in status '{target.status}'"
            )

        actual_hash = build_promotion_payload_hash(
            company_id=writer.company_id,
            charter_revision=current_charter,
            target_binding=target,
            qualification_receipt_id=qual_id,
        )
        now = time_ns()
        if charter != current_charter or exp_hash != actual_hash:
            denied_result = {
                "target_binding_id": target_id,
                "expected_charter_revision": current_charter,
                "actual_payload_hash": actual_hash,
            }
            seq = append_audit_event(
                db,
                company_id=writer.company_id,
                domain="trading",
                entity_type="execution_environment_binding",
                entity_id=target_id,
                event_type="execution_tier.promote_denied_conflict",
                actor_id=actor_id,
                command_id=cmd_id,
                payload=denied_result,
                recorded_at_ns=now,
            )
            admit_command_receipt(
                db,
                company_id=writer.company_id,
                command_id=cmd_id,
                actor_id=actor_id,
                peer_pid=peer_pid,
                command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                contract_version=EXECUTION_TIER_COMMAND,
                payload_sha256=payload_sha,
                preconditions=preconditions,
                state="denied",
                denial_code="conflicting_revision",
                result=denied_result,
                applied_seq=seq,
                now_ns=now,
            )
            writer.verify()
            return BoardCommandOutcome(
                command_id=cmd_id,
                command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                state="denied",
                disposition="denied",
                denial_code="conflicting_revision",
                effective_revision=seq,
                recorded_at_ns=now,
                peer_pid=peer_pid,
                result=denied_result,
            )

        if not has_company_paper_soak_evidence(db, writer.company_id):
            raise BoardControlRefused(
                "Company paper soak evidence (active paper binding plus observed fill, valuation, or statement) is required before live promotion"
            )

        qual_row = db.execute(
            """SELECT adapter_revision, environment, instrument_rules_digest,
                      core_digest, verdict, cases_passed, cases_failed, cases_unrun
               FROM venue_qualification_receipt
               WHERE company_id = ? AND receipt_id = ?""",
            (writer.company_id, qual_id),
        ).fetchone()
        if qual_row is None:
            raise BoardControlRefused(
                "External testnet venue qualification receipt is missing"
            )
        if (
            str(qual_row[1]) != ENV_EXTERNAL_TEST
            or str(qual_row[4]) != "PASS"
            or int(qual_row[5]) <= 0
            or int(qual_row[6]) != 0
            or int(qual_row[7]) != 0
        ):
            raise BoardControlRefused(
                "Live promotion requires a PASS external_test qualification receipt with zero failures or unrun cases"
            )
        if str(qual_row[0]) != target.connector_revision:
            raise BoardControlRefused(
                "Qualification receipt adapter_revision does not match target live binding"
            )
        if str(qual_row[2]) != target.instrument_rules_digest:
            raise BoardControlRefused(
                "Qualification receipt instrument_rules_digest does not match target live binding"
            )
        if str(qual_row[3]) != core_digest:
            raise BoardControlRefused(
                "Qualification receipt core_digest does not match current trading core"
            )

        try:
            _validate_credential_handle(
                target.credential_handle_ref, environment=ENV_LIVE, required=True
            )
        except ExecutionTierRefused as error:
            raise BoardControlRefused(str(error)) from error

        already_live = db.execute(
            """SELECT binding_id FROM execution_environment_binding
               WHERE company_id = ? AND tier = ? AND status = 'active'""",
            (writer.company_id, TIER_COMPANY_LIVE),
        ).fetchone()
        if already_live is not None:
            raise BoardControlRefused(
                f"Another company_live binding ({already_live[0]}) is already active; demote it first"
            )

        paper_row = db.execute(
            """SELECT binding_id FROM execution_environment_binding
               WHERE company_id = ? AND tier = ? AND status = 'active'""",
            (writer.company_id, TIER_COMPANY_PAPER),
        ).fetchone()
        from_binding_id = None if paper_row is None else str(paper_row[0])

        db.execute(
            """UPDATE execution_environment_binding
               SET status = 'active', updated_at_ns = ?
               WHERE binding_id = ?""",
            (now, target_id),
        )
        has_live_val = db.execute(
            "SELECT 1 FROM trading_book_valuation WHERE binding_id = ? LIMIT 1",
            (target_id,),
        ).fetchone()
        if has_live_val is None:
            _insert_book_valuation_in_tx(
                db,
                target,
                label="Live Launch",
                source="live_launch",
                recorded_at_ns=now,
            )
        promotion_id = f"promo-{uuid4().hex[:16]}"
        result_body = {
            "promotion_id": promotion_id,
            "action": "promote_live",
            "from_binding_id": from_binding_id,
            "to_binding_id": target_id,
            "qualification_receipt_id": qual_id,
            "company_paper_preserved": True,
            "agent_sandboxes_preserved": True,
        }
        seq = append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="execution_environment_binding",
            entity_id=target_id,
            event_type="execution_tier.promoted_live",
            actor_id=actor_id,
            command_id=cmd_id,
            payload=result_body,
            recorded_at_ns=now,
        )
        admit_command_receipt(
            db,
            company_id=writer.company_id,
            command_id=cmd_id,
            actor_id=actor_id,
            peer_pid=peer_pid,
            command_type=PROMOTE_EXECUTION_ENVIRONMENT,
            contract_version=EXECUTION_TIER_COMMAND,
            payload_sha256=payload_sha,
            preconditions=preconditions,
            state="applied",
            result=result_body,
            applied_seq=seq,
            now_ns=now,
        )
        db.execute(
            """INSERT INTO environment_promotion_record (
                   promotion_id, company_id, command_id, action,
                   from_binding_id, to_binding_id, qualification_receipt_id,
                   payload_hash, reason, recorded_at_ns
               ) VALUES (?, ?, ?, 'promote_live', ?, ?, ?, ?, ?, ?)""",
            (
                promotion_id,
                writer.company_id,
                cmd_id,
                from_binding_id,
                target_id,
                qual_id,
                actual_hash,
                clean_reason,
                now,
            ),
        )
        writer.verify()
        return BoardCommandOutcome(
            command_id=cmd_id,
            command_type=PROMOTE_EXECUTION_ENVIRONMENT,
            state="applied",
            disposition="applied",
            denial_code=None,
            effective_revision=seq,
            recorded_at_ns=now,
            peer_pid=peer_pid,
            result=result_body,
        )


def demote_live_environment(
    writer: WriterLease,
    *,
    command_id: str,
    charter_revision: Any,
    target_binding_id: Any,
    action: Any,
    reason: Any,
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
) -> BoardCommandOutcome:
    """Immediately suspend or retire an active Tier 3 company_live binding without stopping paper."""
    writer.verify()
    try:
        cmd_id = _require_safe_id(command_id, "command_id")
        charter = _require_text(charter_revision, "charter_revision", max_len=128)
        target_id = _require_safe_id(target_binding_id, "target_binding_id")
        clean_reason = _require_text(reason, "reason", max_len=1000)
    except ExecutionTierRefused as error:
        raise BoardControlRefused(str(error)) from error
    if action not in ("suspend_live", "demote_to_paper"):
        raise BoardControlRefused("action must be 'suspend_live' or 'demote_to_paper'")

    preconditions = {
        "charter_revision": charter,
        "target_binding_id": target_id,
    }
    payload_sha = _sha256_json(
        {
            "command_type": DEMOTE_LIVE_ENVIRONMENT,
            "preconditions": preconditions,
            "action": action,
            "reason": clean_reason,
        }
    )

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        actor_id = _require_board_control(
            db,
            writer.company_id,
            operation=DEMOTE_LIVE_ENVIRONMENT,
            authorize=authorize,
            principal_hint=principal_hint,
        )

        existing_receipt = db.execute(
            """SELECT state, denial_code, result_json, applied_seq, created_at_ns, peer_pid
               FROM command_receipt
               WHERE company_id = ? AND command_id = ?""",
            (writer.company_id, cmd_id),
        ).fetchone()
        if existing_receipt is not None:
            try:
                replayed, _ = admit_command_receipt(
                    db,
                    company_id=writer.company_id,
                    command_id=cmd_id,
                    actor_id=actor_id,
                    peer_pid=peer_pid,
                    command_type=DEMOTE_LIVE_ENVIRONMENT,
                    contract_version=EXECUTION_TIER_COMMAND,
                    payload_sha256=payload_sha,
                    preconditions=preconditions,
                    state=str(existing_receipt[0]),
                )
            except CommandConflict as error:
                raise BoardControlConflict(str(error)) from error
            return BoardCommandOutcome(
                command_id=replayed.command_id,
                command_type=replayed.command_type,
                state=replayed.state,
                disposition="applied" if replayed.state == "applied" else "denied",
                denial_code=replayed.denial_code,
                effective_revision=int(replayed.applied_seq or 0),
                recorded_at_ns=replayed.created_at_ns,
                peer_pid=replayed.peer_pid,
                result=json.loads(replayed.result_json) if replayed.result_json else {},
            )

        if not is_domain_installed(db, "trading"):
            raise BoardControlRefused("Trading domain is not installed")

        current_charter = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?",
                (writer.company_id,),
            ).fetchone()[0]
        )
        now = time_ns()
        if charter != current_charter:
            denied = {"expected_charter_revision": current_charter}
            seq = append_audit_event(
                db,
                company_id=writer.company_id,
                domain="trading",
                entity_type="execution_environment_binding",
                entity_id=target_id,
                event_type="execution_tier.demote_denied_conflict",
                actor_id=actor_id,
                command_id=cmd_id,
                payload=denied,
                recorded_at_ns=now,
            )
            admit_command_receipt(
                db,
                company_id=writer.company_id,
                command_id=cmd_id,
                actor_id=actor_id,
                peer_pid=peer_pid,
                command_type=DEMOTE_LIVE_ENVIRONMENT,
                contract_version=EXECUTION_TIER_COMMAND,
                payload_sha256=payload_sha,
                preconditions=preconditions,
                state="denied",
                denial_code="conflicting_revision",
                result=denied,
                applied_seq=seq,
                now_ns=now,
            )
            writer.verify()
            return BoardCommandOutcome(
                command_id=cmd_id,
                command_type=DEMOTE_LIVE_ENVIRONMENT,
                state="denied",
                disposition="denied",
                denial_code="conflicting_revision",
                effective_revision=seq,
                recorded_at_ns=now,
                peer_pid=peer_pid,
                result=denied,
            )

        target_row = db.execute(
            f"{_SELECT_BINDING_COLS} WHERE company_id = ? AND binding_id = ?",
            (writer.company_id, target_id),
        ).fetchone()
        if target_row is None:
            raise BoardControlRefused("Target live binding not found")
        target = _row_to_binding(target_row)
        if target.tier != TIER_COMPANY_LIVE:
            raise BoardControlRefused("Only company_live bindings can be demoted")
        if target.status == "retired":
            raise BoardControlRefused("Target live binding is already retired")

        next_status = "suspended" if action == "suspend_live" else "retired"
        db.execute(
            """UPDATE execution_environment_binding
               SET status = ?, updated_at_ns = ?
               WHERE binding_id = ?""",
            (next_status, now, target_id),
        )
        if next_status == "retired":
            db.execute(
                """UPDATE execution_environment_binding
                   SET book_role = 'active', shadow_of_live_binding_id = NULL, updated_at_ns = ?
                   WHERE company_id = ? AND shadow_of_live_binding_id = ? AND book_role = 'shadow'""",
                (now, writer.company_id, target_id),
            )
        promotion_id = f"demote-{uuid4().hex[:16]}"
        result_body = {
            "promotion_id": promotion_id,
            "action": action,
            "target_binding_id": target_id,
            "new_status": next_status,
            "reconciliation_preserved": True,
            "company_paper_preserved": True,
        }
        seq = append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="execution_environment_binding",
            entity_id=target_id,
            event_type="execution_tier.live_demoted",
            actor_id=actor_id,
            command_id=cmd_id,
            payload=result_body,
            recorded_at_ns=now,
        )
        admit_command_receipt(
            db,
            company_id=writer.company_id,
            command_id=cmd_id,
            actor_id=actor_id,
            peer_pid=peer_pid,
            command_type=DEMOTE_LIVE_ENVIRONMENT,
            contract_version=EXECUTION_TIER_COMMAND,
            payload_sha256=payload_sha,
            preconditions=preconditions,
            state="applied",
            result=result_body,
            applied_seq=seq,
            now_ns=now,
        )
        db.execute(
            """INSERT INTO environment_promotion_record (
                   promotion_id, company_id, command_id, action,
                   from_binding_id, to_binding_id, qualification_receipt_id,
                   payload_hash, reason, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
            (
                promotion_id,
                writer.company_id,
                cmd_id,
                action,
                target_id,
                target_id,
                payload_sha,
                clean_reason,
                now,
            ),
        )
        writer.verify()
        return BoardCommandOutcome(
            command_id=cmd_id,
            command_type=DEMOTE_LIVE_ENVIRONMENT,
            state="applied",
            disposition="applied",
            denial_code=None,
            effective_revision=seq,
            recorded_at_ns=now,
            peer_pid=peer_pid,
            result=result_body,
        )
