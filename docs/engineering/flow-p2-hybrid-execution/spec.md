# Flow P2-B: Hybrid Execution Planes, Reusable UDS Broker Bridge & Generalized Runtime Registry

## 1. Intent

Sourdough separates company governance and authority from how individual execution tasks are computed. Not every company decision should burn LLM tokens or depend on stochastic text generation:

1. **Reusable UDS Tool Broker Bridge (`agent_runtime/broker_bridge.py`):** Extract the private `0o600` Unix domain socket `OuterToolBroker` server out of `codex_cli.py` into a standalone public `UdsToolBrokerServer` so local CLI adapters, sandboxed workers, and future non-LLM subprocess runtimes share one hardened mediation boundary without duplicating socket framing or fatal-exception handling.
2. **Generalized `AdapterManifest` & `AgentAdapterRegistry`:** Extend `AdapterManifest` with `execution_plane` (`"llm" | "code" | "decision_model"`) and `default_timeout_seconds`, and generalize `AgentAdapterRegistry` / `AgentProfileBinding` to support registered non-LLM and custom execution providers while preserving 100% backward compatibility for persisted `AgentProfileBinding.profile_id()` digests and strict zero-fallback fail-closed dispatch.
3. **4-Plane Hybrid Execution (`runtime/hybrid_execution.py`):** Implement three specialized execution planes unified under one deterministic control-plane commit gate:
   - **Plane 1 — Deterministic Code Plane (`CodePlaneExecutor`):** Pinned pure-code rule/strategy execution at `$0` LLM cost (`TokenUsage(0, 0)`) with deterministic reproducibility digests and anti-lookahead feature validation.
   - **Plane 2 — Version-Pinned Decision Model Plane (`DecisionModelPlaneExecutor`):** Version- and SHA-256-pinned quantitative/ML scoring over timestamped `FeatureSnapshot` inputs, enforcing strict anti-lookahead (`available_at_ns <= decision_at_ns`) and explicit, non-silent deterministic fallback on artifact digest mismatch, schema mismatch, timeout, runtime error, non-finite output, or low confidence.
   - **Plane 3 — Asynchronous LLM Meta-Reasoning Plane (`LlmMetaPlaneExecutor`):** Structured policy/parameter patch proposals strictly gated behind a verified `OfflineEvalVerdict` (matching `policy_id`, `target_revision`, canonical `proposal_digest`, minimum sample count, holdout margin, anti-lookahead timestamp, and declared parameter bounds) before admission.
   - **Plane 4 — Unified Deterministic Control Plane Gate (`DeterministicControlPlaneGate`):** Single authoritative pre-commit boundary enforcing live `WorkRunClaim` fencing, active `standing_grant`, active `restriction_cause` blocking, notional/risk limits, evidence-backed `work_decision` persistence, immutable audit logging, and service-owned `ConnectorProxy` admission for external effects across all three planes identically.

## 2. Authority & Data Boundaries

- **Fencing & Standing Authority:** Every committed proposal must present a live `WorkRunClaim` whose `company_id`, `run_id`, `fencing_token`, and active non-revoked `standing_grant` are verified inside SQLite `BEGIN IMMEDIATE` via `resolve_run` and `require_current`.
- **Restrictions Fail Closed:** Active `restriction_cause` rows scoped to `"company:all"`, `"company"`, `"global"`, `"all"`, `f"company:{company_id}"`, `proposal.target_scope_ref`, or `f"connector:{binding_id}"` block all non-defensive actions (`action_kind` outside `{"hold", "noop", "defer", "reject"}` or any `connector_effect`) regardless of which execution plane produced the proposal. Denied proposals record an immutable `hybrid.proposal_denied` audit event before raising `HybridControlGateDenied`.
- **Domain-Authorized Effectful Connector Mediation:** Proposals carrying a `ConnectorEffectIntent` never supply secrets or forge `admission_id` values. `DeterministicControlPlaneGate` pre-validates active binding, explicit `connector_grant`, and optional `ShiftFlowGuard` taint before persisting `work_decision`. For every `"effectful"` connector operation, the gate fails closed unless an authoritative domain/connector `EffectAdmissionAuthorizer` (registered explicitly or contributed by a `DomainPlugin`) validates the exact payload inside a transactional savepoint and returns an `EffectAdmissionDecision` (`issued_by_service`, `authoritative_notional_minor_units`, optional `reservation_ref`). The gate enforces `max_notional_minor_units` against `max(proposed_notional_minor_units, authoritative_notional_minor_units)`, binds replay to complete proposal + connector terms, issues a single deterministic `connector_admission` per `(company_id, agent_id, action_id)`, and dispatches outside any SQLite write lock exclusively through `ConnectorProxy.invoke`.
- **Anti-Lookahead Integrity:** `FeatureSnapshot` rejects any `FeaturePoint` with `available_at_ns > decision_at_ns` or `available_at_ns < observed_at_ns`. `DecisionModelPlaneExecutor` propagates `HybridAntiLookaheadViolation` across both scorer and fallback execution boundaries as a hard integrity failure rather than falling back silently.
- **No Unreviewed Self-Modification:** `PolicyPatchProposal.canonical_digest()` cryptographically binds `base_parameters`, `parameter_deltas`, and resulting `updated_parameters`. Both `LlmMetaPlaneExecutor` and `DeterministicControlPlaneGate` independently verify `OfflineEvalVerdict`, exact key/arithmetic consistency (`updated == base + delta`), causal evaluation timestamps, and configured parameter bounds before admitting any patch.
- **Storage Ownership & Core Isolation:** `runtime/hybrid_execution.py` belongs to Core, imports zero trading-domain modules, introduces zero new SQLite tables, and performs zero direct SQL mutations—all writes flow through existing authoritative owners (`record_work_decision`, `append_audit_event`, `issue_connector_admission`, `ConnectorProxy`).

## 3. Allowed & Prohibited Behavior

### Allowed
- Executing deterministic rules via `CodePlaneExecutor` or `CodePlaneRuntimeAdapter` with `TokenUsage(0, 0)` and committing decisions/connector effects through `DeterministicControlPlaneGate`.
- Executing version-pinned scoring models via `DecisionModelPlaneExecutor` or `DecisionModelRuntimeAdapter`, emitting explicit `fallback_used=True` + `fallback_reason` proposals and `hybrid.fallback_triggered` audit events whenever degraded conditions trigger deterministic fallback.
- Admitting bounded LLM meta-reasoning `PolicyPatchProposal` updates strictly after verified `OfflineEvalVerdict` approval.
- Running standalone `UdsToolBrokerServer` instances over `0o600` Unix domain sockets with bounded request frame sizes (`max_request_bytes`), strict argument validation, and `OuterToolBroker` mediation.
- Registering built-in or custom slug-validated providers in `AgentAdapterRegistry` and querying adapters by `execution_plane`.

### Prohibited
- Silent fallback from one registered provider adapter to another in `AgentAdapterRegistry`.
- Bypassing `DeterministicControlPlaneGate`, `work_decision`, `restriction_cause`, or `ConnectorProxy` from any execution plane.
- Silent fallback on anti-lookahead leakage (`available_at_ns > decision_at_ns`).
- Auto-activating LLM meta-reasoning policy patches without a matching passing `OfflineEvalVerdict`.
- Mutating `AgentProfileBinding.profile_id()` hash generation for existing persisted profiles.
- Accepting unbounded UDS frames or spoofed identity keys (`agent_id`, `company_id`, `run_id`, `fencing_token`, `tier`, `actor_id`) in `UdsToolBrokerServer`.

## 4. Lifecycle Failures & Recovery

- **Model Timeout / Hung Scorer:** `DecisionModelPlaneExecutor` executes the scorer on a bounded daemon thread joined up to `timeout_seconds`; on expiry it does not block the caller and emits an explicit deterministic fallback proposal (`fallback_reason="model_timeout"`).
- **Corrupted Artifact / Non-Finite Score / Low Confidence:** Detected deterministically before commit; triggers explicit fallback (`artifact_digest_mismatch`, `invalid_model_output`, `confidence_below_threshold`) and records `hybrid.fallback_triggered`.
- **Stale Fencing / Revoked Grant / Active Restriction:** Refused before `work_decision` insertion or `connector_admission` issuance; no external effect is dispatched.
- **Connector Transport Indeterminacy:** Delegated directly to `ConnectorProxy.invoke`, preserving Phase 1/2/3 recovery work items (`connector.reconcile`) without holding SQLite write locks during network I/O.
- **Oversized or Malformed UDS Request:** `UdsToolBrokerServer` caps reads at `max_request_bytes + 1`, refuses overflow with `AgentRuntimeRefused`, records `last_refusal`, and returns a structured JSON error frame without invoking handlers.

## 5. Compatibility

- `AgentProfileBinding.profile_id()` retains the exact canonical JSON and SHA-256 truncation formula (`adapter-v1:<provider>:<model>:<digest12>`).
- Existing LLM adapters (`CodexCliAdapter`, `OpenAiResponsesAdapter`, `AnthropicMessagesAdapter`, `GeminiGenerateContentAdapter`) default to `execution_plane="llm"` and `default_timeout_seconds=120.0`.
- `codex_cli.py` retains `_UdsToolBrokerServer` and `_fatal_broker_exceptions` aliases pointing to `broker_bridge.py`.
- Zero SQLite schema migrations required.

## 6. Acceptance Evidence

- `tests/unit/test_p2_hybrid_execution.py`:
  - Standalone `UdsToolBrokerServer` `0o600` permissions, `OuterToolBroker` mediation, spoofed-identity refusal, and oversized-frame refusal.
  - `AdapterManifest` `execution_plane` / `default_timeout_seconds` validation, `AgentAdapterRegistry` custom/multi-plane registration with zero silent fallback, HTTPS credential enforcement, and `AgentProfileBinding.profile_id()` backward compatibility.
  - Plane 1 `CodePlaneExecutor` `$0` determinism & reproducibility digest.
  - Plane 2 `DecisionModelPlaneExecutor` pinned scoring, anti-lookahead refusal, and explicit deterministic fallback on timeout, digest mismatch, NaN output, and low confidence.
  - Plane 3 `LlmMetaPlaneExecutor` offline-eval gating (missing/failed/mismatched-digest/out-of-bounds/lookahead verdict refused; verified verdict admitted).
  - Plane 4 `DeterministicControlPlaneGate` enforcing fencing, active `restriction_cause` denial + audit, notional cap, `work_decision` persistence, and `ConnectorProxy` service-owned admission identically across all 3 planes.
- `tests/flows/flow-p2-hybrid-execution/test_flow.py`:
  - End-to-end connected flow combining standalone `UdsToolBrokerServer`, Plane 1 deterministic code rule (`$0`), Plane 2 decision model scoring + triggered deterministic fallback, Plane 3 offline-eval-gated LLM policy patch, live loopback connector dispatch through `ConnectorProxy`, and live `restriction_cause` denial verification against `company.sqlite`.
- Existing unit suites (`test_agent_runtime_adapters.py`, `test_storage_ownership.py`, `test_core_domain_isolation.py`, and full `tests/unit`) remain 100% green.
