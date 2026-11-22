# Resolve the original order after a crash

Design draft · [Trade execution, market risk and venues](../README.md) · [Company MVP map](../../../../../README.md) · [Development lifecycle](../../../../../engineering/lifecycle.md)

Business situation: [Authorize and resolve an effect](../../../../../capabilities.md#effects).

## Operating need

After the venue accepts an order the company may die before seeing the answer. It must recover without a duplicate trade or a board command.

## Observable outcome

The company resumes from its original store, resolves or contains the original order and automatically re-arms the Agent's next obligation.

## Scope of discretion

The reconciliation service owns the unresolved-order duty and evidence; the investment subledger owns and applies all reservation transitions. The Agent may continue independent work.

The supervisor restarts and fences stale senders; reconciliation resolves the original external identity even when new submissions are suspended. The same Agent resumes a distinct later duty on its original binding with the recovered position, or the affected exposure remains unresolved and contained. The external source must survive the company process fault; its implementation is not assumed qualified.

## Development changes

- [Resolve accepted, sealed-absent and unknown outcomes](reconcile/spec.md)
- [Reconcile venue-originated or unattributed account effects](reconcile-external/spec.md)

Review this intent against the company outcome above. Evidence for implementation, regression and operating behavior belongs to the individual changes; cross-feature acceptance belongs to the company map.
