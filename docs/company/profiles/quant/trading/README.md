# Q2 · Trade execution, market risk and venues

The [execution-environment intent](execution-environment/intent.md) fixes the common trading-core boundary across paper, external test and live bindings; the company [launches and configures its selected runtime](execution-environment/launch/spec.md) and [qualifies the integration](execution-environment/qualify-engine/spec.md). [Current-market paper](live-paper/intent.md) reads a separately bound production market feed while sending no real orders. External test qualification and fixed-price fault fixtures are separate claims.

Design draft · [Company and quant architecture](../../../../README.md#system-hierarchy) · [Development lifecycle](../../../../engineering/lifecycle.md)

**Responsibility:** Owns admission against exposure, order state, cancellations and recovery of uncertain external effects. The simulator and optional Binance testnet profile are concrete venue adapters; neither belongs in the company kernel.

## Paper binding and risk admission

### [Bind the fund to its simulated account](paper-account/intent.md)

A company can exist without a venue; the fund must know which independent account it may use.

- [bind](paper-account/bind/spec.md) — Admit and reopen a paper mandate.

### [Change the mandate or suspend risk](board-control/intent.md)

Let the board change direction without becoming the approver of ordinary work.

- [apply-control](board-control/apply-control/spec.md) — Apply an authorized, versioned control at risk admission. Show when it took effect; continue to reconcile pre-existing orders.

### [Admit an order against shared risk](admit-intent/intent.md)

Check authority and the remaining account headroom before allowing an economic effect.

- [reserve](admit-intent/reserve/spec.md) — Serialize checks and reservations for an immutable action. A duplicate reuses the receipt; two concurrent orders cannot spend the same headroom.

## Order lifecycle

### [Submit and observe a simulated order](simulated-venue/intent.md)

Provide a venue whose history survives the company process and can be read independently.

- [orders](simulated-venue/orders/spec.md) — Record orders and identifiable fills durably. Permit the test to withhold a reply after a fill without undoing the venue fact.

### [Cancel the intended working order](cancel-order/intent.md)

Stop the unfilled remainder without treating a cancel response as proof that nothing filled.

- [cancel](cancel-order/cancel/spec.md) — Tie cancellation to the original order; reconcile late and partial fills before releasing the confirmed remaining reservation.

### [Resolve an uncertain original order](recover-order/intent.md)

After a crash, find out what happened before risking another submission.

- [reconcile](recover-order/reconcile/spec.md) — Query the original identity; book confirmed fills or release only proven absent or terminal exposure. An inconclusive lookup keeps the reserve and a retry duty.
- [reconcile-external](recover-order/reconcile-external/spec.md) — Account for independently observed venue-originated or unattributed effects without inventing Agent decisions or silently accepting an unexplained position.

## External venue qualification

### [Qualify an external BTC futures venue](venue-qualification/intent.md)

Record what the selected testnet product actually supports; simulator behavior is not transferable evidence.

- [observe](venue-qualification/observe/spec.md) — Check environment, instrument units, timestamps and account identity against independent read-only observations; submit no orders.
- [testnet](venue-qualification/testnet/spec.md) — Only with separate testnet authority, exercise bounded submission, lookup, fill or cancellation and reconciliation. Report unsupported or unobserved cases explicitly.
