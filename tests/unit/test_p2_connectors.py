"""Unit tests for Core connector registry, grants, ConnectorProxy, taint, recovery, and Seam #9."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.company import FoundingTerms, found
from sourdough.connectors import (
    ConnectorAdapterRegistry,
    ConnectorBudgetExceeded,
    ConnectorEnvironmentMismatch,
    ConnectorIndeterminate,
    ConnectorManifest,
    ConnectorProxy,
    ConnectorRateLimited,
    ConnectorRefused,
    ConnectorRequest,
    ConnectorResponse,
    ConnectorUnauthorized,
    OperationSpec,
    disable_connector_binding,
    grant_connector_access,
    issue_connector_admission,
    register_connector_binding,
    revoke_connector_grant,
)
from sourdough.duty_run import WorkRunClaim
from sourdough.runtime.domains import DomainPluginRegistry
from sourdough.runtime.kernel import claim_work_run, create_work_item
from sourdough.runtime.shift.flow_guard import ProvenanceIndex, ShiftFlowGuard
from sourdough.secret_vault import (
    InMemorySecretVault,
    MaskedSecret,
    MaskedSecretBundle,
)
from sourdough.storage.db import scoped_reader, write_uow
from sourdough.trading.binance_venue import (
    MARKET_SPOT,
    BinanceVenueAdapter,
    BinanceVenueConfig,
)
from sourdough.trading.connector import (
    CONNECTOR_KIND_TRADING_VENUE,
    TRADING_QUERY_ORDER_OP,
    TRADING_SUBMIT_ORDER_OP,
    TradingVenueConnectorAdapter,
    build_bound_binance_venue,
)
from sourdough.trading.credentials import InMemoryTestCredentialProvider
from sourdough.trading.plugin import TradingDomainPlugin
from sourdough.writer_lease import hold_company_writer


class _FakeAdapter:
    max_cost_per_call_minor_units: int = 25

    def __init__(self) -> None:
        self.execute_calls: list[ConnectorRequest] = []
        self.lookup_calls: list[ConnectorRequest] = []
        self.next_execute: Any = None
        self.next_lookup: Any = None
        self._manifest = ConnectorManifest(
            connector_kind="fake_service",
            adapter_revision="fake-v1",
            operations=(
                OperationSpec(name="fetch_doc", mode="read", idempotent=True),
                OperationSpec(
                    name="submit_write",
                    mode="effectful",
                    supports_lookup=True,
                    idempotent=False,
                ),
            ),
            supports_lookup=True,
            seals_absent=False,
            requires_credential=True,
        )

    @property
    def manifest(self) -> ConnectorManifest:
        return self._manifest

    def execute(
        self,
        request: ConnectorRequest,
        *,
        secret: MaskedSecret | MaskedSecretBundle | None,
        timeout_seconds: float,
    ) -> ConnectorResponse:
        del timeout_seconds
        self.execute_calls.append(request)
        assert isinstance(secret, MaskedSecret)
        if callable(self.next_execute):
            return self.next_execute(request, secret)
        return ConnectorResponse(
            status="observed",
            payload={
                "ok": True,
                "echo_leaked_secret": f"Bearer {secret.value}",
                "request_id": request.request_id,
            },
            source_ref=f"src:{request.request_id}:{secret.value}",
            cost_minor_units=25,
        )

    def lookup(
        self,
        request: ConnectorRequest,
        *,
        secret: MaskedSecret | MaskedSecretBundle | None,
        timeout_seconds: float,
    ) -> ConnectorResponse:
        del timeout_seconds
        self.lookup_calls.append(request)
        assert isinstance(secret, MaskedSecret)
        if callable(self.next_lookup):
            return self.next_lookup(request, secret)
        return ConnectorResponse(
            status="observed",
            payload={"reconciled": True, "request_id": request.request_id},
            source_ref=f"lookup:{request.request_id}",
            cost_minor_units=0,
        )


def _setup_company_and_claim(root: Path):
    store = root / "co.sqlite"
    leases = root / "leases"
    founding = found(
        store,
        FoundingTerms("Connector Co", "charter-v1", "manage-company", "profile-1"),
    )
    now = time_ns()
    with hold_company_writer(store, founding.company_id, lease_directory=leases) as w:
        with write_uow(w) as db:
            item, _ = create_work_item(
                db,
                company_id=founding.company_id,
                owner_agent_id=founding.founder_id,
                duty_kind="work.assessment_review",
                source_kind="unit",
                source_id="src-conn-1",
                now_ns=now,
            )
            run = claim_work_run(
                db,
                company_id=founding.company_id,
                work_item_id=item.work_item_id,
                lease_owner="worker-1",
                lease_ttl_ns=3_600_000_000_000,
                now_ns=now + 1,
            )
    claim = WorkRunClaim(
        company_id=founding.company_id,
        founder_id=founding.founder_id,
        duty_id=item.work_item_id,
        run_id=run.run_id,
        fencing_token=run.fencing_token,
        grant_id=founding.grant_id,
        grant_scope="manage-company",
        purpose="manage-company",
    )
    return store, leases, founding, claim, now


class ConnectorSubsystemTests(unittest.TestCase):
    def test_registration_revisions_and_environment_immutability(self) -> None:
        with TemporaryDirectory(prefix="sd-c1-") as tmp:
            store, leases, founding, _, now = _setup_company_and_claim(Path(tmp))
            ops = (OperationSpec(name="fetch_doc", mode="read", idempotent=True),)
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                # Reject userinfo, unpinned host, wildcards, and non-loopback http
                with self.assertRaises(ConnectorRefused):
                    register_connector_binding(
                        w,
                        company_id=founding.company_id,
                        binding_id="bad-userinfo",
                        connector_kind="fake_service",
                        environment="external_test",
                        endpoint_url="https://user:pass@api.example.com",
                        allowed_hosts=("api.example.com",),
                        operations=ops,
                        credential_handle_ref="envref:FAKE_TEST_KEY",
                        actor_id="board-1",
                    )
                with self.assertRaises(ConnectorEnvironmentMismatch):
                    register_connector_binding(
                        w,
                        company_id=founding.company_id,
                        binding_id="bad-host",
                        connector_kind="fake_service",
                        environment="external_test",
                        endpoint_url="https://evil.example.com",
                        allowed_hosts=("api.example.com",),
                        operations=ops,
                        credential_handle_ref="envref:FAKE_TEST_KEY",
                        actor_id="board-1",
                    )
                with self.assertRaises(ConnectorEnvironmentMismatch):
                    register_connector_binding(
                        w,
                        company_id=founding.company_id,
                        binding_id="bad-http",
                        connector_kind="fake_service",
                        environment="external_test",
                        endpoint_url="http://api.example.com",
                        allowed_hosts=("api.example.com",),
                        operations=ops,
                        credential_handle_ref="envref:FAKE_TEST_KEY",
                        actor_id="board-1",
                        allow_loopback=True,
                    )

                b1 = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-main",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:8080",
                    allowed_hosts=("127.0.0.1",),
                    operations=ops,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    allow_loopback=True,
                    now_ns=now,
                )
                self.assertEqual(b1.revision, 1)

                # Replacement produces revision 2; environment mutation is refused
                with self.assertRaises(ConnectorEnvironmentMismatch):
                    register_connector_binding(
                        w,
                        company_id=founding.company_id,
                        binding_id="svc-main",
                        connector_kind="fake_service",
                        environment="live",
                        endpoint_url="https://api.example.com",
                        allowed_hosts=("api.example.com",),
                        operations=ops,
                        credential_handle_ref="envref:FAKE_PROD_KEY",
                        actor_id="board-1",
                    )

                b2 = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-main",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:8081",
                    allowed_hosts=("127.0.0.1",),
                    operations=ops,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    allow_loopback=True,
                    now_ns=now + 10,
                )
                self.assertEqual(b2.revision, 2)

    def test_proxy_enforces_grant_taint_admission_rate_budget_and_scrubbing(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-c2-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            secret_raw = "sk-super-secret-canary-4242"
            adapter = _FakeAdapter()
            registry = ConnectorAdapterRegistry([adapter])
            vault = InMemorySecretVault({"envref:FAKE_TEST_KEY": secret_raw})
            proxy = ConnectorProxy(registry, vault, loopback_only=True)

            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-1",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9001",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    max_requests_per_window=2,
                    window_seconds=60,
                    budget_ceiling_minor_units=60,
                    allow_loopback=True,
                    now_ns=now,
                )

                # 1. Registration is NOT permission to act
                with self.assertRaises(ConnectorUnauthorized):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="fetch_doc",
                        request_id="req-ungranted",
                        payload={"doc": "a"},
                        now_ns=now + 10,
                    )

                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("fetch_doc", "submit_write"),
                    granted_by_actor_id="board-1",
                    max_cost_minor_units=60,
                    now_ns=now + 20,
                )

                # 2. ShiftFlowGuard taint checks block private-desk exfiltration & peer-tainted writes
                prov = ProvenanceIndex(
                    store, founding.company_id, founding.founder_id, claim.duty_id
                )
                tool_spec = ConnectorProxy.tool_spec()
                desk_tainted = ShiftFlowGuard(
                    agent_id=founding.founder_id,
                    provenance=prov,
                    specs_by_name={tool_spec.name: tool_spec},
                    public_offloaded_paths=set(),
                    read_private_desk=True,
                )
                with self.assertRaises(ConnectorUnauthorized):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="fetch_doc",
                        request_id="req-tainted-desk",
                        payload={"doc": "secret"},
                        flow_guard=desk_tainted,
                        now_ns=now + 30,
                    )

                peer_tainted = ShiftFlowGuard(
                    agent_id=founding.founder_id,
                    provenance=prov,
                    specs_by_name={tool_spec.name: tool_spec},
                    public_offloaded_paths=set(),
                    read_peer_input=True,
                )
                with self.assertRaises(ConnectorUnauthorized):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="submit_write",
                        request_id="req-tainted-peer",
                        payload={"x": 1},
                        flow_guard=peer_tainted,
                        admission_id="adm-1",
                        now_ns=now + 31,
                    )

                # 3. Effectful operation requires authoritative service-issued admission
                with self.assertRaises(ConnectorUnauthorized):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="submit_write",
                        request_id="req-no-adm",
                        payload={"x": 1},
                        now_ns=now + 32,
                    )
                with self.assertRaises(ConnectorUnauthorized):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="submit_write",
                        request_id="req-forged-adm",
                        payload={"x": 1},
                        admission_id="invented-by-agent",
                        now_ns=now + 33,
                    )
                # Model-visible tool spec MUST NOT accept admission_id!
                self.assertNotIn(
                    "admission_id", ConnectorProxy.tool_spec().parameters["properties"]
                )
                tool_handler = proxy.build_tool_handler(w, claim)
                with self.assertRaises(ConnectorUnauthorized):
                    tool_handler(
                        {
                            "binding_id": binding.binding_id,
                            "operation": "submit_write",
                            "request_id": "req-tool-forged",
                            "payload": {"x": 1},
                            "admission_id": "invented-by-agent",
                        }
                    )
                # Tampering with payload after real admission MUST also be refused!
                real_adm = issue_connector_admission(
                    w,
                    company_id=founding.company_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    operation="submit_write",
                    request_id="req-tampered",
                    payload={"x": 1},
                    issued_by_service="core.domain_service",
                    actor_id=founding.founder_id,
                    now_ns=now + 34,
                )
                with self.assertRaises(ConnectorUnauthorized):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="submit_write",
                        request_id="req-tampered",
                        payload={"x": 999},
                        admission_id=real_adm.admission_id,
                        now_ns=now + 35,
                    )

                # 4. Normal invocation succeeds, posts GL expense, and scrubs echoed secrets
                resp = proxy.invoke(
                    w,
                    claim,
                    binding_id=binding.binding_id,
                    operation="fetch_doc",
                    request_id="req-1",
                    payload={"doc": "a"},
                    now_ns=now + 40,
                )
                self.assertEqual(resp.status, "observed")
                self.assertEqual(resp.cost_minor_units, 25)
                self.assertNotIn(secret_raw, str(resp.payload))
                self.assertNotIn(secret_raw, str(resp.source_ref))
                self.assertIn("[REDACTED_SECRET]", resp.payload["echo_leaked_secret"])

                # 5. Idempotent replay returns cached response without second execute call
                resp_replay = proxy.invoke(
                    w,
                    claim,
                    binding_id=binding.binding_id,
                    operation="fetch_doc",
                    request_id="req-1",
                    payload={"doc": "a"},
                    now_ns=now + 50,
                )
                self.assertEqual(resp_replay.payload, resp.payload)
                self.assertEqual(len(adapter.execute_calls), 1)

                # Conflicting payload on same request_id raises ConnectorRefused
                with self.assertRaises(ConnectorRefused):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="fetch_doc",
                        request_id="req-1",
                        payload={"doc": "DIFFERENT"},
                        now_ns=now + 55,
                    )

                # 6. Second unique call succeeds (total spent becomes 50 <= 60 ceiling)
                proxy.invoke(
                    w,
                    claim,
                    binding_id=binding.binding_id,
                    operation="fetch_doc",
                    request_id="req-2",
                    payload={"doc": "b"},
                    now_ns=now + 60,
                )
                # Third call in same 60s window hits rate limit first (2 per 60s)
                with self.assertRaises(ConnectorRateLimited):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="fetch_doc",
                        request_id="req-3",
                        payload={"doc": "c"},
                        now_ns=now + 70,
                    )
                # After window passes, pre-dispatch reservation (50 + 25 = 75 > 60) blocks BEFORE dispatch!
                with self.assertRaises(ConnectorBudgetExceeded):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="fetch_doc",
                        request_id="req-3",
                        payload={"doc": "c"},
                        now_ns=now + 120_000_000_000,
                    )
                self.assertEqual(len(adapter.execute_calls), 2)

            # Verify zero raw secret bytes anywhere in company.sqlite
            self.assertNotIn(secret_raw.encode("utf-8"), store.read_bytes())

    def test_indeterminate_write_creates_recovery_and_reconciles_after_revocation(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-c3-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            secret_raw = "sk-recover-canary-9876"
            adapter = _FakeAdapter()

            def _timeout_exec(_req: ConnectorRequest, sec: MaskedSecret):
                raise ConnectorIndeterminate(f"Socket timeout with Bearer {sec.value}")

            adapter.next_execute = _timeout_exec
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([adapter]),
                InMemorySecretVault({"envref:FAKE_TEST_KEY": secret_raw}),
                loopback_only=True,
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="venue-test",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9002",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    allow_loopback=True,
                    now_ns=now,
                )
                grant = grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("submit_write",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 5,
                )
                adm = issue_connector_admission(
                    w,
                    company_id=founding.company_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    operation="submit_write",
                    request_id="ord-1",
                    payload={"qty": 1},
                    issued_by_service="trading.execution_service",
                    actor_id=founding.founder_id,
                    now_ns=now + 8,
                )
                resp = proxy.invoke(
                    w,
                    claim,
                    binding_id=binding.binding_id,
                    operation="submit_write",
                    request_id="ord-1",
                    payload={"qty": 1},
                    admission_id=adm.admission_id,
                    now_ns=now + 10,
                )
                self.assertEqual(resp.status, "unknown")
                self.assertTrue(resp.indeterminate)
                self.assertNotIn(secret_raw, str(resp.payload))
                self.assertIn("recovery_work_item_id", resp.payload)
                idem_key = resp.payload["idempotency_key"]

                # Revoke grant AND disable binding — recovery of already-admitted effect MUST STILL SUCCEED!
                revoke_connector_grant(
                    w,
                    company_id=founding.company_id,
                    grant_binding_id=grant.grant_binding_id,
                    actor_id="board-1",
                    now_ns=now + 20,
                )
                disable_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id=binding.binding_id,
                    revision=binding.revision,
                    actor_id="board-1",
                    reason="suspend new writes",
                    now_ns=now + 25,
                )
                reconciled = proxy.reconcile_unknown_action(
                    w, claim, idempotency_key=idem_key, now_ns=now + 30
                )
                self.assertEqual(reconciled.status, "observed")
                self.assertFalse(reconciled.indeterminate)
                self.assertEqual(len(adapter.execute_calls), 1)
                self.assertEqual(len(adapter.lookup_calls), 1)

            with scoped_reader(store) as db:
                row = db.execute(
                    "SELECT status FROM external_actions WHERE idempotency_key = ?",
                    (idem_key,),
                ).fetchone()
                self.assertEqual(row, ("confirmed",))
                rec_item = db.execute(
                    "SELECT duty_kind FROM work_items WHERE source_kind = 'connector_recovery' AND source_id = ?",
                    (idem_key,),
                ).fetchone()
                self.assertEqual(rec_item, ("connector.reconcile",))

    def test_seam9_trading_plugin_contributes_venue_connector(self) -> None:
        plugin_reg = DomainPluginRegistry([TradingDomainPlugin()])
        conn_reg = ConnectorAdapterRegistry()
        conn_reg.register_from_plugins(plugin_reg.plugins)
        self.assertIn("trading_venue", conn_reg.registered_kinds())
        adapter = conn_reg.get("trading_venue")
        self.assertEqual(adapter.manifest.credential_fields, ("api_key", "api_secret"))
        self.assertIsNotNone(adapter.manifest.operation("submit_order"))
        self.assertEqual(adapter.manifest.operation("submit_order").mode, "effectful")

    def test_reconcile_restricts_access_to_originator_or_recovery_owner(self) -> None:
        with TemporaryDirectory(prefix="sd-c4-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            secret_raw = "test-owner-canary-1111"
            adapter = _FakeAdapter()
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([adapter]),
                InMemorySecretVault({"envref:FAKE_TEST_KEY": secret_raw}),
                loopback_only=True,
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                peer_id = "agent-bystander"
                with write_uow(w) as db:
                    db.execute(
                        "INSERT INTO agent VALUES (?, ?, ?)",
                        (peer_id, founding.company_id, "profile-1"),
                    )
                    db.execute(
                        "INSERT INTO standing_grant VALUES (?, ?, ?)",
                        ("grant-bystander", peer_id, "read-only"),
                    )
                    p_item, _ = create_work_item(
                        db,
                        company_id=founding.company_id,
                        owner_agent_id=peer_id,
                        duty_kind="work.assessment_review",
                        source_kind="unit",
                        source_id="src-bystander-1",
                        now_ns=now + 2,
                    )
                    p_run = claim_work_run(
                        db,
                        company_id=founding.company_id,
                        work_item_id=p_item.work_item_id,
                        lease_owner="worker-bystander",
                        lease_ttl_ns=3_600_000_000_000,
                        now_ns=now + 3,
                    )
                bystander_claim = WorkRunClaim(
                    company_id=founding.company_id,
                    founder_id=peer_id,
                    duty_id=p_item.work_item_id,
                    run_id=p_run.run_id,
                    fencing_token=p_run.fencing_token,
                    grant_id="grant-bystander",
                    grant_scope="read-only",
                    purpose="read-only",
                )
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-priv",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9010",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    allow_loopback=True,
                    now_ns=now + 5,
                )
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("fetch_doc",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 6,
                )
                resp = proxy.invoke(
                    w,
                    claim,
                    binding_id=binding.binding_id,
                    operation="fetch_doc",
                    request_id="priv-doc-1",
                    payload={"doc": "sensitive"},
                    now_ns=now + 10,
                )
                self.assertEqual(resp.status, "observed")
                idem_key = (
                    f"conn:{founding.company_id}:{founding.founder_id}:"
                    "svc-priv:r1:fetch_doc:priv-doc-1"
                )

                with self.assertRaises(ConnectorUnauthorized):
                    proxy.reconcile_unknown_action(
                        w, bystander_claim, idempotency_key=idem_key, now_ns=now + 15
                    )

                # Even if peer ALSO gets granted fetch_doc and submits the exact same
                # request_id and payload, it executes independently and NEVER sees founder's receipt!
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=peer_id,
                    standing_grant_id="grant-bystander",
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("fetch_doc",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 18,
                )
                self.assertEqual(len(adapter.execute_calls), 1)
                peer_resp = proxy.invoke(
                    w,
                    bystander_claim,
                    binding_id=binding.binding_id,
                    operation="fetch_doc",
                    request_id="priv-doc-1",
                    payload={"doc": "sensitive"},
                    now_ns=now + 20,
                )
                self.assertEqual(peer_resp.status, "observed")
                self.assertEqual(len(adapter.execute_calls), 2)

    def test_unresolved_read_retries_consume_rate_and_budget_limits(self) -> None:
        with TemporaryDirectory(prefix="sd-c5-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            adapter = _FakeAdapter()

            def _always_timeout(_req: ConnectorRequest, _sec: MaskedSecret):
                raise ConnectorIndeterminate("upstream read timeout")

            adapter.next_execute = _always_timeout
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([adapter]),
                InMemorySecretVault({"envref:FAKE_TEST_KEY": "sk-read-retry-1234"}),
                loopback_only=True,
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-rate",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9011",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    max_requests_per_window=1,
                    window_seconds=60,
                    allow_loopback=True,
                    now_ns=now,
                )
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("fetch_doc",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 5,
                )
                first = proxy.invoke(
                    w,
                    claim,
                    binding_id=binding.binding_id,
                    operation="fetch_doc",
                    request_id="same-read-req",
                    payload={"q": "x"},
                    now_ns=now + 10,
                )
                self.assertEqual(first.status, "unknown")
                self.assertEqual(len(adapter.execute_calls), 1)

                with self.assertRaises(ConnectorRateLimited):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="fetch_doc",
                        request_id="same-read-req",
                        payload={"q": "x"},
                        now_ns=now + 20,
                    )
                self.assertEqual(len(adapter.execute_calls), 1)

    def test_fencing_recheck_after_blocking_secret_resolution_prevents_stale_send(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-c6-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            adapter = _FakeAdapter()

            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-fence",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9012",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    allow_loopback=True,
                    now_ns=now,
                )
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("submit_write",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 5,
                )
                adm = issue_connector_admission(
                    w,
                    company_id=founding.company_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    operation="submit_write",
                    request_id="ord-race-1",
                    payload={"qty": 1},
                    issued_by_service="trading.execution_service",
                    actor_id=founding.founder_id,
                    now_ns=now + 8,
                )

                class _InProcFencingVault:
                    def resolve_secret(
                        self,
                        handle_ref: str,
                        *,
                        expected_environment: str | None = None,
                    ) -> MaskedSecret:
                        del expected_environment
                        with write_uow(w) as db_inner:
                            db_inner.execute(
                                "UPDATE work_runs SET fencing_token = fencing_token + 1 WHERE run_id = ?",
                                (claim.run_id,),
                            )
                        return MaskedSecret(
                            value="sk-fenced-secret-9999", handle_ref=handle_ref
                        )

                    def resolve_bundle(
                        self,
                        handle_ref: str,
                        fields: Any,
                        *,
                        expected_environment: str | None = None,
                    ) -> MaskedSecretBundle:
                        raise NotImplementedError

                proxy = ConnectorProxy(
                    ConnectorAdapterRegistry([adapter]),
                    _InProcFencingVault(),
                    loopback_only=True,
                )
                with self.assertRaises(ConnectorUnauthorized):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="submit_write",
                        request_id="ord-race-1",
                        payload={"qty": 1},
                        admission_id=adm.admission_id,
                        now_ns=now + 10,
                    )
                self.assertEqual(len(adapter.execute_calls), 0)

            idem_key = (
                f"conn:{founding.company_id}:svc-fence:r1:submit_write:ord-race-1"
            )
            with scoped_reader(store) as db:
                row = db.execute(
                    "SELECT status FROM external_actions WHERE idempotency_key = ?",
                    (idem_key,),
                ).fetchone()
                self.assertEqual(row, ("unknown",))
                rec = db.execute(
                    "SELECT duty_kind FROM work_items WHERE source_kind = 'connector_recovery' AND source_id = ?",
                    (idem_key,),
                ).fetchone()
                self.assertEqual(rec, ("connector.reconcile",))

    def test_trading_venue_connector_rejects_mismatched_endpoint_or_credentials(
        self,
    ) -> None:
        secret = MaskedSecretBundle(
            values={"api_key": "k-authorized", "api_secret": "s-authorized"},
            handle_ref="envref:BINANCE_TESTNET",
        )
        req = ConnectorRequest(
            company_id="co-1",
            binding_id="venue-1",
            binding_revision=1,
            operation="query_order",
            request_id="q-1",
            idempotency_key="conn:co-1:venue-1:r1:query_order:q-1",
            payload={"symbol": "BTCUSDT", "client_order_id": "q-1"},
            environment="external_test",
            endpoint_url="http://127.0.0.1:9100",
            allowed_hosts=("127.0.0.1",),
        )
        wrong_url_venue = BinanceVenueAdapter(
            BinanceVenueConfig(
                environment="external_test",
                market_kind=MARKET_SPOT,
                account_id="acct-1",
                destination_id="http://127.0.0.1:9999",
                credential_handle_ref="envref:BINANCE_TESTNET",
                base_url_override="http://127.0.0.1:9999",
                allow_loopback_override=True,
            ),
            InMemoryTestCredentialProvider(
                {"envref:BINANCE_TESTNET": ("k-authorized", "s-authorized")}
            ),
        )
        adapter_wrong_url = TradingVenueConnectorAdapter(
            venue_resolver=lambda _r, _s: wrong_url_venue
        )
        with self.assertRaises(ConnectorEnvironmentMismatch):
            adapter_wrong_url.execute(req, secret=secret, timeout_seconds=5.0)

        wrong_key_venue = BinanceVenueAdapter(
            BinanceVenueConfig(
                environment="external_test",
                market_kind=MARKET_SPOT,
                account_id="acct-other",
                destination_id="http://127.0.0.1:9100",
                credential_handle_ref="envref:BINANCE_TESTNET",
                base_url_override="http://127.0.0.1:9100",
                allow_loopback_override=True,
            ),
            InMemoryTestCredentialProvider(
                {"envref:BINANCE_TESTNET": ("k-OTHER-ACCOUNT", "s-OTHER-ACCOUNT")}
            ),
        )
        adapter_wrong_key = TradingVenueConnectorAdapter(
            venue_resolver=lambda _r, _s: wrong_key_venue
        )
        with self.assertRaises(ConnectorEnvironmentMismatch):
            adapter_wrong_key.execute(req, secret=secret, timeout_seconds=5.0)

        bound_venue = build_bound_binance_venue(req, secret)
        self.assertEqual(bound_venue._config.base_url, "http://127.0.0.1:9100")
        self.assertEqual(
            bound_venue._config.credential_handle_ref, "envref:BINANCE_TESTNET"
        )
        self.assertEqual(CONNECTOR_KIND_TRADING_VENUE, "trading_venue")
        self.assertEqual(TRADING_SUBMIT_ORDER_OP.mode, "effectful")
        self.assertEqual(TRADING_QUERY_ORDER_OP.mode, "read")

    def test_unexpected_exception_in_adapter_is_scrubbed_before_returning(self) -> None:
        with TemporaryDirectory(prefix="sd-c7-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            secret_raw = "sk-crash-canary-7777"
            adapter = _FakeAdapter()

            def _explode(_req: ConnectorRequest, sec: MaskedSecret):
                raise ValueError(f"Invalid header value b'Bearer {sec.value}'")

            adapter.next_execute = _explode
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([adapter]),
                InMemorySecretVault({"envref:FAKE_TEST_KEY": secret_raw}),
                loopback_only=True,
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-crash",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9013",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    allow_loopback=True,
                    now_ns=now,
                )
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("fetch_doc",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 5,
                )
                resp = proxy.invoke(
                    w,
                    claim,
                    binding_id=binding.binding_id,
                    operation="fetch_doc",
                    request_id="crash-1",
                    payload={"q": "x"},
                    now_ns=now + 10,
                )
                self.assertEqual(resp.status, "unknown")
                self.assertNotIn(secret_raw, str(resp.payload))
                self.assertIn("[REDACTED_SECRET]", resp.payload["error"])
            self.assertNotIn(secret_raw.encode("utf-8"), store.read_bytes())

    def test_grant_revocation_during_secret_resolution_blocks_dispatch(self) -> None:
        with TemporaryDirectory(prefix="sd-c8-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            adapter = _FakeAdapter()
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-rev",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9014",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    allow_loopback=True,
                    now_ns=now,
                )
                grant = grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("fetch_doc",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 5,
                )

                class _RevokingDuringResolveVault:
                    def resolve_secret(
                        self,
                        handle_ref: str,
                        *,
                        expected_environment: str | None = None,
                    ) -> MaskedSecret:
                        del expected_environment
                        revoke_connector_grant(
                            w,
                            company_id=founding.company_id,
                            grant_binding_id=grant.grant_binding_id,
                            actor_id="board-1",
                            now_ns=now + 9,
                        )
                        return MaskedSecret(
                            value="test-revoked-secret-1111", handle_ref=handle_ref
                        )

                    def resolve_bundle(
                        self,
                        handle_ref: str,
                        fields: Any,
                        *,
                        expected_environment: str | None = None,
                    ) -> MaskedSecretBundle:
                        raise NotImplementedError

                proxy = ConnectorProxy(
                    ConnectorAdapterRegistry([adapter]),
                    _RevokingDuringResolveVault(),
                    loopback_only=True,
                )
                with self.assertRaises(ConnectorUnauthorized):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="fetch_doc",
                        request_id="rev-race-1",
                        payload={"q": "x"},
                        now_ns=now + 10,
                    )
                self.assertEqual(len(adapter.execute_calls), 0)

    def test_llm_connector_scrubs_http_error_secret_straddling_240_byte_boundary(
        self,
    ) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        from sourdough.connectors import (
            CONNECTOR_KIND_LLM_INFERENCE,
            LLM_COMPLETE_OP,
            ScopedLlmInferenceConnector,
        )

        # Low-entropy test token so gitleaks does not flag it as a real API key,
        # while still long enough (32 chars) to straddle the 240-byte excerpt boundary.
        secret_canary = "dummy-boundary-canary-token-0000"

        class _ErrHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

            def do_POST(self) -> None:
                auth = self.headers.get("Authorization", "")
                body = (("x" * 220) + auth + ("y" * 40)).encode("utf-8")
                self.send_response(401)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), _ErrHandler)
        port = int(server.server_address[1])
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with TemporaryDirectory(prefix="sd-c9-") as tmp:
                store, leases, founding, claim, now = _setup_company_and_claim(
                    Path(tmp)
                )
                proxy = ConnectorProxy(
                    ConnectorAdapterRegistry([ScopedLlmInferenceConnector()]),
                    InMemorySecretVault({"envref:OPENAI_DEV_KEY": secret_canary}),
                    loopback_only=True,
                )
                with hold_company_writer(
                    store, founding.company_id, lease_directory=leases
                ) as w:
                    binding = register_connector_binding(
                        w,
                        company_id=founding.company_id,
                        binding_id="llm-err",
                        connector_kind=CONNECTOR_KIND_LLM_INFERENCE,
                        environment="paper",
                        endpoint_url=f"http://127.0.0.1:{port}",
                        allowed_hosts=("127.0.0.1",),
                        operations=(LLM_COMPLETE_OP,),
                        credential_handle_ref="envref:OPENAI_DEV_KEY",
                        actor_id="board-1",
                        allow_loopback=True,
                        now_ns=now,
                    )
                    grant_connector_access(
                        w,
                        company_id=founding.company_id,
                        agent_id=founding.founder_id,
                        standing_grant_id=founding.grant_id,
                        binding_id=binding.binding_id,
                        binding_revision=binding.revision,
                        allowed_operations=("complete",),
                        granted_by_actor_id="board-1",
                        now_ns=now + 5,
                    )
                    resp = proxy.invoke(
                        w,
                        claim,
                        binding_id="llm-err",
                        operation="complete",
                        request_id="err-boundary-1",
                        payload={"model": "gpt-6-luna", "input": "hi"},
                        now_ns=now + 10,
                    )
                    self.assertEqual(resp.status, "rejected")
                    err_text = str(resp.payload.get("error", ""))
                    self.assertNotIn("dummy-boundary", err_text)
                    self.assertNotIn("canary-token", err_text)
                self.assertNotIn(b"dummy-boundary", store.read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            t.join(timeout=5.0)

    def test_binding_pinned_request_schema_enforced_before_dispatch(self) -> None:
        from sourdough.connectors import (
            CONNECTOR_KIND_LLM_INFERENCE,
            ScopedLlmInferenceConnector,
        )

        with TemporaryDirectory(prefix="sd-c10-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([ScopedLlmInferenceConnector()]),
                InMemorySecretVault(
                    {"envref:OPENAI_DEV_KEY": "test-schema-token-1111"}
                ),
                loopback_only=True,
            )
            restricted_complete = OperationSpec(
                name="complete",
                mode="write",
                request_schema={
                    "type": "object",
                    "properties": {
                        "model": {"type": "string", "enum": ["approved-model"]},
                        "input": {"type": "string", "minLength": 1},
                        "max_output_tokens": {"type": "integer", "maximum": 10},
                    },
                    "required": ["model", "input"],
                    "additionalProperties": False,
                },
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="llm-pinned",
                    connector_kind=CONNECTOR_KIND_LLM_INFERENCE,
                    environment="paper",
                    endpoint_url="http://127.0.0.1:9020",
                    allowed_hosts=("127.0.0.1",),
                    operations=(restricted_complete,),
                    credential_handle_ref="envref:OPENAI_DEV_KEY",
                    actor_id="board-1",
                    allow_loopback=True,
                    now_ns=now,
                )
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("complete",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 5,
                )
                handler = proxy.build_tool_handler(w, claim)
                with self.assertRaises(ConnectorRefused):
                    handler(
                        {
                            "binding_id": "llm-pinned",
                            "operation": "complete",
                            "request_id": "bad-model-1",
                            "payload": {
                                "model": "forbidden-model",
                                "input": "hi",
                                "max_output_tokens": 5,
                            },
                        }
                    )
                with self.assertRaises(ConnectorRefused):
                    handler(
                        {
                            "binding_id": "llm-pinned",
                            "operation": "complete",
                            "request_id": "bad-tokens-1",
                            "payload": {
                                "model": "approved-model",
                                "input": "hi",
                                "max_output_tokens": 1_000_000,
                            },
                        }
                    )
                # Nonpositive max_output_tokens (0 or negative) MUST be rejected, never silently uncapped!
                for bad_cap in (0, -5):
                    with self.assertRaises(ConnectorRefused):
                        handler(
                            {
                                "binding_id": "llm-pinned",
                                "operation": "complete",
                                "request_id": f"bad-cap-{bad_cap}",
                                "payload": {
                                    "model": "approved-model",
                                    "input": "hi",
                                    "max_output_tokens": bad_cap,
                                },
                            }
                        )

    def test_overlapping_in_flight_read_retry_is_blocked_without_second_dispatch(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-c11-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            adapter = _FakeAdapter()
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([adapter]),
                InMemorySecretVault({"envref:FAKE_TEST_KEY": "test-inflight-1111"}),
                loopback_only=True,
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-overlap",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9021",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    budget_ceiling_minor_units=25,
                    allow_loopback=True,
                    now_ns=now,
                )
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("fetch_doc",),
                    granted_by_actor_id="board-1",
                    max_cost_minor_units=25,
                    now_ns=now + 5,
                )

                overlapping_result: list[ConnectorResponse] = []

                def _exec_with_overlap(req: ConnectorRequest, _sec: MaskedSecret):
                    if len(adapter.execute_calls) == 1:
                        # Simulate overlapping repeat while first attempt is in_flight!
                        second = proxy.invoke(
                            w,
                            claim,
                            binding_id=binding.binding_id,
                            operation="fetch_doc",
                            request_id="overlap-read-1",
                            payload={"doc": "a"},
                            now_ns=now + 12,
                        )
                        overlapping_result.append(second)
                        # Calling reconcile_unknown_action while the original run is actively
                        # executing MUST be refused so it cannot clear in_flight & bypass budget!
                        idem_key = (
                            f"conn:{founding.company_id}:{founding.founder_id}:"
                            "svc-overlap:r1:fetch_doc:overlap-read-1"
                        )
                        with self.assertRaises(ConnectorRefused):
                            proxy.reconcile_unknown_action(
                                w, claim, idempotency_key=idem_key, now_ns=now + 12
                            )
                        # Concurrent DISTINCT request_id under the same budget ceiling MUST ALSO be blocked
                        # while the first budgeted request is still in_flight!
                        with self.assertRaises(ConnectorBudgetExceeded):
                            proxy.invoke(
                                w,
                                claim,
                                binding_id=binding.binding_id,
                                operation="fetch_doc",
                                request_id="overlap-read-DISTINCT-2",
                                payload={"doc": "b"},
                                now_ns=now + 13,
                            )
                    return ConnectorResponse(
                        status="observed",
                        payload={"request_id": req.request_id},
                        cost_minor_units=25,
                    )

                adapter.next_execute = _exec_with_overlap
                first = proxy.invoke(
                    w,
                    claim,
                    binding_id=binding.binding_id,
                    operation="fetch_doc",
                    request_id="overlap-read-1",
                    payload={"doc": "a"},
                    now_ns=now + 10,
                )
                self.assertEqual(first.status, "observed")
                self.assertEqual(len(adapter.execute_calls), 1)
                self.assertEqual(len(overlapping_result), 1)
                self.assertEqual(overlapping_result[0].status, "unknown")
                self.assertEqual(
                    overlapping_result[0].payload.get("reason"), "action_in_flight"
                )

    def test_trading_venue_connector_blocks_http_302_redirect_without_leaking_api_key(
        self,
    ) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        stolen_headers: list[str] = []

        class _TargetHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

            def do_GET(self) -> None:
                stolen_headers.append(self.headers.get("X-MBX-APIKEY", ""))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"orderId":1,"status":"FILLED"}')

        target_srv = HTTPServer(("127.0.0.1", 0), _TargetHandler)
        target_port = int(target_srv.server_address[1])

        class _RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header(
                    "Location", f"http://localhost:{target_port}/api/v3/order"
                )
                self.end_headers()

        redir_srv = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
        redir_port = int(redir_srv.server_address[1])
        t1 = threading.Thread(target=target_srv.serve_forever, daemon=True)
        t2 = threading.Thread(target=redir_srv.serve_forever, daemon=True)
        t1.start()
        t2.start()
        try:
            secret = MaskedSecretBundle(
                values={
                    "api_key": "test-venue-redirect-key-1111",
                    "api_secret": "test-venue-redirect-sec-2222",
                },
                handle_ref="envref:BINANCE_TESTNET",
            )
            req = ConnectorRequest(
                company_id="co-1",
                binding_id="venue-redir",
                binding_revision=1,
                operation="query_order",
                request_id="q-redir-1",
                idempotency_key="conn:co-1:venue-redir:r1:query_order:q-redir-1",
                payload={"symbol": "BTCUSDT", "client_order_id": "q-redir-1"},
                environment="external_test",
                endpoint_url=f"http://127.0.0.1:{redir_port}",
                allowed_hosts=("127.0.0.1",),
            )
            adapter = TradingVenueConnectorAdapter(
                venue_resolver=build_bound_binance_venue
            )
            with self.assertRaises(ConnectorEnvironmentMismatch):
                adapter.execute(req, secret=secret, timeout_seconds=5.0)
            self.assertEqual(stolen_headers, [])
        finally:
            redir_srv.shutdown()
            redir_srv.server_close()
            target_srv.shutdown()
            target_srv.server_close()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

    def test_pre_dispatch_budget_reservation_blocks_oversized_calls_and_unknown_reservations(
        self,
    ) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import json
        import threading
        from sourdough.connectors import (
            CONNECTOR_KIND_LLM_INFERENCE,
            LLM_COMPLETE_OP,
            ScopedLlmInferenceConnector,
        )

        with TemporaryDirectory(prefix="sd-c-res-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            adapter = _FakeAdapter()
            llm_adapter = ScopedLlmInferenceConnector()

            received_bodies: list[dict[str, Any]] = []

            class _LlmHandler(BaseHTTPRequestHandler):
                def log_message(self, fmt: str, *args: object) -> None:
                    del fmt, args

                def do_POST(self) -> None:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    received_bodies.append(body)
                    payload = json.dumps(
                        {
                            "id": "resp-res-1",
                            "model": body["model"],
                            "output_text": "ok",
                            "usage": {"input_tokens": 10, "output_tokens": 10},
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            srv = HTTPServer(("127.0.0.1", 0), _LlmHandler)
            port = int(srv.server_address[1])
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            try:
                proxy = ConnectorProxy(
                    ConnectorAdapterRegistry([adapter, llm_adapter]),
                    InMemorySecretVault(
                        {
                            "envref:FAKE_TEST_KEY": "test-res-key-1111",
                            "envref:OPENAI_TEST_KEY": "test-llm-res-key-2222",
                        }
                    ),
                    loopback_only=True,
                )
                with hold_company_writer(
                    store, founding.company_id, lease_directory=leases
                ) as w:
                    # 1. Ceiling 10 with 25-unit adapter blocks even the FIRST call before dispatch
                    b_tiny = register_connector_binding(
                        w,
                        company_id=founding.company_id,
                        binding_id="svc-tiny",
                        connector_kind="fake_service",
                        environment="external_test",
                        endpoint_url="http://127.0.0.1:9020",
                        allowed_hosts=("127.0.0.1",),
                        operations=adapter.manifest.operations,
                        credential_handle_ref="envref:FAKE_TEST_KEY",
                        actor_id="board-1",
                        budget_ceiling_minor_units=10,
                        allow_loopback=True,
                        now_ns=now,
                    )
                    grant_connector_access(
                        w,
                        company_id=founding.company_id,
                        agent_id=founding.founder_id,
                        standing_grant_id=founding.grant_id,
                        binding_id=b_tiny.binding_id,
                        binding_revision=b_tiny.revision,
                        allowed_operations=("fetch_doc",),
                        granted_by_actor_id="board-1",
                        max_cost_minor_units=10,
                        now_ns=now + 1,
                    )
                    with self.assertRaises(ConnectorBudgetExceeded):
                        proxy.invoke(
                            w,
                            claim,
                            binding_id=b_tiny.binding_id,
                            operation="fetch_doc",
                            request_id="tiny-1",
                            payload={"doc": "a"},
                            now_ns=now + 2,
                        )
                    self.assertEqual(len(adapter.execute_calls), 0)

                    # 2. Ceiling 30 allows 1st call (25 <= 30) and blocks 2nd (25 + 25 = 50 > 30) before dispatch
                    b_med = register_connector_binding(
                        w,
                        company_id=founding.company_id,
                        binding_id="svc-med",
                        connector_kind="fake_service",
                        environment="external_test",
                        endpoint_url="http://127.0.0.1:9021",
                        allowed_hosts=("127.0.0.1",),
                        operations=adapter.manifest.operations,
                        credential_handle_ref="envref:FAKE_TEST_KEY",
                        actor_id="board-1",
                        budget_ceiling_minor_units=30,
                        allow_loopback=True,
                        now_ns=now + 5,
                    )
                    grant_connector_access(
                        w,
                        company_id=founding.company_id,
                        agent_id=founding.founder_id,
                        standing_grant_id=founding.grant_id,
                        binding_id=b_med.binding_id,
                        binding_revision=b_med.revision,
                        allowed_operations=("fetch_doc",),
                        granted_by_actor_id="board-1",
                        max_cost_minor_units=30,
                        now_ns=now + 6,
                    )
                    first = proxy.invoke(
                        w,
                        claim,
                        binding_id=b_med.binding_id,
                        operation="fetch_doc",
                        request_id="med-1",
                        payload={"doc": "a"},
                        now_ns=now + 7,
                    )
                    self.assertEqual(first.status, "observed")
                    self.assertEqual(len(adapter.execute_calls), 1)
                    with self.assertRaises(ConnectorBudgetExceeded):
                        proxy.invoke(
                            w,
                            claim,
                            binding_id=b_med.binding_id,
                            operation="fetch_doc",
                            request_id="med-2",
                            payload={"doc": "b"},
                            now_ns=now + 8,
                        )
                    self.assertEqual(len(adapter.execute_calls), 1)

                    # 3. Indeterminate ('unknown') call keeps its reserved_cost_minor_units held
                    b_unk = register_connector_binding(
                        w,
                        company_id=founding.company_id,
                        binding_id="svc-unk",
                        connector_kind="fake_service",
                        environment="external_test",
                        endpoint_url="http://127.0.0.1:9022",
                        allowed_hosts=("127.0.0.1",),
                        operations=adapter.manifest.operations,
                        credential_handle_ref="envref:FAKE_TEST_KEY",
                        actor_id="board-1",
                        budget_ceiling_minor_units=30,
                        allow_loopback=True,
                        now_ns=now + 10,
                    )
                    grant_connector_access(
                        w,
                        company_id=founding.company_id,
                        agent_id=founding.founder_id,
                        standing_grant_id=founding.grant_id,
                        binding_id=b_unk.binding_id,
                        binding_revision=b_unk.revision,
                        allowed_operations=("fetch_doc",),
                        granted_by_actor_id="board-1",
                        max_cost_minor_units=30,
                        now_ns=now + 11,
                    )
                    adapter.next_execute = lambda _r, _s: (_ for _ in ()).throw(
                        ConnectorIndeterminate("timeout")
                    )
                    unk = proxy.invoke(
                        w,
                        claim,
                        binding_id=b_unk.binding_id,
                        operation="fetch_doc",
                        request_id="unk-1",
                        payload={"doc": "a"},
                        now_ns=now + 12,
                    )
                    self.assertEqual(unk.status, "unknown")
                    adapter.next_execute = None
                    with self.assertRaises(ConnectorBudgetExceeded):
                        proxy.invoke(
                            w,
                            claim,
                            binding_id=b_unk.binding_id,
                            operation="fetch_doc",
                            request_id="unk-2",
                            payload={"doc": "b"},
                            now_ns=now + 13,
                        )
                    self.assertEqual(len(adapter.execute_calls), 2)

                    # 4. ScopedLlmInferenceConnector reserves upper-bound cost & enforces cap
                    b_llm = register_connector_binding(
                        w,
                        company_id=founding.company_id,
                        binding_id="llm-tight",
                        connector_kind=CONNECTOR_KIND_LLM_INFERENCE,
                        environment="external_test",
                        endpoint_url=f"http://127.0.0.1:{port}",
                        allowed_hosts=("127.0.0.1",),
                        operations=(LLM_COMPLETE_OP,),
                        credential_handle_ref="envref:OPENAI_TEST_KEY",
                        actor_id="board-1",
                        budget_ceiling_minor_units=5,
                        allow_loopback=True,
                        now_ns=now + 20,
                    )
                    grant_connector_access(
                        w,
                        company_id=founding.company_id,
                        agent_id=founding.founder_id,
                        standing_grant_id=founding.grant_id,
                        binding_id=b_llm.binding_id,
                        binding_revision=b_llm.revision,
                        allowed_operations=("complete",),
                        granted_by_actor_id="board-1",
                        max_cost_minor_units=5,
                        now_ns=now + 21,
                    )
                    # Omitted max_output_tokens reserves 16,384 output tokens (> 5 minor units) -> blocked!
                    with self.assertRaises(ConnectorBudgetExceeded):
                        proxy.invoke(
                            w,
                            claim,
                            binding_id=b_llm.binding_id,
                            operation="complete",
                            request_id="llm-uncapped",
                            payload={"model": "gpt-6-luna", "input": "hello"},
                            now_ns=now + 22,
                        )
                    self.assertEqual(received_bodies, [])
                    # Explicitly bounded max_output_tokens=100 fits within ceiling 5 -> succeeds!
                    llm_ok = proxy.invoke(
                        w,
                        claim,
                        binding_id=b_llm.binding_id,
                        operation="complete",
                        request_id="llm-bounded",
                        payload={
                            "model": "gpt-6-luna",
                            "input": "hello",
                            "max_output_tokens": 100,
                        },
                        now_ns=now + 23,
                    )
                    self.assertEqual(llm_ok.status, "observed")
                    self.assertEqual(len(received_bodies), 1)
                    self.assertEqual(received_bodies[0]["max_output_tokens"], 100)
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join(timeout=5.0)

    def test_trading_venue_connector_reconciles_binance_mapped_client_order_id(
        self,
    ) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import json
        import threading
        from urllib.parse import parse_qs, urlparse

        submitted_coids: list[str] = []
        queried_coids: list[str] = []

        class _BinanceMockHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                length = int(self.headers.get("Content-Length", "0"))
                if length > 0:
                    body_params = parse_qs(self.rfile.read(length).decode("utf-8"))
                    params.update(body_params)
                coid = (params.get("newClientOrderId") or [""])[0]
                submitted_coids.append(coid)
                # Simulate HTTP 500 ambiguous execution reply so ConnectorProxy records 'unknown'
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"code":-1001,"msg":"internal error"}')

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                coid = (params.get("origClientOrderId") or [""])[0]
                queried_coids.append(coid)
                if coid in submitted_coids:
                    body = json.dumps(
                        {
                            "orderId": 98765,
                            "clientOrderId": coid,
                            "symbol": "BTCUSDT",
                            "side": "BUY",
                            "status": "FILLED",
                            "origQty": "0.01",
                            "executedQty": "0.01",
                            "cummulativeQuoteQty": "500.00",
                            "updateTime": 1700000001000,
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"code":-2013,"msg":"Order does not exist."}')

        srv = HTTPServer(("127.0.0.1", 0), _BinanceMockHandler)
        port = int(srv.server_address[1])
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory(prefix="sd-c-bmap-") as tmp:
                store, leases, founding, claim, now = _setup_company_and_claim(
                    Path(tmp)
                )
                adapter = TradingVenueConnectorAdapter(
                    venue_resolver=build_bound_binance_venue
                )
                proxy = ConnectorProxy(
                    ConnectorAdapterRegistry([adapter]),
                    InMemorySecretVault(
                        {
                            "envref:BINANCE_TESTNET": {
                                "api_key": "test-bmap-key-1111",
                                "api_secret": "test-bmap-sec-2222",
                            }
                        }
                    ),
                    loopback_only=True,
                )
                with hold_company_writer(
                    store, founding.company_id, lease_directory=leases
                ) as w:
                    binding = register_connector_binding(
                        w,
                        company_id=founding.company_id,
                        binding_id="binance-spot-test",
                        connector_kind=CONNECTOR_KIND_TRADING_VENUE,
                        environment="external_test",
                        endpoint_url=f"http://127.0.0.1:{port}",
                        allowed_hosts=("127.0.0.1",),
                        operations=adapter.manifest.operations,
                        credential_handle_ref="envref:BINANCE_TESTNET",
                        actor_id="board-1",
                        allow_loopback=True,
                        now_ns=now,
                    )
                    grant_connector_access(
                        w,
                        company_id=founding.company_id,
                        agent_id=founding.founder_id,
                        standing_grant_id=founding.grant_id,
                        binding_id=binding.binding_id,
                        binding_revision=binding.revision,
                        allowed_operations=("submit_order", "query_order"),
                        granted_by_actor_id="board-1",
                        now_ns=now + 5,
                    )
                    action_id = "order-binance-map-1"
                    payload = {
                        "action_id": action_id,
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "time_in_force": "GTC",
                        "quantity": "0.01",
                        "limit_price": "50000",
                    }
                    adm = issue_connector_admission(
                        w,
                        company_id=founding.company_id,
                        binding_id=binding.binding_id,
                        binding_revision=binding.revision,
                        operation="submit_order",
                        request_id=action_id,
                        payload=payload,
                        issued_by_service="trading.execution_service",
                        actor_id=founding.founder_id,
                        now_ns=now + 8,
                    )
                    resp = proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="submit_order",
                        request_id=action_id,
                        payload=payload,
                        admission_id=adm.admission_id,
                        now_ns=now + 10,
                    )
                    self.assertEqual(resp.status, "unknown")
                    idem_key = resp.payload["idempotency_key"]

                    reconciled = proxy.reconcile_unknown_action(
                        w, claim, idempotency_key=idem_key, now_ns=now + 20
                    )
                    self.assertEqual(len(submitted_coids), 1)
                    expected_coid = submitted_coids[0]
                    self.assertTrue(expected_coid.startswith("sd_"))
                    self.assertEqual(queried_coids, [expected_coid])
                    self.assertNotEqual(expected_coid, action_id)
                    self.assertEqual(reconciled.status, "observed")
                    self.assertEqual(reconciled.payload["order_status"], "filled")
                    self.assertEqual(
                        reconciled.payload["client_order_id"], expected_coid
                    )
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5.0)

    def test_llm_connector_caps_response_bytes_output_text_and_embedding_size(
        self,
    ) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import json
        import threading
        from sourdough.connectors import ScopedLlmInferenceConnector

        mode = {"kind": "oversize_bytes"}

        class _BigHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)
                if mode["kind"] == "oversize_bytes":
                    raw = b'{"output_text":"' + b"A" * 4096 + b'"}'
                elif mode["kind"] == "oversize_text":
                    raw = json.dumps({"id": "r1", "output_text": "B" * 64}).encode(
                        "utf-8"
                    )
                else:
                    raw = json.dumps(
                        {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4, 0.5]}]}
                    ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = HTTPServer(("127.0.0.1", 0), _BigHandler)
        port = int(srv.server_address[1])
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = ScopedLlmInferenceConnector(
                max_response_bytes=512,
                max_output_text_chars=32,
                max_embedding_dimensions=3,
            )
            secret = MaskedSecret(
                value="test-cap-key-1111", handle_ref="envref:OPENAI_TEST_KEY"
            )
            req_complete = ConnectorRequest(
                company_id="co-1",
                binding_id="llm-cap",
                binding_revision=1,
                operation="complete",
                request_id="cap-1",
                idempotency_key="conn:co-1:llm-cap:r1:complete:cap-1",
                payload={"model": "gpt-6-luna", "input": "hi"},
                environment="external_test",
                endpoint_url=f"http://127.0.0.1:{port}",
                allowed_hosts=("127.0.0.1",),
            )
            mode["kind"] = "oversize_bytes"
            with self.assertRaises(ConnectorIndeterminate):
                adapter.execute(req_complete, secret=secret, timeout_seconds=5.0)

            mode["kind"] = "oversize_text"
            with self.assertRaises(ConnectorIndeterminate):
                adapter.execute(req_complete, secret=secret, timeout_seconds=5.0)

            req_embed = ConnectorRequest(
                company_id="co-1",
                binding_id="llm-cap",
                binding_revision=1,
                operation="embed",
                request_id="cap-2",
                idempotency_key="conn:co-1:llm-cap:r1:embed:cap-2",
                payload={"model": "text-embedding-3-small", "input": "hi"},
                environment="external_test",
                endpoint_url=f"http://127.0.0.1:{port}",
                allowed_hosts=("127.0.0.1",),
            )
            mode["kind"] = "oversize_embed"
            with self.assertRaises(ConnectorIndeterminate):
                adapter.execute(req_embed, secret=secret, timeout_seconds=5.0)
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5.0)

    def test_unknown_read_retries_accumulate_reservations_and_preserve_ceiling(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-c-cumres-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            adapter = _FakeAdapter()
            adapter.next_execute = lambda _r, _s: (_ for _ in ()).throw(
                ConnectorIndeterminate("upstream timeout")
            )
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([adapter]),
                InMemorySecretVault({"envref:FAKE_TEST_KEY": "test-cum-key-1111"}),
                loopback_only=True,
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                # 1. Ceiling 25 allows attempt 1 (25 <= 25), then blocks retry (25+25=50 > 25)!
                b25 = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-cum-25",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9030",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    max_requests_per_window=10,
                    budget_ceiling_minor_units=25,
                    allow_loopback=True,
                    now_ns=now,
                )
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=b25.binding_id,
                    binding_revision=b25.revision,
                    allowed_operations=("fetch_doc",),
                    granted_by_actor_id="board-1",
                    max_cost_minor_units=25,
                    now_ns=now + 1,
                )
                r1 = proxy.invoke(
                    w,
                    claim,
                    binding_id=b25.binding_id,
                    operation="fetch_doc",
                    request_id="same-read",
                    payload={"doc": "a"},
                    now_ns=now + 2,
                )
                self.assertEqual(r1.status, "unknown")
                with self.assertRaises(ConnectorBudgetExceeded):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=b25.binding_id,
                        operation="fetch_doc",
                        request_id="same-read",
                        payload={"doc": "a"},
                        now_ns=now + 3,
                    )
                self.assertEqual(len(adapter.execute_calls), 1)

                # 2. Ceiling 60 allows attempt 1 (25) and attempt 2 (50 <= 60), then blocks attempt 3 (75 > 60)!
                b60 = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-cum-60",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9031",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    max_requests_per_window=10,
                    budget_ceiling_minor_units=60,
                    allow_loopback=True,
                    now_ns=now + 10,
                )
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=b60.binding_id,
                    binding_revision=b60.revision,
                    allowed_operations=("fetch_doc",),
                    granted_by_actor_id="board-1",
                    max_cost_minor_units=60,
                    now_ns=now + 11,
                )
                proxy.invoke(
                    w,
                    claim,
                    binding_id=b60.binding_id,
                    operation="fetch_doc",
                    request_id="same-read-60",
                    payload={"doc": "a"},
                    now_ns=now + 12,
                )
                proxy.invoke(
                    w,
                    claim,
                    binding_id=b60.binding_id,
                    operation="fetch_doc",
                    request_id="same-read-60",
                    payload={"doc": "a"},
                    now_ns=now + 13,
                )
                with self.assertRaises(ConnectorBudgetExceeded):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=b60.binding_id,
                        operation="fetch_doc",
                        request_id="same-read-60",
                        payload={"doc": "a"},
                        now_ns=now + 14,
                    )
                self.assertEqual(len(adapter.execute_calls), 3)

    def test_recovery_missing_credentials_preserves_unknown_and_rate_limits_lookups(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-c-rec-cred-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            adapter = _FakeAdapter()
            adapter.next_execute = lambda _r, _s: (_ for _ in ()).throw(
                ConnectorIndeterminate("lost reply")
            )
            available_secret: dict[str, str] = {
                "envref:FAKE_TEST_KEY": "test-rec-cred-1111"
            }

            class _DynamicVault:
                def resolve_secret(
                    self,
                    handle_ref: str,
                    *,
                    expected_environment: str | None = None,
                ) -> MaskedSecret:
                    del expected_environment
                    if handle_ref not in available_secret:
                        from sourdough.secret_vault import CredentialRefused

                        raise CredentialRefused(f"Missing secret {handle_ref}")
                    return MaskedSecret(
                        value=available_secret[handle_ref], handle_ref=handle_ref
                    )

                def resolve_bundle(
                    self,
                    handle_ref: str,
                    fields: Any,
                    *,
                    expected_environment: str | None = None,
                ) -> MaskedSecretBundle:
                    raise NotImplementedError

            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([adapter]),
                _DynamicVault(),
                loopback_only=True,
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-rec-cred",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9032",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    max_requests_per_window=2,
                    window_seconds=60,
                    allow_loopback=True,
                    now_ns=now,
                )
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("submit_write",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 1,
                )
                adm = issue_connector_admission(
                    w,
                    company_id=founding.company_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    operation="submit_write",
                    request_id="ord-cred-1",
                    payload={"qty": 1},
                    issued_by_service="trading.execution_service",
                    actor_id=founding.founder_id,
                    now_ns=now + 2,
                )
                r1 = proxy.invoke(
                    w,
                    claim,
                    binding_id=binding.binding_id,
                    operation="submit_write",
                    request_id="ord-cred-1",
                    payload={"qty": 1},
                    admission_id=adm.admission_id,
                    now_ns=now + 3,
                )
                self.assertEqual(r1.status, "unknown")
                idem_key = r1.payload["idempotency_key"]

                # Temporarily remove credentials -> retry in lookup mode MUST NOT turn action 'rejected'!
                del available_secret["envref:FAKE_TEST_KEY"]
                with self.assertRaises(ConnectorRefused):
                    proxy.invoke(
                        w,
                        claim,
                        binding_id=binding.binding_id,
                        operation="submit_write",
                        request_id="ord-cred-1",
                        payload={"qty": 1},
                        admission_id=adm.admission_id,
                        now_ns=now + 4,
                    )
                with scoped_reader(store) as db:
                    row = db.execute(
                        "SELECT status FROM external_actions WHERE idempotency_key = ?",
                        (idem_key,),
                    ).fetchone()
                    self.assertEqual(row, ("unknown",))

                # Restore credentials; 2 attempts already consumed in this 60s window ->
                # reconcile_unknown_action MUST enforce the binding rate limit!
                available_secret["envref:FAKE_TEST_KEY"] = "test-rec-cred-1111"
                with self.assertRaises(ConnectorRateLimited):
                    proxy.reconcile_unknown_action(
                        w, claim, idempotency_key=idem_key, now_ns=now + 5
                    )
                self.assertEqual(len(adapter.lookup_calls), 0)

                # Once the 60s window passes, recovery succeeds cleanly!
                reconciled = proxy.reconcile_unknown_action(
                    w,
                    claim,
                    idempotency_key=idem_key,
                    now_ns=now + 120_000_000_000,
                )
                self.assertEqual(reconciled.status, "observed")
                self.assertEqual(len(adapter.lookup_calls), 1)

    def test_cancel_order_recovery_does_not_confirm_active_order(self) -> None:
        from decimal import Decimal
        from sourdough.trading.external_venue import (
            ExternalOrderStatus,
            VenueCapabilities,
        )

        current_status = {"value": "open"}

        class _StubVenue:
            endpoint_url = "http://127.0.0.1:9040"
            credential_handle_ref = "envref:BINANCE_TESTNET"
            blocks_redirects = True
            capabilities = VenueCapabilities(
                adapter_revision="stub-v1",
                venue_name="stub_exchange",
                market_kind="spot",
                environment="external_test",
                supports_spot=True,
                supports_usdm_futures=False,
                supports_ioc_limit=True,
                supports_gtc_limit=True,
                seals_absent_orders=False,
                supports_external_reconciliation=True,
            )

            def query_order(
                self, *, symbol: str, client_order_id: str
            ) -> ExternalOrderStatus:
                return ExternalOrderStatus(
                    venue_order_id="v-100",
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side="buy",
                    status=current_status["value"],
                    original_quantity=Decimal("1"),
                    filled_quantity=Decimal("0"),
                    remaining_quantity=Decimal("1"),
                    avg_price=Decimal("0"),
                    update_time_ms=1700000000000,
                )

        adapter = TradingVenueConnectorAdapter(
            venue_resolver=lambda _r, _s: _StubVenue()
        )
        secret = MaskedSecretBundle(
            values={"api_key": "test-k-1", "api_secret": "test-s-1"},
            handle_ref="envref:BINANCE_TESTNET",
        )
        req = ConnectorRequest(
            company_id="co-1",
            binding_id="venue-cancel",
            binding_revision=1,
            operation="cancel_order",
            request_id="c-1",
            idempotency_key="conn:co-1:venue-cancel:r1:cancel_order:c-1",
            payload={"symbol": "BTCUSDT", "client_order_id": "ord-open-1"},
            environment="external_test",
            endpoint_url="http://127.0.0.1:9040",
            allowed_hosts=("127.0.0.1",),
        )
        # Still open -> MUST remain unknown (never confirmed!)
        r_open = adapter.lookup(req, secret=secret, timeout_seconds=5.0)
        self.assertEqual(r_open.status, "unknown")
        self.assertTrue(r_open.indeterminate)

        # Partially filled -> MUST still remain unknown!
        current_status["value"] = "partially_filled"
        r_part = adapter.lookup(req, secret=secret, timeout_seconds=5.0)
        self.assertEqual(r_part.status, "unknown")
        self.assertTrue(r_part.indeterminate)

        # Filled -> cancel was superseded -> rejected!
        current_status["value"] = "filled"
        r_fill = adapter.lookup(req, secret=secret, timeout_seconds=5.0)
        self.assertEqual(r_fill.status, "rejected")
        self.assertFalse(r_fill.indeterminate)

        # Canceled -> observed!
        current_status["value"] = "canceled"
        r_cancel = adapter.lookup(req, secret=secret, timeout_seconds=5.0)
        self.assertEqual(r_cancel.status, "observed")
        self.assertFalse(r_cancel.indeterminate)

    def test_journal_posting_failure_rolls_back_action_settlement_atomically(
        self,
    ) -> None:
        from unittest.mock import patch
        from sourdough.general_ledger import JournalRefused

        with TemporaryDirectory(prefix="sd-c-glatom-") as tmp:
            store, leases, founding, claim, now = _setup_company_and_claim(Path(tmp))
            adapter = _FakeAdapter()
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([adapter]),
                InMemorySecretVault({"envref:FAKE_TEST_KEY": "test-gl-key-1111"}),
                loopback_only=True,
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="svc-gl",
                    connector_kind="fake_service",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9045",
                    allowed_hosts=("127.0.0.1",),
                    operations=adapter.manifest.operations,
                    credential_handle_ref="envref:FAKE_TEST_KEY",
                    actor_id="board-1",
                    allow_loopback=True,
                    now_ns=now,
                )
                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("fetch_doc",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 1,
                )
                with patch(
                    "sourdough.connectors.proxy.post_journal_in_tx",
                    side_effect=JournalRefused("simulated GL failure"),
                ):
                    with self.assertRaises(JournalRefused):
                        proxy.invoke(
                            w,
                            claim,
                            binding_id=binding.binding_id,
                            operation="fetch_doc",
                            request_id="gl-atom-1",
                            payload={"doc": "a"},
                            now_ns=now + 2,
                        )
                idem_key = (
                    f"conn:{founding.company_id}:{founding.founder_id}:"
                    "svc-gl:r1:fetch_doc:gl-atom-1"
                )
                with scoped_reader(store) as db:
                    status_row = db.execute(
                        "SELECT status FROM external_actions WHERE idempotency_key = ?",
                        (idem_key,),
                    ).fetchone()
                    # Settlement rolled back atomically: NEVER 'confirmed' without GL!
                    self.assertNotEqual(status_row, ("confirmed",))
                    gl_count = db.execute(
                        "SELECT COUNT(*) FROM journal_entry WHERE source_event = ?",
                        (idem_key,),
                    ).fetchone()[0]
                    self.assertEqual(gl_count, 0)


if __name__ == "__main__":
    unittest.main()
