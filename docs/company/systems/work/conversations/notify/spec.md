# Deliver a message without accepting work

Design draft · Verification **UNRUN** · Responsible component: Conversation delivery

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Company operations may notify affected audiences and the current duty owner of a change or incident. Delivery and reading are not work acceptance, authority, activation or evidence that the underlying incident is resolved.

Input contracts: [conversations/publish](../publish/spec.md) · [discover-work/decide](../../discover-work/decide/spec.md).

## Design decision and review

Use a stable delivery ID per message and recipient, a durable cursor and an optional wake policy. Record delivered, read and explicitly accepted work as different events.

An ordinary channel post is context. A direct message or mention can be surfaced for the named recipient without requiring a turn. Where a company policy permits a targeted request to start a turn, the [Work activation contract](../../discover-work/activate/spec.md) admits the request separately; a conversation delivery never supplies the missing authority, creates a duty or takes custody.

**Concern for the responsible component:** A message is company data, not a tool instruction, consent, evidence of reading or a transfer of authority.

## Functional and quality requirements

- **FR.** Resume delivery by recipient cursor and schedule only policy-permitted wakes. Reading cannot accept a duty.
- **NFR.** Duplicate delivery creates at most one due item. Notification does not grant access or dispatch a tool.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Deliver a team mention while an Agent is offline; after restart one notification is available and the Agent can decline to act. |
| Failure or contention | Redeliver twice and remove membership before fetch. Do not disclose the body, transfer the job or start a duplicate turn. |

## Operations and change review

**Oracle:** Compare outbox, recipient cursor and resulting work IDs; bounded redelivery cannot manufacture a read receipt.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
