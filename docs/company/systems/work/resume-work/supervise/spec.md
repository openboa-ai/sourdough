# Relaunch the company without a board or test driver

Design draft · Verification **UNRUN** · Responsible component: Local service supervisor

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

For the configured company ID and store, own service start, exclusive writer acquisition, readiness observation and bounded restart. A new process must resume the old store or explicitly fail closed. The scheduler, not the supervisor, chooses what work to run.

Input contracts: [Founding receipt](../../../organization/found-company/bootstrap/spec.md).

## Design decision and review

Give the supervisor a process-level readiness check independent of the desktop UI rather than reusing a UI heartbeat.

**Concern for the responsible component:** Select the supported local supervision mechanism during planning. A developer terminal, test driver or two unfenced writers cannot meet this contract.

### Local first delivery

Use one installed, product-owned supervisor process with an explicit company/store binding and a bounded restart policy. It starts the service child, observes its actual exit and requires replacement readiness before reporting recovery. The child holds the local writer lease for all effect-capable entry points, including a direct paper runner. Acquire an exclusive OS lock on the existing store's file identity before startup mutation; do not key it only by a process ID, a mutable path or a Client session. Verify the existing company ID and file identity before effect admission. A hard-link alias must not become a second writer, and a replaced path, missing store or mismatched company is a refusal, not a fresh start.

The qualification host runs one supported binary against the store. Upgrades stop and verify the previous binary before admitting the new writer; an older binary that does not participate in the lease is not a supported concurrent writer. The supervisor never owns order credentials or sends. Agent subprocesses cannot write the company store or inherit the lease. A recovered service announces separately that it has opened the correct store, scanned pending work and resolved or contained each effect dependency. Restart exhaustion leaves a recorded incident and no new dispatch.

## Functional and quality requirements

- **FR.** Observe process exit independently, launch a replacement, verify the writer identity and run recovery scans before enabling dependent risk. Preserve original work and submission IDs.
- **NFR.** Count zero interactive commands, concurrent senders and substitute stores. Record observed exit-to-ready and ready-to-convergence times separately; declare the first deadline before the run.

## Acceptance and independent evidence

**Oracle:** A passive OS observer timestamps process exit, replacement readiness and both writer identities. The external test driver records its own command stream to rule out manual relaunch.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | With the UI closed, kill the service after a durable work decision. Observe product-owned restart on the same store before deadline. The first quant integration tightens the cut to immediately after a venue fill. |
| Failure or contention | Keep the original writer alive or supply a missing store. The contender must not dispatch or bootstrap a blank company. Repeated launch failure produces a bounded backoff and an observable incident. |
| Alias and stale claimant | Start a contender through a hard-link alias, then deliver an old run receipt after a successful replacement. Observe one live effect-capable writer, zero accepted stale mutations and the original company/store identity. |

## Operations and change review

Observe restart count, ready latency and writer conflicts. A rollback may replace the service binary only with a version that understands the current store and outstanding orders.
