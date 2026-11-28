# Flow Spec: P1 Always-On, Proactive Continuity & Skill/Dreaming/RSI UI (`flow-p1-always-on-ui`)

## 1. Intent & Scope

Expose Sourdough's Always-On resident daemon health, proactive work continuity telemetry,
runtime bindings, active skills/methods, work decisions, episodic memory summaries,
redacted dreaming activity, peer delegation depth, 3-stage method learning qualifications,
quarantine blast radius, and RSI transfer verdicts across the modularized Board Client UI —
while locking down strict Board redaction boundaries via automated Vitest DOM leakage tests.

### In Scope
1. **Backend Projection Enrichment (`src/sourdough/ipc/projections/agents.py`):**
   - Enrich `work_items` with `attempt_count` and `next_retry_at_ns`.
   - Enrich `peer_requests` with `hop_count`, `chain_depth`, `root_work_item_id`, and
     `delegation_path_length`.
   - Project non-secret `runtime_binding` (`provider`, `model`, `revision`, `sandbox_backend`,
     `profile_id`, `updated_at_ns`) from `agent_runtime_binding` — strictly omitting
     `credential_handle_ref`.
   - Project `responsibilities` joined with `work_discovery_state` (`consecutive_noops`,
     `cooldown_until_ns`, `window_started_ns`, `window_wakes`).
   - Project redacted `dreaming_summary` aggregated from `work_runs` where
     `duty_kind = 'work.dreaming'` (passing `last_outcome` through `_redact_run_outcome_for_board`).
2. **Client Type Definitions (`apps/client/src/api/types/{common,agents,system}.ts`):**
   - Add `ResidentHealth` on `ControlPlaneMeta`.
   - Add typed projections for `runtime_binding`, `responsibilities`, `dreaming_summary`,
     `active_methods`, `active_snapshot_id`, `work_decisions`, `agent_episodes`, extended
     `armed_conditions`, `method_deltas`, `method_qualifications`, `method_activations`,
     and `rsi_transfer_summaries`.
3. **Always-On Observability UI (`components/shell/AppSidebar.tsx`, `features/system/`):**
   - Surface `meta.resident_health.sleep_state` (`Awake` / `Sleeping`) cleanly in `AppSidebar`.
   - Add `DaemonDiagnosticsPanel` in `SystemAuditView` (`/audit?tab=daemon`) surfacing
     `sleep_state`, `ticks_completed`, `runs_executed`, `triggers_enqueued`, `wake_count`,
     `last_wake_reason`, `recovered_runs_count`, `last_tick_at_ns`, `last_good_tick_at_ns`,
     and `last_error`.
4. **Agent Detail Expansion (`features/agents/detail/`):**
   - `AgentDeskAuthoritySection.tsx`: display non-secret `runtime_binding` and standing
     `responsibilities` / discovery cooldown & wake-budget posture.
   - `AgentMethodsTab.tsx`: display `active_methods` + `active_snapshot_id` honoring
     `one_agent` redaction (`body_redacted`, `method_id_redacted`, digest-only display).
   - `AgentWorkTab.tsx`: display `work_items` retry/backoff (`attempt_count`,
     `next_retry_at_ns`), extended `armed_conditions` (`cooldown_ns`, `fire_count`/`max_fires`),
     redacted `work_decisions`, redacted `agent_episodes`, and redacted `work.dreaming` summary.
   - `AgentHandoffsTab.tsx`: display peer chain depth / hop count alongside 5-state handoffs.
5. **System Evals & Audit Expansion (`features/system/`):**
   - `SystemEvalsView.tsx`: add `Method Learning` tab (`MethodLearningPanel`: `method_deltas`,
     3-stage `method_qualifications`, `method_activations` with `blast_radius_descendants`
     and `fallback_revision`) and `RSI Transfer` tab (`RsiTransferPanel`: `rsi_transfer_summaries`
     verdict, wins/losses/ties, LCB/UCB, permutation p-value).
   - `SystemAuditView.tsx`: add `"learning"` to `AUDIT_DOMAINS`.
6. **DOM Privacy Redaction Regression Tests (Vitest):**
   - Verify no `one_agent` private method body or raw method ID, no `work_decision` raw
     rationale, no `agent_episode` raw summary, and no private Desk filename or credential
     handle renders anywhere in DOM output across Agent Detail and System Evals tabs.

### Out of Scope
- Editing SQLite schema DDL (all queried columns already exist in v1 STRICT tables).
- Interactive mutations for method promotion/quarantine from Board Client (read-only telemetry).
- External resource connectors or hybrid execution planes (P2-A / P2-B).

## 2. Authority, Privacy & Data Boundaries

- **Zero Private Disclosure to Board:**
  - `agent_runtime_binding.credential_handle_ref` MUST NEVER be projected to Board IPC.
  - `one_agent` method bodies (`body_text`), original `method_id`s, `trigger_spec`,
    `uncertainty_note`, `falsification_criteria`, `work_decision.rationale`, and
    `agent_episode.summary_text` remain strictly redacted on the backend AND client components
    must defensively refuse to render unredacted `body_text` when `audience_scope === "one_agent"`
    or `body_redacted === true`, and never render `rationale` or raw `summary_text` when
    redacted flags are set.
  - Private Desk file enumeration (`_count_private_drafts_for_projection`) remains outside
    the SQLite read snapshot and discloses only an integer count.
- **Strictly Read-Only Projections:** All changes in `src/sourdough/ipc/projections/` are
  read-only queries (`0` SQL mutations).

## 3. Allowed & Prohibited Behavior

- **Allowed:** Displaying opaque IDs, SHA-256 digests, redacted badges, token/cost counts,
  cooldown/backoff schedules, peer hop/chain depths, and statistical RSI transfer verdicts.
- **Prohibited:**
  - Rendering raw `one_agent` method bodies/IDs, `work_decision` rationale strings,
    `agent_episode` raw summaries, private Desk filenames, or credential handles.
  - Violating Yogurt 0.2.0 design constraints (`npm run check:yogurt`, 0 literal hex/rgb
    fallbacks, 0 CSS gradients, 0 static card shadows, ≤ 1 status badge per compact row).

## 4. Lifecycle Failures & Compatibility

- **Optional Backend Fields:** Client views gracefully handle older or partial projections
  where `resident_health`, `runtime_binding`, `responsibilities`, `dreaming_summary`,
  `active_methods`, `work_decisions`, `agent_episodes`, `method_deltas`, or
  `rsi_transfer_summaries` are `null`/`undefined`/empty (rendering explicit honest empty or
  `"Unavailable"` states — never coercing missing numbers to `0`).

## 5. Acceptance Evidence

1. **Client Verification Gates (`apps/client`):**
   - `npm run typecheck`, `npm run build`, `npm run check:yogurt`.
   - `npm run test` (`vitest run`) including new DOM privacy redaction suites
     (`AgentDetailPrivacy.test.tsx`, `SystemEvalsPrivacy.test.tsx`).
2. **Backend & Connected Flow (`tests/flows/flow-p1-always-on-ui/test_flow.py`):**
   - Boots live `ResidentCompanyService`, verifies `/v1/meta` `resident_health`,
     `/v1/projections/agents/<id>` enriched fields (`runtime_binding`, `responsibilities`,
     `dreaming_summary`, `work_items` retry/backoff, `peer_requests` chain/hop depth,
     `active_methods`, `work_decisions`, `agent_episodes`), `/v1/projections/system-evals`
     (`method_deltas`, `method_qualifications`, `method_activations`, `rsi_transfer_summaries`),
     `/v1/projections/system-audit` `"learning"` domain events, and zero private canary leakage.
3. **Visual Verification:** Live rendered screenshots inspected across Light & Dark modes at
   Desktop (`1440px`) and Mobile (`390px`) widths.
