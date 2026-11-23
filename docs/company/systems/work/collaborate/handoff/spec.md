# Transfer responsibility with evidence

Design draft · Verification **UNRUN** · Responsible component: Work service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Append a work-scoped request with sender, recipient, evidence references, due condition and expected result. Acceptance checks current owner revision and authority.

Input contracts: [Record a decision and its next condition](../../discover-work/decide/spec.md) · [Check the recipient's current authorization](../../../authority/authorize-actor/check/spec.md). The current roster and work claim identify both participants; the recipient may be the founder or a hired Agent. Handoff does not itself hire an Agent.

## Design decision and review

Use an offered-to-accepted transition instead of clearing the original owner as soon as a message is sent.

**Concern for the responsible component:** Verify receiver access to evidence before transfer; stale acceptance must not be able to resurrect a superseded assignment.

### Settle an assessment review with an explicit next responsibility

The Agent may retain the next review or choose to offer it to an independent reviewer. Retaining it uses the existing review command. Offering it through the terminal assessment-review command includes the normal decision and next condition plus a typed offer: recipient, shared evidence reference, expected result, and expiry. A rationale, message, or request for independent verification is not an offer or evidence of independent review. If no recipient or evidence is eligible, the Agent can keep the unresolved work with a justified next condition. The service never selects the recipient or interprets prose to transfer work.

Bind the offer to the current assessment duty and run. Eligible evidence is a publication made by the sender in that duty, or the exact shared asset and revision assigned to that duty and already read in that run. The latter need not be republished. Check current sender and recipient authority and audience access to the asset; do not change its author, scope, or visibility. Recheck recipient authority, evidence access and availability of the original follow-up at acceptance.

For the terminal offer command, commit the decision, original owner's follow-up, and its offer as one transition. The offer is immediately discoverable by the eligible recipient; do not schedule a recipient duty at that point. Exact command replay returns the original offer and follow-up; changed terms or a competing offer on the same review conflict without creating work. Preserve ordinary review and separate offer compatibility. Quant reviews do not acquire the assessment handoff command.

## Functional and quality requirements

- **FR.** Keep the existing owner responsible until the recipient accepts. At acceptance, record the old and new Agent IDs, fence any outstanding writer on the old claim, and advance the ownership revision atomically. Keep the original authors and effect IDs; do not copy grants, private desk contents or provider state. Require a result link before closure.
- **NFR.** Concurrent acceptance cannot leave two owners or none. Evidence access is checked for the receiving Agent.

## Acceptance and independent evidence

**Oracle:** Observe both Agents' authorized work queues before offer, after acceptance and after redelivery; compare the sole accountable owner and actual output link.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Have the recipient accept, produce a result and link its use in a later decision. Inspect each distinct event. |
| Failure or contention | Reject an unavailable reference or stale assignment. A declined or expired request leaves the previous owner and schedules follow-up. A predecessor's late callback cannot act after acceptance; an unresolved external effect remains with its original domain reconciler. |
| Explicit next responsibility | With the Client closed, let the sender decide how to resolve a company exception whose existing project evidence is already shared with the independent reviewer. A self-follow-up never creates an offer. On an explicit offer, inspect one committed decision, one original owner and one offer before acceptance. Lose the reply and repeat it; verify the same identities. An unobserved or withdrawn asset rejects the command without settlement. Observe a distinct real Agent turn accept the valid offer and a later run of that same recipient read the exact evidence and produce a new result. A recorded offer without actual acceptance and use is incomplete. |

## Operations and change review

Report overdue handoffs by accountable Agent. Reoffer or reassign within the mandate.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
