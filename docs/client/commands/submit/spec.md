# Submit a command and resolve its receipt

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Human query and control access is through Client. Route a received command to its domain owner; a successful submission is not an applied change. Agent and service-originated operations remain independent of Client.

Send only after an explicit board action: authenticated principal, company, typed operation, request ID, argument digest, expected revision and reason. A profile control such as suspend-new-risk uses its registered service contract; it is not required by the base client.

An optional trading-runtime configuration or start command uses the same public command/receipt boundary and only an explicit authorized client action. The service owns validation, credential handling and actual setup. The base client need not have a trading profile, and the company can configure and start the runtime without any client.

Input contracts: [Versioned charter command](../../../company/systems/organization/found-company/amend/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

Keep draft, submitted/unknown and effective separate. Only an authoritative receipt establishes applied or denied status.

**Concern for the responsible component:** A timed-out click might already have committed. Query the original receipt under current authorization; do not allocate a second request ID on reconnect.

## Functional and quality requirements

- **FR.** On stale revision require a new informed decision against the latest facts. An exact retry, where allowed by contract, reuses the request ID and arguments.
- **Compatibility.** Do not submit a command whose type or contract revision the service does not support. The service independently rejects unsupported requests before any effect. Reconnecting after an upgrade never translates an outstanding request into a new command.
- **NFR.** Double-click, refresh and reconnect cannot apply a control twice. Neither cached grants nor client validation override the server.
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
