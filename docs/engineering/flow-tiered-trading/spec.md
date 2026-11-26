# Three-tier execution accounts, realistic paper simulation, and live promotion gate (`flow-tiered-trading`)

Status: Accepted specification · Target branch: `feat/flow-tiered-trading` · Owner: Quant execution, storage kernel, and Board control plane.

## 1. Intent

Sourdough must let every employee Agent independently run isolated paper trading experiments while maintaining a company-wide default paper trading account, and allow the human Board to promote the company to a separately qualified live execution binding only after verified paper soak and external testnet qualification—without turning off or contaminating ongoing paper or Agent sandbox experimentation.

## 2. Three-tier execution model & isolation boundaries

| Tier | `tier` value | `environment` | Scope & isolation boundary |
| --- | --- | --- | --- |
| **Tier 1 · Agent Sandbox Paper** | `agent_sandbox` | `paper` | One active isolated virtual account per active Agent (`owner_agent_id`). Writes exclusively to `agent_sandbox_order`, `agent_sandbox_fill`, and `agent_sandbox_position`. Zero writes to company investment books (`paper_order`, `paper_fill`), company risk limits, or corporate general ledger trading postings. Retired Agents cannot open or submit sandbox orders. |
| **Tier 2 · Company Paper** | `company_paper` | `paper` | Default company-wide simulated trading account (`paper_binding` + `execution_environment_binding`). Runs the full company trading core with realistic configurable `fee_bps` (`0..1000`) and `slippage_bps` (`0..500`), serving as the pre-production operating soak environment. |
| **Tier 3 · Company Live / Prod** | `company_live` | `live` | Registered as `status = 'registered'` first; activates (`status = 'active'`) **only** through a server-side fail-closed Board promotion command (`promote_execution_environment`). Keeps Tier 1 (`agent_sandbox`) and Tier 2 (`company_paper`) active in parallel after activation. |

## 3. Allowed and prohibited behavior

- **Allowed:**
  - Any active (non-retired) Agent may open, trade in (buy/sell with limit price, `fee_bps`, `slippage_bps`), cancel open orders in, read portfolio state of, and reset (retiring prior history immutably) its own `agent_sandbox` account through the server-bound `AgentSandboxTools` broker.
  - The company runs `company_paper` by default and may sync legacy `paper_binding` rows into `execution_environment_binding(tier='company_paper')`.
  - An authorized operator records immutable `external_test` `venue_qualification_receipt` rows binding `(core_digest, adapter_revision, environment='external_test', destination_id, instrument_rules_digest, verdict, evidence_sha256)`.
  - A `board_control` principal submits `promote_execution_environment` (`execution-tier-command-v1`) with `Idempotency-Key` and exact `expected_payload_hash` to activate a registered `company_live` binding, or `demote_live_environment` (`suspend_live` | `demote_to_paper`) to immediately fence live dispatch while retaining historical/unresolved reconciliation records.
- **Prohibited (Fail-Closed):**
  - Raw API secrets or signing keys in `execution_environment_binding`, SQLite, Desk files, or `audit_event`. `credential_handle_ref` MUST be an opaque host handle (`keychain:...` or `envref:...`), and `live` bindings MUST NOT reuse any `paper` or `external_test` credential handle, `account_id`, or `destination_id`.
  - Promoting `company_live` without ALL four gates satisfied atomically: (1) `board_control` authorization (`board_viewer` denied 403), (2) verified `company_paper` soak evidence (active paper binding plus observed `paper_fill`, `paper_valuation`, or `paper_statement`), (3) matching `PASS` `external_test` `venue_qualification_receipt` (`cases_failed == 0`, matching `core_digest` and `instrument_rules_digest`), and (4) matching `charter_revision` + `expected_payload_hash` (`conflicting_revision` persisted on mismatch).
  - Cross-Agent sandbox access or spoofing: `AgentSandboxTools` binds `agent_id` strictly from the server-side `AgentSession`; model arguments cannot target another Agent's sandbox or touch `company_paper` / `company_live`.

## 4. Schema & backward compatibility

- Adds canonical v1 opt-in paper pack `src/sourdough/storage/schema/v1/paper/04_execution_tiers.sql` (`STRICT`, `PRAGMA user_version = 1`, zero incremental migrations).
- Registers all 6 new tables exclusively under `execution_tiers.py` in `src/sourdough/storage/ownership.py`.
- Preserves existing `paper_binding` (`01_paper_operations.sql`) and all existing Flow 01–13 unit and flow tests without modification.
- Relaxes `SandboxPlan` in `nautilus_paper.py` to accept `0 <= fee_bps <= 1000` and `0 <= slippage_bps <= 500` (default `slippage_bps=0`) while preserving full deserialization compatibility with prior `fee_bps=0` plans.

## 5. Lifecycle failures & recovery

- Duplicate `Idempotency-Key` with identical command terms returns the original persisted `command_receipt` without re-applying; changed terms return conflict (`409`).
- Duplicate `(binding_id, action_id)` on `submit_agent_sandbox_order` returns the original sandbox order/fill receipt idempotently; changed terms raise `ExecutionTierRefused`.
- Emergency demotion (`suspend_live` or `demote_to_paper`) atomically transitions `company_live` to `suspended` or `retired`, records `environment_promotion_record` and `audit_event`, leaves `company_paper` and `agent_sandbox` unaffected, and preserves all live bindings/records for reconciliation.

## 6. Acceptance evidence

- Unit tests: `tests/unit/test_execution_tiers.py` covering all Tier 1/2/3 invariants, slippage/fee math, broker isolation, realistic `SandboxPlan`, fail-closed promotion rejections, and emergency demotion.
- Connected flow test: `tests/flows/flow-tiered-trading/test_flow.py` (`ConnectedFlow.test_end_to_end`) proving 2-Agent isolated sandboxes + Company Paper + UDS Control Plane `/v1/projections/execution-tiers` & `/v1/projections/agents/{id}` + `promote_execution_environment` + 3-tier parallel operation + `demote_live_environment`.
