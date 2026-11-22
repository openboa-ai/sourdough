# Publish immutable as-of observations

Design draft · Verification **UNRUN** · Responsible component: Market and account reader

[Feature intent](../intent.md) · [MVP map](../../../../../../README.md) · [SDLC handoff](../../../../../../engineering/lifecycle.md)

## Boundary and dependencies

Ingest source event ID, exchange time, arrival time, instrument and value. A snapshot query joins visible market and account observations with coverage and age.

Input contracts: [Persist the founding charter](../../../../../systems/organization/found-company/bootstrap/spec.md).

## Design decision and review

Index by availability time as well as event time rather than rewriting a historic snapshot after late data.

**Concern for the responsible component:** Declare a freshness limit for each fixture stream before the trial; an unmeasured or missing source must remain unknown.

## Functional and quality requirements

- **FR.** Retain late corrections as new revisions. Make the exact decision snapshot available to research and order admission.
- **NFR.** Events received after the cut stay invisible to that cut. Missing intervals or unavailable account observations retain explicit unknown status.
- **FR.** For each prospective feed record the source identity, instrument and units, event time, availability time, revision, coverage interval and missing or corrected ranges. Resume from a durable cursor after restart without rewriting the original arrival history; bind each evaluation window to its registered source and cut. A fixed fault fixture is labeled separately from a prospective observation.

## Acceptance and independent evidence

**Oracle:** Build an expected event set from feed arrival times at a sealed cut, then compare it by ID and revision; check it again after delivering a correction.

| Case | Setup, action and expected observation |
| --- | --- |
| Normal | Inject two bars, a gap and a late bar. The original snapshot retains the gap; a later snapshot exposes the corrected coverage. |
| Failure or contention | Resubmit an event with conflicting bytes. Preserve the original and flag the conflict. A stale required risk input blocks exposure increases. |
| Prospective cut | Register a window, deliver a late correction and a feed gap, then restart ingestion and redeliver an event. Independently verify original decision-time visibility, one event identity, explicit gap coverage and no fixture observations counted as market evidence. |

## Operations and change review

Measure feed age and gap duration. Mark affected observations stale and continue independent research.

Attach observed results and unresolved cases to the tested revision. Apply the shared lifecycle for review, delivery, rollback and incident follow-up.
