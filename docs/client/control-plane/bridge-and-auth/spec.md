# Transport, loopback bridge, and role authorization

Design draft · Verification **UNRUN** · Responsible component: Control Plane & Host Bridge (`src/sourdough/ipc/`, `src/sourdough/cli.py`, `storage/schema/v1/core/`)

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client build contract](../../build.md) · [Command submission](../../commands/submit/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Define the local Unix domain socket (UDS) transport, bounded HTTP/1.1 framing, `sourdough client-web` loopback HTTP-to-UDS bridge security, Native SDK `0.11.1` / `apps/board/` disposition, canonical `board_binding` and `role_assignment` RBAC schema and admission rules, single-UID `principal_id` derivation, CLI daemon wiring, and `(principal, operation)` receipt lookup scoping.

Input contracts: [Client architecture](../../architecture.md) · [Client build contract](../../build.md) · [Bind a principal](../../../company/systems/authority/authorize-actor/bind/spec.md) · [Current authorization](../../../company/systems/authority/authorize-actor/check/spec.md) · [Manage role definitions](../../../company/systems/authority/authorize-actor/manage-roles/spec.md). These are public service contracts, not imports of client UI packages.

## Design decision and review

### 1. Local UDS transport and bounded HTTP/1.1 framing (`[Live origin/main]`)

`CompanyDaemon` (`src/sourdough/ipc/server.py`, `src/sourdough/ipc/protocol.py`, `src/sourdough/ipc/client.py`) exposes `control-plane-v1` strictly over a local `AF_UNIX` `SOCK_STREAM` socket:

- **Socket path and private directory (`protocol.py`)**:
  - `SOCKET_PATH_LIMIT = 103` bytes (validated via `os.fsencode()`, rejecting empty paths, NUL bytes, or paths exceeding 103 bytes so macOS `sockaddr_un.sun_path` fits within 104 bytes including NUL).
  - Default socket directory `~/Library/Caches/Sourdough/s`; default socket filename `<sha256(f"{company_id}:{dev}:{ino}")[:16]>.sock`.
  - `verify_private_socket_dir` enforces `mode = 0o700`, opens via `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`, and verifies `S_ISDIR`, `st_uid == os.geteuid()`, and `(st_mode & 0o077) == 0`.
- **`WriterLease`-first socket binding (`server.py`)**:
  - `CompanyDaemon.start()` acquires `hold_company_writer(store, company_id, ...)` (`WriterLease`) **before** inspecting or unlinking any socket path.
  - If a socket path exists, it verifies `S_ISSOCK` and `st_uid == os.geteuid()`, probes `_probe_socket_is_active` (failing closed on `EAGAIN` / `EINPROGRESS` / `EACCES`), and unlinks stale sockets via `dir_fd`.
  - Socket bind runs under `_UMASK_LOCK` with `os.umask(0o177)` yielding `0o600`, re-verifies `S_ISSOCK`, `st_uid == os.geteuid()`, and `(st_mode & 0o077) == 0`, and registers `_unlink_owned_socket` keyed by `(st_dev, st_ino)` on cleanup. Client connections (`verify_socket_path`) open the parent directory with `O_NOFOLLOW`, `lstat` via `dir_fd`, and require `S_ISSOCK` and `0o600` user ownership.
- **Mutual kernel peer verification (`extract_peer_credentials`)**:
  - **macOS (`sys.platform`)**: `getsockopt(_SOL_LOCAL=0, _LOCAL_PEERCRED=0x001, 76)` (`xucred`, checking `version == 0` and `1 <= ngroups <= 16`) + `getsockopt(_SOL_LOCAL=0, _LOCAL_PEERPID=0x002, 4)` (`pid`).
  - **Linux**: `getsockopt(SOL_SOCKET, _SO_PEERCRED=17, 12)` (`pid, uid, gid`).
  - Both sides fail closed if `pid <= 0`, `uid < 0`, `gid < 0`, or `peer.uid != os.geteuid()` (server drops mismatched UID connections without writing a response; client raises `IpcRefused`).
- **Bounded HTTP/1.1 framing and worker limits**:
  - Methods restricted to `GET` and `POST`; request targets must start with `/` (never `//`, no `#`, no control characters or newlines); strictly `HTTP/1.1`.
  - Limits: `MAX_REQUEST_LINE_BYTES = 4096`, `MAX_HEADER_COUNT = 64`, `MAX_HEADER_BYTES = 16384` (16 KiB), `MAX_BODY_BYTES = 65536` (64 KiB), `MAX_RESPONSE_BYTES = 4194304` (4 MiB), `MAX_EVENTS_BATCH = 500`.
  - Reject `Transfer-Encoding` headers, duplicate `Content-Length` headers, non-digit or oversized `Content-Length`, duplicate `Idempotency-Key` headers, body bytes without `Content-Length`, and trailing bytes beyond `Content-Length`.
  - Every response sets `Connection: close`, `Cache-Control: no-store`, and `Content-Length` (empty body on `304 Not Modified`, `{"error": "<message>"}` JSON on error responses).
  - Concurrency bounds: `_MAX_WORKERS = 16`, `_MAX_IN_FLIGHT = 32` (`503` when exhausted), `_MAX_EVENT_WAITERS = 8` (`503` when exhausted), `_MAX_WAIT_MS = 5000`, `request_timeout = 10.0s`.

### 2. Loopback HTTP-to-UDS bridge security (`sourdough client-web`, `[M3 Extension]`)

`sourdough client-web` is the primary standalone host serving the React 19 + `@openboa/yogurt` web bundle (`--target web`) and bridging browser HTTP requests to `CompanyDaemon`'s UDS socket:

- **Loopback-only listener**: Bind strictly to `127.0.0.1:<ephemeral>` (never `0.0.0.0` or wildcard interfaces).
- **Strict `Host`, `Origin`, and `Sec-Fetch-Site` pinning**: Every incoming HTTP request must match the exact bound `Host` (`127.0.0.1:<port>` or `localhost:<port>`) and same-origin `Origin` / `Sec-Fetch-Site` (`same-origin` or `none` for initial top-level document navigation); any mismatch or DNS-rebinding attempt fails closed with `403`.
- **Session authentication — Mode 3b preferred / Mode 3a port-bound cookie**:
  - **Preferred — Mode 3b (out-of-band in-memory `Authorization: Bearer`)**: Generate a per-launch CSPRNG bearer token injected out-of-band into host memory (stdin, dedicated file descriptor, or `0o600` ephemeral bootstrap file removed after read) and held strictly in JavaScript memory, attached as `Authorization: Bearer <token>` on API/stream requests. Because JS memory is isolated to the exact `(scheme, host, port)` origin, Mode 3b is inherently port-scoped.
  - **Conditional — Mode 3a (`HttpOnly; SameSite=Strict` cookie)**: Because browser cookies on `127.0.0.1` are host-scoped rather than port-scoped, a second listener on `127.0.0.1:<other_port>` could otherwise receive or overwrite an unbound cookie. Whenever Mode 3a is used, the bridge **MUST** bind both the **cookie name** (`sd_sess_<ephemeral_port>_<instance_id>`) and the **MAC-authenticated cookie payload** (`HMAC(bridge_secret, f"{ephemeral_port}:{instance_id}:{principal_id}:{session_nonce}")`) to `(ephemeral_port, instance_id)` and validate `(ephemeral_port, instance_id)` server-side on every request, rejecting mismatches with `401`.
  - **Hard prohibition**: Never pass tokens in URL query parameters (`?token=`), `window.location.hash`, `localStorage`, or `sessionStorage`.
- **`fetch()` NDJSON streaming (never `EventSource`)**: Browser clients consume `GET /v1/events` strictly via `fetch()` `ReadableStream` or bounded `fetch()` long-polls (`credentials: "same-origin"` or `Authorization: Bearer`) so custom headers, structured `401`/`403`/`503` JSON errors, and synchronous `clearData()` cancellation behave deterministically. `EventSource` is prohibited.
- **Bridge-owned `(company_id, principal_id)` `0o600` `board-pending` persistence and `403` quarantine**:
  - The bridge (not browser storage) writes `LocalCommandIntent` (`{company_id, principal_id, command_id, command_type, contract_version, preconditions, payload, payload_sha256, sent_ns}`) into an `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` (`0o700`) directory using `O_CREAT | O_EXCL | O_NOFOLLOW` (`0o600`), `fsync(fd)`, atomic rename, and `fsync(dir_fd)`.
  - File path is keyed strictly by `(company_id, principal_id)`: `{base_dir}/{company_id}/{principal_id_sha256}.json` (superseding `pending.zig:65` company-only naming).
  - On startup recovery, if `GET /v1/commands/{command_id}?operation=...` returns `403` for a stale or unowned pending file (e.g., revoked binding or foreign leftover file), the bridge quarantines/purges the stale file (moving it to a `0o700` quarantine directory or unlinking after logging an explicit security notice) so the active principal is never deadlocked.
- **Explicit `LOCAL_PEERPID` collapse classification**:
  - Because `sourdough client-web` terminates browser HTTP and opens UDS connections to `CompanyDaemon`, kernel `LOCAL_PEERPID` / `SO_PEERCRED` collapses all connected browser tabs onto the bridge process PID.
  - `CompanyDaemon` MUST type UDS connections originating from `client-web` as `session_class = "bridge_session"` (bridge-proxied local session): unbound or unconfirmed sessions default to `board_viewer`, exercising `board_control` requires an active `board_binding` + `role_assignment('board_control')` + authenticated bridge session + bridge-forwarded confirmation, and a `bridge_session` MUST NEVER assert native hardware signer status or `presence_evidence_ref`.

### 3. Native SDK `0.11.1` / `zero://app` probe verdict and `apps/board/` disposition

- **Host capability probe verdict (recorded 2026-11-25)**: Probing the workspace host (`command -v native`, `which native`, `/opt/homebrew/bin/native`, `/usr/local/bin/native`, `~/.local/bin/native`, `~/.cargo/bin/native`, `brew list`, `command -v zig`, `NATIVE_SDK_PATH`) confirmed `native` (`0.11.1`) and `zig` (`0.16.0`) are **absent** (`NATIVE_SDK_PATH` unset). Consequently, `zero://app` system WebView rendering and JS-to-Zig IPC remain **unavailable / unprobed**, and **`sourdough client-web` is locked as the primary M3 host**. Any future `zero://app` desktop shell remains strictly probe-gated per [Client build contract](../../build.md).
- **Exact file-by-file disposition of `apps/board/` (9 files; `build.zig` and `build.zig.zon` are absent)**:

| File in `apps/board/` | Audited content | M3 Disposition | Target replacement / preserved invariant |
| --- | --- | --- | --- |
| `apps/board/.gitignore` | Excludes `.native/`, `.zig-cache/`, `zig-out/`, `src/yogurt/`, `assets/` | **Keep / archive with `apps/board/`** | No runtime role in `client-web`. |
| `apps/board/README.md` | Zig `0.16.0` / Native SDK `0.11.1` / `prepare-ui.sh` instructions | **Update deprecation note** | Point operators to `sourdough client-web` and React 19 `@openboa/yogurt` build in [Client build contract](../../build.md). |
| `apps/board/app.json` | Native SDK manifest (`allowed_origins: ["zero://app", "zero://inline"]`, `web_engine: "system"`) | **Probe-gated** | Unusable until Native SDK `0.11.1` passes capability probe. |
| `apps/board/dependencies.lock` | `NATIVE_VERSION=0.11.1`, `ZIG_VERSION=0.16.0`, pre-`0.2.0` Yogurt pin | **Superseded** | Replaced by React 19 `package.json` + lockfile (`@openboa/yogurt` `0.2.0-local.1`, `@ui-sdk/react` `0.1.0-beta.13`). |
| `apps/board/prepare-ui.sh` | Runs `yogurt.mjs add button --target native` | **Superseded** | Incompatible with `@openboa/yogurt` `0.2.0-local.1` (which removed `--target native` for `--target web`). |
| `apps/board/src/app.native` | Single `"Refresh"` `yg-button` read-only template (`35` lines) | **Superseded** | Replaced by React 19 `@openboa/yogurt` 7-area IA screens, `ControlledConfirmDialog`, and `CommandOutboxPanel`. |
| `apps/board/src/main.zig` | Metal `.gpu_surface`, `.js_window_api = false` (`78` lines) | **Superseded** | Replaced by `sourdough client-web` (or future probe-passed `zero://app` WebView host). |
| `apps/board/src/pending.zig` | 8-variant `CommandLifecycle`, `0o600` `O_NOFOLLOW` `fsync`, `payloadSha256Hex`, `pendingFilePath` at `line 65` (`{base_dir}/{company_id}.json`) | **Invariants 100% preserved + extended; `line 65` superseded** | Preserve 8-state `CommandLifecycle` and `0o600` `O_NOFOLLOW` `fsync` persistence in `client-web`; extend 6-field `PendingCommand` to 9-field `LocalCommandIntent` (`principal_id`, `contract_version`, full `preconditions`) keyed by `{base_dir}/{company_id}/{principal_id_sha256}.json`. |
| `apps/board/src/timeline.zig` | **Data State vs Link State**, `clearData()`, `controls_locked`, atomic parse swap, cold-start recovery (`983` lines); `lines 66-133` spawn `client-bridge` CLI | **Invariants 100% preserved; `lines 66-133` superseded** | Port Data/Link separation, `clearData()`, `controls_locked`, and cold-start receipt recovery verbatim into React client store; replace per-click CLI subprocesses and `work-timeline-v1` pin with UDS HTTP/1.1 + `work-timeline-v2` + `GET /v1/events`. |

### 4. Canonical `v1/core` RBAC schema, `principal_id` derivation, CLI wiring, and receipt scoping (`[M3 Extension]`)

**Concern for the responsible component:** Same-UID `LOCAL_PEERCRED` proves only OS user equality, not which human or session principal is acting or whether a role has been delegated, replaced, or revoked.

- **Canonical `v1/core` SQLite schema (`storage/schema/v1/core/` & `storage/ownership.py`)**:
  - `origin/main` has zero `board_binding` or `role_assignment` tables and runs `serve-company-daemon` with `authorize_board=None` (failing closed `403` on all `POST /v1/commands`).
  - M3 adds canonical `STRICT` DDL inside `storage/schema/v1/core/` (registered in `verify_v1_schema` so `PRAGMA user_version == 1`, `pack_sha256`, and `sqlite_schema_sha256` remain canonical, and registered in `storage/ownership.py` `DOMAIN_TABLE_OWNERS` under a dedicated authority module so `audit_domain_write_ownership` passes):
    1. `board_owner_challenge(challenge_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, purpose TEXT NOT NULL CHECK (purpose IN ('bootstrap')), service_incarnation_id TEXT NOT NULL, nonce_digest TEXT NOT NULL, expires_at_ns INTEGER NOT NULL, consumed_at_ns INTEGER)` (`STRICT`).
    2. `board_binding(company_id TEXT NOT NULL, principal_id TEXT NOT NULL, binding_revision INTEGER NOT NULL, issuer_kind TEXT NOT NULL, subject_ref TEXT NOT NULL, session_class TEXT NOT NULL CHECK (session_class IN ('direct_uds', 'bridge_session')), state TEXT NOT NULL CHECK (state IN ('active', 'revoked')), created_at_ns INTEGER NOT NULL, revoked_at_ns INTEGER, PRIMARY KEY (company_id, principal_id))` (`STRICT`).
    3. `role_assignment(assignment_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, principal_id TEXT NOT NULL, role_name TEXT NOT NULL CHECK (role_name IN ('board_viewer', 'board_control')), role_revision INTEGER NOT NULL, scope_ref TEXT NOT NULL, delegation_limit INTEGER NOT NULL DEFAULT 0, assigned_by TEXT NOT NULL, expires_at_ns INTEGER, created_at_ns INTEGER NOT NULL, revoked_at_ns INTEGER)` (`STRICT`, indexed on `(company_id, principal_id, revoked_at_ns)`).
- **Binding and role semantics**:
  - `bootstrap_locked`: Zero active `board_binding` rows for `company_id`. `GET /v1/meta` returns `binding_state = "bootstrap_locked"`, `role = "bootstrap_locked"`. Only `register_owner` (`OwnerChallenge` + `OwnerProof`) is permitted; all operational projections and commands fail closed (`403`). Two concurrent `register_owner` attempts serialize under `WriterLease` + `BEGIN IMMEDIATE`: the first atomically consumes `board_owner_challenge`, inserts `board_binding(binding_revision=1, state='active')` and `role_assignment(role_name='board_control', role_revision=1, delegation_limit=1)`, and emits `audit_event`; the second fails closed (`409` / `denied`).
  - `board_viewer`: Active `board_binding` + active `role_assignment(role_name='board_viewer')` covering the requested `scope_ref`. Permitted to read authorized `GET /v1/meta`, `GET /v1/projections/*`, `GET /v1/events`, and own `GET /v1/commands/{command_id}`; all `POST /v1/commands` fail closed with `403`.
  - `board_control`: Active `board_binding` + active `role_assignment(role_name='board_control')` covering the operation and `scope_ref` (and `delegation_limit > 0` for `bind_board_principal`, `assign_role`, `replace_role`, `revoke_role`). Flat, non-inherited role revisions; editing or replacing an assignment increments `role_revision` and invalidates stale cached expectations.
- **Single-UID `principal_id` derivation (`AuthorizeBoard` generalization)**:
  - `origin/main` `AuthorizeBoard = Callable[[Connection, str, str], str | None]` (`paper_risk.py:32`) takes only `(connection, company_id, operation)` and ignores `PeerCredentials` and read scopes. M3 generalizes `AuthorizeBoard` to accept `(connection, company_id, peer_context, operation_or_scope)` where `peer_context` carries kernel `PeerCredentials(uid, gid, pid)`, `session_class` (`"direct_uds"` | `"bridge_session"`), and verified session/local-principal assertion:
    1. **Direct UDS caller (`session_class = "direct_uds"`, e.g. CLI / `client-bridge`)**: `CompanyDaemon` verifies `peer.uid == os.geteuid()` and `peer.pid > 0`. The caller identifies its enrolled `principal_id` (via signed local principal credential / host-verified local binding token or single active `direct_uds` local owner binding when unambiguously enrolled for that UID), which MUST match an active `board_binding(state='active', session_class='direct_uds')`. A raw principal header or same-UID socket without an active `board_binding` NEVER grants `board_viewer` or `board_control`.
    2. **Loopback bridge caller (`session_class = "bridge_session"`, `sourdough client-web`)**: The bridge first authenticates the browser request via Mode 3b `Authorization: Bearer` or Mode 3a `(ephemeral_port, instance_id)`-bound cookie to resolve the session's bound `principal_id`, and forwards the authenticated `principal_id` over its `0o600` UDS connection. To prevent another same-UID local process (`direct_uds`) from spoofing a `bridge_session` `principal_id` header over UDS, `serve-company-daemon` / `sourdough client-web` **MUST** authenticate the bridge-to-daemon UDS session via a per-launch out-of-band bridge UDS secret (e.g., `0o600` ephemeral secret / pipe handoff verified on UDS requests) or an attested child/sibling `bridge_pid` (`peer.pid == attested_bridge_pid` when paired/spawned with the daemon). `CompanyDaemon` verifies `peer.uid == os.geteuid()` plus the bridge UDS authentication, classifies the caller strictly as `bridge_session` (never asserting `direct_uds` or `presence_evidence_ref`), checks active `board_binding(state='active')` + active `role_assignment`, and defaults unprivileged/unconfirmed sessions to `board_viewer` (never auto-elevating).
- **CLI wiring (`src/sourdough/cli.py`)**:
  - Update `serve-company-daemon` (`cli.py:501-509`) to pass the canonical `board_binding`/`role_assignment`-backed `authorize_board` callback by default (closing the `authorize_board=None` gap while preserving an explicit read-only flag when requested).
  - Add `sourdough client-web` subcommand launching the loopback HTTP-to-UDS bridge (`127.0.0.1:<ephemeral>`, Mode 3b/3a auth, `(company_id, principal_id)` `board-pending`).
  - Add `sourdough authorize` (or `board`) subcommands (`issue-owner-challenge`, `bind-principal`, `assign-role`, `replace-role`, `revoke-role`) for local operator enrollment without direct SQLite writes by the UI, and extend `client-bridge` (`cli.py:262-284`) with `submit-command` parity for headless scripted UDS testing.
- **Closing the `GET /v1/commands/{command_id}` cross-principal leak (`server.py:663-695`)**:
  - In `origin/main`, `_handle_read_command` calls `read_local_command_receipt_status` (`paper_risk.py:511-588`), which checks only `st_uid == os.geteuid()` and optional `operation` without invoking `authorize_board` or checking `principal_id`, allowing any same-UID caller to probe existence (`200` vs `404`) and read another principal's receipt.
  - **M3 fix**:
    1. Authenticate caller `principal_id` and verify active `board_binding` + read-receipt authority **before** querying `command_receipt` (if unbound or `bootstrap_locked`, fail closed with `403` before existence disclosure).
    2. Require non-empty query parameter `?operation=<command_type>` (`400` if missing or malformed).
    3. Delegate lookup to `storage/kernel.py:read_command_receipt(connection, company_id=..., command_id=..., principal=principal_id, operation=operation)` (`storage/kernel.py:314-329`), which returns `None` (`404` `not_found`) when `command_id` does not exist and raises `CommandUnauthorized` (`403`) whenever `(receipt.actor_id, receipt.command_type) != (principal_id, operation)`, closing both cross-principal reads and cross-operation probing (with `uuid4()` `command_id` entropy mitigating bound-principal probing; optionally enable uniform `403`/`404` masking in `read_command_receipt` if strict ID-existence hiding among bound principals is desired).

## Functional and quality requirements

- **FR.** Enforce `SOCKET_PATH_LIMIT <= 103`, `0o700` `O_NOFOLLOW` socket dirs, `WriterLease`-first `0o600` UDS binding, mutual `LOCAL_PEERCRED` / `SO_PEERCRED`, and bounded HTTP/1.1 framing.
- **FR.** Enforce `sourdough client-web` `127.0.0.1:<ephemeral>` binding, `Host`/`Origin`/`Sec-Fetch-Site` pinning (`403`), Mode 3b bearer or Mode 3a `(ephemeral_port, instance_id)`-bound cookie, `fetch()` NDJSON streaming, `(company_id, principal_id)` `0o600` `O_NOFOLLOW` `board-pending` with `403` stale-file quarantine, and `bridge_session` `LOCAL_PEERPID` classification.
- **FR.** Store `board_owner_challenge`, `board_binding`, and `role_assignment` in canonical `v1/core` SQLite tables registered in `DOMAIN_TABLE_OWNERS`, wire `authorize_board` into `serve-company-daemon`, and enforce `(principal_id, operation)` scoping on `GET /v1/commands/{command_id}?operation=...` before existence disclosure.
- **NFR.** Keep `apps/board/` deprecation/invariant mapping explicit, keep `zero://app` strictly gated behind an absent-until-probed Native SDK `0.11.1` check, and never allow same-UID UDS access or DOM attributes to substitute for `board_binding` RBAC.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| UDS & bridge origin pinning | Connect from another UID or send an HTTP request to `client-web` with mismatched `Host`, foreign `Origin`, `Transfer-Encoding`, or an unbound cookie from another `127.0.0.1` port. Confirm silent drop / `403` / `401` before any store query. |
| Concurrent bootstrap race | Launch two `register_owner` requests concurrently against a `bootstrap_locked` store using `OwnerChallenge`. Confirm exactly one creates `board_binding(binding_revision=1)` + `role_assignment('board_control')`, while the second fails closed without partial rows. |
| Cross-principal receipt leak closure | Principal A submits a command (`command_id = X`). Unbound/`bootstrap_locked` callers request `GET /v1/commands/X?operation=...` -> confirm `403` pre-query. Bound Principal B (`board_viewer` or another `board_control`) requests `GET /v1/commands/X?operation=...`, a nonexistent `uuid4()` ID `Y`, and without `?operation`. Confirm `403` (`CommandUnauthorized`) for foreign `X`, `404` (`not_found`) for nonexistent `Y` per `storage/kernel.py:314-329` (or uniform masked `403`/`404` if strict bound-principal ID hiding is enabled), never `200`, and `400` when `?operation` is omitted. |
| `board-pending` `403` quarantine | Leave a stale or revoked-principal `board-pending` file, reconnect as Principal B or revoked Principal A, and confirm `403` triggers quarantine/purge with a security notice rather than permanent UI deadlock. |

**Oracle:** Inspect `src/sourdough/ipc/{protocol.py,server.py,client.py}`, `src/sourdough/storage/schema/v1/core/*.sql`, `src/sourdough/storage/ownership.py`, `src/sourdough/storage/kernel.py`, and `src/sourdough/cli.py`, verified against `verify_v1_schema` and `audit_domain_write_ownership`.

## Operations and change review

Record the Native SDK `0.11.1` host probe verdict whenever evaluating desktop WebView hosting. Any update to `v1/core` RBAC tables must keep `verify_v1_schema` and `DOMAIN_TABLE_OWNERS` synchronized.
