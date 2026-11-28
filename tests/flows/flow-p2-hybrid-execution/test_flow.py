"""Connected E2E flow verification for flow-p2-hybrid-execution."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
import unittest
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.agent_runtime import (
    PROVIDER_CODE_PLANE,
    PROVIDER_DECISION_MODEL,
    AgentAdapterRegistry,
    AgentProfileBinding,
    OuterToolBroker,
    ShiftRequest,
    TokenUsage,
    UdsToolBrokerServer,
    hydrate_shift_context,
)
from sourdough.board_control import record_system_restriction
from sourdough.company import FoundingTerms, found
from sourdough.connectors import (
    CONNECTOR_KIND_LLM_INFERENCE,
    LLM_COMPLETE_OP,
    ConnectorAdapterRegistry,
    ConnectorManifest,
    ConnectorProxy,
    ConnectorRequest,
    ConnectorResponse,
    OperationSpec,
    ScopedLlmInferenceConnector,
    grant_connector_access,
    register_connector_binding,
)
from sourdough.duty_run import WorkRunClaim
from sourdough.runtime.hybrid_execution import (
    CodePlaneExecutor,
    CodePlaneRuntimeAdapter,
    CodeRuleDecision,
    ConnectorEffectIntent,
    DecisionModelPlaneExecutor,
    DecisionModelRuntimeAdapter,
    DeterministicControlPlaneGate,
    FeaturePoint,
    FeatureSnapshot,
    HybridControlGateDenied,
    HybridOfflineEvalRequired,
    LlmMetaPlaneExecutor,
    ModelScore,
    OfflineEvalVerdict,
    ParameterBound,
    PolicyPatchProposal,
)
from sourdough.runtime.kernel import (
    DecisionEvidence,
    claim_work_run,
    create_work_item,
    work_run_evidence_revision,
)
from sourdough.secret_vault import InMemorySecretVault, MaskedSecret
from sourdough.storage.db import scoped_reader, write_uow
from sourdough.writer_lease import hold_company_writer

_CANARY_EXEC_TOKEN = "test-hyb-flow-exec-token-1111"


class _LoopbackDispatchServer:
    """Live loopback HTTP destination receiving ConnectorProxy-authorized hybrid effects."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.auth_headers: list[str] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.received.append(body)
                outer.auth_headers.append(self.headers.get("Authorization", ""))
                resp = json.dumps(
                    {
                        "receipt_id": f"rcpt-{len(outer.received)}",
                        "accepted": True,
                        "echo": self.headers.get("Authorization", ""),
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        self._srv = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._srv.server_address[1])
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _LoopbackDispatchServer:
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5.0)


class _LoopbackEffectAdapter:
    max_cost_per_call_minor_units: int = 0

    def __init__(self) -> None:
        self._manifest = ConnectorManifest(
            connector_kind="exec_sink",
            adapter_revision="exec-sink-v1",
            operations=(
                OperationSpec(
                    name="dispatch_action",
                    mode="effectful",
                    request_schema={
                        "type": "object",
                        "properties": {
                            "intent": {"type": "string"},
                            "units": {"type": "integer"},
                        },
                        "required": ["intent", "units"],
                    },
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
        secret: MaskedSecret | Any,
        timeout_seconds: float,
    ) -> ConnectorResponse:
        import urllib.request

        assert isinstance(secret, MaskedSecret)
        req = urllib.request.Request(
            f"{request.endpoint_url.rstrip('/')}/dispatch",
            data=json.dumps(request.payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {secret.value}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            parsed = json.loads(resp.read(65_536).decode("utf-8"))
        return ConnectorResponse(
            status="observed",
            payload=parsed,
            source_ref=str(parsed["receipt_id"]),
            cost_minor_units=0,
        )

    def lookup(
        self,
        request: ConnectorRequest,
        *,
        secret: MaskedSecret | Any,
        timeout_seconds: float,
    ) -> ConnectorResponse:
        del secret, timeout_seconds
        return ConnectorResponse(
            status="observed",
            payload={"receipt_id": request.request_id, "accepted": True},
            source_ref=request.request_id,
            cost_minor_units=0,
        )


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with (
            TemporaryDirectory(prefix="sd-p2h-") as tmp,
            _LoopbackDispatchServer() as sink_srv,
        ):
            root = Path(tmp)
            os.chmod(root, 0o700)
            store = root / "co.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms(
                    "Hybrid Flow Co", "charter-v1", "manage-company", "profile-hyb"
                ),
            )
            cid = founding.company_id
            fid = founding.founder_id
            now = time_ns()

            # 1. Verify standalone UdsToolBrokerServer & generalized multi-plane AgentAdapterRegistry
            uds_broker = OuterToolBroker(
                company_id=cid,
                agent_id=fid,
                run_id="uds-run-1",
                fencing_token=1,
                handlers={"check_health": lambda a: {"status": "healthy", "k": a["k"]}},
            )
            sock_path = root / "standalone-broker.sock"
            with UdsToolBrokerServer(sock_path, uds_broker, unlink_on_stop=True):
                self.assertEqual(stat.S_IMODE(sock_path.stat().st_mode), 0o600)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(sock_path))
                    client.sendall(
                        (
                            json.dumps(
                                {
                                    "call_id": "uds-1",
                                    "tool": "check_health",
                                    "arguments": {"k": "ok"},
                                }
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                    reply = json.loads(client.recv(4096).decode("utf-8"))
                self.assertTrue(reply["ok"])
                self.assertEqual(reply["result"], {"status": "healthy", "k": "ok"})
            self.assertFalse(sock_path.exists())

            adapter_reg = AgentAdapterRegistry()
            adapter_reg.register(
                CodePlaneRuntimeAdapter(
                    handler=lambda p, r, _b: f"code-plane:{p.model}:{r.instruction}"
                )
            )
            adapter_reg.register(
                DecisionModelRuntimeAdapter(
                    handler=lambda p, r, _b: f"dm-plane:{p.model}:{r.instruction}"
                )
            )
            code_prof = AgentProfileBinding(
                provider=PROVIDER_CODE_PLANE, model="spread-guard", revision="v1"
            )
            dm_prof = AgentProfileBinding(
                provider=PROVIDER_DECISION_MODEL, model="regime-v2", revision="v2"
            )
            shift_ctx = hydrate_shift_context(
                company_id=cid,
                agent_id=fid,
                run_id="r-shift",
                fencing_token=1,
                grant_scope="manage-company",
                mandate_summary="Run hybrid execution planes",
            )
            shift_req = ShiftRequest(
                company_id=cid,
                agent_id=fid,
                run_id="r-shift",
                turn_id="t-1",
                fencing_token=1,
                instruction="evaluate",
                context=shift_ctx,
                tools=(),
                budget_ceiling_minor_units=0,
            )
            code_shift = adapter_reg.execute_shift(
                persisted_profile_id=code_prof.profile_id(),
                profile=code_prof,
                request=shift_req,
                broker=uds_broker,
            )
            dm_shift = adapter_reg.execute_shift(
                persisted_profile_id=dm_prof.profile_id(),
                profile=dm_prof,
                request=shift_req,
                broker=uds_broker,
            )
            self.assertEqual(code_shift.usage, TokenUsage(0, 0))
            self.assertEqual(dm_shift.usage, TokenUsage(0, 0))

            # 2. Wire ConnectorProxy + DeterministicControlPlaneGate
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry(
                    [_LoopbackEffectAdapter(), ScopedLlmInferenceConnector()]
                ),
                InMemorySecretVault({"envref:EXEC_SINK_KEY": _CANARY_EXEC_TOKEN}),
                loopback_only=True,
            )
            gate = DeterministicControlPlaneGate(
                connector_proxy=proxy,
                max_notional_minor_units=10_000,
                min_llm_eval_samples=30,
                min_llm_holdout_margin=0.01,
            )

            with hold_company_writer(store, cid, lease_directory=leases) as w:
                with write_uow(w) as db:
                    item, _ = create_work_item(
                        db,
                        company_id=cid,
                        owner_agent_id=fid,
                        duty_kind="work.assessment_review",
                        source_kind="flow",
                        source_id="src-p2h-1",
                        now_ns=now + 1,
                    )
                    run = claim_work_run(
                        db,
                        company_id=cid,
                        work_item_id=item.work_item_id,
                        lease_owner="hyb-worker",
                        lease_ttl_ns=3_600_000_000_000,
                        now_ns=now + 2,
                    )
                    created_ns = int(
                        db.execute(
                            "SELECT created_at_ns FROM work_runs WHERE run_id = ?",
                            (run.run_id,),
                        ).fetchone()[0]
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
                run_ev = DecisionEvidence(
                    kind="work_run",
                    resource_id=run.run_id,
                    revision=work_run_evidence_revision(
                        run.run_id,
                        company_id=cid,
                        work_item_id=item.work_item_id,
                        agent_id=fid,
                        duty_kind="work.assessment_review",
                        fencing_token=run.fencing_token,
                        created_at_ns=created_ns,
                    ),
                    observed_at_ns=created_ns,
                    available_at_ns=created_ns,
                )
                binding = register_connector_binding(
                    w,
                    company_id=cid,
                    binding_id="exec-sink-1",
                    connector_kind="exec_sink",
                    environment="external_test",
                    endpoint_url=sink_srv.base_url,
                    allowed_hosts=("127.0.0.1",),
                    operations=_LoopbackEffectAdapter().manifest.operations,
                    credential_handle_ref="envref:EXEC_SINK_KEY",
                    actor_id="owner-board",
                    allow_loopback=True,
                    now_ns=now + 10,
                )
                grant_connector_access(
                    w,
                    company_id=cid,
                    agent_id=fid,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("dispatch_action",),
                    granted_by_actor_id="owner-board",
                    now_ns=now + 11,
                )

                # 3. Plane 1 (Code Plane) -> $0 LLM cost + live effectful dispatch via ConnectorProxy!
                t_dec = now + 100
                snap = FeatureSnapshot(
                    snapshot_id="snap-flow-1",
                    schema_version="hyb-v1",
                    decision_at_ns=t_dec,
                    features=(
                        FeaturePoint(
                            name="liquidity_ratio",
                            value=0.92,
                            observed_at_ns=t_dec - 20,
                            available_at_ns=t_dec - 10,
                        ),
                    ),
                    source_evidence=(run_ev,),
                )
                code_plane = CodePlaneExecutor(
                    rule_id="rule.liquidity_dispatch",
                    code_revision="code-rev-1",
                    rule_fn=lambda s, p: CodeRuleDecision(
                        action_kind="proceed",
                        rationale="Liquidity ratio above deterministic threshold",
                        parameters={"ratio": s.feature_map()["liquidity_ratio"]},
                        proposed_notional_minor_units=2_500,
                        connector_effect=ConnectorEffectIntent(
                            binding_id=binding.binding_id,
                            operation="dispatch_action",
                            request_id="flow-code-1",
                            payload={"intent": "rebalance", "units": 3},
                            expected_environment="external_test",
                        ),
                    ),
                )
                p1_proposal = code_plane.evaluate(snap)
                self.assertEqual(p1_proposal.token_usage, TokenUsage(0, 0))
                p1_out = gate.commit(w, claim, p1_proposal, now_ns=t_dec)
                self.assertIsNotNone(p1_out.admission_id)
                self.assertIsNotNone(p1_out.connector_response)
                assert p1_out.connector_response is not None
                self.assertEqual(p1_out.connector_response.status, "observed")
                # Echoed bearer token MUST be scrubbed by ConnectorProxy!
                self.assertNotIn(
                    _CANARY_EXEC_TOKEN, json.dumps(p1_out.connector_response.payload)
                )
                self.assertEqual(len(sink_srv.received), 1)
                self.assertEqual(
                    sink_srv.auth_headers[0], f"Bearer {_CANARY_EXEC_TOKEN}"
                )

                # 4. Plane 2 (Decision Model Plane): pinned scoring + explicit timeout fallback
                weights = b"quant-regime-weights-v1"
                weights_sha = sha256(weights).hexdigest()
                safe_fallback = CodePlaneExecutor(
                    rule_id="rule.safe_hold",
                    code_revision="hold-v1",
                    rule_fn=lambda _s, _p: CodeRuleDecision(
                        action_kind="hold",
                        rationale="Safe deterministic fallback hold",
                    ),
                )
                dm_ok = DecisionModelPlaneExecutor(
                    model_id="regime-scorer",
                    model_revision="dm-v1",
                    expected_artifact_sha256=weights_sha,
                    feature_schema_version="hyb-v1",
                    artifact_bytes=weights,
                    min_confidence=0.75,
                    fallback_executor=safe_fallback,
                    scorer_fn=lambda _s, _a: ModelScore(
                        score=0.78,
                        confidence=0.88,
                        regime_label="calm",
                        action_kind="proceed",
                        rationale="Pinned model confirmed calm regime",
                        proposed_notional_minor_units=1_500,
                    ),
                )
                p2_out = gate.commit(
                    w,
                    claim,
                    dm_ok.evaluate(snap, proposal_id="dm-flow-ok"),
                    now_ns=t_dec + 5,
                )
                self.assertFalse(p2_out.proposal.fallback_used)

                dm_timeout = DecisionModelPlaneExecutor(
                    model_id="regime-scorer-slow",
                    model_revision="dm-v1",
                    expected_artifact_sha256=weights_sha,
                    feature_schema_version="hyb-v1",
                    artifact_bytes=weights,
                    timeout_seconds=0.05,
                    fallback_executor=safe_fallback,
                    scorer_fn=lambda _s, _a: (time.sleep(2.0), None)[1],  # type: ignore[return-value]
                )
                fb_prop = dm_timeout.evaluate(snap, proposal_id="dm-flow-fb")
                self.assertTrue(fb_prop.fallback_used)
                self.assertEqual(fb_prop.fallback_reason, "model_timeout")
                p2_fb_out = gate.commit(w, claim, fb_prop, now_ns=t_dec + 10)
                self.assertEqual(p2_fb_out.decision.action_kind, "hold")

                # 5. Plane 3 (LLM Meta-Reasoning Plane): offline eval required -> verified patch admitted!
                meta_plane = LlmMetaPlaneExecutor(
                    parameter_bounds={
                        "min_liquidity_ratio": ParameterBound(
                            min_value=0.5, max_value=0.99, max_abs_delta=0.1
                        )
                    },
                    min_eval_samples=40,
                    min_holdout_margin=0.02,
                    max_drawdown_bps=300,
                )
                patch = PolicyPatchProposal(
                    proposal_id="meta-patch-1",
                    policy_id="policy.liquidity_dispatch",
                    base_revision="rev-1",
                    target_revision="rev-2",
                    parameter_deltas={"min_liquidity_ratio": 0.03},
                    rationale="Raise liquidity buffer after offline regime study",
                    produced_by_run_id=run.run_id,
                    evidence=(run_ev,),
                    token_usage=TokenUsage(900, 220),
                )
                with self.assertRaises(HybridOfflineEvalRequired):
                    meta_plane.admit_policy_patch(
                        patch,
                        current_parameters={"min_liquidity_ratio": 0.85},
                        verdict=None,
                        decision_at_ns=t_dec + 20,
                    )
                verdict = OfflineEvalVerdict(
                    verdict_id="verdict-flow-1",
                    policy_id="policy.liquidity_dispatch",
                    target_revision="rev-2",
                    proposal_digest=patch.canonical_digest(),
                    suite_revision="eval-suite-v3",
                    passed=True,
                    sample_count=80,
                    holdout_margin=0.04,
                    max_drawdown_bps=120,
                    evaluated_at_ns=t_dec + 15,
                    evaluator_id="offline-evaluator",
                )
                p3_prop = meta_plane.admit_policy_patch(
                    patch,
                    current_parameters={"min_liquidity_ratio": 0.85},
                    verdict=verdict,
                    decision_at_ns=t_dec + 20,
                )
                p3_out = gate.commit(w, claim, p3_prop, now_ns=t_dec + 20)
                self.assertEqual(p3_out.decision.action_kind, "approve")

                # 6. Active company restriction blocks risk proposals from ALL planes!
                record_system_restriction(
                    w,
                    restriction_id="rest-hyb-pause-1",
                    cause_category="risk_pause",
                    scope_ref="company",
                    reason="Board risk pause",
                    actor_id="owner-board",
                    now_ns=t_dec + 30,
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(
                        w,
                        claim,
                        code_plane.evaluate(snap, proposal_id="blocked-code-1"),
                        now_ns=t_dec + 35,
                    )
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(
                        w,
                        claim,
                        dm_ok.evaluate(snap, proposal_id="blocked-dm-1"),
                        now_ns=t_dec + 36,
                    )
                # Defensive hold (no connector effect) still succeeds under pause!
                defensive_out = gate.commit(
                    w,
                    claim,
                    safe_fallback.evaluate(snap, proposal_id="safe-hold-under-pause"),
                    now_ns=t_dec + 40,
                )
                self.assertEqual(defensive_out.decision.action_kind, "hold")
                # Zero new outbound dispatches occurred after Board pause
                self.assertEqual(len(sink_srv.received), 1)
                self.assertEqual(CONNECTOR_KIND_LLM_INFERENCE, "llm_inference")
                self.assertEqual(LLM_COMPLETE_OP.name, "complete")

            # Verify durable SQLite records & zero raw secrets in company.sqlite
            self.assertNotIn(_CANARY_EXEC_TOKEN.encode("utf-8"), store.read_bytes())
            with scoped_reader(store) as db:
                dec_count = db.execute(
                    "SELECT COUNT(*) FROM work_decision WHERE company_id = ?", (cid,)
                ).fetchone()[0]
                self.assertEqual(dec_count, 5)
                fb_events = db.execute(
                    "SELECT COUNT(*) FROM audit_event WHERE company_id = ? AND event_type = 'hybrid.fallback_triggered'",
                    (cid,),
                ).fetchone()[0]
                self.assertEqual(fb_events, 1)
                denied_events = db.execute(
                    "SELECT COUNT(*) FROM audit_event WHERE company_id = ? AND event_type = 'hybrid.proposal_denied'",
                    (cid,),
                ).fetchone()[0]
                self.assertEqual(denied_events, 2)
                ext_confirmed = db.execute(
                    "SELECT COUNT(*) FROM external_actions WHERE company_id = ? AND status = 'confirmed'",
                    (cid,),
                ).fetchone()[0]
                self.assertEqual(ext_confirmed, 1)


if __name__ == "__main__":
    unittest.main()
