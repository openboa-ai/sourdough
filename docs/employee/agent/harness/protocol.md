# Agent Harness: running an Agent on its bound runtime

Design draft · Verification **UNRUN** · [Runtime system and development units](README.md) · [First company run](../../../acceptance/first-shift/spec.md#first-integration-run)

The **Agent Harness** is product code: it runs under the scheduler’s current work claim, prepares context, starts an admitted runtime, mediates tools and accounts for the result. The **test driver** is external: it may supply sealed observations, inject the declared fault and watch. A test driver must never impersonate the Agent, issue a wake, restart the company or submit a trade. A model API, a packaged runtime and a company orchestrator are not interchangeable integrations.

## Shift protocol

| Stage and owner | Input → durable output | Failure and boundary |
| --- | --- | --- |
| Wake and claim (scheduler) | Timer occurrence, obligation or authorized event → work ID, lease, generation and trigger receipt. Choose from facts, not a preassigned trade. | Duplicate occurrences join the existing claim. A delivered conversation is not an accepted assignment. Conflicting claimant waits or selects other eligible work. |
| Check binding and reserve (harness + finance) | Roster’s immutable execution profile, registered capabilities, grant and cost ceiling → eligible run and cost reservation. | A newly available adapter is not a fallback. If the bound profile is ineligible or unavailable, block this run; keep other duties and reconciliation active. |
| Hydrate (context service) | Mandate, current work, desk, approved shared refs, prior receipts and active method → versioned [context pack](execute/context-pack/spec.md). | Treat retrieved content as data. The required core pack can run with no market module. Drop optional history before obligations; block when a required reference is stale or inaccessible. |
| Execute (runtime adapter) | Start with run ID and deadline → correlated lifecycle events, usage and typed tool requests. | A provider handle is a resumption hint, not company identity. An unexpected approval prompt is a blocked state; do not auto-approve or ask the board to complete an ordinary demo. |
| Admit and record tools (company service) | Authenticated active generation + schema + scoped request + logical action → deny, existing receipt, new domain admission or unresolved duty. | Adapter permission hooks are defense in depth. Recheck employment, grant, audience, budget and domain policy at effect admission, including after delayed callbacks. Only the service can invoke effectful connectors. |
| Settle and continue (scheduler + finance) | Terminal observation + actual tool receipts → durable work disposition, next condition/owner and separate usage settlement. | Provider success is not company success. Lost transport triggers generation fencing and original-receipt lookup, not a second action. A delayed usage report does not erase or repeat a prior effect. |
| Recover a session (supervisor + scheduler) | Original store, same Agent/profile, open duty and receipts → new fenced generation on the original binding. | A new session is not a new Agent. The supervisor cannot change the binding or hire. For a different runtime, organization must select another Agent and work must record a handoff. Only domain owners reconcile external effects. |

## Wire boundary and state

These are proposed company types, not vendor endpoints. Any local process or remote adapter has to translate them without changing their meaning. The company generates authority-bearing fields; runtime output cannot assign its own company, Agent, cost ceiling or generation.

| Record | Required fields and interpretation |
| --- | --- |
| Adapter manifest | Adapter and protocol revision, local/remote transport, permitted destination, supported event types, tool mediation, native resume or fresh hydration, usage availability, cancellation behavior and tested isolation boundary. Pin capabilities per configuration; unsupported optional capabilities remain explicit. |
| Run request | Company, Agent ID, immutable execution profile (runtime/adapter/provider/model revisions), work, run, generation, trigger, context digest, selected method/policy/tool-schema revisions, workspace handle, allowed evidence refs, budget and deadline. Secrets remain in a credential broker. No expected answer, evaluation label or fault cut. |
| Adapter event | Version, run and generation, provider session/turn/event ref where available, adapter event identity, event kind and observed time. Kinds: started, output, tool-request, approval-needed, usage, terminal. The adapter assigns a deduplication identity for local deliveries if none exists; never infer identical economic actions from identical text. |
| Tool request/receipt | Provider call ID correlates a reply. Company action ID identifies the logical effect and survives a new session. Validate tool schema, canonical argument digest, actor and current grant. Same action + same terms returns the original receipt; changed terms conflict. Separate transport acknowledgement from a domain result. |
| Checkpoint/closure | Claim generation, context and method digests, last accepted event, action receipts, unresolved effects, lease and next wake. Distinguish provider terminal status from work disposition and cost coverage. Record elapsed time, usage or unknown, budget reservation and cost receipt. |

**Concurrent tools and stream gaps.** Independent allowed reads may run concurrently. Serialize dependent writes at the resource owner (per account for risk); multiple provider tool calls in one response are not one transaction. Store each accepted receipt before returning it. Duplicate terminal events cannot close twice; a terminal event arriving before an outstanding result does not prove absence. On a stream gap query original receipts, mark the gap and fail closed for dependent effects. Replay only read operations whose freshness and retry policy allow it.

**Two-plane isolation.** Separate every Agent run into two planes:
1. **Outer Capability Broker (trusted host plane):** Binds `run_id`, `company_id`, `agent_id`, `fencing_token`, capability scope and budget ceiling strictly server-side; model tool arguments can never assert or override identity, role or generation. Registers only tools admitted by the duty's scope and executes `fencing_token -> capability -> budget -> policy/schema -> external_actions idempotency -> atomic SQLite commit -> receipt` before replying, recording an out-of-sandbox append-only run trace.
2. **Inner Sandboxed Desk (untrusted execution plane):** Enforces the Agent's pinned immutable `sandbox_backend` (local macOS sandbox required, no automatic fallback). Permits bounded read/write strictly inside the Agent's isolated per-Agent Desk directory and per-run scratch directory while SQLite `private_desk` remains metadata and revision authority. Blocks all outbound network egress (`EPERM`) and keeps the company SQLite store, credentials, broker secrets and other Agents' Desks completely unreadable. Subprocesses spawned inside the Desk inherit the same zero-egress, Desk-confined sandbox.

A working directory, instructions or provider hook alone is not this boundary. If the adapter or pinned `sandbox_backend` cannot establish it, fail closed and do not qualify it for effectful turns. Persist minimal audit evidence and redacted tool metadata rather than credentials, raw private Desk contents, unrestricted transcripts or hidden model reasoning.

## Adapter qualification cases

These are acceptance requirements, not claims that a runtime is installed or qualified.

| Boundary | Required observation |
| --- | --- |
| Session and receipt | Interrupt after an admitted tool action. Native resume or a fresh session on the same binding returns the original company receipt; a thread handle never becomes Agent identity. |
| Tool grant | Revoke a grant before resuming a session. Deny the next effect at the company boundary even if the native session remains valid. |
| Trigger and authority | Deliver a scheduled trigger twice. Observe one due duty, no automatic authority and rejection of unsupported effects. A request/response adapter must enforce its own deadline and persist its cursor. |
| Writer generation | Delay a prior writer's event until after replacement. Neither company work nor the books may change on its authority. |
| Workspace isolation | Use two authenticated principals. Sharing an excerpt or mentioning an Agent must neither disclose a private desk nor accept an assignment. |
| Work ownership | Race two workers for one duty and drop the winning worker's terminal event. Observe one accountable generation and an outstanding closure duty; the adapter must not schedule a second copy. |

## Example: losing the provider connection after a tool request

This is a proposed contract trace, not a captured run. The adapter relays request `call-A` in generation 3; the service authenticates it and binds it to company action `A17`. The domain commits its admission receipt before the adapter acknowledges. The provider then disconnects. The supervisor fences generation 3. A fresh session on the same Agent/profile in generation 4 receives the open-duty and receipt references, not a prompt to try the same command again. A different provider call ID does not create permission for a new business action. Identical replay of A17 reads the existing receipt; different terms under A17 conflict. If the original submission's outcome is still unknown, only its domain reconciler queries the bound destination. A late generation-3 mutation is denied. The accounting service separately matches any late usage to the original cost reservation.

Qualification evidence for this cut: ordered grant and generation records, provider-originated call observation, service admission receipt, old/new run IDs, independent destination send count, eventual result or still-owned uncertainty, and actual usage or an explicit unresolved cost. The first research-backed trade test applies the stricter venue-commit cut; a provider disconnect alone cannot pass it.

## Qualification ladder and delivery order

1. **Recorded conformance, per adapter revision:** start/decline, schema errors, duplicate and changed-terms calls, missing terminal, stale generation, exhaustion, consent prompt and cancellation. Use both event-stream and request/response-shaped fixtures. This proves normalization only.
2. **Actual single runtime:** under already provisioned consent and budget, witness start, a real model-chosen tool call, the independent service receipt and closure/usage disposition. Use that adapter for the first Agent-originated paper fill. Test research and post-fill recovery in the later founder continuity trial. It must not wait on a second vendor or a plug-in marketplace.
3. **Actual configuration choice and succession:** construct two distinct Agents on different actual runtime/model profiles and observe each use a scoped tool. Attempt to run A on B’s binding: denied before launch. Then fence A’s claim and observe a separately authorized handoff to B; compare attribution, grants, shared references, unresolved receipts and cost. Fixtures prove neither real compatibility nor a successful handoff.
4. **Operational record:** record exact build, adapter/model revision, tool schemas, capability differences, isolation evidence, consent scope, attempted cases and status (observed, blocked, unsupported or unrun). Conformance, vendor access, successful single run, cross-runtime handover and trading performance are separate claims. Requalify affected cases when a provider, isolation profile, tool schema, grant or active method changes.
