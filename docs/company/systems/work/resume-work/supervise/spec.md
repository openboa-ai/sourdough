# Relaunch the company without a board or test driver

Design draft · Verification **UNRUN** · Responsible component: Local service supervisor

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

For the configured company ID and store, own service start, exclusive writer acquisition, readiness observation and bounded restart. A new process must resume the old store or explicitly fail closed. The scheduler, not the supervisor, chooses what work to run.

Input contracts: [Founding receipt](../../../organization/found-company/bootstrap/spec.md).

## Design decision and review

Give the supervisor a process-level readiness check independent of the desktop UI rather than reusing a UI heartbeat.

**Concern for the responsible component:** Select the supported local supervision mechanism during planning. A developer terminal, test driver or two unfenced writers cannot meet this contract.

## Functional and quality requirements

- **FR.** Observe process exit independently, launch a replacement, verify the writer identity and run recovery scans before enabling dependent risk. Preserve original work and submission IDs.
- **NFR.** Count zero interactive commands, concurrent senders and substitute stores. Record observed exit-to-ready and ready-to-convergence times separately; declare the first deadline before the run.

## Acceptance and independent evidence

**Oracle:** A passive OS observer timestamps process exit, replacement readiness and both writer identities. The external test driver records its own command stream to rule out manual relaunch.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | With the UI closed, kill the service after a durable work decision. Observe product-owned restart on the same store before deadline. The first quant integration tightens the cut to immediately after a venue fill. |
| Failure or contention | Keep the original writer alive or supply a missing store. The contender must not dispatch or bootstrap a blank company. Repeated launch failure produces a bounded backoff and an observable incident. |

## Operations and change review

Observe restart count, ready latency and writer conflicts. A rollback may replace the service binary only with a version that understands the current store and outstanding orders.
