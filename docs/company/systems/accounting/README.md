# C7 · Accounting and financial records

Design draft · [Company and quant architecture](../../../README.md#system-hierarchy) · [Development lifecycle](../../../engineering/lifecycle.md)

**Responsibility:** Owns source receipts, balanced journal entries, reconciliation and period close. It answers what actually happened. It neither sets budgets nor instructs a venue.

## General ledger

### [Record the company's economic events](general-ledger/intent.md)

Operating costs and trading events need one auditable financial book, not totals reconstructed from chats or orders.

- [post-journal](general-ledger/post-journal/spec.md) — Post balanced entries and linked reversals. Try a 300/299 entry and a duplicate source with changed terms. Both fail distinctly; an authorized reversal preserves the original lines.

## Reconciliation and close

### [Reconcile and close a reporting period](accounting-close/intent.md)

Balanced entries alone do not show that cash and obligations match independent observations.

- [reconcile-period](accounting-close/reconcile-period/spec.md) — Reconcile statements and lock a period. Supply a statement ending at 960 or repeat the close. Show the 10 discrepancy or return the original receipt; no fabricated balancing entry.
