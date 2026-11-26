"""Connected Flow E2E test for Modular Domain IA, Conditional Sandbox, and Dual-Entry Approvals."""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    DECISION_COMMAND,
    SUBMIT_DECISION_ANSWER,
    bind_board_principal,
    record_decision_question,
    upsert_agent_profile,
)
from sourdough.company import FoundingTerms, found
from sourdough.trading.sandbox_tools import AgentSandboxTools
from sourdough.ipc import ClientWebBridge, CompanyDaemon
from sourdough.storage.db import open_writer, write_tx
from sourdough.writer_lease import hold_company_writer


def _bridge_json(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    body: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with (
            TemporaryDirectory(prefix="sd-mod-flow-") as tmp,
            TemporaryDirectory(prefix="sd-", dir="/tmp") as sock_tmp,
        ):
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            sock_dir = Path(sock_tmp)
            pending_dir = root / "pending"

            founding = found(
                store,
                FoundingTerms("sourdough-modular", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            ceo_id = founding.founder_id
            quant_id = "agent-atlas-mod"
            analyst_id = "agent-mira-mod"

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    for aid, scope in [
                        (quant_id, "paper"),
                        (analyst_id, "assessment"),
                    ]:
                        db.execute(
                            "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                            (aid, company_id, "profile-v1"),
                        )
                        db.execute(
                            "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                            (f"grant-{aid}", aid, scope),
                        )

                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )
                upsert_agent_profile(
                    writer,
                    agent_id=quant_id,
                    display_name="Atlas Quant",
                    role_label="Quant Researcher",
                    monthly_budget_minor=500_000,
                )
                upsert_agent_profile(
                    writer,
                    agent_id=analyst_id,
                    display_name="Mira Analyst",
                    role_label="Research Analyst",
                    monthly_budget_minor=300_000,
                )

                # Only Atlas opens and trades in a personal paper sandbox.
                tools = AgentSandboxTools(writer=writer, agent_id=quant_id)
                tools.invoke(
                    "open_agent_sandbox",
                    {
                        "account_id": "atlas-sbx-mod",
                        "starting_minor_units": 10_000_000,
                        "fee_bps": 4,
                        "slippage_bps": 2,
                    },
                )
                tools.invoke(
                    "submit_agent_sandbox_order",
                    {
                        "action_id": "mod-act-1",
                        "instrument_id": "BTC-USDT",
                        "side": "buy",
                        "quantity": 1,
                        "limit_minor_units": 6_600_000,
                        "market_price_minor_units": 6_500_000,
                    },
                )

                # Create a decision_question surfaced both in Agent Detail and unified Approvals.
                question_id = "q-mod-budget-1"
                question_payload_hash = record_decision_question(
                    writer,
                    question_id=question_id,
                    kind="budget_increase",
                    title="Increase Atlas monthly LLM compute budget to $8,000",
                    summary="Atlas Quant requested expanded compute budget for BTC strategy research.",
                    impact={
                        "action": "update_agent_budget",
                        "agent_id": quant_id,
                        "monthly_budget_minor": 800_000,
                        "confirm_verb": "Approve Budget Increase",
                        "what_changes": [
                            {
                                "label": "Monthly Budget",
                                "value": "$5,000 -> $8,000",
                            }
                        ],
                    },
                    options=("approve", "decline"),
                    created_by_agent_id=ceo_id,
                )

            with (
                CompanyDaemon(
                    store,
                    company_id,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                ) as daemon,
                ClientWebBridge(
                    daemon.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-chair",
                ) as bridge,
            ):
                base = bridge.info.url.rstrip("/")
                tok = bridge.info.session_token

                # 1. Verify /api/v1/meta exposes active_domains for modular sidebar & pending approvals count.
                st_m, meta = _bridge_json(f"{base}/api/v1/meta", tok)
                self.assertEqual(st_m, 200)
                self.assertIn("trading", meta["active_domains"])
                self.assertEqual(meta["pending_approvals_count"], 1)

                # 2. Verify conditional sandbox gating in /projections/agents-graph & /projections/agents/{id}.
                st_g, graph = _bridge_json(
                    f"{base}/api/v1/projections/agents-graph", tok
                )
                self.assertEqual(st_g, 200)
                nodes = {n["agent_id"]: n for n in graph["nodes"]}
                self.assertTrue(nodes[quant_id]["sandbox_authorized"])
                self.assertEqual(nodes[quant_id]["sandbox_activity"]["fill_count"], 1)
                self.assertFalse(nodes[analyst_id]["sandbox_authorized"])
                self.assertIsNone(nodes[analyst_id]["sandbox_activity"])
                self.assertIsNone(nodes[ceo_id]["sandbox_activity"])

                st_a1, quant_detail = _bridge_json(
                    f"{base}/api/v1/projections/agents/{quant_id}", tok
                )
                self.assertEqual(st_a1, 200)
                self.assertTrue(quant_detail["sandbox_authorized"])
                self.assertEqual(quant_detail["agent_sandbox"]["fill_count"], 1)

                st_a2, analyst_detail = _bridge_json(
                    f"{base}/api/v1/projections/agents/{analyst_id}", tok
                )
                self.assertEqual(st_a2, 200)
                self.assertFalse(analyst_detail["sandbox_authorized"])
                self.assertIsNone(analyst_detail["agent_sandbox"])

                # 3. Approve via the contextual entry path (same submit_decision_answer command).
                st_c1, cmd1 = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="ctx-approve-budget-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "ctx-approve-budget-1",
                        "command_type": SUBMIT_DECISION_ANSWER,
                        "contract_version": DECISION_COMMAND,
                        "preconditions": {
                            "question_id": question_id,
                            "expected_revision": 1,
                            "expected_payload_hash": question_payload_hash,
                        },
                        "payload": {
                            "choice": "approve",
                            "reason": "Approved inline from Agent Detail",
                        },
                    },
                )
                self.assertEqual(st_c1, 200, cmd1)
                self.assertEqual(cmd1["state"], "applied")

                # 4. Unified Approvals inbox and /v1/meta immediately reflect zero pending decisions.
                _, meta_after = _bridge_json(f"{base}/api/v1/meta", tok)
                self.assertEqual(meta_after["pending_approvals_count"], 0)

                _, approvals_after = _bridge_json(
                    f"{base}/api/v1/projections/approvals", tok
                )
                self.assertEqual(len(approvals_after["pending"]), 0)
                self.assertEqual(len(approvals_after["history"]), 1)
                self.assertEqual(
                    approvals_after["history"][0]["resolution_choice"], "approve"
                )

                # 5. Attempting a second approval from Approvals inbox is cleanly rejected (stale revision).
                st_c2, cmd2 = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="inbox-duplicate-approve-2",
                    payload={
                        "company_id": company_id,
                        "command_id": "inbox-duplicate-approve-2",
                        "command_type": SUBMIT_DECISION_ANSWER,
                        "contract_version": DECISION_COMMAND,
                        "preconditions": {
                            "question_id": question_id,
                            "expected_revision": 1,
                            "expected_payload_hash": question_payload_hash,
                        },
                        "payload": {
                            "choice": "approve",
                            "reason": "Stale second click",
                        },
                    },
                )
                self.assertNotEqual(cmd2.get("state"), "applied", (st_c2, cmd2))


if __name__ == "__main__":
    unittest.main()
