# Transfer responsibility with evidence

Design draft · Verification **UNRUN** · Responsible component: Work service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Append a work-scoped request with sender, recipient, evidence references, due condition and expected result. Acceptance checks current owner revision and authority.

Input contracts: [Record a decision and its next condition](../../discover-work/decide/spec.md) · [Check the recipient's current authorization](../../../authority/authorize-actor/check/spec.md). The current roster and work claim identify both participants; the recipient may be the founder or a hired Agent. Handoff does not itself hire an Agent.

## Design decision and review

Use an offered-to-accepted transition instead of clearing the original owner as soon as a message is sent.

**Concern for the responsible component:** Verify receiver access to evidence before transfer; stale acceptance must not be able to resurrect a superseded assignment.

## Functional and quality requirements

- **FR.** Keep the existing owner responsible until the recipient accepts. At acceptance, record the old and new Agent IDs, fence any outstanding writer on the old claim, and advance the ownership revision atomically. Keep the original authors and effect IDs; do not copy grants, private desk contents or provider state. Require a result link before closure.
- **NFR.** Concurrent acceptance cannot leave two owners or none. Evidence access is checked for the receiving Agent.

## Acceptance and independent evidence

**Oracle:** Observe both Agents' authorized work queues before offer, after acceptance and after redelivery; compare the sole accountable owner and actual output link.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Have the recipient accept, produce a result and link its use in a later decision. Inspect each distinct event. |
| Failure or contention | Reject an unavailable reference or stale assignment. A declined or expired request leaves the previous owner and schedules follow-up. A predecessor's late callback cannot act after acceptance; an unresolved external effect remains with its original domain reconciler. |

## Operations and change review

Report overdue handoffs by accountable Agent. Reoffer or reassign within the mandate.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
