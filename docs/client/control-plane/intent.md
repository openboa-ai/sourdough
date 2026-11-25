# Serve the local control plane

Design draft · [Board Client](../README.md) · [Client architecture](../architecture.md) · [System map](../../README.md) · [Development lifecycle](../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../capabilities.md#mandate).

## Operating need

An operator or authorized viewer needs a bounded, local-only Unix domain socket (UDS) service contract (`CompanyDaemon`) and authenticated host bridge (`sourdough client-web`) to inspect company projections, follow live state transitions, converse 1:1 exclusively with the Founder/CEO, issue explicitly confirmed governance and risk commands, and verify paper-only execution boundaries without direct SQLite access, per-click CLI spawns, or client-resident credentials.

## Observable outcome

`CompanyDaemon` exposes a single-writer-guarded local UDS HTTP/1.1 + JSON + NDJSON Control Plane (`control-plane-v1`) enforcing kernel peer verification (`LOCAL_PEERCRED` / `LOCAL_PEERPID` / `SO_PEERCRED`), `board_binding` and `role_assignment` RBAC, snapshot-consistent `GET /v1/meta` and `GET /v1/projections/*` reads (`ETag` / `If-None-Match` / `X-Sourdough-Event-Seq`), live `GET /v1/events` NDJSON streaming wired to runtime commits, idempotent `(principal, operation)`-scoped `POST /v1/commands` and `GET /v1/commands/{command_id}` lifecycle receipts, strict CEO-only 1:1 messaging (`send_ceo_message`), CEO-delegated offboarding read-only custody (`offboarding-v1`, zero Board `retire_employee` write), and a swappable `environment == "paper"` execution boundary (`PaperDestination` / `PaperExecution`). Company operation, paper trading, reconciliation, and recovery remain completely functional when the daemon control-plane client or host bridge is stopped.

## Scope of discretion

- **Transport, host bridge, and RBAC**: Define UDS framing limits, loopback HTTP-to-UDS bridge security (`127.0.0.1:<ephemeral>`, `Host`/`Origin` pinning, port-bound cookie or in-memory bearer auth, `(company_id, principal_id)` `0o600` `board-pending`), `LOCAL_PEERPID` collapse classification, Native SDK `0.11.1` probe status and `apps/board/` disposition, fail-closed non-CEO 1:1 chat enforcement, and canonical `v1/core` `board_binding` / `role_assignment` admission.
- **Read projections**: Define `GET /v1/meta` capability negotiation and all **`31` concrete `GET /v1/projections/*` schemas** (`35` `/v1/*` endpoints total, adding `floor-map-v1` and `ceo-thread-v1`, plus `?since_seq=` `Since Last Visit` on `company-overview-v1`) over `open_reader()` (`company.sqlite` and physically separated Flow 13 `offline-trial-registry-v1`), preserving single-`read_tx` anti-tear snapshots, live-quote vs sealed-valuation honesty, and `LIVE_UDS` / `V1_SQLITE_NEEDS_UDS` / `BACKEND_GAP #1–#8` provenance tags.
- **Events and commands**: Define `GET /v1/events` redaction and post-commit runtime wakeup hooks across all 15 silent domain/runtime areas, `Interrupt` / `Approval` / `Digest` routing classification, plus the generalized `POST /v1/commands` and `GET /v1/commands/{command_id}?operation=...` contract registry (including `send_ceo_message` and excluding Board `retire_employee` writes).
- **Paper boundary**: Lock trading endpoints, commands, and telemetry strictly to `environment == "paper"` behind `PaperDestination` / `PaperExecution`, `paper_sender_lease`, `BoundedVenueCoordinator`, and `paper_binding_change` drain preconditions, distinguishing sealed `paper_valuation` snapshots from missing live intraday quote feeds (`BACKEND_GAP #5`).
- **Strict isolation**: Keep UI packages, React build dependencies, and presentation components out of Python/SQLite modules; service contracts depend only on domain owners and SQLite schemas, never on client implementation.

## Development changes

- [Transport, loopback bridge, and role authorization](bridge-and-auth/spec.md)
- [Meta and authorized read projections](projections/spec.md)
- [Event stream, runtime wakeups, and typed commands](commands-and-events/spec.md)
- [Swappable paper-only execution boundary](paper-boundary/spec.md)
