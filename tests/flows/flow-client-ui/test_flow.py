import http.client
import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns

from sourdough.board_control import (
    CEO_CHAT_COMMAND,
    DECISION_COMMAND,
    PAUSE_COMPANY,
    RESTRICTION_COMMAND,
    RESUME_COMPANY,
    SEND_CEO_MESSAGE,
    SUBMIT_DECISION_ANSWER,
    bind_board_principal,
    record_ceo_reply,
    record_decision_question,
    upsert_agent_profile,
)
from sourdough.company import FoundingTerms, found
from sourdough.ipc import ClientWebBridge, CompanyDaemon
from sourdough.storage.db import open_writer, write_tx
from sourdough.storage.schema import enable_domain_v1
from sourdough.writer_lease import hold_company_writer


class ConnectedFlow(unittest.TestCase):
    """End-to-end verification of the Board Client control plane and loopback web bridge."""

    def test_end_to_end(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="sd-ui-")))
        store = root / "c.sqlite"
        leases = root / "l"
        sock_dir = root / "s"
        pending_dir = root / "p"
        static_dir = root / "d"
        static_dir.mkdir()
        (static_dir / "index.html").write_text(
            "<!doctype html><html><body>Sourdough Board Client</body></html>",
            encoding="utf-8",
        )

        receipt = found(
            store, FoundingTerms("flow-ui-co", "charter-v1", "paper", "profile-v1")
        )
        company_id = receipt.company_id
        founder_id = receipt.founder_id
        now = time_ns()

        # Populate realistic multi-domain state across paper, desk opacity, messaging,
        # cancel!=flat, Unavailable!=0, and pending Board approvals.
        with hold_company_writer(store, company_id, lease_directory=leases) as writer:
            bind_board_principal(
                writer, principal_id="board-lead", role_name="board_control"
            )
            bind_board_principal(
                writer, principal_id="board-auditor", role_name="board_viewer"
            )
            upsert_agent_profile(
                writer,
                agent_id=founder_id,
                display_name="Founder · CEO",
                role_label="CEO",
                avatar_variant="wide-oval-top",
                monthly_budget_minor=120000,
            )
            record_ceo_reply(
                writer,
                body="Paper portfolio is live. Ready for Board review of v4 strategy.",
            )

            with closing(open_writer(store)) as db, write_tx(db):
                enable_domain_v1(db, "trading", company_id=company_id, actor_id="setup")

                # Add a second agent (Quant Research)
                quant_id = "agent-quant-01"
                db.execute(
                    "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                    (quant_id, company_id, "profile-v1"),
                )
                db.execute(
                    "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                    ("grant-quant-01", quant_id, "paper-research"),
                )
                db.execute(
                    "INSERT INTO private_desk (agent_id, manifest_revision) VALUES (?, ?)",
                    (quant_id, "desk-rev-1"),
                )
                db.execute(
                    """INSERT INTO duty (id, agent_id, state) VALUES (?, ?, 'running')""",
                    ("duty-quant-1", quant_id),
                )
                db.execute(
                    """INSERT INTO duty_run (run_id, duty_id, grant_id, grant_scope, purpose, is_current)
                       VALUES ('drun-1', 'duty-quant-1', 'grant-quant-01', 'paper-research', 'paper_execution', 1)"""
                )

                # Canonical Trading binding + valuations + partially-filled cancelled order + open position
                db.execute(
                    """INSERT INTO execution_environment_binding (
                           binding_id, company_id, tier, environment, owner_agent_id,
                           account_id, destination_id, connector_revision, instrument_rules_digest,
                           currency, starting_minor_units, exposure_limit_minor_units,
                           fee_bps, slippage_bps, quote_source, binding_revision, book_name,
                           book_role, status, created_at_ns, updated_at_ns
                       ) VALUES ('mandate-1', ?, 'company_paper', 'paper', NULL,
                                 'acct-paper-1', 'dest-1', 'nautilus-paper-v1', 'BTC-USD-SPOT',
                                 'USD', 10000000, 2500000, 0, 0, 'live_read_only', 'mandate-1',
                                 'Paper Desk', 'active', 'active', ?, ?)""",
                    (company_id, now, now),
                )
                db.execute(
                    """INSERT INTO trading_book_valuation (
                           valuation_id, binding_id, environment, company_id, label,
                           nav_minor_units, cash_minor_units, gross_exposure_minor_units,
                           realized_pnl_minor_units, unrealized_pnl_minor_units, total_fees_minor_units,
                           source, binding_revision, policy, evidence_json, revision, recorded_at_ns
                       ) VALUES
                       ('val-1', 'mandate-1', 'paper', ?, 'Day 1', 10050000, 10050000, 0, 50000, 0, 0, 'book_valuation', 'mandate-1', 'mid', '{"label":"Day 1"}', 'vrev-1', ?),
                       ('val-2', 'mandate-1', 'paper', ?, 'Day 2', 10050000, 10050000, 0, 50000, 0, 0, 'book_valuation', 'mandate-1', 'mid', '{"label":"Day 2"}', 'vrev-2', ?),
                       ('val-3', 'mandate-1', 'paper', ?, 'Day 3', 10185000, 10185000, 0, 185000, 0, 0, 'book_valuation', 'mandate-1', 'mid', '{"label":"Day 3"}', 'vrev-3', ?)""",
                    (company_id, now, company_id, now + 1, company_id, now + 2),
                )

                db.execute(
                    """INSERT INTO work_items (
                           work_item_id, company_id, owner_agent_id, duty_kind, state,
                           source_kind, source_id, payload_json, created_at_ns, updated_at_ns
                       ) VALUES ('wi-1', ?, ?, 'strategy.review', 'running', 'manual', 's1',
                                 '{"summary":"Audit v4 fee slippage"}', ?, ?)""",
                    (company_id, quant_id, now, now),
                )
                db.execute(
                    """INSERT INTO work_runs (
                           run_id, company_id, work_item_id, agent_id, duty_kind, status,
                           fencing_token, budget_ceiling_minor, created_at_ns, updated_at_ns
                       ) VALUES ('wrun-1', ?, 'wi-1', ?, 'strategy.review', 'running', 1, 5000, ?, ?)""",
                    (company_id, quant_id, now, now),
                )

                # Order with partial fill + cancel leaving residual open position!
                db.execute(
                    """INSERT INTO trading_book_order (
                           order_id, binding_id, environment, company_id, actor_id, action_id,
                           run_id, mandate_revision, cancelled_at_ns, cancel_reason,
                           instrument_id, side, quantity, limit_minor_units, market_price_minor_units,
                           quote_observed_at_ns, quote_source, fee_bps, slippage_bps,
                           status, filled_quantity, fill_price_minor_units, fee_minor_units,
                           created_at_ns, updated_at_ns
                       ) VALUES ('ord-041', 'mandate-1', 'paper', ?, ?, 'act-1',
                                 'wrun-1', 'mandate-1', ?, 'cancelled_partial',
                                 'BTC-USD-SPOT', 'buy', 10, 6500000, 6500000,
                                 ?, 'fixture', 0, 0,
                                 'cancelled', 4, 6498000, 1800, ?, ?)""",
                    (company_id, quant_id, now, now, now, now),
                )
                db.execute(
                    """INSERT INTO trading_book_fill (
                           fill_id, order_id, binding_id, environment, company_id, actor_id,
                           instrument_id, side, quantity, price_minor_units, fee_minor_units,
                           realized_pnl_minor_units, filled_at_ns
                       ) VALUES ('fill-041-a', 'ord-041', 'mandate-1', 'paper', ?, ?,
                                 'BTC-USD-SPOT', 'buy', 4, 6498000, 1800, 0, ?)""",
                    (company_id, quant_id, now),
                )
                db.execute(
                    """INSERT INTO trading_book_position (
                           binding_id, environment, company_id, instrument_id, quantity,
                           avg_entry_minor_units, mark_price_minor_units, realized_pnl_minor_units,
                           total_fees_minor_units, updated_at_ns
                       ) VALUES ('mandate-1', 'paper', ?, 'BTC-USD-SPOT', 4, 6498000, 6500000, 0, 1800, ?)""",
                    (company_id, now),
                )

                # Open accounting close issue -> Company Net P&L MUST be Unavailable (null), NEVER $0!
                db.execute(
                    """INSERT INTO accounting_close (request_id, scope, supersedes, request, receipt, assessed_ns)
                       VALUES ('close-1', 'monthly', NULL, '{"period":"2026-11"}',
                               '{"status":"incomplete","issues":[{"code":"missing_llm_invoice"}]}', ?)""",
                    (now,),
                )

                # Shared internal channel + inter-agent message (read-only for Board)
                db.execute(
                    """INSERT INTO shared_project (project_id, company_id, revision, members, publishers)
                       VALUES ('strategy-v4-review', ?, 'rev-1', ?, ?)""",
                    (
                        company_id,
                        json.dumps([founder_id, quant_id]),
                        json.dumps([founder_id, quant_id]),
                    ),
                )
                db.execute(
                    """INSERT INTO project_message (
                           message_id, company_id, project_id, sender_id, run_id,
                           body, asset_id, audience, membership_revision, request_id
                       ) VALUES ('pm-1', ?, 'strategy-v4-review', ?, 'wrun-1',
                                 'Fee variance dropped 14 bps across paired runs.',
                                 NULL, ?, 'rev-1', 'req-pm-1')""",
                    (company_id, quant_id, json.dumps([founder_id, quant_id])),
                )

            upsert_agent_profile(
                writer,
                agent_id="agent-quant-01",
                display_name="Quant Researcher",
                role_label="Quant Research",
                avatar_variant="tall-capsule-right",
                monthly_budget_minor=85000,
            )
            q_hash = record_decision_question(
                writer,
                question_id="q-v4",
                kind="strategy_promotion",
                title="Promote Strategy v4 to Paper",
                summary="Challenger passed sequential paired comparison.",
                impact={
                    "what_changes": [
                        {"label": "Active strategy", "value": "Strategy v4"}
                    ],
                    "what_stays_untouched": [
                        {"label": "Environment", "value": "Paper only"}
                    ],
                    "confirm_verb": "Promote Strategy v4",
                },
                created_by_agent_id=founder_id,
            )

        # Write a private Desk draft file with secret text -> MUST stay opaque (count=1, body NEVER leaked!)
        drafts_dir = root / "desks" / "agent-quant-01" / ".desk" / "drafts"
        drafts_dir.mkdir(mode=0o700, parents=True)
        (drafts_dir / "secret-alpha.json").write_text(
            json.dumps(
                {
                    "name": "secret-alpha",
                    "revision": "r1",
                    "content": "TOP_SECRET_UNPUBLISHED_DRAFT_BODY",
                }
            ),
            encoding="utf-8",
        )

        # Launch live CompanyDaemon + ClientWebBridge end-to-end.
        with (
            CompanyDaemon(
                store,
                company_id,
                socket_dir=sock_dir,
                lease_directory=leases,
            ) as daemon,
            ClientWebBridge(
                daemon.socket_path,
                static_dir=static_dir,
                pending_dir=pending_dir,
                principal="board-lead",
            ) as bridge,
        ):
            port = bridge.info.port

            def req(method: str, path: str, *, headers=None, body=b""):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    h = {"Host": f"127.0.0.1:{port}"}
                    if headers:
                        h.update(headers)
                    conn.putrequest(method, path, skip_host=True)
                    for k, v in h.items():
                        conn.putheader(k, v)
                    if body or method in ("POST", "PUT"):
                        conn.putheader("Content-Length", str(len(body)))
                    conn.endheaders(body)
                    r = conn.getresponse()
                    return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
                finally:
                    conn.close()

            # 1. Boot SPA HTML -> acquire HttpOnly SameSite=Strict session cookie.
            st, hdrs, html = req("GET", "/")
            self.assertEqual(st, 200)
            self.assertIn(b"Sourdough Board Client", html)
            cookie = hdrs["set-cookie"].split(";", 1)[0]
            auth = {"Cookie": cookie, "Origin": f"http://127.0.0.1:{port}"}

            # 2. Inspect Trading Desk (paper), verify retired routes return 404, and check order trace!
            st_lt_retired, _, _ = req(
                "GET", "/api/v1/projections/live-trading", headers=auth
            )
            self.assertEqual(st_lt_retired, 404)
            st_et_retired, _, _ = req(
                "GET", "/api/v1/projections/execution-tiers", headers=auth
            )
            self.assertEqual(st_et_retired, 404)

            st, td_hdrs, td_raw = req(
                "GET",
                "/api/v1/projections/trading-desk?environment=paper",
                headers=auth,
            )
            self.assertEqual(st, 200)
            td = json.loads(td_raw)
            self.assertTrue(td["trading_enabled"])
            self.assertEqual(len(td["open_close_issues"]), 1)
            self.assertEqual(len(td["cumulative_return_series"]), 3)
            self.assertEqual(len(td["execution_tape"]), 1)
            self.assertEqual(td["execution_tape"][0]["status"], "cancelled")
            self.assertEqual(td["execution_tape"][0]["filled_quantity"], 4)
            self.assertEqual(td["open_positions"][0]["quantity"], 4)

            st_trace, _, trace_raw = req(
                "GET", "/api/v1/projections/orders/ord-041", headers=auth
            )
            self.assertEqual(st_trace, 200)
            trace = json.loads(trace_raw)
            self.assertEqual(trace["order"]["order_id"], "ord-041")
            self.assertTrue(trace["order"]["cancel_not_flat"])
            self.assertEqual(trace["order"]["residual_position_qty"], 4)
            self.assertEqual(trace["order"]["fills"][0]["fill_id"], "fill-041-a")

            # ETag 304 caching works cleanly.
            st304, _, _ = req(
                "GET",
                "/api/v1/projections/trading-desk?environment=paper",
                headers=auth | {"If-None-Match": td_hdrs["etag"]},
            )
            self.assertEqual(st304, 304)

            # 3. Inspect Agents Graph & Agent Detail: opaque draft count=1, ZERO draft content leak!
            st, _, ag_raw = req("GET", "/api/v1/projections/agents-graph", headers=auth)
            self.assertEqual(st, 200)
            self.assertNotIn(b"TOP_SECRET_UNPUBLISHED_DRAFT_BODY", ag_raw)
            self.assertNotIn(b"secret-alpha", ag_raw)
            ag = json.loads(ag_raw)
            self.assertEqual(ag["default_view_mode"], "graph")
            quant_node = next(
                n for n in ag["nodes"] if n["agent_id"] == "agent-quant-01"
            )
            self.assertEqual(quant_node["private_draft_count"], 1)
            self.assertEqual(quant_node["role_label"], "Quant Research")
            self.assertTrue(
                any(e["channel_id"] == "strategy-v4-review" for e in ag["edges"])
            )

            st, _, det_raw = req(
                "GET", "/api/v1/projections/agents/agent-quant-01", headers=auth
            )
            self.assertEqual(st, 200)
            self.assertNotIn(b"TOP_SECRET_UNPUBLISHED_DRAFT_BODY", det_raw)
            det = json.loads(det_raw)
            self.assertEqual(det["desk_workspace"]["private_draft_count"], 1)

            # 4. Inspect Unified Company Messages: writable CEO 1:1 + read-only internal channel.
            st, _, msg_raw = req("GET", "/api/v1/projections/messages", headers=auth)
            self.assertEqual(st, 200)
            msgs = json.loads(msg_raw)
            self.assertTrue(msgs["ceo_thread"]["writable"])
            self.assertEqual(
                msgs["internal_channels"][0]["channel_id"], "strategy-v4-review"
            )
            self.assertTrue(msgs["internal_channels"][0]["read_only"])

            # 5. Send advisory CEO message via web bridge + verify NDJSON event stream records it.
            st, _, cmd_raw = req(
                "POST",
                "/api/v1/commands",
                headers=auth
                | {
                    "Content-Type": "application/json",
                    "Idempotency-Key": "flow-msg-1",
                },
                body=json.dumps(
                    {
                        "company_id": company_id,
                        "command_id": "flow-msg-1",
                        "command_type": SEND_CEO_MESSAGE,
                        "contract_version": CEO_CHAT_COMMAND,
                        "preconditions": {
                            "ceo_agent_id": founder_id,
                            "charter_revision": "charter-v1",
                        },
                        "payload": {
                            "target_agent_id": founder_id,
                            "body": "Confirm fee variance before promoting v4.",
                        },
                    }
                ).encode("utf-8"),
            )
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(cmd_raw)["state"], "applied")

            # 6. Resolve Board approval inline + verify competing stops non-clearing.
            st, _, app_raw = req(
                "POST",
                "/api/v1/commands",
                headers=auth
                | {
                    "Content-Type": "application/json",
                    "Idempotency-Key": "flow-app-1",
                },
                body=json.dumps(
                    {
                        "company_id": company_id,
                        "command_id": "flow-app-1",
                        "command_type": SUBMIT_DECISION_ANSWER,
                        "contract_version": DECISION_COMMAND,
                        "preconditions": {
                            "question_id": "q-v4",
                            "expected_revision": 1,
                            "expected_payload_hash": q_hash,
                        },
                        "payload": {"choice": "approve", "reason": "Approved in flow"},
                    }
                ).encode("utf-8"),
            )
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(app_raw)["state"], "applied")

            for rid in ("rest-p1", "rest-p2"):
                st, _, _ = req(
                    "POST",
                    "/api/v1/commands",
                    headers=auth
                    | {
                        "Content-Type": "application/json",
                        "Idempotency-Key": f"flow-{rid}",
                    },
                    body=json.dumps(
                        {
                            "company_id": company_id,
                            "command_id": f"flow-{rid}",
                            "command_type": PAUSE_COMPANY,
                            "contract_version": RESTRICTION_COMMAND,
                            "preconditions": {
                                "charter_revision": "charter-v1",
                                "restriction_id": rid,
                                "expected_restriction_revision": 0,
                            },
                            "payload": {
                                "cause_category": "board_pause",
                                "scope_ref": "company:all",
                                "reason": f"Hold {rid}",
                            },
                        }
                    ).encode("utf-8"),
                )
                self.assertEqual(st, 200)

            st, _, clr_raw = req(
                "POST",
                "/api/v1/commands",
                headers=auth
                | {
                    "Content-Type": "application/json",
                    "Idempotency-Key": "flow-clear-p1",
                },
                body=json.dumps(
                    {
                        "company_id": company_id,
                        "command_id": "flow-clear-p1",
                        "command_type": RESUME_COMPANY,
                        "contract_version": RESTRICTION_COMMAND,
                        "preconditions": {
                            "charter_revision": "charter-v1",
                            "restriction_id": "rest-p1",
                            "expected_restriction_revision": 1,
                        },
                        "payload": {"reason": "Clear P1"},
                    }
                ).encode("utf-8"),
            )
            self.assertEqual(st, 200)
            self.assertEqual(
                json.loads(clr_raw)["result"]["remaining_active_causes"], 1
            )

            # 7. Verify Delegated HR Custody is strictly read-only (board_can_retire=False).
            st, _, org_raw = req("GET", "/api/v1/projections/org", headers=auth)
            self.assertEqual(st, 200)
            self.assertFalse(
                json.loads(org_raw)["delegated_hr_custody"]["board_can_retire"]
            )

            # 8. Verify NDJSON events stream returns redacted audit events without private text.
            st, ev_hdrs, ev_raw = req(
                "GET", "/api/v1/events?after_seq=0&limit=50", headers=auth
            )
            self.assertEqual(st, 200)
            self.assertIn("application/x-ndjson", ev_hdrs["content-type"])
            self.assertNotIn(b"Confirm fee variance", ev_raw)
            event_types = [
                json.loads(line)["event_type"]
                for line in ev_raw.decode("utf-8").splitlines()
                if line.strip()
            ]
            self.assertIn("board.ceo_message_sent", event_types)
            self.assertIn("governance.decision_answered", event_types)
            self.assertIn("governance.restriction_cleared", event_types)
