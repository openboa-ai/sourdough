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
                    budget_ceiling_minor_units=30,
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
                    max_cost_minor_units=30,
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

                # 6. Second unique call succeeds (total spent becomes 50 >= 30 ceiling)
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
                # After window passes, budget ceiling (30) blocks new calls!
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
                    f"conn:{founding.company_id}:svc-priv:r1:fetch_doc:priv-doc-1"
                )

                with self.assertRaises(ConnectorUnauthorized):
                    proxy.reconcile_unknown_action(
                        w, bystander_claim, idempotency_key=idem_key, now_ns=now + 15
                    )

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


if __name__ == "__main__":
    unittest.main()
