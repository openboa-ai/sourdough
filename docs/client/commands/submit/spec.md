# Submit a command and resolve its receipt

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Human query and control access is through Client. Route a received command to its domain owner; a successful submission is not an applied change. Agent and service-originated operations remain independent of Client.

Send only after an explicit board action: authenticated principal, company, typed operation, request ID, argument digest, expected revision and reason. A profile control such as suspend-new-risk uses its registered service contract; it is not required by the base client.

An optional trading-runtime configuration or start command uses the same public command/receipt boundary and only an explicit authorized client action. The service owns validation, credential handling and actual setup. The base client need not have a trading profile, and the company can configure and start the runtime without any client.

Input contracts: [Versioned charter command](../../../company/systems/organization/found-company/amend/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Submit commands over the daemon's versioned HTTP/1.1 + JSON + NDJSON Unix domain socket Control Plane (`POST /v1/commands` with `Idempotency-Key` and typed multi-field `preconditions_json` such as `charter_revision` and `expected_control_revision`, plus `GET /v1/commands/{command_id}` scoped to the authenticated `(principal, operation)`). Keep explicit states separate: `draft -> submitting -> unknown -> accepted -> applied | denied | conflicting_revision`. Only the daemon's authoritative `command_receipt` establishes applied, denied or `conflicting_revision` status.

Upon dispatching an active `POST /v1/commands` request, the Client may write-ahead record the request metadata in local `board-pending` storage **solely for already-transmitted unknown requests** (clearing the entry immediately if transport fails before request bytes are transmitted, and retaining it only when delivery or reply status is unknown) so a disconnect, timeout or app restart can resolve the original `command_id` via `GET /v1/commands/{command_id}`. `board-pending` never stores offline queued mutations for automatic replay. The Zig Board Client's internal transport adapter (direct UDS vs single persistent local stdio bridge) and `board-pending` persistence are gated on a pre-Stream-B2 Native SDK 0.11.1 capability probe.

**Concern for the responsible component:** A timed-out click might already have committed. Query the original receipt under current authorization; do not allocate a second request ID on reconnect and never auto-replay an offline draft.

## Functional and quality requirements

- **FR.** On stale revision (`conflicting_revision` persisted in `command_receipt`) require a new informed decision against the latest facts. An exact retry, where allowed by contract, reuses the request ID, arguments and typed multi-field `preconditions_json`.
- **FR.** Persist only already-transmitted unknown requests in `board-pending`; on reconnect query `GET /v1/commands/{command_id}` (scoped to the authenticated `(principal, operation)`). If `404`, present an explicit human choice to retry with the original ID or discard the draft.
- **Compatibility.** Negotiate capabilities via `GET /v1/meta` before enabling mutations. Do not submit a command whose type or contract revision the service does not support. The service independently rejects unsupported requests before any effect. Reconnecting after an upgrade never translates an outstanding request into a new command.
- **NFR.** Double-click, refresh and reconnect cannot apply a control twice. Neither cached grants nor client validation override the server. While disconnected or incompatible, lock all mutating controls fail-closed.
- **FR.** When a trading runtime is enabled, render configuration and start dispositions returned by the service, including blocked and unknown. An exact retry reuses the original request ID; never interpret a proposed configuration or a running client as runtime readiness. Present credential handles or status only, never secret values.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Submit an allowed change. Observe a pending UI state followed by the same effective revision independently read from the service. |
| Failure or contention | Disconnect after commit but before response. Reopen and recover the original receipt. Reject unauthorized and conflicting-revision requests with zero policy changes. |
| Version mismatch | Open an older client against a service that no longer accepts its command revision. Supported projections may still render; the unsupported control is unavailable. A direct request with that revision is rejected without a domain write. An unreadable earlier receipt remains unknown, not denied. |

**Oracle:** Compare user action, request ID, service admission and domain effective revision. Verify zero commands from merely browsing.

## Operations and change review

Expose pending age; if a receipt cannot be obtained keep unknown visible. Closing the app never resumes suspended risk.
