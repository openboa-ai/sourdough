# Track a maintenance request through local resolution

Design draft · Verification **UNRUN** · Responsible component: Company operations

[Feature intent](../intent.md) · [Connector invocation](../../../connectors/connect-resource/invoke/spec.md) · [Company change activation](../../manage-change/activate/spec.md) · [Work handoff](../../../work/collaborate/handoff/spec.md)

## Boundary and dependencies

Operations owns the local incident and release disposition; Work owns the current duty; the connector transports permitted requests; the external maintainer owns its issue and published release. A product update must use an independently recoverable update path and a separately authorized policy; neither an issue comment nor an issue closing is a deployment command.

## Functional and quality requirements

- **FR-1:** Record internal issue, impact, sanitized reproduction, acceptance conditions, owner and follow-up. Check disclosure and destination authorization before sending. Preserve request identity; after an uncertain write reconcile the original before any retry.
- **FR-2:** Track external request, reported fix and release identity separately. If a response is missed, bounded polling or a later wake resumes tracking. A change in issue status alone cannot mark local recovery complete.
- **FR-3:** For a release permitted by standing policy, verify origin, integrity, compatibility, authority and recovery plan; contain affected effects, preserve original obligations, apply, restart and verify the original failure against the actual revision. Do not assume rollback can reverse migrations or external fills.
- **FR-4:** Notify the current duty owner and affected audience using collaboration; retain factual status in Operations. On blocked or failed application retain effective revision, limitations and next duty. New authority or an irreversible change beyond policy stays pending for a human decision through Client.
- **NFR:** An update failure or process death must retain a recoverable supervisor and company records. No external maintainer receives direct authority over the running company.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| End to end | Submit a sanitized issue through an authorized fixture; observe issue closure, release, local activation and independent defect regression as distinct events; route the outcome to the current duty owner. |
| Unknown submission | Lose the creation response; query the original identity before retry. An unavailable lookup leaves an unresolved duty rather than a second issue. |
| Unsafe update | Inject a denied permission, failed compatibility check, unsafe migration or crash. Preserve the effective version or safe recovery state, block affected new effects and record the next action. |

**Oracle:** Join local duty, connector and external issue receipts, release digest, update observations and independent regression result. Source or issue closure alone is not local resolution. All cases are unrun.
