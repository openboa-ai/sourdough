# C3 · Work and Agent communication

Design draft · [Company and quant architecture](../../../README.md#system-hierarchy) · [Development lifecycle](../../../engineering/lifecycle.md)

**Responsibility:** Owns due work, decisions, messages, acceptance and continuity between shifts. The arrival of a message is not acceptance of a task.

## Work and shifts

### [Choose the next useful job](discover-work/intent.md)

The Agent can investigate, act, wait or delegate on an observation or obligation. Trading is an additional choice under the quant profile.

- [decide](discover-work/decide/spec.md) — Record the considered evidence, rationale, action identity and next time or condition. A justified hold still produces accountable follow-up.

### [Resume a shift after interruption](resume-work/intent.md)

Bring the company process back and give unfinished work exactly one current claimant. These are different failure boundaries.

- [supervise](resume-work/supervise/spec.md) — Relaunch the service on its original store without the UI or test driver; independently observe the old and replacement processes.
- [recover-shift](resume-work/recover-shift/spec.md) — Reclaim the same work under a new generation, reject stale completions and schedule a distinct later work item.

## Conversations and responsibility

### [Discuss work without issuing commands](conversations/intent.md)

Agents need direct and team conversations that survive a provider session.

- [publish](conversations/publish/spec.md) — Append to a scoped company thread. Remove a member before its next read; deny thread access. An otherwise authorized reader cannot open an independently restricted attachment.
- [notify](conversations/notify/spec.md) — Deliver a message without accepting work. Redeliver twice and remove membership before fetch. Do not disclose the body, transfer the job or start a duplicate turn.

### [Hand off responsibility for work](collaborate/intent.md)

Make clear who is responsible while a task is being handed to somebody else.

- [handoff](collaborate/handoff/spec.md) — Keep the sender responsible until authorized acceptance. Preserve the evidence, expected output and single accountable owner.
