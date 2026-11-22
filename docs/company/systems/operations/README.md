# C9 · Operations, projections and recovery

Design draft · [Company and quant architecture](../../../README.md#system-hierarchy) · [Development lifecycle](../../../engineering/lifecycle.md)

**Responsibility:** Owns the server work projection, health signals, incident duties and loss-of-store recovery. A presentation client is a consumer, not a dependency.

## Changes and product maintenance

### [Operate a company-owned change](manage-change/intent.md)

- [activate](manage-change/activate/spec.md) — Independently verify, activate, supervise and safely recover an authorized company-owned revision.

### [Request product maintenance](request-maintenance/intent.md)

- [track](request-maintenance/track/spec.md) — Link an external issue and release to a separately verified local update and current duty.

## Work projection

### [Inspect company work without a finance profile](work-timeline/intent.md)

The board needs an explanation of ordinary work even in a company with no instruments or accounts.

- [read](work-timeline/read/spec.md) — Project a read-only work timeline. Delay the projection and withdraw access to an attachment. Show as-of time and limited access; refreshing does not wake the worker or alter work.

## Health and continuity

### [Detect and own an operating incident](monitor-company/intent.md)

Know what is affected, who owns it and when to check again even when the board is offline.

- [incident](monitor-company/incident/spec.md) — Detect and contain stale service or resource signals, assign continuing responsibility and close only with new independent evidence.

### [Recover a lost company store](restore-company/intent.md)

Recover company identity, duties, books and asset custody instead of starting a new company with a similar balance.

- [restore](restore-company/restore/spec.md) — Restore a checkpoint, replay newer events, reconcile active resource bindings and fence the original writer; an installed quant profile must also reconcile its venue.
