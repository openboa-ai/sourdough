"""Unit tests for ResidentCompanyService, production duty handlers, and run-company CLI."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sourdough.agent_runtime import (
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    AgentProfileBinding,
    InMemorySecretResolver,
    build_default_adapter_registry,
)
from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    apply_company_restriction,
    bind_board_principal,
    post_ceo_message,
    record_decision_question,
    submit_decision_answer,
    upsert_agent_profile,
)
from sourdough.company import FoundingTerms, found
from sourdough.ipc import ControlPlaneClient
from sourdough.runtime import (
    DUTY_QUANT_AGENT_SANDBOX,
    DUTY_QUANT_PAPER_DECISION,
    DUTY_QUANT_RECONCILE,
    CompanyRuntime,
    ResidentCompanyService,
    build_production_duty_registry,
    create_work_item,
    sync_board_triggers,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer


class _MockLlmServer:
    """Loopback server answering OpenAI Responses (CEO) and Anthropic Messages (Quant)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.openai_calls = 0
        self.anthropic_calls = 0
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/v1/responses":
                    with parent._lock:
                        parent.openai_calls += 1
                    if payload.get("previous_response_id"):
                        self._send(
                            {
                                "id": "ceo-resp-2",
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "Founder-CEO reviewed status and confirmed all systems nominal.",
                                            }
                                        ],
                                    }
                                ],
                                "usage": {"input_tokens": 900, "output_tokens": 180},
                            }
                        )
                    else:
                        self._send(
                            {
                                "id": "ceo-resp-1",
                                "output": [
                                    {
                                        "type": "function_call",
                                        "call_id": "ceo-status-1",
                                        "name": "read_company_status",
                                        "arguments": "{}",
                                    }
                                ],
                                "usage": {"input_tokens": 800, "output_tokens": 120},
                            }
                        )
                elif self.path == "/v1/messages":
                    with parent._lock:
                        parent.anthropic_calls += 1
                    messages = payload.get("messages", [])
                    if len(messages) == 1:
                        self._send(
                            {
                                "id": "q-msg-1",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "q-open-1",
                                        "name": "open_agent_sandbox",
                                        "input": {
                                            "account_id": "unit-quant-sbx",
                                            "starting_minor_units": 5_000_000,
                                            "fee_bps": 4,
                                            "slippage_bps": 2,
                                        },
                                    }
                                ],
                                "usage": {"input_tokens": 1000, "output_tokens": 200},
                            }
                        )
                    elif len(messages) == 3:
                        self._send(
                            {
                                "id": "q-msg-2",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "q-ord-1",
                                        "name": "submit_agent_sandbox_order",
                                        "input": {
                                            "action_id": "unit-sbx-act-1",
                                            "instrument_id": "ETH-USDT",
                                            "side": "buy",
                                            "quantity": 1,
                                            "limit_minor_units": 360_000,
                                            "market_price_minor_units": 350_000,
                                        },
                                    }
                                ],
                                "usage": {"input_tokens": 950, "output_tokens": 180},
                            }
                        )
                    else:
                        self._send(
                            {
                                "id": "q-msg-3",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Quant sandbox experiment filled 1 ETH-USDT.",
                                    }
                                ],
                                "usage": {"input_tokens": 600, "output_tokens": 110},
                            }
                        )

            def _send(self, body: dict[str, Any]) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)


class ResidentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = _MockLlmServer()
        self.resolver = InMemorySecretResolver(
            {
                "envref:TEST_OPENAI": "local-test-val-1",
                "envref:TEST_ANTHROPIC": "local-test-val-2",
            }
        )
        self.ceo_profile = AgentProfileBinding(
            provider=PROVIDER_OPENAI,
            model="gpt-5-mini",
            revision="2026-11",
            credential_handle_ref="envref:TEST_OPENAI",
        )
        self.quant_profile = AgentProfileBinding(
            provider=PROVIDER_ANTHROPIC,
            model="claude-sonnet-4-5",
            revision="2026-11",
            credential_handle_ref="envref:TEST_ANTHROPIC",
        )

    def tearDown(self) -> None:
        self.mock.close()

    def _build_registry(self, founder_id: str, quant_id: str):
        adapter_registry = build_default_adapter_registry(
            secret_resolver=self.resolver,
            openai_base_url=self.mock.base_url,
            anthropic_base_url=self.mock.base_url,
            allow_loopback_override=True,
        )
        return build_production_duty_registry(
            adapter_registry=adapter_registry,
            profile_bindings={
                founder_id: self.ceo_profile,
                quant_id: self.quant_profile,
            },
        )

    def test_step_with_writer_sync_triggers_and_restriction_gating(self) -> None:
        with TemporaryDirectory(prefix="sd-res-unit-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms(
                    "sourdough-fund",
                    "charter-v1",
                    "paper",
                    self.ceo_profile.profile_id(),
                ),
            )
            company_id = founding.company_id
            founder_id = founding.founder_id
            quant_id = "agent-quant"

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    db.execute(
                        "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                        (quant_id, company_id, self.quant_profile.profile_id()),
                    )
                    db.execute(
                        "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                        ("grant-quant", quant_id, "paper"),
                    )
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )
                upsert_agent_profile(
                    writer,
                    agent_id=quant_id,
                    display_name="Atlas Quant",
                    role_label="Quant Researcher",
                    monthly_budget_minor=100_000,
                )
                # 1. Board sends CEO message and approves a budget decision.
                post_ceo_message(
                    writer,
                    command_id="cmd-msg-1",
                    target_agent_id=founder_id,
                    charter_revision="charter-v1",
                    body="Give me a quick company status update.",
                    principal_hint="board-chair",
                )
                q_hash = record_decision_question(
                    writer,
                    question_id="q-budget-1",
                    kind="budget_adjustment",
                    title="Increase Atlas Quant budget",
                    summary="Raise monthly compute budget to 500000 minor units.",
                    impact={
                        "action": "update_agent_budget",
                        "agent_id": quant_id,
                        "monthly_budget_minor": 500_000,
                    },
                    created_by_agent_id=founder_id,
                )
                submit_decision_answer(
                    writer,
                    command_id="cmd-dec-1",
                    question_id="q-budget-1",
                    expected_revision=1,
                    expected_payload_hash=q_hash,
                    choice="approve",
                    reason="Approved by Board",
                    principal_hint="board-chair",
                )

                # Activate company-wide risk restriction to test gating.
                apply_company_restriction(
                    writer,
                    command_id="cmd-pause-1",
                    restriction_id="rest-risk-1",
                    charter_revision="charter-v1",
                    expected_restriction_revision=0,
                    cause_category="operator_pause",
                    scope_ref="company:all",
                    reason="Hold new company risk",
                    principal_hint="board-chair",
                )

                # Enqueue quant sandbox, paper decision (must stay blocked!), and reconcile.
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    create_work_item(
                        db,
                        company_id=company_id,
                        owner_agent_id=quant_id,
                        duty_kind=DUTY_QUANT_AGENT_SANDBOX,
                        source_kind="manual",
                        source_id="sbx-1",
                        dedupe_key="unit-sbx-1",
                    )
                    paper_item, _ = create_work_item(
                        db,
                        company_id=company_id,
                        owner_agent_id=quant_id,
                        duty_kind=DUTY_QUANT_PAPER_DECISION,
                        source_kind="manual",
                        source_id="paper-1",
                        dedupe_key="unit-paper-1",
                    )
                    create_work_item(
                        db,
                        company_id=company_id,
                        owner_agent_id=founder_id,
                        duty_kind=DUTY_QUANT_RECONCILE,
                        source_kind="manual",
                        source_id="rec-1",
                        dedupe_key="unit-rec-1",
                    )

                # Sync triggers twice -> second call must create 0 duplicates.
                self.assertEqual(sync_board_triggers(writer, company_id), 2)
                self.assertEqual(sync_board_triggers(writer, company_id), 0)

                registry = self._build_registry(founder_id, quant_id)
                runtime = CompanyRuntime(store, company_id, registry)
                commits = 0

                def _on_commit() -> None:
                    nonlocal commits
                    commits += 1

                lock = threading.RLock()
                summary = runtime.step_with_writer(
                    writer,
                    max_dispatches=10,
                    tx_lock=lock,
                    on_commit=_on_commit,
                )
                # 4 duties ran: ceo.respond_message, board.apply_decision, quant.agent_sandbox, quant.reconcile.
                # quant.paper_decision stayed blocked by active restriction!
                self.assertEqual(len(summary.executed_runs), 4)
                self.assertIn(paper_item.work_item_id, summary.blocked_items)
                self.assertGreater(commits, 0)

            with closing(open_reader(store)) as db, read_tx(db):
                # CEO reply persisted + board message marked delivered.
                msgs = db.execute(
                    "SELECT sender_kind, delivery_state, body FROM ceo_message ORDER BY recorded_at_ns"
                ).fetchall()
                self.assertEqual(len(msgs), 2)
                self.assertEqual(msgs[0][0], "board")
                self.assertEqual(msgs[0][1], "delivered")
                self.assertEqual(msgs[1][0], "ceo")
                self.assertIn("Founder-CEO reviewed status", msgs[1][2])

                # Decision applied updated agent_profile budget.
                budget = db.execute(
                    "SELECT monthly_budget_minor FROM agent_profile WHERE agent_id = ?",
                    (quant_id,),
                ).fetchone()[0]
                self.assertEqual(budget, 500_000)

                # Quant paper decision item remained 'due' (not settled while restricted).
                paper_state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (paper_item.work_item_id,),
                ).fetchone()[0]
                self.assertEqual(paper_state, "due")

                # 2 LLM shifts settled into general_ledger (CEO + Quant sandbox).
                gl_count = db.execute(
                    "SELECT COUNT(*) FROM journal_entry WHERE mapping_revision = 'agent-llm-cost-v1'"
                ).fetchone()[0]
                self.assertEqual(gl_count, 2)

    def test_run_company_cli_starts_serves_and_stops_cleanly(self) -> None:
        with (
            TemporaryDirectory(prefix="sd-cli-run-") as tmp,
            TemporaryDirectory(prefix="sd-", dir="/tmp") as sock_tmp,
        ):
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            sock_dir = Path(sock_tmp)
            founding = found(
                store,
                FoundingTerms(
                    "sourdough-fund",
                    "charter-v1",
                    "paper",
                    self.ceo_profile.profile_id(),
                ),
            )
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "sourdough.cli",
                    "run-company",
                    str(store),
                    "--company-id",
                    founding.company_id,
                    "--socket-dir",
                    str(sock_dir),
                    "--lease-dir",
                    str(leases),
                    "--tick-interval",
                    "0.2",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdout is not None
                line = proc.stdout.readline()
                self.assertTrue(line.strip(), "Expected startup JSON announcement")
                info = json.loads(line)
                self.assertEqual(info["company_id"], founding.company_id)
                client = ControlPlaneClient(Path(info["socket_path"]))
                meta = client.meta()
                self.assertEqual(meta["company_id"], founding.company_id)
            finally:
                proc.send_signal(signal.SIGTERM)
                _, stderr_out = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, stderr_out)

    def test_resident_service_rejects_nonpositive_tick_interval(self) -> None:
        with TemporaryDirectory(prefix="sd-bad-tick-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            founding = found(
                store,
                FoundingTerms("sourdough-fund", "charter-v1", "paper", "p1"),
            )
            registry = self._build_registry(founding.founder_id, "q1")
            from sourdough.runtime import RuntimeRefused

            with self.assertRaises(RuntimeRefused):
                ResidentCompanyService(
                    store,
                    founding.company_id,
                    registry,
                    tick_interval_seconds=0,
                )


if __name__ == "__main__":
    unittest.main()
