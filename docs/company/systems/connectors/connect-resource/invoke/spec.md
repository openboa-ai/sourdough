# Dispatch and observe a connector operation

Design draft · Verification **UNRUN** · Responsible component: Connector gateway

[Feature intent](../intent.md) · [MVP map](../../../../../README.md) · [SDLC handoff](../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Input contracts: [connect-resource/register](../register/spec.md).

## Design decision and review

Accept a scoped actor, connector revision, operation, request ID and typed payload. Return observed result, rejection or unknown with source reference. Domain services authorize financial commands before dispatch; connectors cannot book them.

Dispatch checks the effective grant and exact destination at use time, not only at registration. Separate new writes from observation of an existing unresolved effect: a suspension of new risk must not erase the domain's recovery duty. Inbound notifications are observations, not authoritative commands or final business receipts; retain source identity and let the owning domain reconcile duplicates and missed events.

For an effectful invocation, the gateway checks the service-issued admission, immutable arguments, destination, environment and current sender generation at dispatch. A runtime or client cannot supply its own admission. Persist the original operation identity and an owned recovery duty before any send; after a lost response a replacement resolves that identity and cannot create a new write. A revocation or suspension does not erase already admitted effects.

**Concern for the responsible component:** Registering a connector is not permission to act. Never infer order finality or global exactly-once delivery from a generic success response.

## Functional and quality requirements

- **FR.** Persist the scoped operation and observed, rejected or unknown result with its source; retain the original request identity for lookup.
- **NFR.** Read retries obey their budgets. On a write timeout bind recovery to the original operation and its declared lookup guarantees; never blindly resend.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Collect a document from the dataset fixture without any account, order or balance fields. Through the same envelope submit an already admitted simulated order and retain the venue-specific result. |
| Failure or contention | Lose the write response. An independent destination log shows one dispatch; if lookup is unavailable keep outcome unknown and assign a recovery duty. |
| Dispatch race | Delay an admitted operation before send, fence the old generation and start recovery. A stale sender cannot newly dispatch; if the destination may already have accepted a delayed packet, preserve the original unknown effect and block dependent risk until conclusive destination evidence. Do not claim generic exactly-once delivery. |

## Operations and change review

**Oracle:** Compare destination observations, service decisions and call count. Quarantine an incompatible adapter; do not convert transport success into a business receipt.

For delivery, attach the normal and adverse observations to the tested revision and keep unresolved evidence visible.
