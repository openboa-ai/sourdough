# Present Board approvals, safety gates, and read-only delegated HR custody

Design draft · Verification **UNRUN** · Responsible component: Board Client

[Feature intent](../intent.md) · [Client architecture](../../architecture.md) · [Client screens and navigation](../../inspect/screens/spec.md) · [Command submission](../../commands/submit/spec.md) · [System map](../../../README.md) · [Development lifecycle](../../../engineering/lifecycle.md)

## Boundary and dependencies

Define the Board-vs-CEO governance UX inside **Company Management** (`/company/approvals`, `/company/approvals/:questionId`, `/company/mandate`, `/company/roles`, `/company/org`, `/company/org/publications/:assetId`) and **Restrictions** (`/restrictions`), covering 1-page `DecisionQuestion` approval cards, `Interrupt` / `Approval` / `Digest` routing, delegated HR custody (read-only offboarding ledger with zero Board `retire_employee` write command), and plain-language safety explanations.

Input contracts: [Base presentation](../../inspect/present/spec.md) · [Command submission](../../commands/submit/spec.md) · [CEO Direct Line](../ceo-direct-line/spec.md) · [Offboard](../../../company/systems/organization/offboard/intent.md) · [Amend mandate](../../../company/systems/organization/found-company/amend/spec.md) · [Restore company](../../../company/systems/operations/restore-company/restore/spec.md). These are public service contracts, not imports of service implementations.

## Design decision and review

### 1. Inline expandable `DecisionQuestion` approval cards — zero popup modals (`/company/approvals` & `/company/approvals/:questionId`, `[BACKEND_GAP #4]`)

- **Sidebar badge notification**: Pending Board sign-off count renders as a numeric `Badge` directly on the left-sidebar **`Approvals`** navigation item rather than duplicating approval shortcut buttons inside `/`.
- **In-place inline expansion (zero modal popups)**: Clicking any pending `DecisionQuestion` or `ChallengerTrial` approval card expands it smoothly **in-place inside the page flow** (never opening a dimmed modal popup or right slide-over drawer), presenting:
  1. Plain-language decision summary, quantified impact, and `respond_by_ns` deadline.
  2. Side-by-side **`What changes`** vs **`What stays untouched`** impact summary wells (`RecordFacts` `<dl>`).
  3. Inline decision reason field (`payload.reason`, `1..2000` chars) and a closed-by-default **`Technical details`** disclosure row (`question_id`, `revision`, `payload_hash`, release digests).
  4. Inline action footer: **`"Ask CEO in Messages ->"`** (escalating to `/messages` CEO thread with a pinned read-only Context Attachment Card) on the left, and equal-weight **`Decline`** + `#175174` Ink Blue consequence-verb confirm **`SDKButton`** (rejecting `details?.source === "agent"`) on the right.
- **Separate application vs. delivery ledger**: Below the active queue, render `AcceptedAnswer`, `AnswerApplication`, and `AnswerDelivery` as distinct columns (`[BACKEND_GAP #4]`), and on hiring questions keep `hire_receipt` (`appointed`) strictly separate from `hire_onboarding.state` (`provisioning | blocked | ready | accepted`, `attempts/3`, `[V1_SQLITE_NEEDS_UDS]`).

### 2. Delegated HR & read-only offboarding custody (`/company/org`)

- **CEO-delegated execution (`retirement.py`, `docs/company/systems/organization/offboard/intent.md:17`)**: Routine employee hiring execution (`staffing.py`) and employee retirement (`retirement.py:retire_agent` requiring Agent `TERMINATION_SCOPE = "retire-agent"`, blocking `founder_id` and `manager_id`) are **solely CEO/manager-operated inside `CompanyRuntime`**.
- **Removal of `retire_employee` from Board UI & commands**: The Board Client MUST NOT render a `"Retire Employee"` button, `ControlledConfirmDialog`, or `POST /v1/commands` mutation for `retire_employee`. Emergency Board authority cutoff remains `revoke_grant` (`expected_authority_epoch`, `[BACKEND_GAP #4]`).
- **Read-only Custody & Handover Audit Ledger**: `/company/org` renders an explicit header banner **`"Managed by CEO · Board view is read-only"`** above `agent_archive` history (`employment_revision`, `cutoff_seq`, `retired_by`) and unsettled `work_items` custody status (`'blocked'` + `work_custody_transfer` `custody_status = "awaiting_reassignment"` in `[BACKEND_GAP #8]` vs `'cancelled'` in `origin/main` `work_kernel.py:390-395`), preserving historical authorship and `shared_publication` links.

### 3. Executive safety explanations & alert-fatigue routing (`Interrupt` / `Approval` / `Digest`)

- **Clustered `Interrupt` routing**: Cascading downstream failures or stops share a consolidated `Notice[urgent]` below the global header grouped by root `restriction_id` (`RestrictionView`, `[BACKEND_GAP #7]`) rather than flashing stacked banners across normal work screens.
- **`Approval` & `Digest` routing**: Non-urgent reserved matters route calmly to `/company/approvals` (`Approval`), while completed overnight runs, settled orders, and resolved restrictions roll up into `/` **Since Last Visit** (`Digest`, `[BACKEND_GAP #8]`).
- **Plain-language safety explanations inside confirmation & restriction views**:
  1. **`resume_new_risk` (`[LIVE_UDS]`)**: Displays non-binding settlement readiness (`settled_company_ok`, `unresolved_orders_count`, `pending_binding_change`, `last_quote_age_ns`) and explains in plain language that the server verifies a fresh market quote within **`15s` (`QUOTE_MAX_AGE_NS`)** inside `POST /v1/commands` — **with zero pre-fetched 15-second modal countdown timer**.
  2. **`release_restore_quarantine` (`[BACKEND_GAP #7]`)**: Presents the 5-step **"Safe Custody Hold"** readiness checklist (`1. Data cut`, `2. Sole writer`, `3. Current authority`, `4. External fill/fee reconciliation`, `5. Eligible next actions`), keeping `Release Quarantine` disabled until dimensions 1–4 verify (`VERIFIED`).
  3. **`review_tool.quarantine` (`/system/tools`, `[V1_SQLITE_NEEDS_UDS]`)**: Embeds a pre-computed indented DAG **Blast-Radius Tree Preview** (`parent_asset_id` transitive descendants) and the exact automatic global `revision DESC` fallback target (`restored_candidate_id` or `"No change to active tool"` if the active tool lies outside the quarantined subgraph) before human confirmation.

**Concern for the responsible component:** Never expose `retire_employee` as a Board write button, collapse `AcceptedAnswer` with `hire_onboarding`, or replace server-enforced `15s` / 5-step / DAG safety previews with opaque technical codes.

## Functional and quality requirements

- **FR.** Render 1-page `DecisionQuestion` approval cards with equal-weight `Approve` / `Decline` / `"Ask CEO about this item"`, separate `AcceptedAnswer` / `AnswerApplication` / `AnswerDelivery` and `hire_onboarding` statuses, read-only Delegated HR Custody & Offboarding Audit Ledger (`"Managed by CEO · Board view is read-only"`), and plain-language safety gates (`15s` server quote check, 5-step "Safe Custody Hold", DAG Blast-Radius Tree Preview).
- **NFR.** Enforce `board_control` + Confirmation Sheet gating on every mutating governance/safety action (`submit_decision_answer`, `amend_charter`, `revoke_grant`, `suspend_new_risk`, `resume_new_risk`, `pause_company`, `resume_company`, `release_restore_quarantine`, `review_tool.quarantine`), while keeping `retire_employee` absent from Board writes.

## Acceptance and independent evidence

| Case | Setup, action and expected observation |
| --- | --- |
| Delegated HR read-only ledger | Inspect `/company/org` after a CEO-executed `retirement.py:retire_agent`. Confirm `"Managed by CEO · Board view is read-only"` banner, visible `agent_archive` & custody ledger, and **zero** `retire_employee` write buttons or dialogs. |
| 1-page Approval & Confirmation Sheet | Open `/company/approvals/:questionId` and click `Approve`. Confirm Universal Confirmation Sheet renders *What changes*, *What stays untouched*, consequence-verb CTA, and collapsed `Technical details` disclosure. |
| Safety explanations | Inspect `resume_new_risk` (zero client 15s countdown), `release_restore_quarantine` (5-step "Safe Custody Hold" locked until 1–4 verified), and `review_tool.quarantine` (DAG Blast-Radius Tree Preview + fallback target). |

**Oracle:** Cross-verify `/company/*`, `/restrictions`, `/system/checkpoints`, and `/system/tools` views against [Command submission](../../commands/submit/spec.md), [Client screens and navigation](../../inspect/screens/spec.md), and [Usage scenarios](../../scenarios.md).

## Operations and change review

Any future governance command MUST define its *What changes* / *What stays untouched* `<dl>` rows and `Interrupt` / `Approval` / `Digest` routing tier before UI exposure.
