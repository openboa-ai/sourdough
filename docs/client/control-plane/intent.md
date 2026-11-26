# Serve the local control plane

Design draft · [Board Client](../README.md) · [Client architecture](../architecture.md) · [System map](../../README.md) · [Development lifecycle](../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../capabilities.md#mandate).

## Operating need

An operator or authorized viewer needs a bounded, local-only Unix domain socket (UDS) service contract (`CompanyDaemon`) and authenticated host bridge (`ClientWebBridge` via `sourdough client-web`) to inspect company projections, follow live state transitions, converse 1:1 exclusively with the Founder/CEO, issue explicitly confirmed governance and domain-gated Trading commands, and enforce 3-tier execution environment isolation without direct SQLite access, per-click CLI spawns, or client-resident credentials.

## Observable outcome

`CompanyDaemon` exposes a single-writer-guarded local UDS HTTP/1.1 + JSON + NDJSON Control Plane (`control-plane-v1`, `18` total `/v1/*` routes) enforcing kernel peer verification (`LOCAL_PEERCRED` / `LOCAL_PEERPID` / `SO_PEERCRED`), `board_binding` and `role_assignment` RBAC, snapshot-consistent `GET /v1/meta` and `14` `GET /v1/projections/*` endpoints (`15` contract revisions across Core and `TradingDomainPlugin` projections, with `ETag` / `If-None-Match` / `X-Sourdough-Event-Seq`), live `GET /v1/events` NDJSON streaming wired to `ResidentCompanyService` commits, idempotent `(principal, operation)`-scoped `POST /v1/commands` and `GET /v1/commands/{command_id}` (`12` commands: `8` Core + `4` Trading plugin gated `422` when absent), strict CEO-only 1:1 messaging (`send_ceo_message`), CEO-delegated offboarding read-only custody (`/reports`, zero Board `retire_employee` write), and strict 3-tier execution boundaries (`agent_sandbox`, `company_paper`, Board-gated `company_live`). Company operation, trading, reconciliation, and recovery remain completely functional when the client or web bridge is stopped.

## Scope of discretion

- **Transport, host bridge, and RBAC**: Define UDS framing limits, `ClientWebBridge` loopback HTTP-to-UDS bridge security (`127.0.0.1`, `Host`/`Origin`/`Sec-Fetch-Site` pinning, port-bound `sd_sess_<port>_<instance_id>` cookie, `/api/local/pending` `0o600` `O_NOFOLLOW` persistence), `LOCAL_PEERPID` collapse classification, `apps/board/` v0 disposition, fail-closed non-CEO 1:1 chat enforcement, and canonical `v1/core` `board_binding` / `role_assignment` admission.
- **Read projections**: Define `GET /v1/meta` capability negotiation (`active_domains`, `active_restrictions_count`) and all `14` implemented `GET /v1/projections/*` endpoints (`15` contract revisions: `work-timeline` `v1`/`v2`, `10` Core projections with conditional Trading enrichment, and `3` Trading domain projections) over `open_reader()`, preserving single-`read_tx` anti-tear snapshots and missing-data honesty (`"Unavailable"` / `"No reading"`).
- **Events and commands**: Define `GET /v1/events` redaction and `ResidentCompanyService` + `CompanyDaemon.notify_commit` wakeups, `audit_event` emissions, `12` implemented `POST /v1/commands` operations (`8` Core + `4` Trading plugin), `TradingRestrictionGuard` (`<= 15s` quote freshness), and `GET /v1/commands/{command_id}?operation=...` contracts.
- **3-Tier Execution Environment Boundary**: Isolate Tier 1 `agent_sandbox`, Tier 2 `company_paper`, and Board-gated Tier 3 `company_live` behind `execution_environment_binding` + unified `trading_book_{order,fill,position,valuation}` keyed by composite `(binding_id, environment)` FK, `positions_copied = 0`, `sender_lease` (`.writer-lock`), `IndependentProcessGuard`, and strict `404`/`422` exclusion when `"trading"` is not installed.
- **Strict isolation**: Keep UI packages, React build dependencies, and presentation components out of Python/SQLite modules; service contracts depend only on domain owners and SQLite schemas, never on client implementation.

## Development changes

- [Transport, loopback bridge, and role authorization](bridge-and-auth/spec.md)
- [Meta and authorized read projections](projections/spec.md)
- [Event stream, runtime wakeups, and typed commands](commands-and-events/spec.md)
- [3-tier execution environment boundary](paper-boundary/spec.md)
