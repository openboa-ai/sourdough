"""Unit tests for Phase-0 runtime hardening, ShiftRunner, desk_tools, ContextPack v2, and engine retries."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sourdough.agent_desk import desk_directory, scratch_directory
from sourdough.agent_runtime import (
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    AgentBudgetExceeded,
    AgentProfileBinding,
    AgentProfileMismatch,
    AgentRuntimeRefused,
    AgentTransportInterrupted,
    AnthropicMessagesAdapter,
    InMemorySecretResolver,
    OuterToolBroker,
    ShiftRequest,
    TokenUsage,
    build_default_adapter_registry,
    hydrate_shift_context,
    settle_shift_cost_to_ledger,
)
from sourdough.agent_runtime._http import render_context_system_blocks
from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    bind_board_principal,
    post_ceo_message,
)
from sourdough.company import FoundingTerms, found
from sourdough.company_assets import assign_project
from sourdough.duty_run import DutyRefused
from sourdough.episode_index import search_episodes
from sourdough.runtime import (
    CompanyRuntime,
    DutyContext,
    DutyOutcome,
    RunFenced,
    WorkKernelRefused,
    build_default_registry,
    build_production_duty_registry,
    create_work_item,
    sync_board_triggers,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.storage.schema import enable_domain_v1
from sourdough.trading import DUTY_QUANT_AGENT_SANDBOX, TradingDomainPlugin
from sourdough.writer_lease import hold_company_writer


class _Phase0MockServer:
    """Loopback server exercising Anthropic cache_control, tool error recovery, and mid-shift outage."""

    def __init__(self) -> None:
        self.anthropic_payloads: list[dict[str, Any]] = []
        self.openai_payloads: list[dict[str, Any]] = []
        self.mode = "recover_anthropic"
        self._lock = threading.Lock()
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/v1/messages":
                    with parent._lock:
                        parent.anthropic_payloads.append(payload)
                        step = len(parent.anthropic_payloads)
                        mode = parent.mode
                    if mode == "recover_anthropic":
                        if step == 1:
                            self._send(
                                200,
                                {
                                    "id": "m-1",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "call-bad-1",
                                            "name": "desk_read_draft",
                                            "input": {"name": "missing-draft"},
                                        }
                                    ],
                                    "usage": {
                                        "input_tokens": 500,
                                        "cache_creation_input_tokens": 100,
                                        "cache_read_input_tokens": 50,
                                        "output_tokens": 80,
                                    },
                                },
                            )
                        else:
                            self._send(
                                200,
                                {
                                    "id": "m-2",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "Recovered cleanly after tool refusal.",
                                        }
                                    ],
                                    "usage": {"input_tokens": 400, "output_tokens": 60},
                                },
                            )
                    elif mode == "outage_round2":
                        if step == 1:
                            self._send(
                                200,
                                {
                                    "id": "m-out-1",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "call-list-1",
                                            "name": "desk_list_drafts",
                                            "input": {},
                                        }
                                    ],
                                    "usage": {
                                        "input_tokens": 1000,
                                        "output_tokens": 200,
                                    },
                                },
                            )
                        else:
                            self._send(503, {"error": "upstream unavailable"})
                elif self.path == "/v1/responses":
                    with parent._lock:
                        parent.openai_payloads.append(payload)
                        mode = parent.mode
                    if mode == "ceo_desk_flow":
                        if not payload.get("previous_response_id"):
                            self._send(
                                200,
                                {
                                    "id": "oa-1",
                                    "output": [
                                        {
                                            "type": "function_call",
                                            "call_id": "ceo-save-1",
                                            "name": "desk_save_draft",
                                            "arguments": json.dumps(
                                                {
                                                    "name": "board-note",
                                                    "content": "Memo for Board review. "
                                                    * 250,
                                                }
                                            ),
                                        }
                                    ],
                                    "usage": {
                                        "input_tokens": 900,
                                        "output_tokens": 150,
                                    },
                                },
                            )
                        else:
                            self._send(
                                200,
                                {
                                    "id": "oa-2",
                                    "output": [
                                        {
                                            "type": "message",
                                            "content": [
                                                {
                                                    "type": "output_text",
                                                    "text": "Draft saved and reviewed.",
                                                }
                                            ],
                                        }
                                    ],
                                    "usage": {
                                        "input_tokens": 700,
                                        "output_tokens": 100,
                                    },
                                },
                            )
                    elif mode == "ceo_missing_quote":
                        if not payload.get("previous_response_id"):
                            self._send(
                                200,
                                {
                                    "id": "oa-mq-1",
                                    "output": [
                                        {
                                            "type": "function_call",
                                            "call_id": "mq-call-1",
                                            "name": "read_market_quote",
                                            "arguments": json.dumps(
                                                {"instrument_id": "DOGE-USD"}
                                            ),
                                        }
                                    ],
                                    "usage": {"input_tokens": 200, "output_tokens": 40},
                                },
                            )
                        else:
                            self._send(
                                200,
                                {
                                    "id": "oa-mq-2",
                                    "output": [
                                        {
                                            "type": "message",
                                            "content": [
                                                {
                                                    "type": "output_text",
                                                    "text": "DOGE-USD quote unavailable; continuing safely.",
                                                }
                                            ],
                                        }
                                    ],
                                    "usage": {"input_tokens": 250, "output_tokens": 50},
                                },
                            )
                    elif mode == "outage_round2_openai":
                        if not payload.get("previous_response_id"):
                            self._send(
                                200,
                                {
                                    "id": "oa-out-1",
                                    "output": [
                                        {
                                            "type": "function_call",
                                            "call_id": "out-list-1",
                                            "name": "desk_list_drafts",
                                            "arguments": "{}",
                                        }
                                    ],
                                    "usage": {
                                        "input_tokens": 12000,
                                        "output_tokens": 3000,
                                    },
                                },
                            )
                        else:
                            self._send(503, {"error": "upstream unavailable"})
                    elif mode == "desk_run_cmd_unlock":
                        if not payload.get("previous_response_id"):
                            self._send(
                                200,
                                {
                                    "id": "oa-cmd-1",
                                    "output": [
                                        {
                                            "type": "function_call",
                                            "call_id": "cmd-call-1",
                                            "name": "desk_run_command",
                                            "arguments": json.dumps(
                                                {
                                                    "command": [
                                                        sys.executable,
                                                        "-c",
                                                        "import time; time.sleep(0.25); print('unlocked')",
                                                    ]
                                                }
                                            ),
                                        }
                                    ],
                                    "usage": {"input_tokens": 100, "output_tokens": 20},
                                },
                            )
                        else:
                            self._send(
                                200,
                                {
                                    "id": "oa-cmd-2",
                                    "output": [
                                        {
                                            "type": "message",
                                            "content": [
                                                {
                                                    "type": "output_text",
                                                    "text": "Subprocess ran without holding tx_lock.",
                                                }
                                            ],
                                        }
                                    ],
                                    "usage": {"input_tokens": 100, "output_tokens": 20},
                                },
                            )
                    else:
                        self._send(
                            200,
                            {
                                "id": "oa-big",
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "Over budget",
                                            }
                                        ],
                                    }
                                ],
                                "usage": {
                                    "input_tokens": 500_000,
                                    "output_tokens": 100_000,
                                },
                            },
                        )

            def _send(self, code: int, body: dict[str, Any]) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(code)
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


class RuntimeShiftRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = _Phase0MockServer()
        self.resolver = InMemorySecretResolver(
            {
                "envref:KEY_OPENAI": "oa-secret-test",
                "envref:KEY_ANTHROPIC": "ant-secret-test",
            }
        )
        self.ceo_profile = AgentProfileBinding(
            provider=PROVIDER_OPENAI,
            model="gpt-5-mini",
            revision="2026-11",
            credential_handle_ref="envref:KEY_OPENAI",
        )
        self.ant_profile = AgentProfileBinding(
            provider=PROVIDER_ANTHROPIC,
            model="claude-sonnet-4-5",
            revision="2026-11",
            credential_handle_ref="envref:KEY_ANTHROPIC",
        )

    def tearDown(self) -> None:
        self.mock.close()

    def test_cost_accounting_posts_gl_before_raising_budget_exceeded(self) -> None:
        with TemporaryDirectory(prefix="sd-cost-gl-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store, FoundingTerms("cost-co", "charter-v1", "manage", "prof-1")
            )
            lock = threading.RLock()
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as writer:
                with self.assertRaises(AgentBudgetExceeded):
                    settle_shift_cost_to_ledger(
                        writer,
                        profile=self.ant_profile,
                        run_id="run-over-1",
                        turn_id="turn-1",
                        usage=TokenUsage(100_000, 50_000),
                        budget_ceiling_minor_units=2,
                        tx_lock=lock,
                    )
            with closing(open_reader(store)) as db, read_tx(db):
                row = db.execute(
                    """SELECT e.id, l.account, l.amount_minor
                       FROM journal_entry e
                       JOIN journal_line l ON l.entry_id = e.id
                       WHERE e.source_event = 'llm-turn:run-over-1:turn-1'
                       ORDER BY l.account"""
                ).fetchall()
                self.assertEqual(len(row), 2)
                self.assertEqual(row[0][1], "expense:agent_llm_compute")
                self.assertGreater(row[0][2], 2)
                self.assertEqual(row[1][1], "liability:accounts_payable")
                self.assertEqual(row[1][2], -row[0][2])

    def test_outer_tool_broker_fencing_loop_breaker_and_refusal_classification(
        self,
    ) -> None:
        fenced = False
        receipts: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []

        def _check_fence() -> None:
            if fenced:
                raise RunFenced("Run lease lost")

        def _domain_err(_args: dict[str, Any]) -> dict[str, Any]:
            raise WorkKernelRefused("Invalid argument domain constraint")

        def _fatal_storage(_args: dict[str, Any]) -> dict[str, Any]:
            raise RunFenced("Fenced inside handler")

        broker = OuterToolBroker(
            company_id="co-1",
            agent_id="a-1",
            run_id="r-1",
            fencing_token=1,
            handlers={
                "echo": lambda a: {"ok": True, "v": a["v"], "big": "x" * 5000},
                "domain_bad": _domain_err,
                "fatal_fence": _fatal_storage,
            },
            max_tool_calls=12,
            fencing_check=_check_fence,
            receipt_recorder=receipts.append,
            trace_recorder=traces.append,
            offload_handler=lambda seq, name, res: (
                {"offloaded": True, "log_path": f".desk/logs/{seq:04d}-{name}.json"}
                if len(json.dumps(res)) > 4096
                else res
            ),
            max_repeated_identical_calls=3,
        )

        # Forbidden actor_id and created_by_agent_id are always fatal.
        for key in ("actor_id", "created_by_agent_id"):
            with self.assertRaises(AgentRuntimeRefused):
                broker.invoke(f"sp-{key}", "echo", {"v": 1, key: "spoof"})

        # Recoverable domain refusal returns structured ok=False dict and records refused receipt.
        refused_res = broker.invoke("c-dom", "domain_bad", {})
        self.assertEqual(refused_res["ok"], False)
        self.assertEqual(refused_res["error_type"], "WorkKernelRefused")
        self.assertEqual(receipts[-1]["status"], "refused")

        # Offloading triggers on >4KB result.
        big_res = broker.invoke("c-1", "echo", {"v": 1})
        self.assertTrue(big_res["offloaded"])
        self.assertEqual(
            receipts[-1]["offloaded_log_path"], ".desk/logs/0002-echo.json"
        )

        # Repeated identical calls: 2nd and 3rd succeed, 4th trips loop-breaker refusal, 5th aborts fatally.
        broker.invoke("c-2", "echo", {"v": 1})
        broker.invoke("c-3", "echo", {"v": 1})
        loop_refusal = broker.invoke("c-4", "echo", {"v": 1})
        self.assertEqual(loop_refusal["ok"], False)
        self.assertEqual(loop_refusal["error_type"], "TrajectoryLoopDetected")
        with self.assertRaises(AgentRuntimeRefused):
            broker.invoke("c-5", "echo", {"v": 1})

        # RunFenced inside handler is NEVER swallowed by WorkKernelRefused catch.
        with self.assertRaises(RunFenced):
            broker.invoke("c-fatal", "fatal_fence", {})
        self.assertEqual(receipts[-1]["status"], "fatal")

        # Fencing check runs BEFORE _call_cache replay!
        fenced = True
        with self.assertRaises(RunFenced):
            broker.invoke("c-1", "echo", {"v": 1})

    def test_context_pack_v2_compaction_and_anthropic_cache_split(self) -> None:
        pack = hydrate_shift_context(
            company_id="co-1",
            agent_id="a-1",
            run_id="r-1",
            fencing_token=3,
            grant_scope="manage",
            mandate_summary="Mandate summary",
            open_receipts=({"id": "rcpt-1"},),
            history_notes=tuple(f"hist-{i}-" + ("z" * 120) for i in range(10)),
            desk_manifest=({"name": "draft-1", "revision": "rev1", "bytes": 120},),
            active_methods=({"method_id": "m1", "title": "Check first"},),
            active_snapshot_id="snap-v2",
            recent_episodes=tuple(
                {"episode_id": f"ep-{i}", "summary": "s" * 120} for i in range(6)
            ),
            max_context_bytes=950,
        )
        self.assertTrue(pack.compacted)
        self.assertEqual(pack.open_receipts, ({"id": "rcpt-1"},))
        self.assertEqual(pack.active_snapshot_id, "snap-v2")
        self.assertEqual(len(pack.active_methods), 1)
        self.assertLess(len(pack.recent_episodes), 6)

        req1 = ShiftRequest(
            company_id="co-1",
            agent_id="a-1",
            run_id="run-A",
            turn_id="turn-1",
            fencing_token=1,
            instruction="Do A",
            context=pack,
            tools=(),
            budget_ceiling_minor_units=100,
        )
        pack2 = hydrate_shift_context(
            company_id="co-1",
            agent_id="a-1",
            run_id="run-B",
            fencing_token=9,
            grant_scope="manage",
            mandate_summary="Mandate summary",
            active_methods=({"method_id": "m1", "title": "Check first"},),
            active_snapshot_id="snap-v2",
            recent_episodes=({"episode_id": "ep-99", "summary": "different"},),
        )
        self.assertNotEqual(pack.digest, pack2.digest)
        req2 = ShiftRequest(
            company_id="co-1",
            agent_id="a-1",
            run_id="run-B",
            turn_id="turn-1",
            fencing_token=9,
            instruction="Do B",
            context=pack2,
            tools=(),
            budget_ceiling_minor_units=100,
        )
        p1, d1 = render_context_system_blocks(req1)
        p2, d2 = render_context_system_blocks(req2)
        self.assertEqual(p1, p2)
        self.assertNotEqual(d1, d2)

        # Anthropic adapter sends cache_control ephemeral on stable prefix + is_error=True on refusal.
        self.mock.mode = "recover_anthropic"
        adapter = AnthropicMessagesAdapter(
            secret_resolver=self.resolver,
            base_url_override=self.mock.base_url,
            allow_loopback_override=True,
        )
        broker = OuterToolBroker(
            company_id="co-1",
            agent_id="a-1",
            run_id="r-1",
            fencing_token=1,
            handlers={
                "desk_read_draft": lambda _a: (_ for _ in ()).throw(
                    DutyRefused("Draft missing")
                )
            },
        )
        result = adapter.run_shift(self.ant_profile, req1, broker)
        self.assertEqual(result.output_text, "Recovered cleanly after tool refusal.")
        self.assertEqual(broker.accrued_usage, TokenUsage(1050, 140))

        first_req = self.mock.anthropic_payloads[0]
        self.assertIsInstance(first_req["system"], list)
        self.assertEqual(first_req["system"][0]["cache_control"], {"type": "ephemeral"})
        second_req = self.mock.anthropic_payloads[1]
        tool_result_msg = second_req["messages"][-1]["content"][0]
        self.assertTrue(tool_result_msg["is_error"])

    def test_engine_fatal_vs_bounded_transient_retry_and_tx_lock(self) -> None:
        with TemporaryDirectory(prefix="sd-eng-retry-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store, FoundingTerms("retry-co", "charter-v1", "manage", "prof-1")
            )
            with closing(open_writer(store)) as db, write_tx(db):
                fatal_item, _ = create_work_item(
                    db,
                    company_id=founding.company_id,
                    owner_agent_id=founding.founder_id,
                    duty_kind="work.fatal",
                    source_kind="manual",
                    source_id="fatal-1",
                    now_ns=100,
                )
                net_item, _ = create_work_item(
                    db,
                    company_id=founding.company_id,
                    owner_agent_id=founding.founder_id,
                    duty_kind="work.net",
                    source_kind="manual",
                    source_id="net-1",
                    now_ns=200,
                )

            seen_locks: list[threading.RLock | None] = []

            def _fatal_fn(ctx: DutyContext) -> DutyOutcome:
                seen_locks.append(ctx.tx_lock)
                raise AgentProfileMismatch("wrong profile")

            def _net_fn(ctx: DutyContext) -> DutyOutcome:
                with ctx.write_lock_ctx():
                    raise AgentTransportInterrupted("503 outage")

            lock = threading.RLock()
            runtime = CompanyRuntime(
                store,
                founding.company_id,
                build_default_registry({"work.fatal": _fatal_fn, "work.net": _net_fn}),
                lease_directory=leases,
                max_transient_attempts=3,
                retry_base_backoff_ns=1_000,
                retry_max_backoff_ns=10_000,
            )

            # Fatal error transitions directly to blocked without retry.
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as writer:
                with self.assertRaises(AgentProfileMismatch):
                    runtime.step_with_writer(
                        writer, max_dispatches=1, now_ns=1_000, tx_lock=lock
                    )
            self.assertEqual(seen_locks, [lock])
            with closing(open_reader(store)) as db, read_tx(db):
                state_row = db.execute(
                    "SELECT state, attempt_count, next_retry_at_ns FROM work_items WHERE work_item_id = ?",
                    (fatal_item.work_item_id,),
                ).fetchone()
                self.assertEqual(state_row, ("blocked", 1, None))

            # Transient error #1 sets next_retry_at_ns = 2_000 + 1_000 = 3_000.
            with self.assertRaises(AgentTransportInterrupted):
                runtime.step(max_dispatches=1, now_ns=2_000)
            # Before next_retry_at_ns (2_500 < 3_000), item is skipped by _claim_next.
            skipped = runtime.step(max_dispatches=1, now_ns=2_500)
            self.assertEqual(skipped.executed_runs, ())

            # Attempt #2 at 3_000 sets backoff 2_000 -> next_retry_at_ns = 5_000.
            with self.assertRaises(AgentTransportInterrupted):
                runtime.step(max_dispatches=1, now_ns=3_000)
            # Attempt #3 hits max_transient_attempts=3 -> transitions to blocked!
            with self.assertRaises(AgentTransportInterrupted):
                runtime.step(max_dispatches=1, now_ns=5_000)
            with closing(open_reader(store)) as db, read_tx(db):
                net_row = db.execute(
                    "SELECT state, attempt_count, next_retry_at_ns FROM work_items WHERE work_item_id = ?",
                    (net_item.work_item_id,),
                ).fetchone()
                self.assertEqual(net_row, ("blocked", 3, None))

    def test_shift_runner_desk_tools_offload_episode_and_trading_adoption(self) -> None:
        with TemporaryDirectory(prefix="sd-shift-runner-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms(
                    "shift-co",
                    "charter-v1",
                    "paper",
                    self.ceo_profile.profile_id(),
                ),
            )
            quant_id = "agent-quant-1"
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    enable_domain_v1(
                        db, "trading", company_id=founding.company_id, actor_id="test"
                    )
                    db.execute(
                        "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                        (quant_id, founding.company_id, self.ant_profile.profile_id()),
                    )
                    db.execute(
                        "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                        ("grant-q", quant_id, "paper"),
                    )
                bind_board_principal(
                    writer, principal_id="board-1", role_name=BOARD_CONTROL_ROLE
                )
                post_ceo_message(
                    writer,
                    command_id="cmd-ceo-1",
                    target_agent_id=founding.founder_id,
                    charter_revision="charter-v1",
                    body="Draft a board memo.",
                    principal_hint="board-1",
                )
                sync_board_triggers(writer, founding.company_id)

            adapter_registry = build_default_adapter_registry(
                secret_resolver=self.resolver,
                openai_base_url=self.mock.base_url,
                anthropic_base_url=self.mock.base_url,
                allow_loopback_override=True,
            )
            duty_registry = build_production_duty_registry(
                adapter_registry=adapter_registry,
                profile_bindings={
                    founding.founder_id: self.ceo_profile,
                    quant_id: self.ant_profile,
                },
                domain_plugins=(TradingDomainPlugin(),),
            )
            runtime = CompanyRuntime(
                store, founding.company_id, duty_registry, lease_directory=leases
            )

            # 1. Core CEO shift uses ShiftRunner + desk_save_draft + cleans scratch + records episode & FTS5.
            self.mock.mode = "ceo_desk_flow"
            summary = runtime.step(max_dispatches=4, now_ns=10_000)
            self.assertEqual(len(summary.executed_runs), 1)
            ceo_run_id = summary.executed_runs[0]

            # Scratch directory cleaned up after run.
            self.assertFalse(
                scratch_directory(store, founding.founder_id, ceo_run_id).exists()
            )
            # Draft written on disk via desk_save_draft.
            draft_file = (
                desk_directory(store, founding.founder_id)
                / ".desk"
                / "drafts"
                / "board-note.json"
            )
            self.assertTrue(draft_file.is_file())

            with closing(open_reader(store)) as db, read_tx(db):
                rcpt = db.execute(
                    "SELECT tool_name, status FROM tool_call_receipt WHERE run_id = ?",
                    (ceo_run_id,),
                ).fetchall()
                self.assertEqual(rcpt, [("desk_save_draft", "ok")])
                ep_rows = db.execute(
                    "SELECT episode_id, outcome_status, summary_text FROM agent_episode WHERE run_id = ?",
                    (ceo_run_id,),
                ).fetchall()
                self.assertEqual(len(ep_rows), 1)
                self.assertEqual(ep_rows[0][1], "completed")

            hits = search_episodes(
                store, founding.company_id, founding.founder_id, "Draft saved"
            )
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["run_id"], ceo_run_id)

            # 2. Trading shift via ShiftRunner recovers from tool refusal and receives Core Desk tools.
            self.mock.mode = "recover_anthropic"
            self.mock.anthropic_payloads.clear()
            with closing(open_writer(store)) as db, write_tx(db):
                q_item, _ = create_work_item(
                    db,
                    company_id=founding.company_id,
                    owner_agent_id=quant_id,
                    duty_kind=DUTY_QUANT_AGENT_SANDBOX,
                    source_kind="manual",
                    source_id="q-sbx-1",
                    now_ns=20_000,
                )
            q_summary = runtime.step(max_dispatches=2, now_ns=21_000)
            self.assertEqual(len(q_summary.executed_runs), 1)
            sent_tool_names = {
                t["name"] for t in self.mock.anthropic_payloads[0]["tools"]
            }
            self.assertIn("open_agent_sandbox", sent_tool_names)
            self.assertIn("desk_list_drafts", sent_tool_names)
            self.assertIn("memory_search_episodes", sent_tool_names)
            self.assertNotIn("desk_run_command", sent_tool_names)

            # 3. Partial round outage on Anthropic still settles round-1 GL cost and records failed episode.
            self.mock.mode = "outage_round2"
            self.mock.anthropic_payloads.clear()
            with closing(open_writer(store)) as db, write_tx(db):
                fail_item, _ = create_work_item(
                    db,
                    company_id=founding.company_id,
                    owner_agent_id=quant_id,
                    duty_kind=DUTY_QUANT_AGENT_SANDBOX,
                    source_kind="manual",
                    source_id="q-sbx-outage",
                    now_ns=30_000,
                )
            with self.assertRaises(AgentTransportInterrupted):
                runtime.step(max_dispatches=1, now_ns=31_000)
            with closing(open_reader(store)) as db, read_tx(db):
                gl_row = db.execute(
                    """SELECT COUNT(*) FROM journal_entry
                       WHERE source = 'agent_llm:anthropic'"""
                ).fetchone()
                # Both the recovered shift and the partial-outage shift posted GL entries!
                self.assertEqual(gl_row[0], 2)
                ep_fail = db.execute(
                    "SELECT outcome_status, input_tokens FROM agent_episode WHERE work_item_id = ?",
                    (fail_item.work_item_id,),
                ).fetchone()
                self.assertEqual(ep_fail, ("failed", 1000))

            # 4. Budget exceeded posts GL first, records blocked episode, and engine blocks item.
            self.mock.mode = "over_budget"
            with closing(open_writer(store)) as db, write_tx(db):
                budget_item, _ = create_work_item(
                    db,
                    company_id=founding.company_id,
                    owner_agent_id=founding.founder_id,
                    duty_kind="ceo.respond_message",
                    source_kind="manual",
                    source_id="ceo-over-budget",
                    payload={"message_id": "m-over", "body": "Hi"},
                    now_ns=40_000,
                )
            low_budget_registry = build_production_duty_registry(
                adapter_registry=adapter_registry,
                profile_bindings={founding.founder_id: self.ceo_profile},
                default_budget_ceiling_minor=1,
            )
            low_runtime = CompanyRuntime(
                store, founding.company_id, low_budget_registry, lease_directory=leases
            )
            with self.assertRaises(AgentBudgetExceeded):
                low_runtime.step(max_dispatches=1, now_ns=41_000)
            with closing(open_reader(store)) as db, read_tx(db):
                b_state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (budget_item.work_item_id,),
                ).fetchone()
                self.assertEqual(b_state, ("blocked",))
                ep_blk = db.execute(
                    "SELECT outcome_status, cost_minor_units FROM agent_episode WHERE work_item_id = ?",
                    (budget_item.work_item_id,),
                ).fetchone()
                self.assertEqual(ep_blk[0], "blocked")
                self.assertGreater(ep_blk[1], 1)

    def test_desk_tools_exec_gate_and_offload_read(self) -> None:
        from sourdough.runtime.desk_tools import build_core_desk_tool_contribution

        with TemporaryDirectory(prefix="sd-desk-tools-exec-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms("exec-co", "charter-v1", "manage-company", "prof-1"),
            )
            with closing(open_writer(store)) as db, write_tx(db):
                db.execute(
                    """INSERT INTO work_responsibilities (
                           responsibility_id, company_id, owner_agent_id, duty_kind,
                           remit, readable_scope_json, status, created_at_ns, updated_at_ns
                       ) VALUES (?, ?, ?, ?, ?, ?, 'active', 100, 100)""",
                    (
                        "resp-exec",
                        founding.company_id,
                        founding.founder_id,
                        "work.exec",
                        "Execute sandboxed commands",
                        json.dumps({"grants": ["desk:exec"]}),
                    ),
                )
                item, _ = create_work_item(
                    db,
                    company_id=founding.company_id,
                    owner_agent_id=founding.founder_id,
                    duty_kind="work.exec",
                    source_kind="manual",
                    source_id="exec-1",
                    now_ns=100,
                )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as writer:
                from sourdough.duty_run import work_run_claim_from_context
                from sourdough.runtime.work_kernel import claim_work_run

                assign_project(
                    writer,
                    founding.grant_id,
                    "proj-1",
                    (founding.founder_id,),
                    publishers=(founding.founder_id,),
                )
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    run = claim_work_run(
                        db,
                        company_id=founding.company_id,
                        work_item_id=item.work_item_id,
                        lease_owner="test",
                        lease_ttl_ns=300_000_000_000,
                        now_ns=200,
                    )
                    claim = work_run_claim_from_context(
                        db,
                        DutyContext(
                            writer, founding.company_id, item, run, lambda: 200
                        ),
                    )

                contrib = build_core_desk_tool_contribution(
                    writer=writer,
                    claim=claim,
                    purpose=claim.purpose,
                    sandbox_backend="local-macos",
                )
                spec_names = {s.name for s in contrib.specs}
                self.assertIn("desk_run_command", spec_names)

                # Exercise desk_save_draft -> desk_list_drafts -> desk_publish_selection -> read_shared_asset -> desk_delete_draft.
                saved = contrib.handlers["desk_save_draft"](
                    {"name": "alpha", "content": "Hello shared world"}
                )
                listed = contrib.handlers["desk_list_drafts"]({})
                self.assertEqual(len(listed["drafts"]), 1)

                proj_info = contrib.handlers["discover_project_references"]({})
                proj_rev = proj_info["projects"][0]["revision"]
                pub = contrib.handlers["desk_publish_selection"](
                    {
                        "request_id": "req-pub-1",
                        "draft_name": "alpha",
                        "draft_revision": saved["revision"],
                        "project_id": "proj-1",
                        "project_revision": proj_rev,
                        "audience": [founding.founder_id],
                        "start": 0,
                        "end": 12,
                    }
                )
                shared = contrib.handlers["read_shared_asset"](
                    {"asset_id": pub["asset_id"]}
                )
                self.assertEqual(shared["content"], "Hello shared")

                # Write an offloaded log and read it paged.
                logs_dir = desk_directory(store, founding.founder_id) / ".desk" / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                (logs_dir / "0001-big.json").write_text(
                    "0123456789ABCDEF", encoding="utf-8"
                )
                chunk1 = contrib.handlers["desk_read_offloaded_log"](
                    {"log_path": ".desk/logs/0001-big.json", "offset": 0, "limit": 10}
                )
                self.assertEqual(chunk1["content"], "0123456789")
                self.assertFalse(chunk1["eof"])
                chunk2 = contrib.handlers["desk_read_offloaded_log"](
                    {"log_path": ".desk/logs/0001-big.json", "offset": 10, "limit": 10}
                )
                self.assertEqual(chunk2["content"], "ABCDEF")
                self.assertTrue(chunk2["eof"])

                deleted = contrib.handlers["desk_delete_draft"]({"name": "alpha"})
                self.assertTrue(deleted["deleted"])

                if sys.platform == "darwin":
                    cmd_res = contrib.handlers["desk_run_command"](
                        {"command": [sys.executable, "-c", "print('sandbox-ok')"]}
                    )
                    self.assertTrue(cmd_res["ok"])
                    self.assertEqual(cmd_res["stdout"].strip(), "sandbox-ok")

    def test_ceo_respond_message_recovers_from_trading_plugin_refusal(self) -> None:
        from sourdough.trading.quotes import MarketQuote, StaticQuoteProvider

        with TemporaryDirectory(prefix="sd-ceo-trading-refuse-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms(
                    "ceo-trd-co",
                    "charter-v1",
                    "paper",
                    self.ceo_profile.profile_id(),
                ),
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    enable_domain_v1(
                        db, "trading", company_id=founding.company_id, actor_id="test"
                    )
                bind_board_principal(
                    writer, principal_id="board-1", role_name=BOARD_CONTROL_ROLE
                )
                post_ceo_message(
                    writer,
                    command_id="cmd-ceo-q-1",
                    target_agent_id=founding.founder_id,
                    charter_revision="charter-v1",
                    body="Check DOGE-USD quote.",
                    principal_hint="board-1",
                )
                sync_board_triggers(writer, founding.company_id)

            plugin = TradingDomainPlugin(
                quote_provider=StaticQuoteProvider(
                    {
                        "BTC-USDT": MarketQuote(
                            instrument_id="BTC-USDT",
                            price_minor_units=9_000_000,
                            observed_at_ns=10_000,
                            source="static",
                        )
                    }
                )
            )
            adapter_registry = build_default_adapter_registry(
                secret_resolver=self.resolver,
                openai_base_url=self.mock.base_url,
                allow_loopback_override=True,
            )
            duty_registry = build_production_duty_registry(
                adapter_registry=adapter_registry,
                profile_bindings={founding.founder_id: self.ceo_profile},
                domain_plugins=(plugin,),
            )
            runtime = CompanyRuntime(
                store, founding.company_id, duty_registry, lease_directory=leases
            )

            self.mock.mode = "ceo_missing_quote"
            self.mock.openai_payloads.clear()
            summary = runtime.step(max_dispatches=2, now_ns=15_000)
            self.assertEqual(len(summary.executed_runs), 1)
            ceo_run_id = summary.executed_runs[0]

            # Model received structured ok=False ExecutionTierRefused payload on turn 2.
            tool_out_raw = self.mock.openai_payloads[1]["input"][0]["output"]
            tool_out = json.loads(tool_out_raw)
            self.assertEqual(tool_out["ok"], False)
            self.assertEqual(tool_out["error_type"], "ExecutionTierRefused")

            with closing(open_reader(store)) as db, read_tx(db):
                rcpt = db.execute(
                    "SELECT tool_name, status, error_type FROM tool_call_receipt WHERE run_id = ?",
                    (ceo_run_id,),
                ).fetchall()
                self.assertEqual(
                    rcpt, [("read_market_quote", "refused", "ExecutionTierRefused")]
                )
                wi_state = db.execute(
                    "SELECT state FROM work_items WHERE duty_kind = 'ceo.respond_message'"
                ).fetchone()
                self.assertEqual(wi_state, ("settled",))

    def test_budget_exceeded_precedes_transport_error_and_desk_cmd_skips_tx_lock(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-budget-vs-net-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms(
                    "bgt-net-co",
                    "charter-v1",
                    "manage",
                    self.ceo_profile.profile_id(),
                ),
            )
            with closing(open_writer(store)) as db, write_tx(db):
                bgt_item, _ = create_work_item(
                    db,
                    company_id=founding.company_id,
                    owner_agent_id=founding.founder_id,
                    duty_kind="ceo.respond_message",
                    source_kind="manual",
                    source_id="bgt-net-1",
                    payload={"message_id": "m-bgt-net", "body": "Hi"},
                    now_ns=100,
                )

            adapter_registry = build_default_adapter_registry(
                secret_resolver=self.resolver,
                openai_base_url=self.mock.base_url,
                allow_loopback_override=True,
            )
            low_registry = build_production_duty_registry(
                adapter_registry=adapter_registry,
                profile_bindings={founding.founder_id: self.ceo_profile},
                default_budget_ceiling_minor=5,
            )
            runtime = CompanyRuntime(
                store, founding.company_id, low_registry, lease_directory=leases
            )

            self.mock.mode = "outage_round2_openai"
            with self.assertRaises(AgentBudgetExceeded):
                runtime.step(max_dispatches=1, now_ns=1_000)

            with closing(open_reader(store)) as db, read_tx(db):
                row = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (bgt_item.work_item_id,),
                ).fetchone()
                self.assertEqual(row, ("blocked",))
                ep = db.execute(
                    "SELECT outcome_status, input_tokens, output_tokens FROM agent_episode WHERE work_item_id = ?",
                    (bgt_item.work_item_id,),
                ).fetchone()
                self.assertEqual(ep, ("blocked", 12000, 3000))

            if sys.platform == "darwin":
                with closing(open_writer(store)) as db, write_tx(db):
                    db.execute(
                        """INSERT INTO work_responsibilities (
                               responsibility_id, company_id, owner_agent_id, duty_kind,
                               remit, readable_scope_json, status, created_at_ns, updated_at_ns
                           ) VALUES (?, ?, ?, ?, ?, ?, 'active', 200, 200)""",
                        (
                            "resp-cmd-unlock",
                            founding.company_id,
                            founding.founder_id,
                            "ceo.respond_message",
                            "Run local sandbox checks",
                            json.dumps({"grants": ["desk:exec"]}),
                        ),
                    )
                    cmd_item, _ = create_work_item(
                        db,
                        company_id=founding.company_id,
                        owner_agent_id=founding.founder_id,
                        duty_kind="ceo.respond_message",
                        source_kind="manual",
                        source_id="cmd-unlock-1",
                        payload={"message_id": "m-cmd-unlock", "body": "Run check"},
                        now_ns=200,
                    )

                ceo_unlock_profile = AgentProfileBinding(
                    provider=PROVIDER_OPENAI,
                    model="gpt-5-mini",
                    revision="2026-11",
                    credential_handle_ref="envref:KEY_OPENAI",
                    sandbox_backend="local-macos",
                )
                unlock_registry = build_production_duty_registry(
                    adapter_registry=adapter_registry,
                    profile_bindings={founding.founder_id: ceo_unlock_profile},
                )
                unlock_runtime = CompanyRuntime(
                    store, founding.company_id, unlock_registry, lease_directory=leases
                )
                self.mock.mode = "desk_run_cmd_unlock"
                tx_lock = threading.RLock()
                acquired_during_cmd = {"ok": False}
                done = threading.Event()

                def _probe_lock() -> None:
                    while not done.is_set():
                        if len(self.mock.openai_payloads) >= 1:
                            if tx_lock.acquire(blocking=False):
                                acquired_during_cmd["ok"] = True
                                tx_lock.release()
                            return
                        done.wait(0.01)

                self.mock.openai_payloads.clear()
                t = threading.Thread(target=_probe_lock, daemon=True)
                t.start()
                try:
                    with hold_company_writer(
                        store, founding.company_id, lease_directory=leases
                    ) as writer:
                        unlock_runtime.step_with_writer(
                            writer, max_dispatches=1, now_ns=2_000, tx_lock=tx_lock
                        )
                finally:
                    done.set()
                    t.join(timeout=2.0)

                self.assertTrue(acquired_during_cmd["ok"])
                with closing(open_reader(store)) as db, read_tx(db):
                    wi = db.execute(
                        "SELECT state FROM work_items WHERE work_item_id = ?",
                        (cmd_item.work_item_id,),
                    ).fetchone()
                    self.assertEqual(wi, ("settled",))


if __name__ == "__main__":
    unittest.main()
