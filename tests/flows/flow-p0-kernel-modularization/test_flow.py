"""Connected flow verification for flow-p0-kernel-modularization."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns

from sourdough.agent_runtime import PROVIDER_OPENAI, AgentProfileBinding
from sourdough.board_control import (
    bind_board_principal,
    post_ceo_message,
)
from sourdough.company import FoundingTerms, found
from sourdough.duty_run import ExecutionClaim, WorkRunClaim, resolve_run
from sourdough.ipc.projections import (
    read_agent_detail_projection,
    read_agents_graph_projection,
    read_messages_projection,
    read_system_audit_projection,
)
from sourdough.learning import distill_method_delta, resolve_active_methods
from sourdough.runtime.kernel import (
    claim_work_run,
    complete_work_run,
    evaluate_conditions,
    register_responsibility,
    set_condition,
)
from sourdough.storage.db import write_uow
from sourdough.trading.accounts import open_company_paper_book
from sourdough.trading.ledger import submit_company_paper_book_order
from sourdough.writer_lease import hold_company_writer


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with TemporaryDirectory() as tmp:
            co_dir = Path(tmp) / "co"
            co_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            store = co_dir / "company.sqlite"
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-mock",
                revision="rev-1",
                credential_handle_ref="envref:OPENAI_API_KEY",
            )
            founding = found(
                store,
                FoundingTerms(
                    "Modular Co",
                    "charter-v1",
                    "manage-company",
                    profile.profile_id(),
                ),
            )
            company_id = founding.company_id
            founder_id = founding.founder_id
            now = time_ns()

            with hold_company_writer(store, company_id) as w:
                # 1. Board Control write_uow + idempotent command envelope
                bind_board_principal(w, principal_id="owner", role_name="board_control")
                ceo_out = post_ceo_message(
                    w,
                    command_id="flow-ceo-1",
                    target_agent_id=founder_id,
                    charter_revision="charter-v1",
                    body="Run periodic review and verify paper execution.",
                    principal_hint="owner",
                )
                self.assertEqual(ceo_out.state, "applied")

                # 2. Split Work Kernel: responsibility -> timer condition -> claim -> ExecutionClaim
                with write_uow(w) as db:
                    resp = register_responsibility(
                        db,
                        company_id=company_id,
                        owner_agent_id=founder_id,
                        duty_kind="work.assessment_review",
                        remit="Periodic company assessment",
                        readable_scope="company",
                        now_ns=now,
                    )
                    set_condition(
                        db,
                        company_id=company_id,
                        owner_agent_id=founder_id,
                        responsibility_id=resp.responsibility_id,
                        trigger_kind="timer",
                        due_at_ns=now + 50,
                        now_ns=now,
                    )
                    admitted = evaluate_conditions(
                        db, company_id=company_id, now_ns=now + 100
                    )
                    self.assertEqual(len(admitted), 1)
                    run = claim_work_run(
                        db,
                        company_id=company_id,
                        work_item_id=admitted[0].work_item_id,
                        lease_owner="flow-worker",
                        lease_ttl_ns=3_600_000_000_000,
                        now_ns=now + 110,
                    )
                    claim = WorkRunClaim(
                        company_id=company_id,
                        founder_id=founder_id,
                        duty_id=admitted[0].work_item_id,
                        run_id=run.run_id,
                        fencing_token=run.fencing_token,
                        grant_id=founding.grant_id,
                        grant_scope="manage-company",
                        purpose="manage-company",
                    )
                    self.assertIsInstance(claim, ExecutionClaim)
                    self.assertTrue(resolve_run(db, claim, now_ns=now + 110).is_current)

                # 3. Split Learning package: distill one_agent method delta & verify projection redaction
                delta = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    item_kind="work_method",
                    method_id="private-review-checklist",
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body="Verify exposure and cash before completing review.",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{run.run_id}"],
                    now_ns=now + 120,
                )
                self.assertEqual(delta.audience_scope, "one_agent")

                # 4. Unified Trading Ledger: open paper book & execute order via shared kernel
                paper = open_company_paper_book(
                    w,
                    account_id="flow-paper-acct",
                    book_name="Flow Paper",
                    starting_minor_units=5_000_000,
                    exposure_limit_minor_units=2_000_000,
                    fee_bps=5,
                    slippage_bps=2,
                )
                order = submit_company_paper_book_order(
                    w,
                    binding_id=paper.binding_id,
                    actor_id=founder_id,
                    action_id="flow-paper-1",
                    instrument_id="ETH-USD",
                    side="buy",
                    quantity=1,
                    limit_minor_units=260_000,
                    market_price_minor_units=250_000,
                    run_id=run.run_id,
                    now_ns=now + 140,
                )
                self.assertEqual(order["status"], "filled")

                with write_uow(w) as db:
                    complete_work_run(
                        db,
                        run.run_id,
                        run.fencing_token,
                        outcome={
                            "summary": "Flow verified",
                            "order_id": order["order_id"],
                        },
                        now_ns=now + 150,
                    )

            # 5. Split IPC Projections + Redaction package (strictly read-only)
            graph_payload, _ = read_agents_graph_projection(store, company_id)
            self.assertTrue(
                any(a["agent_id"] == founder_id for a in graph_payload["nodes"])
            )

            detail_payload, _ = read_agent_detail_projection(
                store, company_id, founder_id
            )
            self.assertIsNotNone(detail_payload)

            msgs_payload, _ = read_messages_projection(store, company_id)
            self.assertTrue(
                any(
                    "Run periodic" in m.get("body", "")
                    for m in msgs_payload["ceo_thread"]["messages"]
                )
            )

            audit_payload, _ = read_system_audit_projection(store, company_id)
            # Private one_agent method_id must never leak into Board audit entity_ids
            for ev in audit_payload["events"]:
                self.assertNotIn(
                    "private-review-checklist", str(ev.get("entity_id", ""))
                )

            # Active methods lookup via split learning package works cleanly
            self.assertIsInstance(
                resolve_active_methods(store, company_id, agent_id=founder_id), tuple
            )


if __name__ == "__main__":
    unittest.main()
