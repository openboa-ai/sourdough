# Launch and configure the company trading runtime

Design draft · **UNRUN** · Responsible component: company trading-runtime supervisor. [Environment intent](../intent.md) · [Environment binding](../bind/spec.md) · [Runtime integration qualification](../qualify-engine/spec.md) · [Company supervisor](../../../../../systems/work/resume-work/supervise/spec.md)

## Purpose and ownership

Sourdough manages NautilusTrader as its selected trading runtime: provision an authorized pinned distribution, configure and start the runtime, connect company strategy and data, monitor it, stop it and recover its operation. Reuse the runtime's trading, backtest, market-data and simulation functions through supported interfaces. This unit does not implement a new matching engine, backtesting engine or exchange API as a prerequisite for launch.

Sourdough owns runtime configuration, credential handles, grants, process lifecycle, order admission and durable action/recovery records. The selected runtime owns execution facts. NautilusTrader provides Binance market observations and the execution/account facts for the bound environment; sandbox fills come from NautilusTrader, not Binance. Sourdough consumes those facts only through its connector and does not use a direct exchange path. A successful process start or model statement is not execution evidence.

The connector exposes the post-order position as a typed observation scoped to the original order, account and instrument. Company books record admitted requests, reservations and witnessed fills for accountability; a sum of booked fills does not establish the account position. A scheduled Agent duty reopens its immutable, order-scoped observation, not a claim about the current account balance. If that observation was already captured, settlement reopens it without another connector lookup. Missing or mismatched observations must not create a position result or advance that dependent duty. A later read uses the same order identity; it does not submit a replacement. Zero-fill cancellation can release the reservation on independently verified terminal evidence; the still-unobserved position stays pending for the dependent duty and is retried with bounded backoff.

## Inputs and configuration

Given an existing company, authorized actor and cost/authority envelope, pin runtime distribution/version and integrity, compatible execution environment, mode (historical replay, current-market paper or separately authorized external execution), selected instrument and account binding, market-data source, strategy artifact and revision, persistent-state location, scoped access, model/fee and risk settings, deadlines and expected readiness. Store a versioned configuration and its non-secret digest as a company asset. A client may present and request configuration, but neither its installation nor its presence is required to prepare or run the company.

Credential values stay in the company secret facility. Configuration stores scoped handles, never raw keys in prompts, logs or strategy files. A production data read does not grant production order access. An external/live destination, credentials, increased cost or authority is usable only when already authorized for the company and exact environment; this spec grants none.

## Functional requirements

- **FR-1 Provision and validate.** Under an existing approved source, version, license and installation/spending policy, prepare the declared runtime and required dependencies through the company-managed setup path; alternatively recognize an already provisioned matching installation. Verify identity, version, compatibility and integrity. Missing dependency, inaccessible source, incompatible version or absent authorization remains a visible blocked setup with owner and next action; do not substitute an unpinned build or silently invoke another provider.
- **FR-2 Configure.** A permitted Agent or company service can submit a versioned configuration to the company. Validate actor, requested mode, destination, data source, strategy and risk/credential handles before activation. Commit an effective revision or return a specific denied/blocked/conflict receipt. A repeated request with identical arguments returns its existing result; changed content or stale revision cannot silently overwrite active configuration. Normal operation does not require an interactive board confirmation.
- **FR-3 Launch and observe.** Product supervision starts the pinned runtime through its supported entry and binds the intended configuration and company scope. Publish starting, ready, blocked, failed, stopping and stopped with run generation, effective mode, revision and last observation. Ready means the declared runtime initialized and its required data/venue bindings are verified; process existence alone is not readiness. Backtest and paper start without access to live execution credentials.
- **FR-4 Stop and restart.** Stop requests and exits must not erase outstanding duties, positions or effects. On unexpected exit use bounded automated restart under the same authorized configuration and stable company/runtime identities; fence stale calls and reconcile original outstanding effects before dependent new risk. On missing evidence retain the owner and retry or visible blocked state. Do not create another account or silently select another mode. Stop/restart is not a new order, and a company restart is distinct from independent venue loss.
- **FR-5 Mode use.** Agent or company program invokes the runtime through scoped company interfaces for historical backtest, real-data paper or an already-authorized external mode. Capture actual runtime and strategy revisions, source, effective binding, invocation identity, status and usage. The same company trading and risk contracts apply; switching mode is an explicit versioned operation, not an automatic fallback.

## Non-functional requirements

- **NFR-1 Isolation:** A paper or backtest execution cannot reach live order credentials or endpoints, even with a malformed runtime configuration. The market data connection is read-only and separately authorized.
- **NFR-2 Continuity:** Runtime setup and execution remain usable with the board client closed. Provider, dependency or secret failure is reported as blocked or failed rather than silently repaired by changing authority.
- **NFR-3 Evidence:** Keep config and actual binary/artifact identity, process generation, status and typed result receipts without secrets. Model or adapter source support is not evidence of an installed or running integration.

## Acceptance and independent evidence

| Case | Setup and observation |
| --- | --- |
| Company-managed setup | With an approved pinned distribution and no external operator process, a permitted actor configures paper mode through the company. The company provisions or recognizes the matching runtime, starts it and records a verified ready receipt. Compare the actual distribution, effective configuration, data read source, account binding and independent process observation. No manual shell start or UI dependency. |
| Repeated setup | Repeat the same configuration and start request after a lost response. Observe one effective configuration and one active runtime for the same scope. A changed payload under the same request ID conflicts. |
| Start fails safely | Remove a required dependency, source, permission or paper binding. Record blocked/failed with an owner, without switching mode, creating a different account or calling a live order endpoint. |
| Runtime interruption | Terminate the runtime while the company is running and separately terminate the company with an original paper order unresolved. The appropriate supervisor resumes under the original binding; reconcile original effects before dependent dispatch. Compare generations, venue receipts and account records, with no manual intervention or duplicate order. This does not replace either full Agent integration. |
| Position ownership | Provide a connector position different from the sum of company postings and verify that the Agent sees the connector value. Withhold or mismatch the observation: preserve independently witnessed execution facts without inventing a position or advancing the dependent duty. Restore the original observation and verify continuation without resubmission. Exercise full and terminal zero-fill outcomes with the SDK. |
| Mode isolation | Request historical replay and current-market paper, then attempt paper-to-live endpoint or credential substitution. Both authorized modes remain virtual; independent destination logs show zero unauthorized real order calls. |

This is a development-unit design. Runtime version, package source, installation policy, deployment host and provider credentials are not selected or installed by this document. See [engine qualification](../qualify-engine/spec.md) and the founder/new-hire acceptance contracts for stronger runtime, independent-venue and Agent evidence.

For the managed company-process cut, pause the company at its first read of the original SDK fill before the company books it. The observer independently checks the venue journal and the absence of a company posting before issuing the cut. A submission acknowledgement, timeout, or cancellation without a fill is not the cut point.

## Preparing the runtime package

The local deployment service can run `sourdough prepare-trading-engine STORE ENGINE_DIRECTORY --company-id ID` before activating a trading configuration. Use a private, company-owned directory. The command installs the packaged wheel lock in an isolated environment on supported Apple Silicon hosts, probes the required entry points, and reopens the same verified installation receipt on an identical request. An operator-provided `--wheelhouse` selects offline, hash-checked artifacts with no index fallback. The trading runtime is an external dependency installed separately from the Sourdough package.

An existing receipt is accepted only if the installed files still match its content fingerprint. Changes to installed files fail before importing the runtime, even if the distribution metadata is unchanged.

The setup receipt establishes package identity and importability only. It authorizes no market access or submission and is not a running worker, active trading configuration, or verified fill.

## Read-only market activation

`sourdough observe-trading-market STORE ENGINE_DIRECTORY --company-id ID --symbol BTCUSDT` opens an already prepared engine and directs the public spot data client to an immutable, company-owned market journal separate from the company database. The readiness receipt requires verified spot currency and sizing terms and a fresh source event committed to the original journal. Instrument term drift blocks activation; repeated or excessive-age events do not count as readiness. The run is bounded, the worker receives no inherited provider credentials, and no execution client is configured.

This observation uses exchange trade IDs and exchange trade times at the provider's millisecond precision, correcting only the pinned SDK's bounded float-conversion error. It is not a quote or order-book continuity guarantee, paper execution, testnet qualification, or evidence of any financial result.

## Bounded virtual cash activation

After observing the spot instrument, `prepare-nautilus-paper` seals the virtual account using the same engine installation. `serve-nautilus-paper` starts the sandbox separately from the company worker. It authenticates the virtual cash identity and waits for a fresh public quote and exchange trade before readiness. `bind-nautilus-paper` binds that account to the company; `operate-nautilus-paper` runs the existing company admission, work and book path against the bound venue.

The supported initial verification is an IOC buy with a full observed fill, exact spot lot increments and quote atoms of (10^{-8}). The company records its reservation before queueing an original order ID. The sandbox worker records actual SDK fill evidence in a separate journal; account sync requires a matching, separately observed SDK position and cash balance. The package config runs only the public market data and sandbox execution factories and uses a declared zero test fee. It does not represent actual fees, slippage, depth or returns.

The company reads all identified fills for the original order from the SDK-event journal, including partial fills already recorded before the read. Account corroboration and restart readiness use the sum of those fills and their exact cost; the last fill is not the order total. An ambiguous or partial result is retained and does not permit new risk. Starting a fresh engine with unresolved effects is refused. A successful bounded full-fill activation does not prove an SDK-generated partial or cancellation, portable restart, or testnet parity.

An SDK cancellation notification that lacks corroboration is not a cancellation receipt. It leaves the original order unresolved unless identified fills already account for the entire order in the same worker generation. Preserve any prior fill evidence and accept later original-order evidence; do not release the remainder on a notification alone. A notification cannot overwrite a full fill or prevent the account observation. Conflicting reported quantities and stale generations are rejected. Verify both cases and re-run the current-market founder flow on the installed candidate.
