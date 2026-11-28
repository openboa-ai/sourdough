"""Connected E2E flow verification for flow-p2-connectors."""

from __future__ import annotations

import json
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns

from sourdough.company import FoundingTerms, found
from sourdough.connectors import (
    CONNECTOR_KIND_LLM_INFERENCE,
    LLM_COMPLETE_OP,
    LLM_EMBED_OP,
    ConnectorAdapterRegistry,
    ConnectorProxy,
    ConnectorUnauthorized,
    ScopedLlmInferenceConnector,
    disable_connector_binding,
    grant_connector_access,
    register_connector_binding,
    revoke_connector_grant,
)
from sourdough.secret_vault import InMemorySecretVault
from sourdough.duty_run import WorkRunClaim
from sourdough.runtime.kernel import claim_work_run, create_work_item
from sourdough.storage.db import scoped_reader, write_uow
from sourdough.trading.connector import (
    CONNECTOR_KIND_TRADING_VENUE,
    TRADING_QUERY_ORDER_OP,
    TRADING_SUBMIT_ORDER_OP,
    TradingVenueConnectorAdapter,
)
from sourdough.trading.external_venue import (
    ExternalAccountSnapshot,
    ExternalOrderReplyUnknown,
    ExternalOrderRequest,
    ExternalOrderStatus,
    VenueCapabilities,
)
from sourdough.writer_lease import hold_company_writer

_CANARY_LLM_SECRET = "sk-live-canary-connector-secret-847291"
_CANARY_VENUE_KEY = "vk-testnet-canary-1111"
_CANARY_VENUE_SECRET = "vs-testnet-canary-2222"


class _LoopbackMockLlmServer:
    """In-process loopback HTTP server simulating /v1/responses + redirect trap."""

    def __init__(self) -> None:
        self.received_auth_headers: list[str] = []
        self.request_count = 0
        self.force_redirect = False
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                req_obj = json.loads(raw.decode("utf-8"))
                auth = self.headers.get("Authorization", "")
                outer.received_auth_headers.append(auth)
                outer.request_count += 1

                if outer.force_redirect:
                    self.send_response(302)
                    self.send_header("Location", "https://evil.invalid/steal")
                    self.end_headers()
                    return

                # Deliberately echo Authorization value to prove ConnectorProxy scrubs it!
                body = json.dumps(
                    {
                        "id": f"resp-{outer.request_count}",
                        "model": req_obj.get("model", "gpt-6-luna"),
                        "output_text": f"Analysis complete (echo:{auth})",
                        "usage": {"input_tokens": 1200, "output_tokens": 400},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _LoopbackMockLlmServer:
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


class _SimulatedIndeterminateVenue:
    """Simulated external testnet venue that loses the first submit reply then resolves via query_order."""

    def __init__(self) -> None:
        self.submit_calls = 0
        self.query_calls = 0
        self.accepted_orders: dict[str, ExternalOrderRequest] = {}
        self.capabilities = VenueCapabilities(
            adapter_revision="sim-venue-v1",
            venue_name="sim_exchange",
            market_kind="spot",
            environment="external_test",
            supports_spot=True,
            supports_usdm_futures=False,
            supports_ioc_limit=True,
            supports_gtc_limit=True,
            seals_absent_orders=False,
            supports_external_reconciliation=True,
        )

    def submit_order(self, order: ExternalOrderRequest) -> ExternalOrderStatus:
        self.submit_calls += 1
        self.accepted_orders[order.action_id] = order
        raise ExternalOrderReplyUnknown(
            f"Lost reply after submit for {order.action_id} ({_CANARY_VENUE_SECRET})"
        )

    def cancel_order(self, *, symbol: str, client_order_id: str) -> ExternalOrderStatus:
        raise NotImplementedError

    def query_order(self, *, symbol: str, client_order_id: str) -> ExternalOrderStatus:
        self.query_calls += 1
        req = self.accepted_orders[client_order_id]
        return ExternalOrderStatus(
            venue_order_id=f"ven-{client_order_id}",
            client_order_id=client_order_id,
            symbol=symbol or req.symbol,
            side=req.side,
            status="filled",
            original_quantity=req.quantity,
            filled_quantity=req.quantity,
            remaining_quantity=Decimal("0"),
            avg_price=req.limit_price,
            update_time_ms=1700000000000,
        )

    def fetch_account_snapshot(self) -> ExternalAccountSnapshot:
        raise NotImplementedError


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with (
            TemporaryDirectory(prefix="sd-p2c-") as tmp,
            _LoopbackMockLlmServer() as llm_srv,
        ):
            root = Path(tmp)
            store = root / "co.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms(
                    "Connector Flow Co", "charter-v1", "manage-company", "profile-p2"
                ),
            )
            cid = founding.company_id
            fid = founding.founder_id
            now = time_ns()

            sim_venue = _SimulatedIndeterminateVenue()
            adapters = ConnectorAdapterRegistry(
                [
                    ScopedLlmInferenceConnector(),
                    TradingVenueConnectorAdapter(venue_resolver=lambda _req: sim_venue),
                ]
            )
            vault = InMemorySecretVault(
                {
                    "envref:OPENAI_DEV_KEY": _CANARY_LLM_SECRET,
                    "envref:SIM_VENUE_TESTNET": {
                        "api_key": _CANARY_VENUE_KEY,
                        "api_secret": _CANARY_VENUE_SECRET,
                    },
                }
            )
            proxy = ConnectorProxy(adapters, vault, loopback_only=True)

            with hold_company_writer(store, cid, lease_directory=leases) as w:
                with write_uow(w) as db:
                    item, _ = create_work_item(
                        db,
                        company_id=cid,
                        owner_agent_id=fid,
                        duty_kind="work.assessment_review",
                        source_kind="unit",
                        source_id="src-p2c-1",
                        now_ns=now,
                    )
                    run = claim_work_run(
                        db,
                        company_id=cid,
                        work_item_id=item.work_item_id,
                        lease_owner="p2-worker",
                        lease_ttl_ns=3_600_000_000_000,
                        now_ns=now + 1,
                    )
                claim = WorkRunClaim(
                    company_id=cid,
                    founder_id=fid,
                    duty_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    grant_id=founding.grant_id,
                    grant_scope="manage-company",
                    purpose="manage-company",
                )

                # 1. Register scoped LLM inference binding (opaque handle only!)
                llm_binding = register_connector_binding(
                    w,
                    company_id=cid,
                    binding_id="llm-primary",
                    connector_kind=CONNECTOR_KIND_LLM_INFERENCE,
                    environment="paper",
                    endpoint_url=llm_srv.base_url,
                    allowed_hosts=("127.0.0.1",),
                    operations=(LLM_COMPLETE_OP, LLM_EMBED_OP),
                    credential_handle_ref="envref:OPENAI_DEV_KEY",
                    actor_id="board-owner",
                    budget_ceiling_minor_units=10_000,
                    allow_loopback=True,
                    now_ns=now + 10,
                )

                # Ungranted agent MUST be refused
                with self.assertRaises(ConnectorUnauthorized):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=llm_binding.binding_id,
                        operation="complete",
                        request_id="llm-ungranted",
                        payload={"model": "gpt-6-luna", "input": "hello"},
                        now_ns=now + 15,
                    )

                # Grant explicit `complete` operation only (NOT `embed`)
                grant_connector_access(
                    w,
                    company_id=cid,
                    agent_id=fid,
                    standing_grant_id=founding.grant_id,
                    binding_id=llm_binding.binding_id,
                    binding_revision=llm_binding.revision,
                    allowed_operations=("complete",),
                    granted_by_actor_id="board-owner",
                    max_cost_minor_units=5_000,
                    now_ns=now + 20,
                )

                # Invoke through OuterToolBroker handler (zero secrets passed by Agent!)
                handler = proxy.build_tool_handler(w, claim)
                out = handler(
                    {
                        "binding_id": "llm-primary",
                        "operation": "complete",
                        "request_id": "llm-req-1",
                        "payload": {
                            "model": "gpt-6-luna",
                            "input": "Summarize portfolio liquidity",
                        },
                    }
                )
                self.assertEqual(out["status"], "observed")
                self.assertGreater(out["cost_minor_units"], 0)
                self.assertEqual(
                    llm_srv.received_auth_headers[0], f"Bearer {_CANARY_LLM_SECRET}"
                )
                # Server deliberately echoed Bearer token -> MUST be scrubbed!
                self.assertNotIn(_CANARY_LLM_SECRET, json.dumps(out))
                self.assertIn("[REDACTED_SECRET]", out["payload"]["output_text"])

                # HTTP 302 redirect trap MUST be rejected without credential bounce
                llm_srv.force_redirect = True
                redirect_out = proxy.invoke(
                    w,
                    claim,
                    binding_id="llm-primary",
                    operation="complete",
                    request_id="llm-req-redirect",
                    payload={"model": "gpt-6-luna", "input": "redirect test"},
                    now_ns=now + 30,
                )
                self.assertEqual(redirect_out.status, "rejected")
                self.assertIn("redirect", redirect_out.payload["error"].lower())
                llm_srv.force_redirect = False

                # 2. Register Trading Venue connector (Seam #9), simulate lost reply -> recovery -> reconcile
                venue_binding = register_connector_binding(
                    w,
                    company_id=cid,
                    binding_id="sim-testnet",
                    connector_kind=CONNECTOR_KIND_TRADING_VENUE,
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9999",
                    allowed_hosts=("127.0.0.1",),
                    operations=(TRADING_SUBMIT_ORDER_OP, TRADING_QUERY_ORDER_OP),
                    credential_handle_ref="envref:SIM_VENUE_TESTNET",
                    recovery_capabilities={
                        "supports_lookup": True,
                        "seals_absent": False,
                    },
                    actor_id="board-owner",
                    allow_loopback=True,
                    now_ns=now + 40,
                )
                venue_grant = grant_connector_access(
                    w,
                    company_id=cid,
                    agent_id=fid,
                    standing_grant_id=founding.grant_id,
                    binding_id=venue_binding.binding_id,
                    binding_revision=venue_binding.revision,
                    allowed_operations=("submit_order", "query_order"),
                    granted_by_actor_id="board-owner",
                    now_ns=now + 45,
                )

                ord_resp = proxy.invoke(
                    w,
                    claim,
                    binding_id="sim-testnet",
                    operation="submit_order",
                    request_id="ord-p2-1",
                    payload={
                        "action_id": "ord-p2-1",
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "quantity": "0.01",
                        "limit_price": "50000",
                    },
                    admission_id="adm-p2-order-1",
                    now_ns=now + 50,
                )
                self.assertEqual(ord_resp.status, "unknown")
                self.assertTrue(ord_resp.indeterminate)
                self.assertEqual(sim_venue.submit_calls, 1)
                self.assertNotIn(_CANARY_VENUE_SECRET, json.dumps(ord_resp.payload))
                ord_idem = ord_resp.payload["idempotency_key"]

                # Revoke grant & disable binding -> reconcile_unknown_action STILL resolves without resending!
                revoke_connector_grant(
                    w,
                    company_id=cid,
                    grant_binding_id=venue_grant.grant_binding_id,
                    actor_id="board-owner",
                    now_ns=now + 60,
                )
                disable_connector_binding(
                    w,
                    company_id=cid,
                    binding_id=venue_binding.binding_id,
                    revision=venue_binding.revision,
                    actor_id="board-owner",
                    reason="halt new external writes",
                    now_ns=now + 65,
                )
                reconciled = proxy.reconcile_unknown_action(
                    w,
                    claim,
                    idempotency_key=ord_idem,
                    lookup_payload={"symbol": "BTCUSDT", "client_order_id": "ord-p2-1"},
                    now_ns=now + 70,
                )
                self.assertEqual(reconciled.status, "observed")
                self.assertEqual(reconciled.payload["order_status"], "filled")
                self.assertEqual(sim_venue.submit_calls, 1)
                self.assertEqual(sim_venue.query_calls, 1)

            # 3. Verify GL expense posted & ZERO raw secret bytes exist anywhere in company.sqlite!
            with scoped_reader(store) as db:
                gl_row = db.execute(
                    "SELECT SUM(amount_minor) FROM journal_line WHERE account = 'expense:connector_usage'"
                ).fetchone()
                self.assertIsNotNone(gl_row)
                self.assertGreater(int(gl_row[0]), 0)

                audit_events = [
                    str(r[0])
                    for r in db.execute(
                        "SELECT event_type FROM audit_event WHERE domain = 'connectors'"
                    ).fetchall()
                ]
                self.assertIn("connectors.binding_registered", audit_events)
                self.assertIn("connectors.grant_created", audit_events)
                self.assertIn("connectors.operation_confirmed", audit_events)
                self.assertIn("connectors.operation_unknown", audit_events)
                self.assertIn("connectors.operation_reconciled", audit_events)

            raw_sqlite_bytes = store.read_bytes()
            for canary in (_CANARY_LLM_SECRET, _CANARY_VENUE_KEY, _CANARY_VENUE_SECRET):
                self.assertNotIn(
                    canary.encode("utf-8"),
                    raw_sqlite_bytes,
                    f"Raw secret leaked into company.sqlite: {canary}",
                )


if __name__ == "__main__":
    unittest.main()
