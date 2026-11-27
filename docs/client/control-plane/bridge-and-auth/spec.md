# Transport, loopback bridge, and role authorization

Design draft · Verification **UNRUN** · Responsible component: Control Plane & Host Bridge (`src/sourdough/ipc/`, `src/sourdough/board_control.py`, `src/sourdough/cli.py`, `storage/schema/v1/core/08_board_client.sql`)

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client build contract](../../build.md) · [Command submission](../../commands/submit/spec.md) · [Company Messages](../../governance/ceo-direct-line/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Define the local Unix domain socket (UDS) transport, bounded HTTP/1.1 framing, `ClientWebBridge` (`sourdough client-web`) loopback HTTP-to-UDS bridge security, `apps/board/` v0 disposition, canonical `08_board_client.sql` RBAC schema (`board_owner_challenge`, `board_binding`, `board_principal_binding`, `role_assignment`, `decision_question`), `AuthorizeBoard` (`src/sourdough/board_control.py`), fail-closed non-CEO 1:1 chat enforcement, and `(principal, operation)` receipt lookup scoping.

Input contracts: [Client architecture](../../architecture.md) · [Client build contract](../../build.md) · [Bind a principal](../../../company/systems/authority/authorize-actor/bind/spec.md) · [Current authorization](../../../company/systems/authority/authorize-actor/check/spec.md) · [Manage role definitions](../../../company/systems/authority/authorize-actor/manage-roles/spec.md). These are public service contracts, not imports of client UI packages.

## Design decision and review

### 1. Local UDS transport and bounded HTTP/1.1 framing

`CompanyDaemon` (`src/sourdough/ipc/server.py`, `src/sourdough/ipc/protocol.py`, `src/sourdough/ipc/client.py`) exposes `control-plane-v1` strictly over a local `AF_UNIX` `SOCK_STREAM` socket:

- **Socket path and private directory (`protocol.py`)**:
  - `SOCKET_PATH_LIMIT = 103` bytes (validated via `os.fsencode()`, rejecting empty paths, NUL bytes, or paths exceeding 103 bytes so macOS `sockaddr_un.sun_path` fits within 104 bytes including NUL).
  - Private socket directory enforced at `mode = 0o700`, opened via `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`, verifying `S_ISDIR`, `st_uid == os.geteuid()`, and `(st_mode & 0o077) == 0`.
- **`WriterLease`-first socket binding (`server.py`)**:
  - `CompanyDaemon.start()` acquires `hold_company_writer(store, company_id, ...)` (`WriterLease`) **before** inspecting or unlinking any socket path.
  - Socket bind runs under `_UMASK_LOCK` with `os.umask(0o177)` yielding `0o600`, re-verifying `S_ISSOCK`, `st_uid == os.geteuid()`, and `(st_mode & 0o077) == 0`, and unlinking owned sockets on cleanup.
- **Mutual kernel peer verification (`extract_peer_credentials`)**:
  - **macOS (`sys.platform`)**: `getsockopt(_SOL_LOCAL=0, _LOCAL_PEERCRED=0x001, 76)` + `getsockopt(_SOL_LOCAL=0, _LOCAL_PEERPID=0x002, 4)` (`pid`).
  - **Linux**: `getsockopt(SOL_SOCKET, _SO_PEERCRED=17, 12)` (`pid, uid, gid`).
  - Fails closed if `pid <= 0`, `uid < 0`, `gid < 0`, or `peer.uid != os.geteuid()`.
- **Bounded HTTP/1.1 framing and worker limits**:
  - Methods restricted to `GET` and `POST`; strictly `HTTP/1.1`; `Connection: close`, `Cache-Control: no-store`, `Content-Length`.
  - Limits: `MAX_REQUEST_LINE_BYTES = 4096`, `MAX_HEADER_COUNT = 64`, `MAX_HEADER_BYTES = 16384`, `MAX_BODY_BYTES = 65536`, `MAX_RESPONSE_BYTES = 4194304`, `MAX_EVENTS_BATCH = 500`, `_MAX_EVENT_WAITERS = 8`, `_MAX_WAIT_MS = 5000`.

### 2. Loopback HTTP-to-UDS bridge security (`ClientWebBridge`, `src/sourdough/ipc/web_bridge.py`)

`ClientWebBridge` (`sourdough client-web`, or integrated via `sourdough run-company ... --web-port <port>`) serves the compiled React 19 + `@openboa/yogurt` bundle (`--target web`) and proxies `/api/v1/*` to `CompanyDaemon` over UDS:

- **Loopback-only listener**: Binds strictly to `127.0.0.1:<port|ephemeral>` (never `0.0.0.0` or wildcard interfaces).
- **Strict `Host`, `Origin`, and `Sec-Fetch-Site` pinning**: Rejects any request with mismatched `Host` (`127.0.0.1:<port>` or `localhost:<port>`) or cross-origin `Origin` / `Sec-Fetch-Site` with `403`.
- **Per-response CSP style nonce & port-bound session cookie (`sd_sess_<port>_<instance_id>`)**: Serves `index.html` with a per-response CSP style nonce and authenticates requests via a port-bound `HttpOnly; SameSite=Strict` cookie (`sd_sess_<port>_<instance_id>`) or in-memory bearer token, rejecting URL query-parameter tokens (`?token=`) and never storing secrets in `localStorage` or `sessionStorage`.
- **`fetch()` NDJSON streaming (never `EventSource`)**: Browser clients consume `GET /v1/events` via `fetch()` `ReadableStream` / bounded long-polls so cookies/headers, structured `401`/`403`/`503` errors, and synchronous `clearData()` cancellation behave deterministically.
- **Bridge-owned `/api/local/pending` (`0o600` `O_NOFOLLOW` `board-pending` persistence & `403` quarantine)**:
  - `GET|PUT|POST|DELETE /api/local/pending` (`web_bridge.py:432-545`) writes `{company_id, principal_id, command_id, command_type, contract_version, preconditions, payload, label, sent_ns}` into an `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` (`0o700`) directory using atomic `O_CREAT | O_EXCL | O_NOFOLLOW` (`0o600`), `fsync(fd)`, and `fsync(dir_fd)`, keyed strictly by `(company_id, principal_id)`.
  - On startup recovery, if `GET /v1/commands/{command_id}?operation=...` returns `403` for a stale or foreign pending file, `ClientWebBridge` quarantines/purges the stale file cleanly so the active principal is never deadlocked.
- **Explicit `LOCAL_PEERPID` collapse classification**: `CompanyDaemon` classifies bridge-proxied UDS calls as `bridge_session`; `board_control` requires an active `board_binding` + `role_assignment('board_control')` + authenticated bridge session + human confirmation.

### 3. Disposition of `apps/board/` v0

The legacy `apps/board/` v0 native Zig (`0.16.0`) / Native SDK (`0.11.1`) directory is superseded by `apps/client/` (React 19 + `@openboa/yogurt` `0.2.0-local.1`) + `ClientWebBridge`. `apps/client/src/state/store.tsx`, `ClientWebBridge`, and `CommandOutboxPanel` preserve the Data/Link separation, `clearData()` wipe discipline, `0o600` `O_NOFOLLOW` `board-pending` persistence, and 8-variant `CommandLifecycle` invariants.

### 4. Canonical `v1/core` RBAC (`08_board_client.sql`), `AuthorizeBoard`, CEO-only chat & receipt scoping

- **Canonical `v1/core` tables (`src/sourdough/storage/schema/v1/core/08_board_client.sql`, owned by `board_control.py`):**
  - `board_binding` / `board_principal_binding`, `role_assignment` (`board_viewer` | `board_control`), `ceo_message`, and `decision_question` (`application_status IN ('pending', 'applied', 'declined', 'not_applicable', 'failed')`).
- **Binding and role semantics (`src/sourdough/board_control.py`):**
  - `bootstrap_locked`: Zero active `board_binding` rows for `company_id`. `GET /v1/meta` returns `board_access.bootstrap_locked = true` and `role = "bootstrap_locked"`. Only `register_owner` (`rbac-command-v1`) is permitted; all other operational projections/commands fail closed (`403`).
  - `board_viewer`: Permitted to read authorized `GET /v1/meta`, `GET /v1/projections/*`, `GET /v1/events`, and own `GET /v1/commands/{command_id}`; `POST /v1/commands` fails closed (`403`).
  - `board_control`: Active `board_binding` + active `role_assignment(role_name='board_control')` permitted to submit authorized commands.
- **`AuthorizeBoard` in Core (`src/sourdough/board_control.py`):** `AuthorizeBoard` lives in Core `board_control.py` (with zero dependency on Trading modules), verifying kernel `PeerCredentials`, `session_class`, active `board_binding`, and `role_assignment`.
- **Fail-closed non-CEO 1:1 chat:** `send_ceo_message` (`ceo-chat-command-v1`) requires `target_agent_id === preconditions.ceo_agent_id === ceo_agent_id`, failing closed (`400`/`403`) on any non-CEO target.
- **`GET /v1/commands/{command_id}?operation=<command_type>` scoping (`board_control.py:read_scoped_command_receipt`):** Authenticates caller `principal_id` and active `board_binding` before querying `command_receipt`, requires `?operation=<command_type>`, and enforces `(receipt.actor_id, receipt.command_type) == (principal_id, operation)` (`403` on mismatch, `404` when absent).

## Functional and quality requirements

- **FR.** Enforce `SOCKET_PATH_LIMIT <= 103`, `0o700` `O_NOFOLLOW` socket dirs, `WriterLease`-first `0o600` UDS binding, mutual `LOCAL_PEERCRED` / `SO_PEERCRED`, and bounded HTTP/1.1 framing.
- **FR.** Enforce `ClientWebBridge` `127.0.0.1` binding, `Host`/`Origin`/`Sec-Fetch-Site` pinning (`403`), port-bound `sd_sess_<port>_<instance_id>` cookie + CSP style nonce, `fetch()` NDJSON streaming, `(company_id, principal_id)` `0o600` `O_NOFOLLOW` `/api/local/pending` with `403` stale-file quarantine, and `bridge_session` classification.
- **FR.** Maintain `08_board_client.sql` tables in `DOMAIN_TABLE_OWNERS` (`board_control.py`), enforce `(principal_id, operation)` scoping on `GET /v1/commands/{command_id}?operation=...` before existence disclosure, and fail closed on non-CEO 1:1 chat targets.
- **NFR.** Never allow same-UID UDS access or DOM attributes to substitute for `board_binding` RBAC.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| UDS & bridge origin pinning | Connect from another UID or send an HTTP request to `ClientWebBridge` with mismatched `Host`, foreign `Origin`, `Transfer-Encoding`, or `?token=` query parameter. Confirm silent drop / `403` / `401` before any store query. |
| Concurrent bootstrap race | Launch two `register_owner` requests concurrently against a `bootstrap_locked` store. Confirm exactly one creates the active `board_binding` + `role_assignment('board_control')`, while the second fails closed without partial rows. |
| Cross-principal receipt leak closure | Principal A submits a command (`command_id = X`). Unbound/`bootstrap_locked` callers request `GET /v1/commands/X?operation=...` (`403`). Bound Principal B requests `GET /v1/commands/X?operation=...` (`403`), nonexistent `Y` (`404`), and without `?operation` (`400`). |
| `/api/local/pending` `403` quarantine & CEO-only gate | Leave a stale or foreign `board-pending` file, reconnect as Principal B, and confirm `403` triggers quarantine/purge without deadlocking. Submit `send_ceo_message` targeting a non-CEO `agent_id` and confirm `400`/`403` fail-closed rejection. |

**Oracle:** Inspect `src/sourdough/ipc/{protocol.py,server.py,client.py,web_bridge.py}`, `src/sourdough/board_control.py`, `src/sourdough/storage/schema/v1/core/08_board_client.sql`, `src/sourdough/storage/ownership.py`, and `src/sourdough/cli.py`.

## Operations and change review

Any update to `08_board_client.sql` RBAC tables must keep `verify_v1_schema` and `DOMAIN_TABLE_OWNERS` synchronized, preserving `application_status IN ('pending', 'applied', 'declined', 'not_applicable', 'failed')`.
