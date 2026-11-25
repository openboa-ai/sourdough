# Submit a command and resolve its receipt

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Usage scenarios](../../scenarios.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Human query and control access is through Client. Route a received command to its domain owner; a successful submission is not an applied change. Agent and service-originated operations remain independent of Client.

Send only after an explicit board action: authenticated principal, company, typed operation, request ID (`command_id`), argument digest (`payload_sha256`), expected preconditions (`preconditions_json`), and reason. A profile control such as `suspend_new_risk` or `resume_new_risk` uses its registered service contract; it is not required by the base client.

An optional paper-trading-runtime configuration or start command uses the same public command/receipt boundary and only an explicit authorized client action (`environment == "paper"`). The service owns validation, credential handling, and actual setup. The base client need not have a trading profile, and the company can configure and start the runtime without any client.

Input contracts: [Versioned charter command](../../../company/systems/organization/found-company/amend/spec.md) · [Board control](../../../company/profiles/quant/trading/board-control/apply-control/spec.md) · [Client architecture](../../architecture.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Submit commands over `CompanyDaemon`'s versioned HTTP/1.1 + JSON + NDJSON Unix domain socket Control Plane (`POST /v1/commands` with `Idempotency-Key` matching `command_id` and typed multi-field `preconditions_json`, plus `GET /v1/commands/{command_id}?operation=...`).

1. **Host transport and `(company_id, principal_id)` `board-pending` persistence:** The React 19 + `@openboa/yogurt` Client UI dispatches commands through the authenticated `sourdough client-web` loopback HTTP-to-UDS bridge (or a capability-probed `zero://app` Native SDK `0.11.1` WebView host) described in [Client architecture](../../architecture.md). Upon dispatching an active `POST /v1/commands` request, the host/bridge atomically writes `LocalCommandIntent` (`{command_id, command_type, payload, payload_sha256, expected_revision, sent_ns}`) inside an `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` (`0o700`) directory using `O_CREAT | O_EXCL | O_NOFOLLOW` (`0o600`), `fsync(fd)`, atomic rename, and `fsync(dir_fd)` — **solely for already-transmitted unknown requests** (clearing immediately if transport fails before request bytes are transmitted, and retaining it only when delivery or reply status is unknown). Scope `board-pending` storage strictly by **`(company_id, principal_id)`** (`{base_dir}/{company_id}/{principal_id_sha256}.json`), never by `company_id` alone; on startup recovery, if `GET /v1/commands/{command_id}` returns `403` for a stale or unowned pending file, quarantine/purge that stale file with an explicit security notice so another principal is never deadlocked. `board-pending` never stores offline queued mutations for automatic replay.
2. **8-variant `CommandLifecycle` and `CommandOutboxPanel` (`Receipt`):** Maintain the exact 8-variant `CommandLifecycle` state machine: `idle`, `draft`, `submitting`, `unknown`, `accepted`, `applied`, `denied`, `not_found_retry_choice`. Represent `conflicting_revision` strictly as `state = "denied"` with `denial_code = "conflicting_revision"` (never a 9th lifecycle variant). Map lifecycle states into `CommandOutboxPanel` wrapping `@openboa/yogurt`'s `Receipt`:
   - `submitting` and `accepted` render `ReceiptRecord.state = "pending"` with elapsed `pending_age` (`now - sent_ns`) and **omit `onLookup`**.
   - `unknown` renders `ReceiptRecord.state = "unknown"` and **passes `onLookup`** (`"Check original request"`) **solely in this state**.
   - `applied` deletes the pending file first (failing closed to `unknown` + `controls_locked` if delete fails) and renders `ReceiptRecord.state = "confirmed"`.
   - `denied` deletes the pending file first and renders `ReceiptRecord.state = "failed"` with `denial_code` (requiring a brand-new human decision against the latest revision when `conflicting_revision`).
   - `not_found_retry_choice` (`404` on receipt lookup) retains the pending file, omits `onLookup`, keeps `controls_locked = true`, and renders an explicit choice: **`Retry same ID`** (reusing exact persisted `command_id`, `payload`, and `preconditions_json`) or **`Discard draft`** (deleting the pending file and returning to `idle`).
3. **Confirmation boundary (`ControlledConfirmDialog` on `@ui-sdk/react/button`):** Every mutating action confirms inside `ControlledConfirmDialog` built on `@ui-sdk/react/button` (`SDKButton`). The dialog rejects automated invocation (`details?.source === "agent"`), places `data-agent-confirm="<action>"` strictly on the confirm button, latches the exact revision/digest preconditions shown to the operator, and closes immediately (`onOpenChange(false)`) to hand off execution state to `CommandOutboxPanel`.
4. **Preconditions & competing-stop non-clearing (`[Live origin/main]` vs `[M3 Extension]`):** On `origin/main`, `POST /v1/commands` restricts `_ALLOWED_PRECONDITION_KEYS` to `{"charter_revision", "expected_revision"}` (`suspend_new_risk`, `resume_new_risk`) and `serve-company-daemon` binds `authorize_board=None` (fail-closed `403`). Under `[M3 Extension]`, `board_binding` and `role_assignment` enable scoped `board_control` commands, `GET /v1/commands/{command_id}` is scoped to `(principal, operation)`, and multi-cause `RestrictionView` assigns a distinct `restriction_id` per cause (`{restriction_id, expected_restriction_revision, charter_revision}`) so clearing `R1` (`rest-p1`) never clears an active competing stop `P2` (`rest-p2`).
5. **Server-side `QUOTE_MAX_AGE_NS = 15s` freshness (`resume_new_risk`):** `resume_managed_paper_risk` evaluates `_settled_company` and `venue.read_current_risk_cut()` (`QUOTE_MAX_AGE_NS = 15s`) **server-side inside `POST /v1/commands`** across Phase 1 (lock-free read) and Phase 2 (`BEGIN IMMEDIATE` verifying `observed_ns <= time_ns() <= expires_ns`). The Client MUST NOT render a pre-fetched 15-second modal countdown prior to clicking Confirm; instead display non-binding settlement/quote readiness pre-checks (`[M3 Extension]`) and handle retryable server `400` expiry errors cleanly without mutating `paper_risk_state`.

**Concern for the responsible component:** A timed-out click might already have committed. Query the original receipt under `(company_id, principal_id)` authorization; do not allocate a second request ID on reconnect, never auto-replay an offline draft, and never allow clearing one restriction cause to bypass another active stop.

## Functional and quality requirements

- **FR.** On stale revision (`conflicting_revision` persisted in `command_receipt`) require a new informed decision against the latest facts. An exact retry, where allowed by contract, reuses the original `command_id`, arguments, and typed multi-field `preconditions_json`.
- **FR.** Persist only already-transmitted unknown requests in `(company_id, principal_id)`-scoped `board-pending`; on reconnect query `GET /v1/commands/{command_id}?operation=...`. If `404`, transition to `not_found_retry_choice` with explicit `Retry same ID` vs `Discard draft` controls. If `403` on startup recovery of a stale file, quarantine/purge it safely.
- **FR.** Render `Receipt.onLookup` strictly when `lifecycle === "unknown"`, confirm exclusively through `ControlledConfirmDialog` (`SDKButton` rejecting `details?.source === "agent"`), and never display a pre-fetched 15s quote countdown before `resume_new_risk` confirmation.
- **Compatibility.** Negotiate capabilities via `GET /v1/meta` before enabling mutations. Do not submit a command whose type or contract revision the service does not support. The service independently rejects unsupported requests before any effect. Reconnecting after an upgrade never translates an outstanding request into a new command.
- **NFR.** Double-click, refresh, and reconnect cannot apply a control twice. Neither cached grants nor client validation override the server. While disconnected, stale, `host_locked`, or `unknown`, lock all mutating controls fail-closed (`controls_locked = true`).
- **FR.** When a paper trading runtime is enabled (`environment == "paper"`), render configuration and start dispositions returned by the service, including blocked and unknown. An exact retry reuses the original request ID; never interpret a proposed configuration or a running client as runtime readiness. Present credential handles or status only, never secret values.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Submit an allowed change via `ControlledConfirmDialog`. Observe immediate modal close handoff to `CommandOutboxPanel` (`pending`, no `onLookup`), followed by `applied` and matching effective revision read from the service. |
| Failure or contention | Disconnect after commit but before response (`unknown` enables `onLookup`). Reopen and recover the original `(company_id, principal_id)` receipt (`applied`, `denied(conflicting_revision)`, or `404` -> `not_found_retry_choice` `Retry same ID`/`Discard draft`). Verify `403` stale-file quarantine does not deadlock a different principal. |
| Competing stops & 15s cut | Trigger `P1` and `P2`; verify stale `R1` (`conflicting_revision`) leaves suspension active (and `[M3 Extension]` `R1` clearing `rest-p1` leaves `rest-p2` active). Force >15s Phase 1->2 delay on `resume_new_risk`; verify server `400` preserves `suspended = 1` cleanly. |
| Version mismatch | Open an older client against a service that no longer accepts its command revision. Supported projections may still render; the unsupported control is unavailable. A direct request with that revision is rejected without a domain write. An unreadable earlier receipt remains `unknown`, not `denied`. |

**Oracle:** Compare user action, `command_id`, service admission (`command_receipt`), `(company_id, principal_id)` `board-pending` file state, and domain effective revision across [Usage scenarios](../../scenarios.md). Verify zero commands from merely browsing.

## Operations and change review

Expose `pending_age`; if a receipt cannot be obtained keep `unknown` visible and `controls_locked = true`. Closing the app never resumes suspended risk or clears an active restriction.
