# Pin the external observation profile without trading

Design draft · Verification **UNRUN** · Responsible component: Venue data adapter

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Select an authorized testnet-only product and account mode before binding external market, instrument and account observations to internal snapshot fields. Record the independently obtained instrument rules, observation time, provider event identity and units. Read access does not imply dispatch authority.

Input contracts: [Point-in-time snapshot](../../../investments/market-snapshot/snapshot/spec.md) · [Scoped connector invocation](../../../../../systems/connectors/connect-resource/invoke/spec.md).

## Design decision and review

Verify a read-only binding before granting permission for a testnet order.

**Concern for the responsible component:** Product, account mode and destination are still undecided. An unsupported identity or conversion is not a verified mapping.

## Functional and quality requirements

- **FR.** Represent unknown, unsupported, partial and stale values explicitly. Key orders and fills to the selected profile and account; do not join observations from another environment.
- **NFR.** For each declared identity, quantity, price, fee and timestamp field, publish the source and conversion or mark it unsupported. Zero order or cancellation calls are permitted during read qualification.

## Acceptance and independent evidence

**Oracle:** Use an independently authorized observer of the same testnet profile to compare raw units and event times with the normalized snapshot. Record any mismatch as unverified.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Given an authorized read-only profile, compare adapter output with independently retrieved venue observations from the same cut, including units and as-of time. |
| Failure or contention | A changed contract multiplier, stale snapshot or account mismatch invalidates the affected evidence; no fallback to a live endpoint or fabricated zero is accepted. |

## Operations and change review

Track profile revision drift and freshness. Disable qualification for the changed mapping until independently rechecked. Never record credentials with the evidence.
