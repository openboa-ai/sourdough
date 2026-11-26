# Binance Spot & USD-M Futures external venue adapter, emulator, and qualification runner (`flow-live-venue`)

Status: Accepted specification · Target branch: `feat/flow-live-venue` · Owner: Quant execution, external venue integration, and venue qualification.

## 1. Intent

Provide a single shared-core external venue execution port (`ExternalVenueAdapter`), an opaque host credential provider (`CredentialProvider`), a production-grade `BinanceVenueAdapter` supporting both Spot (`/api/v3/*`) and USD-M Futures (`/fapi/v1/*`, `/fapi/v2/*`) across `external_test` and `live` environments, a faithful local wire-protocol emulator (`BinanceWireEmulator`), and an automated `VenueQualificationRunner` that produces sealed `venue_qualification_receipt` records feeding directly into Flow 1's fail-closed Board live promotion gate.

## 2. Authority, environment pinning, and credential boundaries

- **Strict endpoint pinning:**
  - `external_test` + `spot` → `https://testnet.binance.vision`
  - `external_test` + `usdm_futures` → `https://testnet.binancefuture.com`
  - `live` + `spot` → `https://api.binance.com`
  - `live` + `usdm_futures` → `https://fapi.binance.com`
  - `paper` bindings are unconditionally refused (`ExternalEnvironmentMismatch`) by `BinanceVenueAdapter`.
  - A `base_url_override` is accepted **only** when `allow_loopback_override=True`, `environment == 'external_test'`, and the target host is strictly loopback (`127.0.0.1` or `localhost`). A `live` binding can never be redirected to an arbitrary host, and an `external_test` binding can never reach production `api.binance.com` or `fapi.binance.com`.
- **Opaque credential boundary (`credentials.py`):**
  - Only opaque handles (`keychain:...` or `envref:...`) may be persisted in company SQLite or passed across config boundaries.
  - `ApiCredentialPair` masks its `api_key` and `api_secret` in `__repr__` / `__str__`. Secrets are resolved strictly at HMAC-SHA256 signing time outside SQLite, Agent Desks, and audit events.
  - Zero live credentials or live funds are permitted in source, fixtures, or CI tests.

## 3. Protocol normalization & failure semantics

- **Deterministic order identity:** Maps company `action_id` deterministically to a valid Binance `newClientOrderId` (`sd_<sha256[:28]>`).
- **Unknown outcomes preserve reservation:** Network timeout, HTTP `429`, or `5xx` during `submit_order` raises `ExternalOrderReplyUnknown` without fabricating a rejection or resending a second order. Recovery queries `query_order` + `query_fills` by original `clientOrderId`.
- **Unsealed absence:** Binance error `-2013` (order does not exist) normalizes to `status = 'absent_unsealed'` rather than claiming guaranteed permanent absence when the venue lacks delayed-send fencing.
- **Cancel != flat:** Partial fills followed by terminal cancellation (`CANCELED` / `EXPIRED`) preserve exact `filled_quantity` and `remaining_quantity` so open residual exposure stays visible.
- **External reconciliation:** `reconcile_external(known_order_ids)` detects venue-originated fills or unexpected non-zero positions outside company-admitted orders and reports them explicitly without silently mutating company books.

## 4. Qualification runner & Flow 1 integration

`VenueQualificationRunner` executes six independent qualification cases against an `external_test` adapter binding (normal fill, over-limit rejection, partial fill + terminal cancel, lost-reply recovery without duplicate send, unsealed absence, and environment/credential isolation + external-fill detection), seals the canonical `evidence_sha256` and `compute_trading_core_digest()`, and records an immutable `venue_qualification_receipt` via `execution_tiers.record_venue_qualification_receipt`.

## 5. Acceptance evidence

- Unit tests: `tests/unit/test_credentials.py`, `tests/unit/test_binance_venue.py`
- Connected flow test: `tests/flows/flow-live-venue/test_flow.py` (`ConnectedFlow.test_end_to_end`) verifying Spot + USD-M Futures qualification against `BinanceWireEmulator`, receipt sealing, and end-to-end UDS `promote_execution_environment` activation with zero secret leakage into SQLite.
