"""Unit tests for PR-7: UdsToolBrokerServer, generalized AdapterManifest/Registry, and 4-Plane Hybrid Execution."""

from __future__ import annotations

import json
import os
import socket
import stat
import time
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.agent_runtime import (
    PROVIDER_ANTHROPIC,
    PROVIDER_CODE_PLANE,
    PROVIDER_CODEX_CLI,
    PROVIDER_DECISION_MODEL,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    AdapterManifest,
    AgentAdapterRegistry,
    AgentProfileBinding,
    AgentRuntimeRefused,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
    UdsToolBrokerServer,
    hydrate_shift_context,
)
from sourdough.company import FoundingTerms, found
from sourdough.connectors import (
    ConnectorAdapterRegistry,
    ConnectorManifest,
    ConnectorProxy,
    ConnectorRequest,
    ConnectorResponse,
    OperationSpec,
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
    EffectAdmissionDecision,
    FeaturePoint,
    FeatureSnapshot,
    HybridAntiLookaheadViolation,
    HybridControlGateDenied,
    HybridExecutionRefused,
    HybridOfflineEvalRequired,
    LlmMetaPlaneExecutor,
    ModelScore,
    OfflineEvalVerdict,
    ParameterBound,
    PlaneExecutionProposal,
    PolicyPatchProposal,
)
from sourdough.runtime.kernel import (
    DecisionEvidence,
    claim_work_run,
    create_work_item,
    work_run_evidence_revision,
)
from sourdough.runtime.shift.flow_guard import ProvenanceIndex, ShiftFlowGuard
from sourdough.secret_vault import InMemorySecretVault, MaskedSecret
from sourdough.storage.db import scoped_reader, write_uow
from sourdough.writer_lease import hold_company_writer


def _send_uds_json(sock_path: Path, payload: bytes) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5.0)
        conn.connect(str(sock_path))
        conn.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8"))


class _StubEffectConnector:
    max_cost_per_call_minor_units: int = 0

    def __init__(self) -> None:
        self.calls: list[ConnectorRequest] = []
        self._manifest = ConnectorManifest(
            connector_kind="hybrid_target",
            adapter_revision="hybrid-target-v1",
            operations=(
                OperationSpec(
                    name="execute_intent",
                    mode="effectful",
                    request_schema={
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "qty": {"type": "integer"},
                        },
                        "required": ["action", "qty"],
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
        del secret, timeout_seconds
        self.calls.append(request)
        return ConnectorResponse(
            status="observed",
            payload={"accepted": True, "request_id": request.request_id},
            source_ref=f"ext:{request.request_id}",
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
            payload={"accepted": True, "request_id": request.request_id},
            source_ref=f"ext:{request.request_id}",
            cost_minor_units=0,
        )


def _setup_company_and_run(root: Path):
    store = root / "co.sqlite"
    leases = root / "leases"
    founding = found(
        store,
        FoundingTerms("Hybrid Co", "charter-v1", "manage-company", "profile-hyb-1"),
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
                source_id="src-hyb-1",
                now_ns=now,
            )
            run = claim_work_run(
                db,
                company_id=founding.company_id,
                work_item_id=item.work_item_id,
                lease_owner="worker-hyb",
                lease_ttl_ns=3_600_000_000_000,
                now_ns=now + 1,
            )
            run_row = db.execute(
                "SELECT created_at_ns FROM work_runs WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
            assert run_row is not None
            run_created_ns = int(run_row[0])
    run_rev = work_run_evidence_revision(
        run.run_id,
        company_id=founding.company_id,
        work_item_id=item.work_item_id,
        agent_id=founding.founder_id,
        duty_kind="work.assessment_review",
        fencing_token=run.fencing_token,
        created_at_ns=run_created_ns,
    )
    evidence = DecisionEvidence(
        kind="work_run",
        resource_id=run.run_id,
        revision=run_rev,
        observed_at_ns=run_created_ns,
        available_at_ns=run_created_ns,
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
    return store, leases, founding, claim, evidence, now + 100


class UdsBrokerAndRegistryTests(unittest.TestCase):
    def test_standalone_uds_broker_permissions_mediation_and_frame_limits(self) -> None:
        with TemporaryDirectory(prefix="sd-uds-") as tmp:
            os.chmod(tmp, 0o700)
            sock_path = Path(tmp) / "broker.sock"
            seen: list[dict[str, Any]] = []
            broker = OuterToolBroker(
                company_id="co-1",
                agent_id="agent-1",
                run_id="run-1",
                fencing_token=3,
                handlers={
                    "ping": lambda args: seen.append(args) or {"pong": args["msg"]}
                },
            )
            with UdsToolBrokerServer(
                sock_path, broker, max_request_bytes=256, unlink_on_stop=True
            ) as srv:
                mode = stat.S_IMODE(sock_path.stat().st_mode)
                self.assertEqual(mode, 0o600)
                self.assertEqual(srv.socket_path, sock_path)

                ok = _send_uds_json(
                    sock_path,
                    (
                        json.dumps(
                            {
                                "call_id": "c-1",
                                "tool": "ping",
                                "arguments": {"msg": "hello"},
                            }
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
                self.assertTrue(ok["ok"])
                self.assertEqual(ok["result"], {"pong": "hello"})
                self.assertEqual(len(seen), 1)

                # Spoofed identity key is refused fail-closed
                spoof = _send_uds_json(
                    sock_path,
                    (
                        json.dumps(
                            {
                                "call_id": "c-2",
                                "tool": "ping",
                                "arguments": {"msg": "x", "agent_id": "other"},
                            }
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
                self.assertFalse(spoof["ok"])
                self.assertEqual(spoof["error_type"], "AgentRuntimeRefused")
                self.assertIsNotNone(srv.last_refusal)

                # Oversized frame > max_request_bytes is refused without invoking broker
                oversized = _send_uds_json(
                    sock_path,
                    (
                        json.dumps(
                            {
                                "call_id": "c-3",
                                "tool": "ping",
                                "arguments": {"msg": "A" * 512},
                            }
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
                self.assertFalse(oversized["ok"])
                self.assertIn("exceeded maximum size", oversized["error"])
                self.assertEqual(len(seen), 1)

            # unlink_on_stop=True removes the socket cleanly
            self.assertFalse(sock_path.exists())

            # Symlink target is refused
            link_path = Path(tmp) / "link.sock"
            link_path.symlink_to(Path(tmp) / "real.sock")
            with self.assertRaises(AgentRuntimeRefused):
                UdsToolBrokerServer(link_path, broker)

    def test_generalized_manifest_registry_and_profile_id_compatibility(self) -> None:
        # 1. Existing persisted profile_id() formula MUST remain bit-for-bit identical!
        legacy_openai = AgentProfileBinding(
            provider=PROVIDER_OPENAI,
            model="gpt-5-mini",
            revision="2026-11",
            credential_handle_ref="envref:OPENAI_KEY",
        )
        expected_payload = json.dumps(
            {
                "provider": "openai",
                "model": "gpt-5-mini",
                "revision": "2026-11",
                "sandbox_backend": "local-macos",
                "credential_handle_ref": "envref:OPENAI_KEY",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_id = (
            f"adapter-v1:openai:gpt-5-mini:{sha256(expected_payload).hexdigest()[:12]}"
        )
        self.assertEqual(legacy_openai.profile_id(), expected_id)

        # Remote LLM providers still require opaque credential_handle_ref
        for remote in (PROVIDER_OPENAI, PROVIDER_ANTHROPIC, PROVIDER_GEMINI):
            with self.assertRaises(AgentRuntimeRefused):
                AgentProfileBinding(provider=remote, model="m-1", revision="r-1")

        # Invalid AdapterManifest fields are rejected
        with self.assertRaises(AgentRuntimeRefused):
            AdapterManifest(
                provider="INVALID UPPER",
                adapter_revision="v1",
                transport="in_process",
                supports_tool_mediation=True,
                supports_usage_accounting=True,
                supports_sandbox_desk=False,
            )
        with self.assertRaises(AgentRuntimeRefused):
            AdapterManifest(
                provider="code_plane",
                adapter_revision="v1",
                transport="in_process",
                supports_tool_mediation=True,
                supports_usage_accounting=True,
                supports_sandbox_desk=False,
                execution_plane="unknown_plane",  # type: ignore[arg-type]
            )
        with self.assertRaises(AgentRuntimeRefused):
            AdapterManifest(
                provider="code_plane",
                adapter_revision="v1",
                transport="in_process",
                supports_tool_mediation=True,
                supports_usage_accounting=True,
                supports_sandbox_desk=False,
                default_timeout_seconds=0.0,
            )

        # 2. Multi-plane registration & execution with zero silent fallback
        registry = AgentAdapterRegistry()
        registry.register(
            CodePlaneRuntimeAdapter(
                handler=lambda prof, req, brk: (
                    f"code:{prof.model}:{brk.invoke('c1', 'echo', {'v': req.instruction})['v']}"
                )
            )
        )
        registry.register(
            DecisionModelRuntimeAdapter(
                handler=lambda prof, req, brk: f"dm:{prof.model}:{req.instruction}"
            )
        )

        # Custom HTTPS adapter enforces credential_handle_ref even for non-builtin slugs
        class _CustomHttpsAdapter:
            manifest = AdapterManifest(
                provider="custom_https_llm",
                adapter_revision="custom-v1",
                transport="https_json",
                supports_tool_mediation=False,
                supports_usage_accounting=True,
                supports_sandbox_desk=False,
                execution_plane="llm",
                default_timeout_seconds=30.0,
            )

            def run_shift(
                self,
                profile: AgentProfileBinding,
                request: ShiftRequest,
                broker: OuterToolBroker,
            ) -> ShiftResult:
                del broker
                return ShiftResult(
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    output_text="custom-ok",
                    usage=TokenUsage(1, 1),
                    tool_calls_executed=0,
                )

        registry.register(_CustomHttpsAdapter())
        self.assertEqual(
            [a.manifest.provider for a in registry.adapters_for_plane("code")],
            [PROVIDER_CODE_PLANE],
        )
        self.assertEqual(
            [
                a.manifest.provider
                for a in registry.adapters_for_plane("decision_model")
            ],
            [PROVIDER_DECISION_MODEL],
        )
        self.assertEqual(
            [a.manifest.provider for a in registry.adapters_for_plane("llm")],
            ["custom_https_llm"],
        )

        code_profile = AgentProfileBinding(
            provider=PROVIDER_CODE_PLANE,
            model="rebalance-rule-v1",
            revision="rev-1",
        )
        ctx = hydrate_shift_context(
            company_id="co-1",
            agent_id="a-1",
            run_id="r-1",
            fencing_token=1,
            grant_scope="manage-company",
            mandate_summary="Execute deterministic rule",
        )
        req = ShiftRequest(
            company_id="co-1",
            agent_id="a-1",
            run_id="r-1",
            turn_id="t-1",
            fencing_token=1,
            instruction="check-balance",
            context=ctx,
            tools=(),
            budget_ceiling_minor_units=0,
        )
        broker = OuterToolBroker(
            company_id="co-1",
            agent_id="a-1",
            run_id="r-1",
            fencing_token=1,
            handlers={"echo": lambda a: {"v": a["v"]}},
        )
        res = registry.execute_shift(
            persisted_profile_id=code_profile.profile_id(),
            profile=code_profile,
            request=req,
            broker=broker,
        )
        self.assertEqual(res.usage, TokenUsage(0, 0))
        self.assertEqual(res.output_text, "code:rebalance-rule-v1:check-balance")
        self.assertEqual(res.tool_calls_executed, 1)

        # Custom HTTPS provider without credential_handle_ref fails closed
        custom_no_cred = AgentProfileBinding(
            provider="custom_https_llm", model="m-1", revision="r-1"
        )
        with self.assertRaises(AgentRuntimeRefused):
            registry.execute_shift(
                persisted_profile_id=custom_no_cred.profile_id(),
                profile=custom_no_cred,
                request=req,
                broker=broker,
            )

        # Unregistered provider fails closed (zero silent fallback)
        missing_prof = AgentProfileBinding(
            provider=PROVIDER_CODEX_CLI, model="codex-1", revision="r-1"
        )
        with self.assertRaises(AgentRuntimeRefused):
            registry.execute_shift(
                persisted_profile_id=missing_prof.profile_id(),
                profile=missing_prof,
                request=req,
                broker=broker,
            )


class HybridExecutionPlaneTests(unittest.TestCase):
    def test_code_plane_determinism_and_anti_lookahead(self) -> None:
        now = 1_700_000_000_000_000_000
        # Future feature available_at_ns > decision_at_ns MUST raise HybridAntiLookaheadViolation
        with self.assertRaises(HybridAntiLookaheadViolation):
            FeatureSnapshot(
                snapshot_id="snap-bad",
                schema_version="feat-v1",
                decision_at_ns=now,
                features=(
                    FeaturePoint(
                        name="spread_bps",
                        value=12.5,
                        observed_at_ns=now - 10,
                        available_at_ns=now + 1,
                    ),
                ),
            )

        snap = FeatureSnapshot(
            snapshot_id="snap-1",
            schema_version="feat-v1",
            decision_at_ns=now,
            features=(
                FeaturePoint(
                    name="spread_bps",
                    value=12.5,
                    observed_at_ns=now - 20,
                    available_at_ns=now - 10,
                ),
            ),
        )
        code_exec = CodePlaneExecutor(
            rule_id="rule.spread_guard",
            code_revision="git-abc123",
            rule_fn=lambda s, p: CodeRuleDecision(
                action_kind="proceed"
                if float(s.feature_map()["spread_bps"]) <= float(p["max_spread"])
                else "hold",
                rationale="Deterministic spread threshold check",
                parameters={"observed_spread": s.feature_map()["spread_bps"]},
                proposed_notional_minor_units=500,
            ),
            default_parameters={"max_spread": 20.0},
        )
        p1 = code_exec.evaluate(snap)
        p2 = code_exec.evaluate(snap)
        self.assertEqual(p1.plane, "code")
        self.assertEqual(p1.action_kind, "proceed")
        self.assertEqual(p1.token_usage, TokenUsage(0, 0))
        self.assertEqual(p1.reproducibility_digest, p2.reproducibility_digest)
        self.assertEqual(p1.proposal_id, p2.proposal_id)

    def test_decision_model_plane_scoring_and_explicit_fallbacks(self) -> None:
        now = 1_700_000_000_000_000_000
        artifact = b"pinned-linear-model-weights-v1"
        art_sha = sha256(artifact).hexdigest()

        fallback_code = CodePlaneExecutor(
            rule_id="rule.safe_fallback",
            code_revision="fb-v1",
            rule_fn=lambda s, _p: CodeRuleDecision(
                action_kind="hold",
                rationale="Fallback rule holds position",
                parameters={"fallback_rule": True},
            ),
        )
        snap = FeatureSnapshot(
            snapshot_id="snap-dm-1",
            schema_version="feat-v1",
            decision_at_ns=now,
            features=(
                FeaturePoint(
                    name="momentum",
                    value=0.82,
                    observed_at_ns=now - 10,
                    available_at_ns=now - 5,
                ),
            ),
        )

        # 1. Normal pinned scoring succeeds at $0 LLM cost
        dm = DecisionModelPlaneExecutor(
            model_id="regime-classifier",
            model_revision="v2.1",
            expected_artifact_sha256=art_sha,
            feature_schema_version="feat-v1",
            artifact_bytes=artifact,
            min_confidence=0.7,
            timeout_seconds=1.0,
            fallback_executor=fallback_code,
            scorer_fn=lambda s, _art: ModelScore(
                score=0.85,
                confidence=0.91,
                regime_label="trend_up",
                action_kind="proceed",
                rationale="High-confidence regime signal",
                proposed_notional_minor_units=1000,
            ),
        )
        ok_prop = dm.evaluate(snap)
        self.assertEqual(ok_prop.plane, "decision_model")
        self.assertFalse(ok_prop.fallback_used)
        self.assertEqual(ok_prop.token_usage, TokenUsage(0, 0))
        self.assertEqual(ok_prop.action_kind, "proceed")
        self.assertEqual(ok_prop.parameters["regime_label"], "trend_up")

        # 2. Digest mismatch -> explicit deterministic fallback
        bad_digest_dm = DecisionModelPlaneExecutor(
            model_id="regime-classifier",
            model_revision="v2.1",
            expected_artifact_sha256=sha256(b"other-weights").hexdigest(),
            feature_schema_version="feat-v1",
            artifact_bytes=artifact,
            fallback_executor=fallback_code,
            scorer_fn=lambda _s, _a: (_ for _ in ()).throw(
                AssertionError("must not run")
            ),
        )
        fb_dig = bad_digest_dm.evaluate(snap)
        self.assertTrue(fb_dig.fallback_used)
        self.assertEqual(fb_dig.fallback_reason, "artifact_digest_mismatch")
        self.assertEqual(fb_dig.action_kind, "hold")

        # 3. Schema mismatch -> explicit fallback
        wrong_schema_snap = FeatureSnapshot(
            snapshot_id="snap-dm-2",
            schema_version="feat-v99",
            decision_at_ns=now,
            features=snap.features,
        )
        fb_schema = dm.evaluate(wrong_schema_snap)
        self.assertTrue(fb_schema.fallback_used)
        self.assertEqual(fb_schema.fallback_reason, "feature_schema_mismatch")

        # 4. Timeout -> explicit fallback without blocking caller
        slow_dm = DecisionModelPlaneExecutor(
            model_id="slow-model",
            model_revision="v1",
            expected_artifact_sha256=art_sha,
            feature_schema_version="feat-v1",
            artifact_bytes=artifact,
            timeout_seconds=0.05,
            fallback_executor=fallback_code,
            scorer_fn=lambda _s, _a: (time.sleep(2.0), None)[1],  # type: ignore[return-value]
        )
        t0 = time.monotonic()
        fb_to = slow_dm.evaluate(snap)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5)
        self.assertTrue(fb_to.fallback_used)
        self.assertEqual(fb_to.fallback_reason, "model_timeout")

        # 5. NaN output -> explicit fallback
        nan_dm = DecisionModelPlaneExecutor(
            model_id="nan-model",
            model_revision="v1",
            expected_artifact_sha256=art_sha,
            feature_schema_version="feat-v1",
            artifact_bytes=artifact,
            fallback_executor=fallback_code,
            scorer_fn=lambda _s, _a: ModelScore(
                score=float("nan"),
                confidence=0.9,
                regime_label="bad",
                action_kind="proceed",
                rationale="NaN",
            ),
        )
        fb_nan = nan_dm.evaluate(snap)
        self.assertTrue(fb_nan.fallback_used)
        self.assertEqual(fb_nan.fallback_reason, "invalid_model_output")

        # 6. Confidence below threshold -> explicit fallback
        low_conf_dm = DecisionModelPlaneExecutor(
            model_id="lowconf-model",
            model_revision="v1",
            expected_artifact_sha256=art_sha,
            feature_schema_version="feat-v1",
            artifact_bytes=artifact,
            min_confidence=0.8,
            fallback_executor=fallback_code,
            scorer_fn=lambda _s, _a: ModelScore(
                score=0.5,
                confidence=0.45,
                regime_label="chop",
                action_kind="proceed",
                rationale="Uncertain",
            ),
        )
        fb_conf = low_conf_dm.evaluate(snap)
        self.assertTrue(fb_conf.fallback_used)
        self.assertEqual(fb_conf.fallback_reason, "confidence_below_threshold")

    def test_llm_meta_plane_offline_eval_gating_and_bounds(self) -> None:
        now = 1_700_000_000_000_000_000
        meta = LlmMetaPlaneExecutor(
            parameter_bounds={
                "max_spread_bps": ParameterBound(
                    min_value=5.0, max_value=50.0, max_abs_delta=10.0
                ),
            },
            min_eval_samples=50,
            min_holdout_margin=0.02,
            max_drawdown_bps=500,
        )
        patch = PolicyPatchProposal(
            proposal_id="patch-1",
            policy_id="policy.liquidity",
            base_revision="rev-1",
            target_revision="rev-2",
            base_parameters={"max_spread_bps": 25.0},
            parameter_deltas={"max_spread_bps": -5.0},
            rationale="Tighten spread threshold based on offline replay",
            produced_by_run_id="run-meta-1",
            token_usage=TokenUsage(1200, 350),
        )
        current = {"max_spread_bps": 25.0}

        # Missing verdict -> refused
        with self.assertRaises(HybridOfflineEvalRequired):
            meta.admit_policy_patch(
                patch, current_parameters=current, verdict=None, decision_at_ns=now
            )

        # Failed verdict -> refused
        failed_v = OfflineEvalVerdict(
            verdict_id="ev-fail",
            policy_id="policy.liquidity",
            target_revision="rev-2",
            proposal_digest=patch.canonical_digest(),
            suite_revision="suite-v1",
            passed=False,
            sample_count=100,
            holdout_margin=0.05,
            max_drawdown_bps=120,
            evaluated_at_ns=now - 10,
            evaluator_id="eval-worker-1",
        )
        with self.assertRaises(HybridOfflineEvalRequired):
            meta.admit_policy_patch(
                patch, current_parameters=current, verdict=failed_v, decision_at_ns=now
            )

        # Mismatched digest -> refused
        wrong_dig_v = OfflineEvalVerdict(
            verdict_id="ev-wrong",
            policy_id="policy.liquidity",
            target_revision="rev-2",
            proposal_digest=sha256(b"other-patch").hexdigest(),
            suite_revision="suite-v1",
            passed=True,
            sample_count=100,
            holdout_margin=0.05,
            max_drawdown_bps=120,
            evaluated_at_ns=now - 10,
            evaluator_id="eval-worker-1",
        )
        with self.assertRaises(HybridOfflineEvalRequired):
            meta.admit_policy_patch(
                patch,
                current_parameters=current,
                verdict=wrong_dig_v,
                decision_at_ns=now,
            )

        # Lookahead verdict -> HybridAntiLookaheadViolation
        future_v = OfflineEvalVerdict(
            verdict_id="ev-future",
            policy_id="policy.liquidity",
            target_revision="rev-2",
            proposal_digest=patch.canonical_digest(),
            suite_revision="suite-v1",
            passed=True,
            sample_count=100,
            holdout_margin=0.05,
            max_drawdown_bps=120,
            evaluated_at_ns=now + 100,
            evaluator_id="eval-worker-1",
        )
        with self.assertRaises(HybridAntiLookaheadViolation):
            meta.admit_policy_patch(
                patch, current_parameters=current, verdict=future_v, decision_at_ns=now
            )

        # Out-of-bounds delta -> refused even with passing verdict
        huge_patch = PolicyPatchProposal(
            proposal_id="patch-huge",
            policy_id="policy.liquidity",
            base_revision="rev-1",
            target_revision="rev-2",
            base_parameters={"max_spread_bps": 25.0},
            parameter_deltas={"max_spread_bps": 25.0},
            rationale="Too large delta",
            produced_by_run_id="run-meta-1",
        )
        huge_v = OfflineEvalVerdict(
            verdict_id="ev-huge",
            policy_id="policy.liquidity",
            target_revision="rev-2",
            proposal_digest=huge_patch.canonical_digest(),
            suite_revision="suite-v1",
            passed=True,
            sample_count=100,
            holdout_margin=0.05,
            max_drawdown_bps=120,
            evaluated_at_ns=now - 10,
            evaluator_id="eval-worker-1",
        )
        with self.assertRaises(HybridExecutionRefused):
            meta.admit_policy_patch(
                huge_patch,
                current_parameters=current,
                verdict=huge_v,
                decision_at_ns=now,
            )

        # Valid verdict -> admitted!
        good_v = OfflineEvalVerdict(
            verdict_id="ev-ok",
            policy_id="policy.liquidity",
            target_revision="rev-2",
            proposal_digest=patch.canonical_digest(),
            suite_revision="suite-v1",
            passed=True,
            sample_count=120,
            holdout_margin=0.06,
            max_drawdown_bps=180,
            evaluated_at_ns=now - 10,
            evaluator_id="eval-worker-1",
        )
        admitted = meta.admit_policy_patch(
            patch, current_parameters=current, verdict=good_v, decision_at_ns=now
        )
        self.assertEqual(admitted.plane, "llm")
        self.assertEqual(admitted.action_kind, "approve")
        self.assertEqual(
            admitted.parameters["updated_parameters"], {"max_spread_bps": 20.0}
        )
        self.assertEqual(admitted.eval_verdict_digest, good_v.canonical_digest())

    def test_control_plane_gate_enforces_fencing_restrictions_taint_tampering_and_connector(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-hyb-gate-") as tmp:
            store, leases, founding, claim, evidence, now = _setup_company_and_run(
                Path(tmp)
            )
            stub = _StubEffectConnector()
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([stub]),
                InMemorySecretVault({"envref:HYB_TEST_KEY": "test-hyb-secret-1111"}),
                loopback_only=True,
            )
            gate = DeterministicControlPlaneGate(
                connector_proxy=proxy,
                effect_authorizers={
                    "hybrid_target": lambda _db, _cid, _aid, _b, _op, payload, _p: (
                        EffectAdmissionDecision(
                            issued_by_service="unit.hybrid_target_authorizer",
                            authoritative_notional_minor_units=int(
                                payload.get("qty", 0)
                            )
                            * 100,
                        )
                    )
                },
                max_notional_minor_units=5_000,
                min_llm_eval_samples=20,
                min_llm_holdout_margin=0.01,
            )

            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="hyb-exec-1",
                    connector_kind="hybrid_target",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9120",
                    allowed_hosts=("127.0.0.1",),
                    operations=stub.manifest.operations,
                    credential_handle_ref="envref:HYB_TEST_KEY",
                    actor_id="board-1",
                    allow_loopback=True,
                    now_ns=now,
                )

                # 1. Missing connector grant is denied BEFORE recording work_decision
                prop_ungranted = PlaneExecutionProposal(
                    proposal_id="p-ungranted",
                    plane="code",
                    engine_id="rule.exec",
                    engine_revision="r1",
                    action_kind="proceed",
                    rationale="Attempt without connector grant",
                    decision_at_ns=now + 10,
                    evidence=(evidence,),
                    proposed_notional_minor_units=1_000,
                    connector_effect=ConnectorEffectIntent(
                        binding_id=binding.binding_id,
                        operation="execute_intent",
                        request_id="req-ungranted-1",
                        payload={"action": "rebalance", "qty": 1},
                        expected_environment="external_test",
                    ),
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(w, claim, prop_ungranted, now_ns=now + 10)
                self.assertEqual(len(stub.calls), 0)

                grant_connector_access(
                    w,
                    company_id=founding.company_id,
                    agent_id=founding.founder_id,
                    standing_grant_id=founding.grant_id,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    allowed_operations=("execute_intent",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 12,
                )

                # 2. Tainted ShiftFlowGuard blocks connector_effect before decision commit
                prov = ProvenanceIndex(
                    store, founding.company_id, founding.founder_id, claim.duty_id
                )
                tainted = ShiftFlowGuard(
                    agent_id=founding.founder_id,
                    provenance=prov,
                    specs_by_name={},
                    public_offloaded_paths=set(),
                    read_private_desk=True,
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(
                        w,
                        claim,
                        prop_ungranted,
                        flow_guard=tainted,
                        now_ns=now + 15,
                    )
                self.assertEqual(len(stub.calls), 0)

                # 3. Notional cap blocks oversized proposal
                prop_big = PlaneExecutionProposal(
                    proposal_id="p-big",
                    plane="code",
                    engine_id="rule.exec",
                    engine_revision="r1",
                    action_kind="proceed",
                    rationale="Oversized notional",
                    decision_at_ns=now + 16,
                    evidence=(evidence,),
                    proposed_notional_minor_units=9_999,
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(w, claim, prop_big, now_ns=now + 16)

                # 4. Tampered LLM proposal (forged deltas under real verdict) is denied!
                real_patch = PolicyPatchProposal(
                    proposal_id="p-tamper",
                    policy_id="policy.risk",
                    base_revision="v1",
                    target_revision="v2",
                    base_parameters={"limit": 10.0},
                    parameter_deltas={"limit": 1.0},
                    rationale="Small change",
                    produced_by_run_id=claim.run_id,
                )
                real_verdict = OfflineEvalVerdict(
                    verdict_id="ev-real",
                    policy_id="policy.risk",
                    target_revision="v2",
                    proposal_digest=real_patch.canonical_digest(),
                    suite_revision="s1",
                    passed=True,
                    sample_count=50,
                    holdout_margin=0.05,
                    max_drawdown_bps=50,
                    evaluated_at_ns=now + 10,
                    evaluator_id="eval-1",
                )
                tampered_llm = PlaneExecutionProposal(
                    proposal_id="p-tamper",
                    plane="llm",
                    engine_id="policy.risk",
                    engine_revision="v2",
                    action_kind="approve",
                    rationale="Small change",
                    decision_at_ns=now + 18,
                    evidence=(evidence,),
                    parameters={
                        "base_revision": "v1",
                        "parameter_deltas": {"limit": 999999.0},
                    },
                    eval_verdict=real_verdict,
                    eval_verdict_digest=real_verdict.canonical_digest(),
                    policy_patch_digest=real_patch.canonical_digest(),
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(w, claim, tampered_llm, now_ns=now + 18)

                # 5. Valid Plane 1 proposal commits work_decision, issues admission, dispatches once & replays idempotently
                prop_ok = PlaneExecutionProposal(
                    proposal_id="p-ok-1",
                    plane="code",
                    engine_id="rule.exec",
                    engine_revision="r1",
                    action_kind="proceed",
                    rationale="Authorized deterministic dispatch",
                    decision_at_ns=now + 20,
                    evidence=(evidence,),
                    proposed_notional_minor_units=2_000,
                    connector_effect=ConnectorEffectIntent(
                        binding_id=binding.binding_id,
                        operation="execute_intent",
                        request_id="req-ok-1",
                        payload={"action": "rebalance", "qty": 2},
                        expected_environment="external_test",
                    ),
                )
                out1 = gate.commit(w, claim, prop_ok, now_ns=now + 20)
                self.assertIsNotNone(out1.admission_id)
                self.assertIsNotNone(out1.connector_response)
                assert out1.connector_response is not None
                self.assertEqual(out1.connector_response.status, "observed")
                self.assertEqual(len(stub.calls), 1)

                # Idempotent replay returns exact same work_decision & cached connector result without 2nd dispatch!
                out2 = gate.commit(w, claim, prop_ok, now_ns=now + 25)
                self.assertEqual(out2.decision.decision_id, out1.decision.decision_id)
                self.assertEqual(out2.admission_id, out1.admission_id)
                self.assertEqual(len(stub.calls), 1)

                # 6. Stale fencing token is denied!
                stale_claim = WorkRunClaim(
                    company_id=claim.company_id,
                    founder_id=claim.founder_id,
                    duty_id=claim.duty_id,
                    run_id=claim.run_id,
                    fencing_token=claim.fencing_token + 99,
                    grant_id=claim.grant_id,
                    grant_scope=claim.grant_scope,
                    purpose=claim.purpose,
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(
                        w,
                        stale_claim,
                        PlaneExecutionProposal(
                            proposal_id="p-stale",
                            plane="code",
                            engine_id="rule.exec",
                            engine_revision="r1",
                            action_kind="hold",
                            rationale="Stale run",
                            decision_at_ns=now + 30,
                            evidence=(evidence,),
                        ),
                        now_ns=now + 30,
                    )

            with scoped_reader(store) as db:
                denied_cnt = db.execute(
                    "SELECT COUNT(*) FROM audit_event WHERE company_id = ? AND event_type = 'hybrid.proposal_denied'",
                    (founding.company_id,),
                ).fetchone()[0]
                self.assertGreaterEqual(denied_cnt, 4)

    def test_proposal_replay_rejects_mutated_connector_intent_and_never_dispatches_twice(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-hyb-rep-") as tmp:
            store, leases, founding, claim, evidence, now = _setup_company_and_run(
                Path(tmp)
            )
            stub = _StubEffectConnector()
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([stub]),
                InMemorySecretVault({"envref:HYB_TEST_KEY": "test-hyb-rep-1111"}),
                loopback_only=True,
            )
            gate = DeterministicControlPlaneGate(
                connector_proxy=proxy,
                effect_authorizers={
                    "hybrid_target": lambda _db, _cid, _aid, _b, _op, payload, _p: (
                        EffectAdmissionDecision(
                            issued_by_service="unit.hybrid_target_authorizer",
                            authoritative_notional_minor_units=int(
                                payload.get("qty", 0)
                            )
                            * 100,
                        )
                    )
                },
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="hyb-exec-rep",
                    connector_kind="hybrid_target",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9121",
                    allowed_hosts=("127.0.0.1",),
                    operations=stub.manifest.operations,
                    credential_handle_ref="envref:HYB_TEST_KEY",
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
                    allowed_operations=("execute_intent",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 1,
                )
                p_first = PlaneExecutionProposal(
                    proposal_id="p-replay-1",
                    plane="code",
                    engine_id="rule.exec",
                    engine_revision="r1",
                    action_kind="proceed",
                    rationale="First intent",
                    decision_at_ns=now + 5,
                    evidence=(evidence,),
                    connector_effect=ConnectorEffectIntent(
                        binding_id=binding.binding_id,
                        operation="execute_intent",
                        request_id="req-A",
                        payload={"action": "rebalance", "qty": 1},
                        expected_environment="external_test",
                    ),
                )
                gate.commit(w, claim, p_first, now_ns=now + 5)
                self.assertEqual(len(stub.calls), 1)

                # Repeating the exact same proposal_id with mutated request_id & qty MUST be refused!
                p_mutated = PlaneExecutionProposal(
                    proposal_id="p-replay-1",
                    plane="code",
                    engine_id="rule.exec",
                    engine_revision="r1",
                    action_kind="proceed",
                    rationale="First intent",
                    decision_at_ns=now + 5,
                    evidence=(evidence,),
                    connector_effect=ConnectorEffectIntent(
                        binding_id=binding.binding_id,
                        operation="execute_intent",
                        request_id="req-B",
                        payload={"action": "rebalance", "qty": 99},
                        expected_environment="external_test",
                    ),
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(w, claim, p_mutated, now_ns=now + 6)
                self.assertEqual(len(stub.calls), 1)

    def test_canonical_company_all_restriction_and_extra_nonnumeric_llm_delta_denied(
        self,
    ) -> None:
        from sourdough.board_control import record_system_restriction

        with TemporaryDirectory(prefix="sd-hyb-rest-") as tmp:
            store, leases, founding, claim, evidence, now = _setup_company_and_run(
                Path(tmp)
            )
            stub = _StubEffectConnector()
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([stub]),
                InMemorySecretVault({"envref:HYB_TEST_KEY": "test-hyb-rest-1111"}),
                loopback_only=True,
            )
            gate = DeterministicControlPlaneGate(
                connector_proxy=proxy,
                min_llm_eval_samples=20,
                min_llm_holdout_margin=0.01,
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                binding = register_connector_binding(
                    w,
                    company_id=founding.company_id,
                    binding_id="hyb-exec-rest",
                    connector_kind="hybrid_target",
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9122",
                    allowed_hosts=("127.0.0.1",),
                    operations=stub.manifest.operations,
                    credential_handle_ref="envref:HYB_TEST_KEY",
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
                    allowed_operations=("execute_intent",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 1,
                )
                # 1. Extra non-numeric delta smuggled under valid verdict MUST be rejected!
                patch = PolicyPatchProposal(
                    proposal_id="p-smuggle",
                    policy_id="policy.risk",
                    base_revision="v1",
                    target_revision="v2",
                    base_parameters={"limit": 10.0},
                    parameter_deltas={"limit": 1.0},
                    rationale="Small change",
                    produced_by_run_id=claim.run_id,
                )
                verdict = OfflineEvalVerdict(
                    verdict_id="ev-smuggle",
                    policy_id="policy.risk",
                    target_revision="v2",
                    proposal_digest=patch.canonical_digest(),
                    suite_revision="s1",
                    passed=True,
                    sample_count=50,
                    holdout_margin=0.05,
                    max_drawdown_bps=50,
                    evaluated_at_ns=now + 2,
                    evaluator_id="eval-1",
                )
                smuggled_llm = PlaneExecutionProposal(
                    proposal_id="p-smuggle",
                    plane="llm",
                    engine_id="policy.risk",
                    engine_revision="v2",
                    action_kind="approve",
                    rationale="Small change",
                    decision_at_ns=now + 5,
                    evidence=(evidence,),
                    parameters={
                        "base_revision": "v1",
                        "parameter_deltas": {
                            "limit": 1.0,
                            "unreviewed": "arbitrary-value",
                        },
                    },
                    eval_verdict=verdict,
                    eval_verdict_digest=verdict.canonical_digest(),
                    policy_patch_digest=patch.canonical_digest(),
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(w, claim, smuggled_llm, now_ns=now + 5)

                # 2. Canonical 'company:all' restriction blocks 'proceed' with default target_scope_ref='company'
                record_system_restriction(
                    w,
                    restriction_id="rest-canon-all",
                    cause_category="operator_pause",
                    scope_ref="company:all",
                    reason="Canonical company-wide pause",
                    actor_id="board-1",
                    now_ns=now + 10,
                )
                blocked_prop = PlaneExecutionProposal(
                    proposal_id="p-blocked-all",
                    plane="code",
                    engine_id="rule.exec",
                    engine_revision="r1",
                    action_kind="proceed",
                    rationale="Should be blocked by company:all",
                    decision_at_ns=now + 12,
                    evidence=(evidence,),
                    connector_effect=ConnectorEffectIntent(
                        binding_id=binding.binding_id,
                        operation="execute_intent",
                        request_id="req-blocked-all",
                        payload={"action": "rebalance", "qty": 1},
                        expected_environment="external_test",
                    ),
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(w, claim, blocked_prop, now_ns=now + 12)
                self.assertEqual(len(stub.calls), 0)

    def test_code_plane_enforces_deadline_during_execution_and_fallback_degrades(
        self,
    ) -> None:
        now = 1_700_000_000_000_000_000
        snap = FeatureSnapshot(
            snapshot_id="snap-timeout",
            schema_version="feat-v1",
            decision_at_ns=now,
            features=(
                FeaturePoint(
                    name="x",
                    value=1.0,
                    observed_at_ns=now - 10,
                    available_at_ns=now - 5,
                ),
            ),
        )
        slow_code = CodePlaneExecutor(
            rule_id="rule.slow",
            code_revision="v1",
            timeout_seconds=0.05,
            rule_fn=lambda _s, _p: (
                time.sleep(2.0),
                CodeRuleDecision(action_kind="hold", rationale="late"),
            )[1],
        )
        t0 = time.monotonic()
        with self.assertRaises(HybridExecutionRefused):
            slow_code.evaluate(snap)
        self.assertLess(time.monotonic() - t0, 0.5)

        # Even if a DecisionModelPlaneExecutor's fallback code rule hangs, evaluation
        # degrades cleanly to deterministic 'hold' within the bounded deadline!
        art = b"weights-v1"
        dm = DecisionModelPlaneExecutor(
            model_id="dm-slow-fb",
            model_revision="v1",
            expected_artifact_sha256=sha256(art).hexdigest(),
            feature_schema_version="feat-v1",
            artifact_bytes=art,
            timeout_seconds=0.05,
            fallback_executor=slow_code,
            scorer_fn=lambda _s, _a: (time.sleep(2.0), None)[1],  # type: ignore[return-value]
        )
        t1 = time.monotonic()
        fb_prop = dm.evaluate(snap)
        self.assertLess(time.monotonic() - t1, 0.6)
        self.assertTrue(fb_prop.fallback_used)
        self.assertEqual(fb_prop.fallback_reason, "model_timeout:fallback_failed")
        self.assertEqual(fb_prop.action_kind, "hold")

    def test_non_llm_providers_settle_zero_cost_without_key_error(self) -> None:
        from sourdough.agent_runtime import (
            resolve_model_pricing,
            settle_shift_cost_to_ledger,
        )

        with TemporaryDirectory(prefix="sd-hyb-cost-") as tmp:
            store, leases, founding, _, _, _ = _setup_company_and_run(Path(tmp))
            profiles = (
                AgentProfileBinding(
                    provider=PROVIDER_CODE_PLANE, model="rule-1", revision="v1"
                ),
                AgentProfileBinding(
                    provider=PROVIDER_DECISION_MODEL, model="scorer-1", revision="v1"
                ),
                AgentProfileBinding(
                    provider="custom_zero_cost", model="worker-1", revision="v1"
                ),
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                for idx, prof in enumerate(profiles):
                    settled = settle_shift_cost_to_ledger(
                        w,
                        profile=prof,
                        run_id=f"run-zero-{idx}",
                        turn_id="t-1",
                        usage=TokenUsage(0, 0),
                        budget_ceiling_minor_units=100,
                    )
                    self.assertEqual(settled.cost_minor_units, 0)
                    self.assertEqual(settled.released_unused_minor_units, 100)
                    self.assertIsNone(settled.journal_entry_id)

            # Custom unknown provider with nonzero tokens still fails closed!
            with self.assertRaises(AgentRuntimeRefused):
                resolve_model_pricing("custom_unpriced", usage=TokenUsage(100, 50))

    def test_effectful_trading_venue_admission_requires_domain_authorizer_and_honors_authoritative_notional(
        self,
    ) -> None:
        from decimal import Decimal
        from sourdough.trading.connector import (
            CONNECTOR_KIND_TRADING_VENUE,
            TradingVenueConnectorAdapter,
        )
        from sourdough.trading.external_venue import (
            ExternalOrderRequest,
            ExternalOrderStatus,
            VenueCapabilities,
        )

        submitted_orders: list[ExternalOrderRequest] = []

        class _RecordingVenue:
            endpoint_url = "http://127.0.0.1:9130"
            credential_handle_ref = "envref:BINANCE_TESTNET"
            blocks_redirects = True
            capabilities = VenueCapabilities(
                adapter_revision="rec-v1",
                venue_name="rec_venue",
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
                submitted_orders.append(order)
                return ExternalOrderStatus(
                    venue_order_id=f"vo-{len(submitted_orders)}",
                    client_order_id=order.action_id,
                    symbol=order.symbol,
                    side=order.side,
                    status="filled",
                    original_quantity=order.quantity,
                    filled_quantity=order.quantity,
                    remaining_quantity=Decimal("0"),
                    avg_price=order.limit_price,
                    update_time_ms=1700000000000,
                )

        with TemporaryDirectory(prefix="sd-hyb-tv-") as tmp:
            store, leases, founding, claim, evidence, now = _setup_company_and_run(
                Path(tmp)
            )
            tv_adapter = TradingVenueConnectorAdapter(
                venue_resolver=lambda _r, _s: _RecordingVenue()
            )
            proxy = ConnectorProxy(
                ConnectorAdapterRegistry([tv_adapter]),
                InMemorySecretVault(
                    {
                        "envref:BINANCE_TESTNET": {
                            "api_key": "test-tv-key-1111",
                            "api_secret": "test-tv-sec-2222",
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
                    binding_id="tv-spot-1",
                    connector_kind=CONNECTOR_KIND_TRADING_VENUE,
                    environment="external_test",
                    endpoint_url="http://127.0.0.1:9130",
                    allowed_hosts=("127.0.0.1",),
                    operations=tv_adapter.manifest.operations,
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
                    allowed_operations=("submit_order",),
                    granted_by_actor_id="board-1",
                    now_ns=now + 1,
                )
                huge_order_prop = PlaneExecutionProposal(
                    proposal_id="p-tv-huge-zero-declared",
                    plane="code",
                    engine_id="rule.tv",
                    engine_revision="v1",
                    action_kind="proceed",
                    rationale="Attempt huge order declaring zero notional",
                    decision_at_ns=now + 5,
                    evidence=(evidence,),
                    proposed_notional_minor_units=0,
                    connector_effect=ConnectorEffectIntent(
                        binding_id=binding.binding_id,
                        operation="submit_order",
                        request_id="tv-ord-1",
                        payload={
                            "action_id": "tv-ord-1",
                            "symbol": "BTCUSDT",
                            "side": "BUY",
                            "time_in_force": "GTC",
                            "quantity": "1000000",
                            "limit_price": "100000",
                        },
                        expected_environment="external_test",
                    ),
                )

                # A. WITHOUT an authoritative trading_venue EffectAdmissionAuthorizer -> FAIL CLOSED!
                gate_no_auth = DeterministicControlPlaneGate(
                    connector_proxy=proxy, max_notional_minor_units=1
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate_no_auth.commit(w, claim, huge_order_prop, now_ns=now + 5)
                self.assertEqual(len(submitted_orders), 0)

                # B. WITH an authoritative trading authorizer computing real qty * price ->
                #    still DENIED because authoritative notional exceeds max_notional_minor_units=1!
                def _trading_authorizer(
                    _db: Any,
                    _cid: str,
                    _aid: str,
                    _binding: Any,
                    _op: str,
                    payload: Any,
                    _prop: Any,
                ) -> EffectAdmissionDecision:
                    notional = int(
                        Decimal(str(payload["quantity"]))
                        * Decimal(str(payload["limit_price"]))
                    )
                    return EffectAdmissionDecision(
                        issued_by_service="trading.risk_authorizer",
                        authoritative_notional_minor_units=notional,
                    )

                gate_with_auth = DeterministicControlPlaneGate(
                    connector_proxy=proxy,
                    effect_authorizers={
                        CONNECTOR_KIND_TRADING_VENUE: _trading_authorizer
                    },
                    max_notional_minor_units=1,
                )
                with self.assertRaises(HybridControlGateDenied):
                    gate_with_auth.commit(w, claim, huge_order_prop, now_ns=now + 6)
                self.assertEqual(len(submitted_orders), 0)

                # C. Small order within authoritative notional limit (1 <= 1) succeeds!
                tiny_order_prop = PlaneExecutionProposal(
                    proposal_id="p-tv-tiny-ok",
                    plane="code",
                    engine_id="rule.tv",
                    engine_revision="v1",
                    action_kind="proceed",
                    rationale="Bounded authorized order",
                    decision_at_ns=now + 8,
                    evidence=(evidence,),
                    proposed_notional_minor_units=1,
                    connector_effect=ConnectorEffectIntent(
                        binding_id=binding.binding_id,
                        operation="submit_order",
                        request_id="tv-ord-tiny",
                        payload={
                            "action_id": "tv-ord-tiny",
                            "symbol": "BTCUSDT",
                            "side": "BUY",
                            "time_in_force": "GTC",
                            "quantity": "1",
                            "limit_price": "1",
                        },
                        expected_environment="external_test",
                    ),
                )
                out = gate_with_auth.commit(w, claim, tiny_order_prop, now_ns=now + 8)
                self.assertEqual(len(submitted_orders), 1)
                self.assertIsNotNone(out.admission_id)

    def test_decision_model_propagates_anti_lookahead_violation_without_fallback(
        self,
    ) -> None:
        now = 1_700_000_000_000_000_000
        art = b"weights-v1"
        snap = FeatureSnapshot(
            snapshot_id="snap-la-prop",
            schema_version="feat-v1",
            decision_at_ns=now,
            features=(
                FeaturePoint(
                    name="x",
                    value=1.0,
                    observed_at_ns=now - 10,
                    available_at_ns=now - 5,
                ),
            ),
        )
        fallback_ran = {"count": 0}
        fb = CodePlaneExecutor(
            rule_id="rule.fb",
            code_revision="v1",
            rule_fn=lambda _s, _p: (
                fallback_ran.__setitem__("count", fallback_ran["count"] + 1)
                or CodeRuleDecision(action_kind="proceed", rationale="must not run")
            ),
        )
        dm = DecisionModelPlaneExecutor(
            model_id="dm-la-scorer",
            model_revision="v1",
            expected_artifact_sha256=sha256(art).hexdigest(),
            feature_schema_version="feat-v1",
            artifact_bytes=art,
            fallback_executor=fb,
            scorer_fn=lambda _s, _a: (_ for _ in ()).throw(
                HybridAntiLookaheadViolation("Derived feature leaked future timestamp")
            ),
        )
        with self.assertRaises(HybridAntiLookaheadViolation):
            dm.evaluate(snap)
        self.assertEqual(fallback_ran["count"], 0)

        # Fallback raising HybridAntiLookaheadViolation must also propagate!
        fb_la = CodePlaneExecutor(
            rule_id="rule.fb.la",
            code_revision="v1",
            rule_fn=lambda _s, _p: (_ for _ in ()).throw(
                HybridAntiLookaheadViolation("Fallback feature leaked future timestamp")
            ),
        )
        dm_bad_art = DecisionModelPlaneExecutor(
            model_id="dm-la-fb",
            model_revision="v1",
            expected_artifact_sha256=sha256(b"other").hexdigest(),
            feature_schema_version="feat-v1",
            artifact_bytes=art,
            fallback_executor=fb_la,
            scorer_fn=lambda _s, _a: (_ for _ in ()).throw(
                AssertionError("unreachable")
            ),
        )
        with self.assertRaises(HybridAntiLookaheadViolation):
            dm_bad_art.evaluate(snap)

    def test_llm_gate_rejects_tampered_updated_parameters_and_enforces_gate_bounds(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-hyb-llm-upd-") as tmp:
            store, leases, founding, claim, evidence, now = _setup_company_and_run(
                Path(tmp)
            )
            bounds = {
                "limit": ParameterBound(
                    min_value=1.0, max_value=20.0, max_abs_delta=2.0
                )
            }
            meta = LlmMetaPlaneExecutor(
                parameter_bounds=bounds,
                min_eval_samples=20,
                min_holdout_margin=0.01,
            )
            patch = PolicyPatchProposal(
                proposal_id="p-upd-tamper",
                policy_id="policy.risk",
                base_revision="v1",
                target_revision="v2",
                base_parameters={"limit": 10.0},
                parameter_deltas={"limit": 1.0},
                rationale="Bounded +1 adjustment",
                produced_by_run_id=claim.run_id,
                evidence=(evidence,),
            )
            verdict = OfflineEvalVerdict(
                verdict_id="ev-upd-1",
                policy_id="policy.risk",
                target_revision="v2",
                proposal_digest=patch.canonical_digest(),
                suite_revision="s1",
                passed=True,
                sample_count=50,
                holdout_margin=0.05,
                max_drawdown_bps=50,
                evaluated_at_ns=now + 1,
                evaluator_id="eval-1",
            )
            admitted = meta.admit_policy_patch(
                patch,
                current_parameters={"limit": 10.0},
                verdict=verdict,
                decision_at_ns=now + 5,
            )
            # Tamper ONLY parameters['updated_parameters'] after admit_policy_patch!
            tampered_params = dict(admitted.parameters)
            tampered_params["updated_parameters"] = {"limit": 1_000_000.0}
            tampered_prop = PlaneExecutionProposal(
                proposal_id=admitted.proposal_id,
                plane=admitted.plane,
                engine_id=admitted.engine_id,
                engine_revision=admitted.engine_revision,
                action_kind=admitted.action_kind,
                rationale=admitted.rationale,
                decision_at_ns=admitted.decision_at_ns,
                evidence=admitted.evidence,
                parameters=tampered_params,
                eval_verdict=admitted.eval_verdict,
                eval_verdict_digest=admitted.eval_verdict_digest,
                policy_patch_digest=admitted.policy_patch_digest,
            )
            gate = DeterministicControlPlaneGate(
                llm_parameter_bounds=bounds,
                min_llm_eval_samples=20,
                min_llm_holdout_margin=0.01,
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as w:
                with self.assertRaises(HybridControlGateDenied):
                    gate.commit(w, claim, tampered_prop, now_ns=now + 5)
                # Untampered admitted proposal commits cleanly!
                out = gate.commit(w, claim, admitted, now_ns=now + 6)
                self.assertEqual(out.decision.action_kind, "approve")


if __name__ == "__main__":
    unittest.main()
