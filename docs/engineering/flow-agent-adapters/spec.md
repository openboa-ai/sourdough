# Multi-provider real LLM agent runtime adapters (`flow-agent-adapters`)

Status: Accepted specification · Target branch: `feat/flow-agent-adapters` · Owner: Agent Harness, runtime adapters, and corporate accounting.

## 1. Intent

Provide a unified, provider-agnostic `AgentRuntimeAdapter` port and four concrete adapters—`codex_cli`, `openai` (Responses API), `anthropic` (Messages API), and `gemini` (`generateContent` API)—so each Sourdough employee Agent can execute real tool-calling shifts on its own immutable pinned runtime/model profile while preserving Two-Plane Harness isolation, strict Outer Capability Broker mediation, and exact corporate general-ledger cost accounting.

## 2. Authority, profile pinning, and secret boundaries

- **Immutable per-Agent profile pinning:** Every Agent binds a deterministic `AgentProfileBinding(provider, model, revision, sandbox_backend, credential_handle_ref)` serialized into `profile_id()`. Before launching any shift, `AgentAdapterRegistry` verifies the Agent's persisted `execution_profile` matches the requested `AgentProfileBinding` fail-closed (`AgentProfileMismatch`). Sourdough NEVER silently switches or falls back to another provider or model.
- **Outer Capability Broker mediation (`OuterToolBroker`):** Model tool calls from any provider are treated as untrusted data. `OuterToolBroker` enforces:
  1. Strict tool allowlist per shift (`tools` declared in `ShiftRequest`).
  2. Forbidden server-identity keys (`agent_id`, `company_id`, `run_id`, `fencing_token`, `tier`) in model tool arguments are unconditionally rejected.
  3. Bounded maximum tool invocations per shift (`max_tool_calls`).
  4. Idempotent call deduplication by provider `call_id` (replaying an already-committed call returns its original receipt without re-executing side effects).
- **Two-Plane isolation for local CLI (`CodexCliAdapter`):** Connects the subprocess strictly over a user-private (`0o700`/`0o600`) UDS broker socket and supports macOS Seatbelt (`sandbox-exec`) Desk confinement via `harness.build_desk_sandbox_profile`, with scrubbed environment variables (zero API keys or company SQLite access inside the Desk).
- **Opaque API secret resolution (`SecretHandleResolver`):** HTTP adapters (`openai`, `anthropic`, `gemini`) resolve provider API keys strictly in memory at request time via `envref:` or `keychain:` handles wrapped in `MaskedSecret` (always masked in `__repr__`/`__str__`). Zero raw secrets are persisted to SQLite, trace logs, Desk files, or `audit_event`.

## 3. Context hydration & cost settlement

- `hydrate_shift_context(...)` builds a versioned `ContextPack` containing mandate, work item, grants, and open-effect receipts. Compaction truncates optional history first and NEVER evicts open-effect receipts or active grant boundaries.
- `settle_shift_cost_to_ledger(...)` computes exact integer minor-unit cost from `(input_tokens, output_tokens)` and pinned `ModelPricing`, verifies spend against the shift budget ceiling, and posts one idempotent balanced `JournalEntry` via `general_ledger.post_journal` (`expense:agent_llm_compute` debit / `liability:accounts_payable` credit) keyed by `source_event = "llm-turn:<run_id>:<turn_id>"`.

## 4. Lifecycle failures & recovery

- Transport timeouts, HTTP `429`/`5xx`, or malformed JSON raise `AgentTransportInterrupted` without retrying side-effecting calls automatically or erasing already-committed tool receipts.
- A retry shift reuses the company's idempotent `action_id` receipts (e.g., `AgentSandboxTools` / `paper_order`) without duplicating economic effects.

## 5. Acceptance evidence

- Unit tests: `tests/unit/test_agent_runtime_adapters.py` covering all 4 adapters (`codex_cli`, `openai`, `anthropic`, `gemini`) against local loopback mock HTTP/CLI servers, broker spoofing denial, profile mismatch fail-closed denial, context receipt preservation, transport interruption recovery, and exact GL cost posting.
- Connected flow test: `tests/flows/flow-agent-adapters/test_flow.py` (`ConnectedFlow.test_end_to_end`) running 4 distinct Agents (one per provider) on one company store, executing isolated Tier 1 sandbox trades through `OuterToolBroker`, posting balanced GL costs, confirming zero secret leakage in SQLite, and observing all 4 sandboxes on `/v1/projections/execution-tiers`.
