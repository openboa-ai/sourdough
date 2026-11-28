# flow-p2-connectors — Unified SecretVault & Credential-Proxied Connector Boundary

## Intent

Implement Sourdough's canonical connector registration and invocation contracts
(`docs/company/systems/connectors/connect-resource/register/spec.md` and
`invoke/spec.md`) backed by a single unified host `SecretVault`, so external
venues, data sources, and company-approved external LLM APIs share one
fail-closed, credential-proxied outer-host boundary where Agents never see raw
API keys or secrets.

## Authority & Data Boundaries

- **Unified Secret Vault (`src/sourdough/secret_vault.py`):**
  - Consolidates opaque `envref:...` and `keychain:<service>/<account>` handle
    validation, environment isolation (`live` vs `external_test`/`paper`),
    masked secret containers (`MaskedSecret`, `MaskedSecretBundle`), and
    `/usr/bin/security` macOS Keychain resolution previously duplicated across
    `agent_runtime/protocol.py` and `trading/credentials.py`.
  - Raw secret values are resolved strictly in memory inside the trusted outer
    host at request-signing time and are scrubbed from errors, logs, audit
    events, and persisted receipts.
- **Authoritative Registry (`connector_binding`, `connector_grant`, `connector_admission`, `STRICT`):**
  - `connector_binding` pins `(binding_id, revision)` to `company_id`,
    `connector_kind`, `environment`, `endpoint_url`, `allowed_hosts_json`,
    versioned `operations_json`, opaque `credential_handle_ref` (enforced by
    SQLite `CHECK` constraint), `recovery_capabilities_json`, sliding-window
    rate limits, and cost ceiling.
  - Replacement produces a strictly higher revision and refuses environment
    changes on the same `binding_id`. Disabling a binding disables only that
    revision while preserving all outstanding `external_actions` records.
  - Registering a connector does **not** grant permission to act. Every Agent
    invocation requires an active `connector_grant` bound to an active
    `standing_grant` and explicitly naming the allowed operations.
  - `connector_admission` records service-issued admissions bound to the exact
    immutable `(binding_id, binding_revision, operation, request_id,
    payload_sha256, environment, endpoint_url)`. `admission_id` is never exposed
    as a model-visible tool argument in `OuterToolBroker`.
- **Outer-Host Gateway (`ConnectorProxy`):**
  - Enforces before any outbound dispatch:
    1. Live `WorkRunClaim` fencing token, unexpired lease, and active standing grant.
    2. Active `connector_binding` + exact `environment` + pinned host allowlist
       (loopback-only when `loopback_only=True`; HTTP 3xx redirects disabled;
       malformed CR/LF/NUL secret header values rejected without exposing contents).
    3. Active `connector_grant` authorizing the exact requested `operation`.
    4. `ShiftFlowGuard` taint checks (no external exfiltration after reading
       private Desk data; no `write`/`effectful` dispatch after reading peer input).
    5. Sliding-window rate limit (accounting for every outbound attempt,
       including unresolved `read` retries) and company/grant minor-unit budget ceilings.
    6. For `effectful` operations, verification and atomic consumption of a
       service-issued `connector_admission` matching the exact immutable request.
    7. Idempotent persistence of the outbound intent in `external_actions`
       (`prepared` → `in_flight`) **before** releasing the SQLite write lock and
       performing network I/O, followed by a post-secret-resolution sender
       fencing recheck immediately before `adapter.execute()` (retaining
       `'unknown'` + recovery item without sending if the sender was fenced).
  - **SQLite Lock Hygiene:** No SQLite transaction or write lock is ever held
    across outbound HTTP/network I/O.
  - **Indeterminate Outcome & Recovery:** Transport timeouts or indeterminate
    replies settle `external_actions` as `'unknown'`, return
    `ConnectorResponse(status='unknown', indeterminate=True)`, refuse blind
    write resends on retry, and create an idempotent recovery `work_item`
    (`duty_kind='connector.reconcile'`). Observation/reconciliation of an
    existing action is restricted strictly to the action's originating Agent or
    assigned recovery owner, and remains allowed even after new writes or grants
    are suspended/revoked.
  - **GL & Audit Settlement:** Positive invocation costs post balanced
    double-entry journal lines (`expense:connector_usage` /
    `liability:accounts_payable`) via `general_ledger.post_journal`, and every
    invocation/denial/reconciliation appends an immutable `audit_event` with
    redacted metadata.
- **DomainPlugin Seam #9 & Built-In `llm_inference` Connector:**
  - Adds `DomainPlugin.contribute_connectors()` so `TradingDomainPlugin`
    exposes `ExternalVenueAdapter` operations (`submit_order`, `cancel_order`,
    `query_order`, `account_snapshot`) through the shared `ConnectorProxy`.
  - Ships built-in `llm_inference` (`complete`, `embed`) so Agent-authored
    programs/tools can call company-approved external LLM endpoints by
    `binding_id` without ever accessing API keys, metered deterministically to
    the company General Ledger.

## Allowed & Prohibited Behavior

- **Allowed:** Registering typed connectors with opaque handles; granting
  explicit operation subsets to Agents; invoking granted operations through
  `ConnectorProxy` (or `OuterToolBroker` `invoke_connector` tool); replaying
  confirmed/rejected idempotency keys; reconciling `'unknown'` effects via
  `lookup`.
- **Prohibited:** Storing raw credentials in SQLite, audit events, tool
  receipts, or Agent Desks; mutating `environment` under the same `binding_id`;
  acting without an explicit `connector_grant`; blind write resends when an
  prior `external_actions` row is `'in_flight'` or `'unknown'`; following HTTP
  redirects to unpinned hosts; bypassing `ShiftFlowGuard` taint rules; calling
  `effectful` operations without an `admission_id`.

## Compatibility & Ownership

- Existing `agent_runtime/protocol.py` (`MaskedSecret`, `EnvSecretResolver`,
  `InMemorySecretResolver`) and `trading/credentials.py` (`ApiCredentialPair`,
  `EnvRefCredentialProvider`, `MacOsKeychainCredentialProvider`,
  `CompositeCredentialProvider`, `InMemoryTestCredentialProvider`) remain 100%
  API-compatible while delegating core parsing, environment isolation, and
  Keychain/env resolution to `sourdough.secret_vault`.
- `connector_binding` and `connector_grant` are added to `schema/v1/core/10_connectors.sql`,
  `storage/migration.py` (`CORE_NEW_TABLES`), and `storage/ownership.py`
  (`DOMAIN_TABLE_OWNERS` → `connectors/registry.py`).
- Core → Trading static imports remain strictly `0`.

## Acceptance & Evidence

- Unit suites: `tests/unit/test_p2_credentials.py`, `tests/unit/test_p2_connectors.py`,
  `tests/unit/test_storage_ownership.py`, and full `tests/unit` suite.
- Connected E2E flow: `tests/flows/flow-p2-connectors/test_flow.py`
  (`ConnectedFlow.test_end_to_end`) verifying live loopback `llm_inference`
  secret injection + GL posting + zero raw secret bytes anywhere in
  `company.sqlite`, response payloads, or errors, plus effectful venue timeout
  → `'unknown'` → recovery `work_item` → post-revocation reconciliation and
  Seam #9 plugin connector contribution.
