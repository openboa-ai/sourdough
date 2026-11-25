# Board Client architecture

Design draft · [Board Client](README.md) · [System map](../README.md) · [Development lifecycle](../engineering/lifecycle.md)

The Board Client is a presentation and control-plane interface over `CompanyDaemon`. It translates authorized projections into operator views and forwards explicit human decisions to the daemon's local Unix domain socket (UDS) Control Plane. The company operates, executes paper trades, reconciles fills, and recovers from crashes without the client installed or running.

## 1. Ownership boundaries

| Layer | Component | Authoritative ownership | Hard prohibitions |
| --- | --- | --- | --- |
| **Presentation** | React 19 + `@openboa/yogurt` (`0.2.0-local.1`) + `@ui-sdk/react` (`0.1.0-beta.13`) | Active route, local table sort/filter, `LinkState`, host-lock privacy shield, form drafts, `ControlledConfirmDialog` state, `CommandOutboxPanel` display | Zero direct SQLite access; zero venue/exchange/owner keys or secrets in renderer; zero client-side P&L/return calculation; zero synthetic command receipts |
| **Host / Local Bridge** | `sourdough client-web` loopback HTTP-to-UDS bridge (or probe-gated `zero://app` Native SDK shell) | Loopback HTTP session auth, `Host`/`Origin` pinning, UDS transport framing, `0o600` `O_NOFOLLOW` `(company_id, principal_id)` `board-pending` write-ahead file | Never mutates SQLite directly; never mints a second `command_id` for an unresolved request; never exposes browser tokens in URLs or `localStorage` |
| **Control Plane** | `CompanyDaemon` (`src/sourdough/ipc/server.py`, `control-plane-v1`) | UDS kernel peer verification (`LOCAL_PEERCRED` / `LOCAL_PEERPID` / `SO_PEERCRED`), `board_binding` & `role_assignment` RBAC (`[M3 Extension]`), `GET /v1/meta`, `GET /v1/projections/*`, `GET /v1/events`, `POST /v1/commands`, `GET /v1/commands/{id}` | Never assumes a UI click equals an applied transition; never grants `board_control` solely from same-UID socket access |
| **Domain Owners** | `CompanyRuntime`, `WorkKernel`, `DutyRegistry`, `PaperExecution`, Evaluator Store | SQLite canonical `v1` (`core` + opt-in `paper`, `STRICT` + `WAL` + `WriterLease`), separate Flow 13 `offline-trial-registry-v1` store, `PaperExecution` venue boundary | Zero imports of client code or UI packages; zero reliance on UI state for admission, reconciliation, or recovery |

Per-click CLI subprocesses (`sourdough client-bridge` or direct-store `inspect-work` / `read-command-receipt`) are prohibited in the target client. All reads and writes traverse `CompanyDaemon`'s resident UDS `/v1/*` Control Plane (`0o700` socket directory, `0o600` socket, `<= 103`-byte path limit).

## 2. Host topology and loopback HTTP-to-UDS bridge security

### 2.1 Primary standalone host (`sourdough client-web`)

Until a native WebView host passes capability verification, the primary host serves the React 19 `@openboa/yogurt` bundle via `sourdough client-web`, a local HTTP-to-UDS bridge that terminates browser HTTP on loopback and forwards exclusively to `CompanyDaemon`'s UDS socket:

- **Loopback bind only**: Listen strictly on `127.0.0.1:<ephemeral>` (never `0.0.0.0` or wildcard interfaces).
- **Strict `Host` and `Origin` pinning**: Reject any request whose `Host` header is not the exact bound `127.0.0.1:<port>` or `localhost:<port>`, or whose `Origin` / `Sec-Fetch-Site` indicates a cross-origin or DNS-rebinding caller, failing closed with `403`.
- **Out-of-band session authentication**: Authenticate browser requests using either (a) a one-time bootstrap exchange that sets an `HttpOnly; SameSite=Strict` session cookie (`fetch(..., { credentials: "same-origin" })`) or (b) a per-launch CSPRNG bearer token delivered out-of-band into host memory (stdin/FD/`0o600` bootstrap handoff) and sent via `Authorization: Bearer`. Never pass tokens in URL query parameters (`?token=`) and never persist tokens in `localStorage` or `sessionStorage`.
- **`fetch()` NDJSON streaming (never `EventSource`)**: Consume `GET /v1/events?after_seq=<n>&limit=<k>&wait_ms=<ms>` (`application/x-ndjson`, `wait_ms <= 5000`, `Connection: close`, `Cache-Control: no-store`) via `fetch()` `ReadableStream` / bounded long-poll. Do not use `EventSource` because it cannot attach custom `Authorization` headers, cannot inspect structured HTTP `401`/`403`/`503` disconnect payloads before retrying, and cannot synchronize cleanly with `clearData()`.
- **Bridge-owned `0o600` `board-pending` persistence**: The bridge (not browser storage) owns write-ahead `LocalCommandIntent` persistence (`{company_id, principal_id, command_id, command_type, contract_version, preconditions, payload, payload_sha256, sent_ns}`, retaining `expected_revision` only if needed for legacy display) inside an `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` (`0o700`) directory, writing atomic `O_CREAT | O_EXCL | O_NOFOLLOW` (`0o600`) files with `fsync(fd)` and `fsync(dir_fd)`, keyed strictly by **`(company_id, principal_id)`** (`{base_dir}/{company_id}/{principal_id_sha256}.json`). Persisting full canonical `preconditions` (including `charter_revision`, `expected_revision`, and all `[M3 Extension]` preconditions) plus `contract_version` ensures cold-start `Retry same ID` resubmits the exact original request without reading newer live revisions.
- **Explicit `LOCAL_PEERPID` collapse boundary**: `CompanyDaemon` verifies kernel peer identity on the UDS socket via `LOCAL_PEERCRED` + `LOCAL_PEERPID` (macOS) or `SO_PEERCRED` (Linux). When `sourdough client-web` bridges browser HTTP requests over UDS, all connected browser tabs collapse onto the bridge process's PID. Consequently, `client-web` must be typed by the control plane as a bridge-proxied local session: it defaults to `board_viewer`, requires explicit local-launch principal binding (`board_binding`) and bridge-enforced user confirmation to exercise `board_control`, and never masquerades as a hardware-backed native signer with `presence_evidence_ref`.

### 2.2 Probe-gated desktop WebView option (`zero://app`)

Reusing `apps/board/` (Native SDK `0.11.1`) as a desktop WebView host loading the same React 19 `@openboa/yogurt` bundle from `zero://app` is permitted **only after** passing a pre-implementation Native SDK `0.11.1` capability probe verifying:
1. System WebView availability and strict `zero://app` origin/CSP enforcement (no remote origins).
2. Bounded JS-to-Zig IPC keeping UDS socket access, `LOCAL_PEERPID` identity, and `0o600` `O_NOFOLLOW` `(company_id, principal_id)` `board-pending` persistence inside the native Zig layer rather than WebView storage.

## 3. Disposition of `apps/board/` v0 and preserved invariants

`origin/main` contains a minimal read-only Zig `0.16.0` / Native SDK `0.11.1` v0 skeleton under `apps/board/` (`main.zig`, `timeline.zig`, `pending.zig`, `app.native`, `prepare-ui.sh`, `app.json`, `dependencies.lock`). The target React 19 Client UI resolves `apps/board/` as follows:

| Artifact / Mechanism in `apps/board/` | Disposition | Reason & Target Replacement |
| --- | --- | --- |
| `src/app.native`, `prepare-ui.sh` (`--target native`), `dependencies.lock` (`yg-button`) | **Superseded** | `@openboa/yogurt` `0.2.0-local.1` removed `--target native` and supports `--target web` only; `app.native` renders only a single `"Refresh"` button with no command submission, retry/discard UI, or `pending_age`. Replaced by React 19 + `@openboa/yogurt` web bundle. |
| `src/main.zig` (`.gpu_surface`, Metal backend, `.js_window_api = false`) | **Superseded** | Replaced by `sourdough client-web` (primary) or probe-passed `zero://app` WebView host (optional). |
| `src/timeline.zig:66-133` per-click `client-bridge` / direct SQLite CLI spawns & `work-timeline-v1` pin | **Superseded** | Violates UDS-only access and live streaming; replaced by persistent UDS HTTP/1.1 + `work-timeline-v2` (`ETag`, `If-None-Match`, `event_seq`) + `GET /v1/events` NDJSON streaming. |
| `src/pending.zig:65` company-only pending path (`{base_dir}/{company_id}.json`) | **Superseded** | Under `(principal, operation)` receipt scoping, a company-only pending file left by Principal A causes `403` deadlock for Principal B. Replaced by `(company_id, principal_id)` keying + `403` stale-file quarantine. |
| `src/timeline.zig` **Data State vs Link State** model, `controls_locked`, `clearData()` triggers, and atomic parse swap | **100% Preserved** | Ported verbatim into the React client state store (see §6 and [present/spec.md](inspect/present/spec.md)). |
| `src/pending.zig` **8-variant `CommandLifecycle`**, `0o600` `O_NOFOLLOW` `fsync`, `payload_sha256`, and `not_found_retry_choice` | **100% Preserved + Extended** | Ported into the `client-web` / WebView bridge and `CommandOutboxPanel`, extending `LocalCommandIntent` from the 6-field Zig struct to `{company_id, principal_id, command_id, command_type, contract_version, preconditions, payload, payload_sha256, sent_ns}` so cold-start `Retry same ID` replays exact canonical preconditions and contract version (see [submit/spec.md](commands/submit/spec.md)). |

## 4. Seven-area domain-derived Information Architecture

The navigation architecture uses `SidebarLayout` derived strictly from Sourdough's 13 operational flows and domain boundaries—never from generic chat-workspace templates. The default root route is **`Overview & Alerts`**, not a conversation stream.

| # | IA Area | Primary Flows | Core Operational Questions Answered |
| --- | --- | --- | --- |
| **1** | **Overview & Alerts** *(Default)* | 01, 05, 11 | Is the company bound or `bootstrap_locked`? What restrictions (`RestrictionView`), incidents, or unresolved decisions are active right now? How fresh and complete is the current snapshot? |
| **2** | **Decisions & Governance** | 01, 06, 11 | Which reserved `DecisionQuestion`s (`owner_authority`, `clarification`, `provider_permission`) await human action? What is the current charter/mandate revision, and which `board_binding` / `role_assignment` grants exist? |
| **3** | **Work & Runs** | 02, 05, 10 | Which `work_items` are due, running, waiting, or blocked? Which `work_runs` hold active `fencing_token` leases, what `work_conditions` are armed, and what `ScopedRun` trace metadata and `shared_publication` evidence exist? |
| **4** | **Organization & Collaboration** | 06, 10 | Who is hired, in what `hire_onboarding` stage (`provisioning \| blocked \| ready \| accepted`), and under what `standing_grant`? Which non-assigning project discussions, blocking `work_peer_requests`, `work_handoff` offers, `shared_publication` excerpts, and offboarding custody handovers are active? |
| **5** | **Paper Fund & Risk** | 03, 04, 07, 08, 09 | Strict `environment == "paper"` boundary: which `PaperExecution` adapter, `BoundedVenueCoordinator` segment, and `paper_sender_lease` are active? What is the full `Duty -> Decision -> Order -> Fill -> Position` lineage (including `cancel != flat` open exposure), Two-Layer P&L (`Trading BOR` vs `Agent LLM Opex`, `Unavailable` on gaps), paper risk gate, and paired `ChallengerTrial` / `StrategyDeployment` state? |
| **6** | **Operations & Continuity** | 11, 12 | What WAL-normalized `sourdough-checkpoint-v2` cuts exist? Is a restored store in `restore quarantine` (`RecoveryReceipt` 5-dimension readiness)? Which review tools passed `pass^3` qualification, what is the `parent_asset_id` DAG quarantine blast radius, and what maintenance items are open? |
| **7** | **Offline Evaluations** | 13 | Physically separated read-only workspace over `offline-trial-registry-v1`: `Outcome` vs `Transcript` (`BlindedTrial`), `(AB, BA)` position-swapped `tie_noise`, `RsiTransferSummary` (`SUPPORTED \| UNSUPPORTED \| INCONCLUSIVE`), plus `[M3 Extension]` `AccessRecord` holdout locks and `parent_method_ids` method DAGs. |

### 4.1 Persistent Global Status & Safety Bar

Rendered identically across all seven IA areas:
- **Company & Environment Context**: Company selector, `company_id`, `store_key`, `service_incarnation_id` (`[M3 Extension]`), and explicit `environment: paper` badge (when the `paper` domain is installed).
- **Principal & Authority Chip**: Authenticated `principal_id` and effective role (`bootstrap_locked` | `board_viewer` | `board_control`).
- **Link & Freshness Telemetry**: `LinkState` (`connected` | `connecting` | `disconnected` | `incompatible`), server `as_of_ns` (UTC), projection `revision`, `event_seq`, and `controls_locked` lock chip.
- **Safety & Privacy Controls**: Active `RestrictionView` cause count pill (navigating directly to active restrictions) and an explicit **Lock Screen / Privacy Shield** trigger.

### 4.2 Persistent `CommandOutboxPanel` & `ControlledConfirmDialog`

- **`CommandOutboxPanel`**: Mounted globally whenever `command_state !== "idle"`, displaying `command_id`, `command_type`, elapsed `pending_age` (computed from `sent_ns`), `denial_code` (when `denied`), `"Check original request"` (`Receipt.onLookup`) **passed strictly when `command_state === "unknown"`**, and explicit **`Retry same ID`** / **`Discard draft`** buttons **strictly when `command_state === "not_found_retry_choice"`**.
- **`ControlledConfirmDialog`**: All state-mutating governance commands confirm through `ControlledConfirmDialog` built on `@ui-sdk/react/button` (`SDKButton`) rather than unwrapped action callbacks. It:
  1. Rejects automated invocation (`if (details?.source === "agent") return;`).
  2. Attaches `data-agent-confirm="<action>"` strictly on the confirm button (never on cancel or secondary navigation).
  3. Latches the composite preconditions (`charter_revision`, `expected_revision`, `restriction_revision`, `question_payload_hash`, etc.) displayed in the dialog body.
  4. Closes immediately via `onOpenChange(false)` upon human click and hands off lifecycle tracking (`submitting -> unknown | accepted | applied | denied | not_found_retry_choice`) to `CommandOutboxPanel`.

## 5. Role and principal visibility matrix

In `origin/main` (`6694fa9`), `serve-company-daemon` runs with `authorize_board=None` (failing closed `403` on all `POST /v1/commands`). Under `[M3 Extension]` `board_binding` and `role_assignment`, the UI gates surfaces and controls strictly by authenticated principal state:

| Surface / Capability | `bootstrap_locked` *(Unbound New Store)* | `board_viewer` *(Bound Read-Only Principal)* | `board_control` *(Bound Control Principal)* | `controls_locked` *(Disconnected / Pending Unknown / Stale)* | `host_locked` *(OS / Session Lock)* |
| --- | --- | --- | --- | --- | --- |
| **Projections (`GET /v1/meta`, `/v1/projections/*`, `/v1/events`)** | Company identity (`company_id`, `store_key`, `schema_version`) and bootstrap prompt only | Full authorized projections (`as_of_ns`, `revision`, `event_seq`) | Full authorized projections (`as_of_ns`, `revision`, `event_seq`) | Last verified `Data State` preserved under urgent Stale banner (`stale = true`) | **Concealed** behind opaque full-viewport Privacy Shield (`data-agent-hidden`); `as_of_ns` not mutated |
| **Operational Commands (`POST /v1/commands`)** | `register_owner` only | Hidden or disabled with explicit `"Read-only role (board_viewer)"` notice; daemon enforces `403` | Enabled iff `link_state === "connected" && !stale && scopeMatchesData() && command_state in {"idle","applied","denied"}` | **Fail-closed locked** (`controls_locked = true`) | Hidden + disabled (`data-agent-hidden`); requires unlock + fresh `GET /v1/meta` re-verification |
| **Private Desk Drafts (`.desk/drafts/*`)** | Never exposed | Never exposed (`private_desk.manifest_revision`, draft count, and offloaded `.desk/logs/` metadata only) | Never exposed | Never exposed | Never exposed |
| **Pending Outbox (`board-pending`)** | Empty | Read-only status if prior `(company_id, principal_id)` record exists | Scoped strictly to `(company_id, principal_id)`; `403` on startup quarantines/purges foreign stale file | Retains `LocalCommandIntent`; auto-queries `GET /v1/commands/{id}` on reconnect | Concealed behind shield without deleting persisted `board-pending` |
| **Auth Loss / Scope Switch (`401`, `403`, Principal / `company_id` / `store_key` change)** | N/A | Instant `clearData()` wipe | Instant `clearData()` wipe + in-flight cancellation (`clearInMemoryCommandState()`, preserving on-disk `board-pending` unless `403` stale quarantine) | Instant `clearData()` wipe | Instant `clearData()` wipe if underlying session revokes while locked |

## 6. Link state, data wipe, and host-lock privacy shield

Three failure/privacy states are strictly disjoint and must never be conflated:

1. **Transient Disconnect / Daemon Restart**: Triggers on UDS `ECONNREFUSED`, EOF, timeout, or `503`. Sets `link_state = "disconnected"`, `stale = true`, `controls_locked = true`, and keeps verified `Data State` visible beneath an urgent stale `Notice` showing elapsed disconnect time. Never calls `clearData()`.
2. **Authorization Loss (`401`/`403`) or Scope/Principal Switch**: Triggers on `401`, `403`, `IncompatibleTimeline`, or change of `company_id`, `store_key`, `socket`, or `principal_id`. Executes **instant zero-grace `clearData()`**, freeing cached projection stores, wiping all sensitive DOM nodes, and resetting in-memory command state while preserving valid on-disk `board-pending` (or quarantining stale `403` pending files on startup).
3. **Host / Session Lock (`host_locked = true`)**: Triggers on OS screen lock, host idle timeout, or operator privacy lock. Mounts an **opaque full-viewport Privacy Shield** (`data-agent-hidden`) over all financial, decision, work, and outbox DOM (unmounting or redacting protected subtrees). Crucially, host lock **does not** alter `data_as_of_ns`, set `stale = true`, or mutate `LinkState`. Unlocking requires OS/session verification plus a fresh `GET /v1/meta` and projection confirmation before lifting the shield.

## 7. Non-imitation and domain-grounding constraints

All screen structures and interactions derive strictly from Sourdough's 13 flows and authority invariants:

- **Overview-first, never chat-first**: The root route is `Overview & Alerts` showing charter/restriction/incident/freshness state, never an agent chat stream.
- **No workspace-peeking drawers**: Agents' active `.desk/drafts/` remain private (`agent_desk.py`). No drawer or pane inspects live private agent drafts; human visibility begins at `shared_publication` (`publish_selection`), `work_peer_requests`, `project_message`, or `ScopedRun` trace metadata.
- **No unconfirmed inline approval switches**: Every state-changing board action routes through `ControlledConfirmDialog` with explicit revision preconditions and `CommandOutboxPanel` receipt verification.
- **Honest unknowns and chart framing**: Missing financial/accounting coverage renders `"Unavailable"` / `"No reading"` (never `0` or `$0.00`). Time-series charts render percentage-return or cumulative-P&L-from-zero series (or explicit `baseline` framing with table deltas) so high-baseline account values are never visually flattened.

[Inspect authorized company state](inspect/intent.md) · [Request a governance change](commands/intent.md) · [Usage scenarios](scenarios.md)
