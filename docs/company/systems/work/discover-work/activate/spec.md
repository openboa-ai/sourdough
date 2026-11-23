# Activate an Agent for a company responsibility

Design draft · Verification **NOT_RUN** · Responsible component: Work service

[Intent](../intent.md) · [Decision and next condition](../decide/spec.md) · [Conversation delivery](../../conversations/notify/spec.md) · [Handoff](../../collaborate/handoff/spec.md) · [Claim and wake](../../resume-work/recover-shift/spec.md)

## Outcome

An Agent can discover and take useful work that nobody assigned. With the Client closed, a company-approved standing responsibility makes a bounded review of permitted observations eligible. The Agent decides whether to take work within its delegation, ask a colleague, schedule a return or do nothing. The company can explain each resulting turn without reconstructing a conversation or provider transcript.

Scheduled continuation and discovery use the same Work scheduler but different inputs. A continuation carries an existing duty and its unmet condition. A discovery review carries a standing responsibility and a scoped set of observations, not a preassigned action. A changed observation can prompt a review but does not determine what work is done.

## Ownership and path

| Component | Owns | Does not own |
| --- | --- | --- |
| Conversation | Thread history, audience and recipient delivery | Work ownership or permission to run |
| Work | Responsibilities, scoped requests, next conditions, causal receipts and claims | Interpretation of business facts or authority to produce external effects |
| Harness | Running the claimed duty on the bound runtime, in the Agent's own Desk, with scoped tools | A parallel scheduler, standing grants or Work custody |
| Company authority and finance | Current permission and available operating limits | The Agent's business judgment |

    scoped condition → admitted Work occurrence → current claim → Agent turn
                           ↑                                      |
                           └──── decision, work or next check ────┘

Use the unified `CompanyRuntime`, `DutyRegistry` and three-entity Work kernel in the authoritative SQLite store:
- `work_items` owns durable duties, ownership and disposition.
- `work_runs` owns execution claims with partial unique indexes permitting at most one active `running` execution per `agent_id` and per `work_item_id`, monotonic `fencing_token`, an **anti-self-wake** invariant (an Agent's own message or status comment never triggers a wake for the same `agent_id`), and **zero-compute durable waits** (`status = 'waiting'` with `wait_until_ns` and/or `wait_event_key`, releasing execution slots until due time or a matching domain event).
- `work_responsibilities` and `work_conditions` own standing responsibilities and time/event triggers with `condition_revision` and `(source_identity, condition_revision)` deduplication before creating eligible work.

Start with one-shot time conditions and one typed internal event: a committed revision of a designated shared asset. Do not create separate per-scenario worker loops, another Work queue, a provider-native schedule, a process per Agent, or an external broker. The typed event is a reference to company data; the asset service remains its authority. External connectors and recurring schedules use the same `DutyRegistry` and Work kernel interface, not a parallel execution path.

## Requirements

| ID | Contract | Independent evidence |
| --- | --- | --- |
| FR-1 · Discovery | A standing review names its owner, remit and readable scope. An eligible review opens without a specific task, action or expected answer. The Agent may leave no new work or create work only within current delegation. | With the Client absent, inspect the seeded responsibility, scoped context and real Agent tool receipts. Supply observations but do not submit a duty, choose its outcome or wake either Agent in the test driver. |
| FR-2 · Own schedule | An Agent with authority for its responsibility can list, set, replace or cancel its own next condition. A condition links work or a standing review, its owner, time or typed source, expiry and policy. Completing a duty with a next condition persists both together. | Compare the condition revision and work decision in the store. Replace a pending time with a designated asset revision; deliver both and show only the current condition can become eligible. Cancel another pending condition and show no turn. |
| FR-3 · Peer request | An explicit Work request records sender, recipient, expected response, due condition and permitted references. When the company permits that request route, one bounded recipient turn may respond, defer or decline. This is a response duty, not acceptance of the underlying work. A handoff offer alone remains discoverable without creating a recipient duty. | Send a context post, direct message and mention: zero dedicated turns. Submit an authorized request and observe one separately identified response duty, with no change in the original owner or grants. Deny an unauthorized peer route without starting the recipient. |
| NFR-1 · Bound and explain | Deduplicate by source identity and condition revision before creating eligible work; claim each logical occurrence through the existing scheduler. Recheck Agent status, scope and budget before admission and respect the existing effect gate. Serialize turns for the same Agent initially and retain independent obligations. | Replay a typed event; deliver one after access revocation or retirement. Compare stable work/claim IDs and recorded cause or denial. A retired identity has no next turn. Cancellation does not delete audit or undo an admitted action. |

Record the triggering actor or service, source fact or due slot, matching condition and Work ID, decision time, run disposition, and next condition. Record why a relevant trigger was held or denied. No audit of every irrelevant message or private reasoning is needed. Preserve the rules for an existing duty whose deadline arrives without any change in the source.

The first [request and response work unit](../../collaborate/respond/spec.md) applies the peer-request contract when the requester cannot finish before a colleague answers. The response does not wait for the requester's publication or transfer ownership.

## First verification and delivery

1. Exercise the Work entry point for time, typed event, peer request, no-wake communication, replacement, replay and revocation. Assert durable state separately from the returned receipt. Neither a fixture nor a local spec check proves a real Agent turn.
2. With the Client closed, observe A in a standing review identify a plausible discrepancy in an authorized shared asset without an investigation ticket. A may ask B for a check and arm its own return. Observe B's independently scoped response and A's later disposition using that result. Compare with a quiet observation; report unnecessary launches, missed or late responsibilities and spent resources separately from useful decisions. Do not force a model answer or turn this evaluation into a prerequisite for an independent platform change.

Reuse existing identities, grants, run budgets and paper boundaries. This design does not grant deployment authority, external writes or new provider spend. It does not replace the separate company-only, fund or handoff acceptance cases. Admission of a concrete implementation remains subject to review of this contract and the existing flow gates.
