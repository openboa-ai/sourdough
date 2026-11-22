# U1 · Board Client

[System map](../README.md) · [Client build contract](build.md) · [Vocabulary](../glossary.md)

The Board Client is the presentation and control-plane UI. It owns interaction state, not the read-model source or command authority. The company can run, trade, reconcile and recover without this process installed or connected. A generic client can inspect a company without installing investment panels.

## Projection presentation

### [Inspect authorized company state](inspect/intent.md)

- [present](inspect/present/spec.md) — Show only the authorized projection at its actual revision, with readable missing and stale states.
- [investment-panel](inspect/investment-panel/spec.md) — When the quant profile is enabled, inspect decision lineage and reported returns without recalculating them.

## Control-plane interaction

### [Request a governance change](commands/intent.md)

- [submit](commands/submit/spec.md) — Send an explicit typed command. Resolve lost acknowledgments through the original receipt, not another command.
