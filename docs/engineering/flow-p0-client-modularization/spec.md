# Flow Spec: P0 Client Modularization, ETag Caching & Vitest (`flow-p0-client-modularization`)

## 1. Intent & Scope

Decompose `apps/client/src/` from monolithic state, API type, chart, shell, and view files
into clean domain contexts, feature slices, shared utilities, ETag-aware projection hooks,
and an automated Vitest + Testing Library unit test foundation — with 100% visual, route,
keyboard-shortcut, Yogurt-token, and control-plane behavior parity.

### In Scope
1. **Domain Context Split (`apps/client/src/state/`):**
   - Split monolithic `state/store.tsx` (1,400 LOC) into 5 focused contexts:
     - `RouterContext` (`route`, `navigate`, `goBack`, `useEscapeBack`, `splitRoute`, `decodeSingleSegment`)
     - `PreferencesContext` (`theme`, `setTheme`, `privacyMode`, `togglePrivacyMode`, `toasts`, `pushToast`, `dismissToast`)
     - `ControlPlaneContext` (`meta`, `linkState`, `eventSeq`, `refreshSeq`, `bumpRefresh`, `retryConnect`, `lastEvent`)
     - `OutboxContext` (durable `/api/local/pending` outbox state machine, command submission, conflict/replay reconciliation, `outboxPanelOpen`)
     - `ApprovalsCacheContext` (shared pending approvals subscription/cache)
   - Keep `state/store.tsx` as a thin `AppProvider` composition + backward-compatible `useApp()` facade while migrating high-frequency subscribers (shell, topbar, views) to narrow hooks so `/v1/events` ticks do not rerender unrelated subtrees.
2. **ETag / `304 Not Modified` Projection Hook (`apps/client/src/api/useProjection.ts`):**
   - Introduce shared `useProjection<T>(path, opts)` hook with per-path in-memory ETag + payload cache, `If-None-Match` conditional GETs, `304 Not Modified` cache hits preserving referential equality, `AbortController` cancellation on unmount/path change, and `refreshSeq` reactivity.
   - Adopt `useProjection<T>` across projection views and `ApprovalsCacheContext`.
3. **Shared Utilities & Type Split (`apps/client/src/lib/`, `apps/client/src/api/types/`):**
   - Consolidate duplicated formatters and helpers into:
     - `lib/format.ts` (`normalizeToNs`, `formatMinorCurrency`, `formatSignedPct`, `formatRelativeNs`, `formatNsClock`, `formatDeadlineNs`, `formatTimestamp`, `formatNsWithClock`, `newCommandId`)
     - `lib/statusTones.ts` (shared Yogurt status tone mappers)
     - `lib/domFocus.ts` (focus restore & overlay helpers)
     - `lib/routes.ts` (canonical route definitions, titles, keyboard shortcuts, and nav metadata)
   - Split `api/types.ts` (1,016 LOC) into `api/types/{common,agents,company,system,trading,index}.ts` with 100%-compatible re-exports via `api/types.ts` / `api/types/index.ts`.
   - Extract shared `components/ProjectionBoundary.tsx` and deduplicate shared transcript styles between `styles/app.css` and `styles/graph.css`.
4. **Feature Slice Decomposition of Monolithic Components & Views:**
   - `components/shell/`: split `AppShell.tsx` (1,113 LOC) into `AppShell`, `AppSidebar`, `AppTopbar`, `CommandPalette`, `PrivacyShield`.
   - `components/chart/`: split `PortfolioChart.tsx` (1,100 LOC) into pure `math.ts` + SVG/Scrubber presentation components.
   - `features/agents/`:
     - `features/agents/graph/`: extract pure graph layout math (`layout.ts`) and split `AgentsActivityView.tsx` (1,881 LOC).
     - `features/agents/detail/`: split `AgentDetailView.tsx` (1,424 LOC) into memoized tab components (`OverviewTab`, `WorkTab`, `AuthorityTab`, `HandoffsTab`, etc.).
   - `features/company/`: split `CompanyViews.tsx` (1,737 LOC) into `FinanceView`, `OrgReportsView`, `RestrictionsView`.
   - `features/system/`: split `SystemViews.tsx` (1,489 LOC) into `SystemSafetyView`, `SystemEvalsView`, `SystemAuditView`, `AuditHubView`, `BootstrapView`.
   - `features/trading/`: split `TradingDeskView.tsx` (1,859 LOC) — including standalone `NewPaperBookCard` — and `StrategiesView.tsx` (1,116 LOC) into cohesive feature subcomponents.
   - Preserve thin re-export facades in `views/*.tsx` and `components/{AppShell,PortfolioChart}.tsx` where helpful for clean migration.
5. **Vitest + Testing Library Test Foundation (`apps/client/`):**
   - Configure `vitest` + `@testing-library/react` + `@testing-library/jest-dom` + `jsdom` with `npm run test` (`vitest run`).
   - Add deterministic unit & hook tests covering `lib/format.ts`, `features/agents/graph/layout.ts`, `components/chart/math.ts`, `api/useProjection.ts` (`200` -> ETag -> `304` cache hit & abort cleanup), and `state/OutboxContext.tsx` command state machine.

### Out of Scope
- Backend projection schema changes or new P1 Always-On / Skill / RSI UI panels (delivered in PR-5 `flow-p1-always-on-ui`).
- Visual redesign or Yogurt token edits (strict visual parity and `npm run check:yogurt` compliance).
- Committing binary `.tgz` packages (remains gitignored per workspace hygiene rules).

## 2. Authority, Privacy & Data Boundaries

- **Read-Only Projections & ETag Integrity:** `useProjection<T>` caches projections strictly in client memory scoped to the current company/session and clears or isolates cache entries when `company_id` changes so cross-company data never leaks across sessions.
- **No Unredacted Secrets or Raw Private Content:** Refactoring must preserve all `PrivacyShield` redactions, `one_agent` redacted badges, and `"Unavailable"` numeric honesty (never coercing `null`/`undefined` to `0`).
- **Outbox Atomicity & Idempotency:** Splitting `OutboxContext` from `store.tsx` must preserve durable `/api/local/pending` persistence, single-flight in-progress locking, `Idempotency-Key` headers, and conflict/replay handling.

## 3. Allowed & Prohibited Behavior

- **Allowed:** Narrow context hooks (`useRouter`, `usePreferences`, `useControlPlane`, `useOutbox`, `useApprovalsCache`) alongside `useApp()` facade; conditional `If-None-Match` GET requests returning `304`.
- **Prohibited:**
  - Hardcoded hex/rgb fallbacks inside `var(--yg-..., #...)`, CSS gradients, static card shadows, or unlayered CSS violating `@openboa/yogurt` rules (`npm run check:yogurt` must stay green).
  - Coercing `null`/`undefined` financial or metric readings to `0`.
  - Committing `apps/client/vendor/*.tgz`, `node_modules/`, or `dist/`.

## 4. Lifecycle Failures & Compatibility

- **`304 Not Modified` Handling:** When the server returns `304`, `useProjection` keeps the previously cached payload without clearing UI state or triggering error states.
- **Aborted Requests & Stale Responses:** Fast route navigation aborts in-flight requests via `AbortController` and ignores late resolutions from superseded requests.
- **Backend Disconnect / Reconnect:** `ControlPlaneContext` retains transient retry backoff and reconnect behavior; `OutboxContext` reconciles pending commands against `/api/v1/commands/<id>` on reload.

## 5. Acceptance Evidence

1. **Client Verification Gates (`apps/client`):**
   - `npm run typecheck` (`tsc --noEmit`): 0 errors.
   - `npm run build` (`tsc --noEmit && vite build`): clean production bundle.
   - `npm run check:yogurt`: 0 token/CSS/primitive violations.
   - `npm run test` (`vitest run`): all unit & React hook/context tests pass (`lib/format`, `chart/math`, `agents/graph/layout`, `useProjection` ETag/`304`, `OutboxContext`).
2. **Connected End-to-End Flow (`tests/flows/flow-p0-client-modularization/test_flow.py`):**
   - Verifies live `ControlPlaneServer` ETag / `If-None-Match` / `304 Not Modified` parity across modularized client projection endpoints (`agents/graph`, `approvals`, `messages`, `system/audit`) and verifies client modular source hygiene (existence of domain contexts, `useProjection`, `lib/format`, Vitest test suite, and zero literal hex fallbacks in client CSS).
3. **Python Regression & Repo Hygiene:**
   - Full `tests/unit`, `ruff==0.14.10`, `reference_guard.py staged`, and `gitleaks` pass cleanly.
