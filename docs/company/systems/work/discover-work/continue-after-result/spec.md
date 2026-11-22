# Continue work after a committed result

Review draft · Verification **NOT_RUN** · Owner: Work service

[Intent](../intent.md) · [Decision contract](../decide/spec.md) · [Claim and wake](../../resume-work/recover-shift/spec.md) · [Flow #25](https://github.com/openboa-ai/sourdough/issues/25)

## Outcome

One start through the installed company entry point is enough. An employee completes a duty with an independently verified result. The running company schedules a distinct follow-up for the same employee. In that turn, the employee reads the committed result through its allowed scope, chooses a response, and leaves a decision and the next responsibility. An observer may inspect but may not launch either turn, publish a result or submit a decision after activation. The Board Client is absent.

The first binding is a founder's confirmed paper fill followed by a review of the booked position. A reasoned decision to hold, investigate or recommend an exposure change is valid. This review duty records judgment but cannot dispatch an order. The separate unattended trading duty must execute Agent-originated proposals through the existing admission path before Flow 05 claims repeated-trading coverage. The normal continuation run is not the [post-fill crash trial](../../../../../acceptance/first-shift/spec.md).

## Boundary and smallest implementation

- **Work service** owns due conditions, claims and their IDs, generation checks, dispositions and decisions. Use the existing duty records for additional duties; remove the one-duty-per-founder storage constraint without changing existing IDs, founding receipts, claim/order links or grant semantics. Add a reviewed schema transition from revision 1 and check foreign keys before publishing the upgrade. Keep the previous store readable by the new binary through the supported transition; an old binary must refuse a new schema. A failed migration leaves the source valid. Rollback is from a pre-upgrade copy only before new work effects are admitted; afterward preserve settled facts and repair forward.
- **Result producer** owns the immutable result and exposes an opaque reference (kind, ID, revision and availability time). For the first binding, Quant supplies a scoped book observation from posted fills: account and instrument identity, signed position with units, and the source fill identity. Do not take quantity from a venue reply or model transcript. Work knows only whether the authorized result revision is available, not how a position is calculated.
- **Harness** owns the pinned runtime check, run identity and scoped tools. Use the existing subscription-backed app Codex connection; a changed execution binding must not take over an existing employee's work. Qualification may provision a fresh founder with the candidate's binding before the run; binding, founder and store stay the same across both turns. The Agent may read the scoped observation and propose a decision, but not mutate work or books directly.
- **Company runner** claims eligible work and invokes the same order admission and booking path as the first-trade flow. Keep provider and venue IO outside SQLite write transactions. One bounded runner is sufficient; no separate scheduler service or new venue engine. Persist future work even when a bounded observation ends.

## Requirements and independent oracle

| ID | Contract | Required observation |
| --- | --- | --- |
| FR-1 | A verified prior result and its eligible occurrence unlock one successor for the same employee. Completion and the successor condition commit together. | Compare independently read work rows and original receipts. Each replay has the same duty ID; early wake stays pending until the result is committed. An unfilled or unknown submission cannot unlock a fill-dependent review. |
| FR-2 | The follow-up claim has a new work and run ID under the same principal and execution profile. Its review tool returns only the allowed committed observation before decision. | Join the revision the Agent actually received to an independent company-books read and venue history; compare account, instrument, fill and signed quantity. No fixture recommendation or answer in the prompt. |
| FR-3 | The current authenticated run selects the response and records its rationale, cited revision and next due condition. Persist a settled decision and that condition atomically. | Observe the Agent's real tool request; independently read one decision and one successor condition. An unsupported answer, decision with no observation, or text-only reply is not a completed review. |
| NFR-1 | Admission checks grant, scope and generation at use. Exact replay returns the original receipt; conflicting terms and stale or foreign claims cannot create a decision or wake. | Race two claims, replay both wake and decision, withdraw authority and deliver a delayed obsolete mutation. Observe one valid work lineage and zero unauthorized changes. |
| NFR-2 | Budget and elapsed time are bounded. Timeouts and unknown effects remain visible; a timeout is never a fill, an explicit hold, or permission to resend. | Seal a time and turn budget before activation. Count launches, record exact statuses, and compare the original destination journal before and after a duplicate delivery. On a missing result, prove that no dependent Agent turn or extra send took place. |

**First operating observation:** On one candidate, use the installed entry point with the Client absent. Start once and observe the same app-bound Codex founder across both authenticated turns. Retain company, grant, work and run IDs, binding, quote basis, original action/order/fill IDs, book observation revision, decision and next condition. Check zero operator duty commands, zero new orders from the review tool and zero live destinations. The deterministic flow test covers plumbing, including both arrival orders and denial; it does not establish real Agent judgment.

## Delivery

1. Deliver durable successor scheduling and claims through the Work service, with old-store verification, event-order and contention cases.
2. Connect booked result, scoped Codex review and atomic decision settlement through one runner, with the installed normal-operation observation and 01–04 regression.
3. Follow with ordinary unattended trading and the separate research, supervisor and original-order recovery units owned by their existing specs. Qualify the actual founder crash episode separately before aggregate merge.

Keep source access, rate limits and authority unchanged. If an approved runtime, a complete observation or a required decision is missing, record the last observed boundary and the missing prerequisite. Do not substitute a provider fixture for the installed observation.
