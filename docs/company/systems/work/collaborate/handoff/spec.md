# Transfer responsibility with evidence

Design draft · Verification **UNRUN** · Responsible component: Work service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Append a work-scoped request with sender, recipient, evidence references, due condition and expected result. Acceptance checks current owner revision and authority.

Input contracts: [Record a decision and its next condition](../../discover-work/decide/spec.md) · [Check the recipient's current authorization](../../../authority/authorize-actor/check/spec.md). The current roster and work claim identify both participants; the recipient may be the founder or a hired Agent. Handoff does not itself hire an Agent.

## Design decision and review

Use an offered-to-accepted transition instead of clearing the original owner as soon as a message is sent.

The first local transition supports company assessment review. The sender offers its next follow-up during a review; the offer becomes actionable only after that exact review commits its decision and follow-up. The sender's follow-up stays scheduled under its original owner until accepted. The recipient can discover an actionable offer in a separate authorized turn. Acceptance must win against the sender claiming that follow-up: atomically retire the still-scheduled duty and schedule one successor for the recipient, referencing the selected shared evidence. It does not inherit the sender's private record, standing grant or unresolved trading effect.

The accepted expected result is part of the successor's authorized work-result read, with the offer ID and sender. A new provider session does not need to reconstruct the offer from chat or private notes.

**Concern for the responsible component:** Verify receiver access to evidence before transfer; stale acceptance must not be able to resurrect a superseded assignment.

## Functional and quality requirements

- **FR.** Keep the existing owner responsible until the recipient accepts. At acceptance, record the old and new Agent IDs, fence any outstanding writer on the old claim, and advance the ownership revision atomically. Keep the original authors and effect IDs; do not copy grants, private desk contents or provider state. Require a result link before closure.
- **NFR.** Concurrent acceptance cannot leave two owners or none. Evidence access is checked for the receiving Agent.

## Acceptance and independent evidence

**Oracle:** Observe both Agents' authorized work queues before offer, after acceptance and after redelivery; compare the sole accountable owner and actual output link.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | In one company activation, have the sender offer and settle a review, the recipient discover and accept the offer in its own turn, then the recipient cite the shared result in a fresh review. Inspect separate run IDs and the original and successor queue entries. |
| Failure or contention | Reject an unavailable reference or stale assignment. A declined or expired request leaves the previous owner and schedules follow-up. A predecessor's late callback cannot act after acceptance; an unresolved external effect remains with its original domain reconciler. |

## Operations and change review

Report overdue handoffs by accountable Agent. Reoffer or reassign within the mandate.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
