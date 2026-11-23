# Request and resume after a peer decision

Review draft · Verification **NOT_RUN** · Responsible component: Work service

[Intent](../intent.md) · [Activation](../../discover-work/activate/spec.md) · [Handoff](../handoff/spec.md) · [Conversation delivery](../../conversations/notify/spec.md) · [Flow 10](https://github.com/openboa-ai/sourdough/issues/60)

## Outcome

An Agent may need a colleague's decision before it can finish. The requesting Agent records what it needs and yields its run. The company starts the authorized colleague in a separate duty. The original Agent returns to its own duty after the colleague answers or the request is closed without an answer. The Client can remain closed throughout. No publication by the requester is required to start the response duty.

The first binding is a project assessment: a reviewer asks the manager responsible for the assignment to decide between two valid next steps. This is a Work dependency, not a special conversation rule. The standing project policy determines the recipient and readable references for this binding; another workflow may use the same contract when its own delegation permits it.

    A's current duty -> explicit request -> B's response duty
          |                                    |
          +-- same owner waits <--- response, refusal or expiry
                   |
                   +--> same duty on a fresh run

## Contract

| ID | Requirement | Independent observation |
| --- | --- | --- |
| FR-1 · Request and yield | A current Agent chooses a named, authorized recipient, a question, the expected decision, a deadline and explicit evidence references. Admit one request per logical operation and one unresolved blocking request per duty initially. Commit the request, response eligibility and the original duty's waiting disposition together. Yield ends a run without completing, publishing or transferring the original duty. | After the tool succeeds, separately inspect one waiting duty with its original owner, one response duty with its own owner, and no publication. Replaying the same request returns its receipts; altered terms conflict. A rejected request leaves the original duty unchanged. |
| FR-2 · Respond | The recipient's separate run reads the admitted question and only evidence currently visible to that recipient. It can answer or decline within its standing grant. The response receipt is authoritative; a conversation can refer to it but does not serve as acceptance or as the durable answer. | Show a different run and principal reading the request and producing a response. Verify no private drafts, unrelated project references, new grants or unrelated tools were provided. |
| FR-3 · Resume | An answer, decline or due expiry makes the original duty eligible once. A fresh authorized run of the same Agent obtains the linked outcome and decides what to do. Late answers never overwrite a closed result. A revoked or archived Agent is not restarted; its unfinished obligation remains visible under the normal ownership policy. | Join the request ID, response or expiry, new run and original duty ID. Check the original owner, the actual disposition, and separate next work. Demonstrate both a response and expiry, without any manual wake. |
| NFR-1 · Authority and bounds | Recheck the caller, recipient, scope, references and available run budget against standing company policy. Use the existing Work scheduler and claim controls. Do not infer a request from prose, run on message delivery, open arbitrary agent subscriptions or clone the requester’s authority. | A regular post and direct mention launch no run. An out-of-scope recipient or reference is denied. Duplicate delivery, reordered callbacks and revoked access cannot create another turn or disclose evidence. |

## Implementation boundary

Work owns request identity, outcomes and both duty links. Use an explicit work result for `waiting for response`; do not infer it from process exit, uncompleted tool exchange or chat. The Harness can yield only after the admitted Work receipt, and it cannot create or resolve a request itself. On resume it uses the original duty and bound runtime, but a new run claim and fresh authorization. Company Resources stays the authority for any shared asset revisions. Reuse the current store and scheduler; no broker, new service, provider-native schedule or second conversation store is needed.

Keep ordinary assessment publication and the post-publication review path compatible. They remain valid when no input is required before publication. A response work item is not a handoff offer; it neither removes the original owner nor modifies another Agent's future schedule.

## First evidence

1. Exercise the public company Work entry point with a scoped request and authorized response. Observe the original duty resume after that response. Separately reject an unrelated project, replay a terminal tool reply, and expire an unanswered request. Confirm no publication before resume and no additional response duty for the same request.
2. With the Client closed, start from a real existing standing delegation. Observe an Agent identify a decision it cannot make, seek the authorized colleague's input, and yield without publishing an incomplete assessment. Observe the colleague respond, then the same requesting Agent resume and record its choice using the response. Keep the decision open and do not inject a reply, supply a correct answer or shorten the declared window. Save independent Work, claim, scope and tool receipts.

This unit does not qualify voluntary initiative under an open mandate, transfer of custody, private workspace retention, or fund execution. No new external connection, provider allocation, trading authority or human intervention is required.
