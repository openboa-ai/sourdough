# Amend the standing charter

Design draft · Verification **UNRUN** · Responsible component: Company governance

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [Founding receipt](../bootstrap/spec.md) · [Current authorization](../../../authority/authorize-actor/check/spec.md).

## Design decision and review

Accept the board principal, company, expected mandate revision, reason and request ID. Persist immutable old and new versions with an effective sequence. Consumers check the current mandate when admitting a new effect.

**Concern for the responsible component:** A stale amendment cannot restore removed grants; a previously admitted obligation remains owned until resolved.

## Functional and quality requirements

- **FR.** Accept a supported command contract from the authenticated board principal with company, expected mandate revision, reason and request ID. The service checks contract compatibility before applying an amendment; the command's format version and the company's mandate revision are separate checks.
- **NFR.** A stale amendment cannot restore removed grants; a previously admitted obligation remains owned until resolved.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Lower the document review cost ceiling. Independently compare the service revision and the next admission; no individual task needs board consent. |
| Failure or contention | Submit two amendments against the same revision. Accept one, reject the stale one and retain its reason; a retry cannot apply either twice. |
| Incompatible command | Submit an unsupported command version with a valid principal and current mandate revision. Observe rejection and no new charter revision. An earlier command's receipt is not superseded. |

## Operations and change review

**Oracle:** Compare charter history to admission receipts. Preserve the old revision for audit; reversal requires a new authorized amendment.
