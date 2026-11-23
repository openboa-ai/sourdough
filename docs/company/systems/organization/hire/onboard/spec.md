# Take a hired Agent to the first accepted duty

Design draft · Verification **UNRUN** · Responsible component: Organization service

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Consume a committed hire receipt. Track hired → provisioning → ready → first-duty-accepted; blocked retains a reason, owner and bounded next attempt. Correlate workspace, the hire’s exact runtime/model profile, budget and first duty with the Agent ID.

Input contracts: [hire/issue-grant](../issue-grant/spec.md) · [desk/reopen](../../../../../employee/agent/desk/reopen/spec.md) · [execute/tool-turn](../../../../../employee/agent/harness/execute/tool-turn/spec.md) · [discover-work/decide](../../../work/discover-work/decide/spec.md).

## Design decision and review

Keep hire atomic but provision resources in resumable steps. Use an idempotent operation key per hire and step, not a new identity on retry.

### Starting in an existing project

A staffing assignment may designate one existing project and the narrow participation allowed for its first duty. Bind that designation to the committed hire. The Organization service uses the recorded delegation and the existing project access service to provision the new Agent's membership before making that duty runnable. Hiring authority alone grants no project access; the project and permitted operations must be authorized separately under the standing policy. Neither a title nor a message can supply the missing authority.

When no project is designated, onboarding follows its existing path. If designated access cannot be established under current policy, keep the hire and its budget receipt, expose the provisioning block and do not start the Agent with fallback access. An exact retry must not duplicate membership or the first duty. Keep the Desk private and use the same company asset and conversation boundaries as any existing member.

**Concern for the responsible component:** No worker should run with default privileges while its workspace or budget is incomplete. A role title is not proof of readiness.

## Functional and quality requirements

- **FR.** A real run reads the mandate and explicitly accepts its first assigned duty. Granting access, creating a folder or delivering a mention does not count.
- **NFR.** Zero duplicate Agents, grants, allocations or first duties on a lost acknowledgment. Onboarding cannot expand scope to make a run succeed.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Interrupt between grant and desk creation; resume automatically and observe the same hire in a live first turn accepting the original duty. |
| Failure or contention | Deny the workspace temporarily; show hired/blocked rather than working. Deliver duplicate readiness events and an out-of-scope write: no new hire and zero unauthorized effects. |
| Project work | Under standing scoped authority, observe a manager Agent hire for one small task without the Client. The hire gets one Desk and one budget allocation, reads the designated shared asset, replies to the manager in the project, and records a result. Recheck that a different project and the manager's private Desk remain inaccessible. No trade is necessary. |

**Oracle:** Independently compare roster, budget reservation, desk inventory, runtime start and accepted work receipt. Run [H1](../../../../../acceptance/episodes.md#h1-hiring).

## Operations and change review

Operations can inspect the last completed step. Retry within grant and deadline; if exhausted keep the cost and duty attributable to the company.
