# C1 · Company mandate and organization

Design draft · [Company and quant architecture](../../../README.md#system-hierarchy) · [Development lifecycle](../../../engineering/lifecycle.md)

**Responsibility:** Owns the company, its purpose, founding Agent, staffing decisions and delegation ceilings. No department or exchange account is required at founding.

## Company formation

### [Found the company](found-company/intent.md)

Establish company, founder, mandate and first duty. A business profile binds its own resources later.

- [bootstrap](found-company/bootstrap/spec.md) — Create the company once. A company with no venue or trading account must be able to open and resume.
- [amend](found-company/amend/spec.md) — Apply a new standing mandate with a revision check. Do not turn routine Agent decisions into board approvals.

## Organization design and staffing

### [Choose how to staff a need](organize-work/intent.md)

Use observed backlog, capacity and cost to choose between doing, delegating, automating, hiring or waiting.

- [staffing-decision](organize-work/staffing-decision/spec.md) — Record the alternatives, choice and review condition. A recommendation to hire creates no Agent and spends no budget.

### [Hire within delegated authority](hire/intent.md)

Create a new Agent only after a justified decision and a valid grant; do not pre-create departments.

- [issue-grant](hire/issue-grant/spec.md) — Bind the Agent, bounded grant and hiring allocation atomically. Replaying the same request must not produce a second hire.
- [onboard](hire/onboard/spec.md) — Resume incomplete provisioning under the existing hire rather than creating a second Agent.

### [Retire an Agent without abandoning company work](offboard/intent.md)

Stopping a runtime retires a session, not an Agent. The company must withdraw new authority and retain accepted obligations.

- [fence](offboard/fence/spec.md) — Commit the employment cutoff.
- [settle-custody](offboard/settle-custody/spec.md) — Reassign obligations and certify retirement.
