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
    register_connector_binding,
    revoke_connector_grant,
)
from sourdough.secret_vault import (
    InMemorySecretVault,
    MaskedSecret,
    MaskedSecretBundle,
)
from sourdough.duty_run import WorkRunClaim
from sourdough.runtime.domains import DomainPluginRegistry
from sourdough.runtime.kernel import claim_work_run, create_work_item
from sourdough.runtime.shift.flow_guard import ProvenanceIndex, ShiftFlowGuard
from sourdough.storage.db import scoped_reader, write_uow
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

                # 3. Effectful operation requires admission_id
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
                resp = proxy.invoke(
                    w,
                    claim,
                    binding_id=binding.binding_id,
                    operation="submit_write",
                    request_id="ord-1",
                    payload={"qty": 1},
                    admission_id="adm-ord-1",
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


if __name__ == "__main__":
    unittest.main()
