"""Fail-closed Paper-to-Live migration engine and execution tiers snapshot."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict
from sqlite3 import Connection
from time import time_ns
from typing import Any
from uuid import uuid4

from ..board_control import record_decision_question
from ..company import _identity
from ..duty_run import employment_cutoff
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..storage.kernel import append_audit_event
from ..storage.schema import is_domain_installed
from ..writer_lease import WriterLease
from .domain import (
    _SELECT_BINDING_COLS,
    ENV_EXTERNAL_TEST,
    ENV_LIVE,
    TIER_AGENT_SANDBOX,
    TIER_COMPANY_LIVE,
    TIER_COMPANY_PAPER,
    VALID_MIGRATION_MODES,
    VALID_SOURCE_PAPER_DISPOSITIONS,
    ExecutionTierRefused,
    _canonical_json,
    _require_safe_id,
    _require_text,
    _row_to_binding,
    _validate_credential_handle,
    build_promotion_payload_hash,
    compute_trading_core_digest,
)
from .ledger import (
    _insert_book_valuation_in_tx,
    has_company_paper_soak_evidence,
    read_agent_sandbox_portfolio_from_db,
    read_company_paper_book_portfolio_from_db,
)
from .risk import require_trading_risk_clear

_has_company_paper_soak_evidence = has_company_paper_soak_evidence


def propose_paper_to_live_migration(
    writer: WriterLease,
    *,
    question_id: str,
    source_paper_binding_id: str,
    target_live_binding_id: str,
    qualification_receipt_id: str,
    created_by_agent_id: str,
    migration_mode: str = "clean_start",
    source_paper_disposition: str = "keep_shadow",
    title: str | None = None,
    summary: str | None = None,
    respond_by_ns: int | None = None,
    expected_core_digest: str | None = None,
) -> dict[str, Any]:
    """Nominate a soaked Company Paper Book for Live Trading via a Board decision_question."""
    writer.verify()
    _require_safe_id(question_id, "question_id")
    _require_safe_id(source_paper_binding_id, "source_paper_binding_id")
    _require_safe_id(target_live_binding_id, "target_live_binding_id")
    _require_safe_id(qualification_receipt_id, "qualification_receipt_id")
    _require_safe_id(created_by_agent_id, "created_by_agent_id")
    if migration_mode not in VALID_MIGRATION_MODES:
        raise ExecutionTierRefused(
            "migration_mode must be 'clean_start' or 'drain_incumbent'"
        )
    if source_paper_disposition not in VALID_SOURCE_PAPER_DISPOSITIONS:
        raise ExecutionTierRefused(
            "source_paper_disposition must be 'keep_shadow' or 'retire_source'"
        )

    core_digest = (
        compute_trading_core_digest()
        if expected_core_digest is None
        else expected_core_digest
    )

    with closing(open_reader(writer.store)) as db, read_tx(db):
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "trading"):
            raise ExecutionTierRefused("Trading domain is not installed")
        source_book = read_company_paper_book_portfolio_from_db(
            db, writer.company_id, source_paper_binding_id
        )
        if source_book is None or source_book["status"] != "active":
            raise ExecutionTierRefused(
                "Source paper book must be an active company_paper binding"
            )
        if not source_book["soak_ready"]:
            raise ExecutionTierRefused(
                "Source paper book lacks soak evidence required before nominating for live"
            )
        target_row = db.execute(
            f"{_SELECT_BINDING_COLS} WHERE company_id = ? AND binding_id = ?",
            (writer.company_id, target_live_binding_id),
        ).fetchone()
        if target_row is None:
            raise ExecutionTierRefused("Target live candidate binding was not found")
        target = _row_to_binding(target_row)
        if target.tier != TIER_COMPANY_LIVE or target.environment != ENV_LIVE:
            raise ExecutionTierRefused(
                "Migration target must be a company_live binding"
            )
        if target.status not in ("registered", "suspended"):
            raise ExecutionTierRefused(
                f"Target live binding in status '{target.status}' cannot be nominated"
            )
        qual_row = db.execute(
            """SELECT adapter_revision, environment, instrument_rules_digest,
                      core_digest, verdict, cases_passed, cases_failed, cases_unrun
               FROM venue_qualification_receipt
               WHERE company_id = ? AND receipt_id = ?""",
            (writer.company_id, qualification_receipt_id),
        ).fetchone()
        if qual_row is None:
            raise ExecutionTierRefused(
                "Venue qualification receipt was not found for migration nomination"
            )
        if (
            str(qual_row[1]) != ENV_EXTERNAL_TEST
            or str(qual_row[4]) != "PASS"
            or int(qual_row[5]) <= 0
            or int(qual_row[6]) != 0
            or int(qual_row[7]) != 0
        ):
            raise ExecutionTierRefused(
                "Migration nomination requires a PASS external_test qualification receipt with zero failed/unrun cases"
            )
        if str(qual_row[0]) != target.connector_revision:
            raise ExecutionTierRefused(
                "Qualification receipt adapter_revision does not match target live binding"
            )
        if str(qual_row[2]) != target.instrument_rules_digest:
            raise ExecutionTierRefused(
                "Qualification receipt instrument_rules_digest does not match target live binding"
            )
        if str(qual_row[3]) != core_digest:
            raise ExecutionTierRefused(
                "Qualification receipt core_digest does not match current trading core"
            )

    book_name = str(source_book["book_name"])
    resolved_title = (
        title.strip()
        if isinstance(title, str) and title.strip()
        else f"Migrate Paper Book '{book_name}' to Live ({target.account_id})"
    )
    resolved_summary = (
        summary.strip()
        if isinstance(summary, str) and summary.strip()
        else (
            f"Company recommends promoting '{book_name}' ({source_book['fill_count']} paper fills, "
            f"strategy {source_book['strategy_release_id'] or 'baseline'}) to Live venue "
            f"{target.account_id} ({target.destination_id}) via {migration_mode} with zero virtual "
            f"position carryover."
        )
    )
    impact = {
        "action": "migrate_paper_to_live",
        "source_paper_binding_id": source_paper_binding_id,
        "source_book_name": book_name,
        "strategy_release_id": source_book["strategy_release_id"],
        "target_live_binding_id": target_live_binding_id,
        "target_account_id": target.account_id,
        "target_destination_id": target.destination_id,
        "qualification_receipt_id": qualification_receipt_id,
        "migration_mode": migration_mode,
        "source_paper_disposition": source_paper_disposition,
        "confirm_verb": "Approve Live Migration",
        "consequence_note": (
            f"Activates Live Trading on {target.account_id} starting flat (0 virtual positions copied) "
            f"under cap {target.exposure_limit_minor_units} {target.currency} minor units, and "
            + (
                "retains the source Paper Book in parallel as a Shadow Benchmark."
                if source_paper_disposition == "keep_shadow"
                else "retires the source Paper Book."
            )
        ),
        "what_changes": [
            {
                "label": "Live Venue",
                "value": f"{target.account_id} ({target.destination_id}) activated ({migration_mode})",
            },
            {
                "label": "Source Paper Book",
                "value": f"{book_name} transitions to "
                + (
                    "Shadow Benchmark"
                    if source_paper_disposition == "keep_shadow"
                    else "Retired"
                ),
            },
            {
                "label": "Position Carryover",
                "value": "0 virtual positions copied (real capital starts flat)",
            },
        ],
        "what_stays_untouched": [
            {
                "label": "Other Paper Books",
                "value": "Peer Company Paper Books continue running undisturbed",
            },
            {
                "label": "Agent Sandboxes",
                "value": "Personal per-Agent sandboxes stay strictly isolated",
            },
            {
                "label": "Host API Secrets",
                "value": f"Resolved only in memory via {target.credential_handle_ref}",
            },
        ],
    }

    payload_hash = record_decision_question(
        writer,
        question_id=question_id,
        kind="live_promotion",
        title=resolved_title,
        summary=resolved_summary,
        impact=impact,
        options=("approve", "decline"),
        created_by_agent_id=created_by_agent_id,
        respond_by_ns=respond_by_ns,
    )

    now = time_ns()
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        db.execute(
            """UPDATE execution_environment_binding
               SET book_role = 'nominated', nominated_question_id = ?, updated_at_ns = ?
               WHERE company_id = ? AND binding_id = ?""",
            (question_id, now, writer.company_id, source_paper_binding_id),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="execution_environment_binding",
            entity_id=source_paper_binding_id,
            event_type="execution_tier.paper_migration_nominated",
            actor_id=created_by_agent_id,
            payload={
                "question_id": question_id,
                "source_paper_binding_id": source_paper_binding_id,
                "target_live_binding_id": target_live_binding_id,
                "qualification_receipt_id": qualification_receipt_id,
                "migration_mode": migration_mode,
                "source_paper_disposition": source_paper_disposition,
            },
            recorded_at_ns=now,
        )
        writer.verify()

    return {
        "question_id": question_id,
        "payload_hash": payload_hash,
        "source_paper_binding_id": source_paper_binding_id,
        "target_live_binding_id": target_live_binding_id,
        "qualification_receipt_id": qualification_receipt_id,
        "migration_mode": migration_mode,
        "source_paper_disposition": source_paper_disposition,
    }


def execute_paper_to_live_migration(
    writer: WriterLease,
    *,
    source_paper_binding_id: str,
    target_live_binding_id: str,
    qualification_receipt_id: str,
    migration_mode: str = "clean_start",
    source_paper_disposition: str = "keep_shadow",
    reason: str = "Board approved Paper-to-Live migration",
    question_id: str | None = None,
    actor_id: str = "board_apply_decision",
    expected_core_digest: str | None = None,
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Fail-closed execution engine migrating a soaked Paper Book into Live Trading."""
    writer.verify()
    _require_safe_id(source_paper_binding_id, "source_paper_binding_id")
    _require_safe_id(target_live_binding_id, "target_live_binding_id")
    _require_safe_id(qualification_receipt_id, "qualification_receipt_id")
    clean_reason = _require_text(reason, "reason", max_len=1000)
    if migration_mode not in VALID_MIGRATION_MODES:
        raise ExecutionTierRefused(
            "migration_mode must be 'clean_start' or 'drain_incumbent'"
        )
    if source_paper_disposition not in VALID_SOURCE_PAPER_DISPOSITIONS:
        raise ExecutionTierRefused(
            "source_paper_disposition must be 'keep_shadow' or 'retire_source'"
        )
    if question_id is not None:
        _require_safe_id(question_id, "question_id")

    core_digest = (
        compute_trading_core_digest()
        if expected_core_digest is None
        else expected_core_digest
    )
    now = time_ns() if now_ns is None else now_ns

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "trading"):
            raise ExecutionTierRefused("Trading domain is not installed")

        # Gate 1: Zero active company/execution/live restrictions.
        require_trading_risk_clear(db, writer.company_id, ENV_LIVE)

        # Gate 2: Zero open trading_book_order rows on source/retiring bindings and zero unresolved external_actions.
        incumbent_row = db.execute(
            """SELECT binding_id FROM execution_environment_binding
               WHERE company_id = ? AND tier = ? AND status = 'active'""",
            (writer.company_id, TIER_COMPANY_LIVE),
        ).fetchone()
        checked_bindings = [source_paper_binding_id]
        if incumbent_row is not None and migration_mode == "drain_incumbent":
            checked_bindings.append(str(incumbent_row[0]))
        placeholders = ", ".join("?" for _ in checked_bindings)
        unresolved_count = int(
            db.execute(
                f"""SELECT COUNT(*) FROM trading_book_order
                    WHERE company_id = ? AND status = 'open'
                      AND binding_id IN ({placeholders})""",
                (writer.company_id, *checked_bindings),
            ).fetchone()[0]
        )
        if unresolved_count > 0:
            raise ExecutionTierRefused(
                f"Migration blocked: {unresolved_count} unresolved order(s) must settle or cancel before live activation"
            )
        unresolved_ext_actions = int(
            db.execute(
                """SELECT COUNT(*) FROM external_actions
                   WHERE company_id = ?
                     AND status IN ('prepared', 'in_flight', 'unknown')""",
                (writer.company_id,),
            ).fetchone()[0]
        )
        if unresolved_ext_actions > 0:
            raise ExecutionTierRefused(
                f"Migration blocked: {unresolved_ext_actions} unresolved external action(s) must settle before live activation"
            )

        # Gate 3: Source Paper Book active & soak ready.
        source_book = read_company_paper_book_portfolio_from_db(
            db, writer.company_id, source_paper_binding_id
        )
        if source_book is None or source_book["status"] != "active":
            raise ExecutionTierRefused(
                "Source paper book must be an active company_paper binding"
            )
        if not source_book["soak_ready"]:
            raise ExecutionTierRefused(
                "Source paper book lacks soak evidence required for live migration"
            )
        if (
            source_paper_disposition == "retire_source"
            and source_book["open_orders_count"] > 0
        ):
            raise ExecutionTierRefused(
                f"Cannot retire source paper book while {source_book['open_orders_count']} resting order(s) remain open"
            )

        # Gate 4: Target Live binding valid & opaque credential handle verified.
        target_row = db.execute(
            f"{_SELECT_BINDING_COLS} WHERE company_id = ? AND binding_id = ?",
            (writer.company_id, target_live_binding_id),
        ).fetchone()
        if target_row is None:
            raise ExecutionTierRefused("Target live candidate binding was not found")
        target = _row_to_binding(target_row)
        if target.tier != TIER_COMPANY_LIVE or target.environment != ENV_LIVE:
            raise ExecutionTierRefused(
                "Migration target must be a company_live binding"
            )
        if target.status not in ("registered", "suspended"):
            raise ExecutionTierRefused(
                f"Cannot migrate into target live binding in status '{target.status}'"
            )
        _validate_credential_handle(
            target.credential_handle_ref, environment=ENV_LIVE, required=True
        )

        # Gate 5: Matching PASS external_test venue qualification receipt & current core digest.
        qual_row = db.execute(
            """SELECT adapter_revision, environment, instrument_rules_digest,
                      core_digest, verdict, cases_passed, cases_failed, cases_unrun
               FROM venue_qualification_receipt
               WHERE company_id = ? AND receipt_id = ?""",
            (writer.company_id, qualification_receipt_id),
        ).fetchone()
        if qual_row is None:
            raise ExecutionTierRefused(
                "External testnet venue qualification receipt is missing"
            )
        if (
            str(qual_row[1]) != ENV_EXTERNAL_TEST
            or str(qual_row[4]) != "PASS"
            or int(qual_row[5]) <= 0
            or int(qual_row[6]) != 0
            or int(qual_row[7]) != 0
        ):
            raise ExecutionTierRefused(
                "Migration requires a PASS external_test qualification receipt with zero failures or unrun cases"
            )
        if str(qual_row[0]) != target.connector_revision:
            raise ExecutionTierRefused(
                "Qualification receipt adapter_revision does not match target live binding"
            )
        if str(qual_row[2]) != target.instrument_rules_digest:
            raise ExecutionTierRefused(
                "Qualification receipt instrument_rules_digest does not match target live binding"
            )
        if str(qual_row[3]) != core_digest:
            raise ExecutionTierRefused(
                "Qualification receipt core_digest does not match current trading core"
            )

        # Gate 6: Incumbent live binding handling.
        replaced_live_binding_id: str | None = None
        if incumbent_row is not None:
            incumbent_id = str(incumbent_row[0])
            if migration_mode == "clean_start":
                raise ExecutionTierRefused(
                    f"Active incumbent live binding ({incumbent_id}) exists; use migration_mode='drain_incumbent' or demote incumbent first"
                )
            replaced_live_binding_id = incumbent_id
            db.execute(
                """UPDATE execution_environment_binding
                   SET status = 'retired', book_role = 'retired', updated_at_ns = ?
                   WHERE binding_id = ?""",
                (now, incumbent_id),
            )
            db.execute(
                """UPDATE execution_environment_binding
                   SET book_role = 'active', shadow_of_live_binding_id = NULL, updated_at_ns = ?
                   WHERE company_id = ? AND shadow_of_live_binding_id = ? AND book_role = 'shadow'""",
                (now, writer.company_id, incumbent_id),
            )

        # Activate target Live binding (real capital starts flat: positions_copied = 0).
        db.execute(
            """UPDATE execution_environment_binding
               SET status = 'active', updated_at_ns = ?
               WHERE binding_id = ?""",
            (now, target_live_binding_id),
        )
        has_live_val = db.execute(
            "SELECT 1 FROM trading_book_valuation WHERE binding_id = ? LIMIT 1",
            (target_live_binding_id,),
        ).fetchone()
        if has_live_val is None:
            _insert_book_valuation_in_tx(
                db,
                target,
                label="Live Launch",
                source="live_launch",
                recorded_at_ns=now,
            )

        # Transition source Paper Book to shadow benchmark OR retired.
        if source_paper_disposition == "keep_shadow":
            db.execute(
                """UPDATE execution_environment_binding
                   SET book_role = 'shadow',
                       shadow_of_live_binding_id = ?,
                       nominated_question_id = COALESCE(?, nominated_question_id),
                       updated_at_ns = ?
                   WHERE company_id = ? AND binding_id = ?""",
                (
                    target_live_binding_id,
                    question_id,
                    now,
                    writer.company_id,
                    source_paper_binding_id,
                ),
            )
            source_book_final_role = "shadow"
        else:
            db.execute(
                """UPDATE execution_environment_binding
                   SET status = 'retired',
                       book_role = 'retired',
                       shadow_of_live_binding_id = NULL,
                       nominated_question_id = COALESCE(?, nominated_question_id),
                       updated_at_ns = ?
                   WHERE company_id = ? AND binding_id = ?""",
                (question_id, now, writer.company_id, source_paper_binding_id),
            )
            source_book_final_role = "retired"

        migration_id = f"mig-{uuid4().hex[:16]}"
        pre_check_summary = {
            "active_restrictions": 0,
            "unresolved_orders": unresolved_count,
            "unresolved_external_actions": unresolved_ext_actions,
            "source_book_fills": source_book["fill_count"],
            "source_book_soak_ready": True,
            "qualification_cases_passed": int(qual_row[5]),
            "core_digest_verified": True,
            "positions_copied": 0,
        }
        db.execute(
            """INSERT INTO trading_live_migration_record (
                   migration_id, company_id, question_id,
                   source_paper_binding_id, target_live_binding_id,
                   replaced_live_binding_id, qualification_receipt_id,
                   migration_mode, source_paper_disposition, positions_copied,
                   pre_check_summary_json, reason, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                migration_id,
                writer.company_id,
                question_id,
                source_paper_binding_id,
                target_live_binding_id,
                replaced_live_binding_id,
                qualification_receipt_id,
                migration_mode,
                source_paper_disposition,
                _canonical_json(pre_check_summary),
                clean_reason,
                now,
            ),
        )
        result = {
            "migration_id": migration_id,
            "question_id": question_id,
            "source_paper_binding_id": source_paper_binding_id,
            "source_book_name": source_book["book_name"],
            "source_book_final_role": source_book_final_role,
            "target_live_binding_id": target_live_binding_id,
            "target_account_id": target.account_id,
            "replaced_live_binding_id": replaced_live_binding_id,
            "qualification_receipt_id": qualification_receipt_id,
            "migration_mode": migration_mode,
            "source_paper_disposition": source_paper_disposition,
            "positions_copied": 0,
            "pre_check_summary": pre_check_summary,
            "recorded_at_ns": now,
        }
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="trading_live_migration_record",
            entity_id=migration_id,
            event_type="execution_tier.paper_migrated_to_live",
            actor_id=actor_id,
            payload=result,
            recorded_at_ns=now,
        )
        writer.verify()
        return result


def read_execution_tiers_snapshot(db: Connection, company_id: str) -> dict[str, Any]:
    """Read a redacted snapshot of all three execution tiers inside an open read_tx."""
    if not is_domain_installed(db, "trading"):
        return {
            "trading_domain_installed": False,
            "company_paper": None,
            "company_paper_books": [],
            "company_paper_soak_ready": False,
            "company_live": None,
            "live_candidates": [],
            "agent_sandboxes": [],
            "qualification_receipts": [],
            "promotion_history": [],
            "migration_history": [],
            "current_core_digest": compute_trading_core_digest(),
        }

    bindings = [
        _row_to_binding(r)
        for r in db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ?
                ORDER BY created_at_ns ASC, binding_id ASC""",
            (company_id,),
        ).fetchall()
    ]
    company_paper = next(
        (
            asdict(b)
            for b in bindings
            if b.tier == TIER_COMPANY_PAPER and b.status == "active"
        ),
        None,
    )
    company_live = next(
        (
            asdict(b)
            for b in bindings
            if b.tier == TIER_COMPANY_LIVE and b.status == "active"
        ),
        None,
    )
    charter_row = db.execute(
        "SELECT charter_revision FROM company WHERE id = ?",
        (company_id,),
    ).fetchone()
    charter_revision = str(charter_row[0]) if charter_row is not None else ""
    current_core = compute_trading_core_digest()

    qual_receipts = [
        {
            "receipt_id": str(r[0]),
            "adapter_revision": str(r[1]),
            "environment": str(r[2]),
            "destination_id": str(r[3]),
            "instrument_rules_digest": str(r[4]),
            "core_digest": str(r[5]),
            "verdict": str(r[6]),
            "cases_passed": int(r[7]),
            "cases_failed": int(r[8]),
            "cases_unrun": int(r[9]),
            "evidence_sha256": str(r[10]),
            "recorded_at_ns": int(r[11]),
        }
        for r in db.execute(
            """SELECT receipt_id, adapter_revision, environment, destination_id,
                      instrument_rules_digest, core_digest, verdict,
                      cases_passed, cases_failed, cases_unrun,
                      evidence_sha256, recorded_at_ns
               FROM venue_qualification_receipt
               WHERE company_id = ?
               ORDER BY recorded_at_ns DESC LIMIT 20""",
            (company_id,),
        ).fetchall()
    ]

    live_candidates = []
    for b in bindings:
        if b.tier == TIER_COMPANY_LIVE and b.status != "active":
            matching = next(
                (
                    q
                    for q in qual_receipts
                    if q["environment"] == ENV_EXTERNAL_TEST
                    and q["verdict"] == "PASS"
                    and q["cases_failed"] == 0
                    and q["cases_unrun"] == 0
                    and q["adapter_revision"] == b.connector_revision
                    and q["instrument_rules_digest"] == b.instrument_rules_digest
                    and q["core_digest"] == current_core
                ),
                None,
            )
            matching_id = str(matching["receipt_id"]) if matching is not None else None
            promo_hash = (
                build_promotion_payload_hash(
                    company_id=company_id,
                    charter_revision=charter_revision,
                    target_binding=b,
                    qualification_receipt_id=matching_id,
                )
                if matching_id is not None and charter_revision
                else None
            )
            live_candidates.append(
                asdict(b)
                | {
                    "matching_qualification_receipt_id": matching_id,
                    "promotion_payload_hash": promo_hash,
                }
            )
    agent_sandboxes = []
    for b in bindings:
        if b.tier == TIER_AGENT_SANDBOX and b.status == "active" and b.owner_agent_id:
            if employment_cutoff(db, b.owner_agent_id) is not None:
                continue
            grant_rows = db.execute(
                "SELECT scope FROM standing_grant WHERE agent_id = ?",
                (b.owner_agent_id,),
            ).fetchall()
            if grant_rows and all(
                str(r[0]).strip().lower() == "revoked" for r in grant_rows
            ):
                continue
            portfolio = read_agent_sandbox_portfolio_from_db(
                db, company_id, b.owner_agent_id
            )
            if portfolio is not None:
                agent_sandboxes.append(
                    {
                        "agent_id": b.owner_agent_id,
                        "binding_id": b.binding_id,
                        "account_id": b.account_id,
                        "currency": b.currency,
                        "fee_bps": b.fee_bps,
                        "slippage_bps": b.slippage_bps,
                        "quote_source": b.quote_source,
                        "cash_minor_units": portfolio["cash_minor_units"],
                        "gross_exposure_minor_units": portfolio[
                            "gross_exposure_minor_units"
                        ],
                        "realized_pnl_minor_units": portfolio[
                            "realized_pnl_minor_units"
                        ],
                        "total_fees_minor_units": portfolio["total_fees_minor_units"],
                        "open_positions_count": sum(
                            1 for p in portfolio["positions"] if p["quantity"] != 0
                        ),
                        "fill_count": portfolio["fill_count"],
                    }
                )

    promotions = [
        {
            "promotion_id": str(r[0]),
            "command_id": str(r[1]),
            "action": str(r[2]),
            "from_binding_id": None if r[3] is None else str(r[3]),
            "to_binding_id": str(r[4]),
            "qualification_receipt_id": None if r[5] is None else str(r[5]),
            "payload_hash": str(r[6]),
            "reason": str(r[7]),
            "recorded_at_ns": int(r[8]),
        }
        for r in db.execute(
            """SELECT promotion_id, command_id, action, from_binding_id,
                      to_binding_id, qualification_receipt_id, payload_hash,
                      reason, recorded_at_ns
               FROM environment_promotion_record
               WHERE company_id = ?
               ORDER BY recorded_at_ns DESC LIMIT 20""",
            (company_id,),
        ).fetchall()
    ]
    company_paper_books = []
    binding_account_by_id = {b.binding_id: b.account_id for b in bindings}
    book_name_by_id: dict[str, str] = {}
    for b in bindings:
        if b.tier == TIER_COMPANY_PAPER:
            book = read_company_paper_book_portfolio_from_db(
                db, company_id, b.binding_id
            )
            if book is not None:
                company_paper_books.append(book)
                book_name_by_id[b.binding_id] = str(book["book_name"])

    migrations = [
        {
            "migration_id": str(r[0]),
            "question_id": None if r[1] is None else str(r[1]),
            "source_paper_binding_id": str(r[2]),
            "source_book_name": book_name_by_id.get(
                str(r[2]), binding_account_by_id.get(str(r[2]), str(r[2]))
            ),
            "target_live_binding_id": str(r[3]),
            "target_account_id": binding_account_by_id.get(str(r[3]), str(r[3])),
            "replaced_live_binding_id": None if r[4] is None else str(r[4]),
            "qualification_receipt_id": str(r[5]),
            "migration_mode": str(r[6]),
            "source_paper_disposition": str(r[7]),
            "positions_copied": int(r[8]),
            "pre_check_summary": json.loads(str(r[9])),
            "reason": str(r[10]),
            "recorded_at_ns": int(r[11]),
        }
        for r in db.execute(
            """SELECT migration_id, question_id, source_paper_binding_id,
                      target_live_binding_id, replaced_live_binding_id,
                      qualification_receipt_id, migration_mode,
                      source_paper_disposition, positions_copied,
                      pre_check_summary_json, reason, recorded_at_ns
               FROM trading_live_migration_record
               WHERE company_id = ?
               ORDER BY recorded_at_ns DESC LIMIT 20""",
            (company_id,),
        ).fetchall()
    ]
    return {
        "trading_domain_installed": True,
        "charter_revision": charter_revision,
        "company_paper": company_paper,
        "company_paper_books": company_paper_books,
        "company_paper_soak_ready": has_company_paper_soak_evidence(db, company_id),
        "company_live": company_live,
        "live_candidates": live_candidates,
        "agent_sandboxes": agent_sandboxes,
        "qualification_receipts": qual_receipts,
        "promotion_history": promotions,
        "migration_history": migrations,
        "current_core_digest": current_core,
    }
