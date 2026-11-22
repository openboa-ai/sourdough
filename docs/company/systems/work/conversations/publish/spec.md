# Append to a scoped company thread

Design draft · Verification **UNRUN** · Responsible component: Conversation service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [authorize-actor/check](../../../authority/authorize-actor/check/spec.md).

## Design decision and review

Append a message by company/thread, sender, membership revision, message ID and optional work/evidence links. Support direct and group audiences. References never widen access to their targets.

**Concern for the responsible component:** A message is company data, not a tool instruction, consent, evidence of reading or a transfer of authority.

## Functional and quality requirements

- **FR.** Persist the authorized message and stable sender receipt; enforce audience on append and fetch.
- **NFR.** Repeating an ID with identical body returns the original receipt; changed content conflicts. Enforce current membership on append and read.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Two Agents discuss a research question with one shared link; both see ordered messages after reconnect, and work ownership does not change. |
| Failure or contention | Remove a member before its next read; deny thread access. An otherwise authorized reader cannot open an independently restricted attachment. |

## Operations and change review

**Oracle:** Compare thread history, membership and independent access receipts; retain conflicting requests without mutating history.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
