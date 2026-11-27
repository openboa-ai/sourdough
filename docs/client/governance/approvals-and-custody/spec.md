# Present Board approvals, safety gates, and read-only delegated HR custody

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Command submission](../../commands/submit/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Define the Board-vs-CEO governance UX across **Approvals** (`/approvals`, `/approvals/:questionId`, and inline `ContextualApprovalCard`), **Reports / Delegated HR Custody** (`/reports`), **Controls** (`/controls`), **Backups** (`/backups`), and **Audit & Evaluations** (`/audit?tab=evals`), covering dual-entry `DecisionQuestion` approval cards (including `live_promotion` `application_status = 'failed'` handling), `Interrupt` / `Approval` / `Digest` routing, delegated HR custody (read-only offboarding ledger with zero Board `retire_employee` write command), and plain-language safety explanations.

Input contracts: [Base presentation](../../inspect/present/spec.md) · [Command submission](../../commands/submit/spec.md) · [Company Messages](../ceo-direct-line/spec.md) · [Offboard](../../../company/systems/organization/offboard/intent.md) · [Restore company](../../../company/systems/operations/restore-company/restore/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

### 1. Dual-entry `DecisionQuestion` approval cards — zero popup modals (`/approvals`, `/approvals/:questionId`, `ContextualApprovalCard`)

- **Sidebar & topbar badge notification**: Pending Board sign-off count renders as a numeric `Badge` on the left-sidebar **`Approvals`** item and in Header Slot 2 (`Approvals (N)` pill when `pendingApprovals > 0`).
- **Dual-entry in-place expansion (zero modal popups)**: Rendered both in `ApprovalsView` (`/approvals`, `/approvals/:questionId`) and contextually via `ContextualApprovalCard` inside matching views (`TradingDeskView`, `StrategiesView`, `AgentDetailView`, `RestrictionsView`). Approving or declining from either entry dispatches the identical `submit_decision_answer` command (`question_id`, `expected_revision`, `expected_payload_hash`):
  1. Plain-language decision summary, quantified impact (`impact`), and `respond_by_ns` deadline.
  2. Side-by-side **`What changes`** vs **`What stays untouched`** impact summary wells (`RecordFacts` `<dl>`) inside `ControlledInlineConfirm`.
  3. Inline decision reason field (`payload.reason`) and a closed-by-default **`Technical details`** disclosure row (`question_id`, `revision`, `payload_hash`).
  4. Inline action footer: **`"Ask CEO about this item"`** (navigating to `/messages?context_kind=decision_question&context_id=...`) and equal-weight **`Decline`** + `#175174` Ink Blue consequence-verb confirm **`SDKButton`** (rejecting `details?.source === "agent"`).
- **Refused domain action persistence (`application_status = 'failed'`)**: When `board.apply_decision` catches `ExecutionTierRefused` / `DomainActionRefused` (e.g., an active restriction or open order blocks `live_promotion`), `08_board_client.sql` persists `decision_question.application_status = 'failed'` cleanly and `ApprovalsView` / `TradingDeskView` surface the refusal reason without activating `company_live`.
- **Separate application vs. onboarding readiness**: On hiring decisions, `decision_question.application_status` stays separate from `hire_onboarding.state` (`provisioning | blocked | ready | accepted`).

### 2. Delegated HR & read-only offboarding custody (`/reports`)

- **CEO-delegated execution (`retirement.py`)**: Routine employee hiring execution (`staffing.py`) and employee retirement (`retirement.py:retire_agent` requiring Agent `TERMINATION_SCOPE = "retire-agent"`, blocking `founder_id` and `manager_id`) are **solely CEO/manager-operated inside `ResidentCompanyService`**.
- **Removal of `retire_employee` from Board UI & commands**: The Board Client MUST NOT render a `"Retire Employee"` button, confirmation surface, or `POST /v1/commands` mutation for `retire_employee`.
- **Read-only Custody & Handover Audit Ledger**: `OrgReportsView` (`/reports`, `org-v1`) renders an explicit banner **`"Managed by CEO · Board view is read-only"`** above `delegated_hr_custody` (`agent_archive` history and unsettled work custody status), preserving historical authorship and `shared_publication` links.

### 3. Executive safety explanations & alert-fatigue routing (`Interrupt` / `Approval` / `Digest`)

- **Clustered `Interrupt` routing**: Active `restriction_cause` rows (`RestrictionsView` `/controls`) and transient disconnect/error states share a consolidated sub-header `Notice` (`"Operational restrictions are currently active"` -> `/controls`) rather than stacking banners.
- **`Approval` & `Digest` routing**: Non-urgent reserved matters route calmly to `/approvals` (`Approval`), while completed runs and fills update live projections smoothly via `/v1/events`.
- **Plain-language safety explanations inside safety views**:
  1. **Trading execution resume (`/controls`, `resume_company` on `execution:*`):** When `"trading"` is installed (`data.trading_risk`), displays non-binding readiness telemetry (`settled_company_ok`, `unresolved_orders_count`, `unresolved_external_actions_count`, `server_freshness_note`) and explains that `TradingRestrictionGuard` verifies zero open orders, zero unresolved live external actions, and a fresh market quote within **`15s` (`QUOTE_FRESHNESS_MAX_NS`)** inside `POST /v1/commands` — with zero pre-fetched 15-second client countdown timer.
  2. **5-step "Safe Custody Hold" (`/backups`, `SystemSafetyView`):** Presents `checkpoints`, `restore_receipts`, `store_retired`, and the 5-step **"Safe Custody Hold"** readiness diagnostics (`1. Data cut`, `2. Sole writer`, `3. Current authority`, `4. External reconciliation`, `5. Eligible next actions`).
  3. **DAG Blast-Radius Tree Preview (`/audit?tab=evals`, `system-evals-v1`):** Displays `ThreeStageVerdict` (`pass^3`) and `blast_radius_descendants` DAG tree preview alongside Flow 13 offline evaluation metrics.

**Concern for the responsible component:** Never expose `retire_employee` as a Board write button, collapse `decision_question.application_status` with `hire_onboarding`, or replace server-enforced `15s` / 5-step / DAG safety previews with opaque technical codes.

## Functional and quality requirements

- **FR.** Render dual-entry `DecisionQuestion` approval cards with equal-weight `Approve` / `Decline` / `"Ask CEO"`, clean `application_status = 'failed'` surfacing, read-only Delegated HR Custody (`"Managed by CEO · Board view is read-only"` on `/reports`), and plain-language safety gates (`15s` server quote check on `/controls`, 5-step "Safe Custody Hold" on `/backups`, DAG Blast-Radius Tree Preview on `/audit?tab=evals`).
- **NFR.** Enforce `board_control` + `ControlledInlineConfirm` gating on mutating governance/safety actions while keeping `retire_employee` absent from Board writes.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Delegated HR read-only ledger | Inspect `/reports` after a CEO-executed `retirement.py:retire_agent`. Confirm `"Managed by CEO · Board view is read-only"` banner, visible `delegated_hr_custody` ledger, and **zero** `retire_employee` write buttons or dialogs. |
| Dual-entry Approval & `application_status = 'failed'` | Open `/approvals/:questionId` and approve a `live_promotion` decision when blocked by a restriction -> confirm `ControlledInlineConfirm` renders *What changes* / *What stays untouched*, `submit_decision_answer` succeeds, and `application_status = 'failed'` surfaces cleanly. |
| Safety explanations | Inspect `/controls` (`data.trading_risk` `15s` note with zero client countdown), `/backups` (5-step "Safe Custody Hold"), and `/audit?tab=evals` (DAG Blast-Radius Tree Preview). |

**Oracle:** Cross-verify `/approvals`, `/reports`, `/controls`, `/backups`, and `/audit?tab=evals` against [Command submission](../../commands/submit/spec.md), [Client screens and navigation](../../inspect/screens/spec.md), and [Usage scenarios](../../scenarios.md).

## Operations and change review

Any future governance command MUST define its *What changes* / *What stays untouched* `<dl>` rows and `Interrupt` / `Approval` / `Digest` routing tier before UI exposure.
