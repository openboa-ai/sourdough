import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentDetailProjection } from "../../../api/types";
import { clearProjectionCache } from "../../../api/useProjection";
import { ControlPlaneProvider } from "../../../state/ControlPlaneContext";
import { OutboxProvider } from "../../../state/OutboxContext";
import { PreferencesProvider } from "../../../state/PreferencesContext";
import { RouterProvider } from "../../../state/RouterContext";
import { AgentDeskAuthoritySection } from "./AgentDeskAuthoritySection";
import { AgentHandoffsTab } from "./AgentHandoffsTab";
import { AgentMethodsTab } from "./AgentMethodsTab";
import { AgentWorkTab } from "./AgentWorkTab";

function Providers({ children }: { children: ReactNode }) {
  return (
    <PreferencesProvider>
      <RouterProvider>
        <ControlPlaneProvider onClearCache={clearProjectionCache}>
          <OutboxProvider>{children}</OutboxProvider>
        </ControlPlaneProvider>
      </RouterProvider>
    </PreferencesProvider>
  );
}

const CANARY_PRIVATE_METHOD_BODY = "CANARY_SECRET_ONE_AGENT_METHOD_BODY_99182";
const CANARY_DECISION_RATIONALE = "CANARY_SECRET_DECISION_RATIONALE_77412";
const CANARY_EPISODE_NARRATIVE = "CANARY_SECRET_EPISODE_NARRATIVE_55309";
const CANARY_PRIVATE_DRAFT_NAME = "canary-private-alpha-notes.md";

const SAMPLE_DETAIL: AgentDetailProjection = {
  projection: "agent_detail",
  company_id: "co-p1",
  as_of_ns: 1_700_000_100_000_000_000,
  event_seq: 42,
  profile: {
    agent_id: "agent-quant",
    display_name: "Quant Lead",
    role_label: "Quant Researcher",
    avatar_variant: "wide-oval-top",
    avatar_state: "ready",
    live_status: "ready",
    current_task: "Reviewing risk posture",
    published_count: 1,
    is_ceo: false,
    archived: false,
    grant_scope: "assessment",
    monthly_budget_minor: 500_000,
    private_draft_count: 2,
    active_run: null,
    execution_profile: "profile-hash-1",
    grant_id: "grant-1",
    archive_reason: null,
    onboarding_state: "accepted",
    onboarding_purpose: "assessment",
    onboarding_attempts: 1,
    allocated_turns: 20,
  },
  runtime_binding: {
    provider: "openai",
    model: "gpt-6-luna",
    revision: "rev-1",
    sandbox_backend: "seatbelt",
    profile_id: "profile-hash-1",
    updated_at_ns: 1_700_000_000_000_000_000,
  },
  active_snapshot_id: "snap_abcdef1234567890",
  active_methods: [
    {
      delta_id: "private_method:0123456789abcdef:rev111111111111",
      method_id: "private_method:0123456789abcdef",
      method_id_redacted: true,
      revision_sha256: "rev111111111111aaaa",
      item_kind: "work_method",
      audience_scope: "one_agent",
      activation_seq: 7,
      // Adversarial canary: even if a buggy server sent body_text, client MUST NOT render it!
      body_text: CANARY_PRIVATE_METHOD_BODY,
      body_redacted: true,
      body_digest: "deadbeef00112233445566778899aabb",
    },
    {
      delta_id: "shared-risk-checklist:rev222222222222",
      method_id: "shared-risk-checklist",
      method_id_redacted: false,
      revision_sha256: "rev222222222222bbbb",
      item_kind: "work_method",
      audience_scope: "company",
      activation_seq: 8,
      body_text: "Always verify gross exposure ceiling.",
      body_redacted: false,
    },
  ],
  responsibilities: [
    {
      responsibility_id: "resp-1",
      duty_kind: "work.discovery_review",
      remit: "Monitor daily market anomalies",
      status: "active",
      consecutive_noops: 1,
      cooldown_until_ns: 0,
      window_started_ns: 1_700_000_000_000_000_000,
      window_wakes: 2,
      updated_at_ns: 1_700_000_050_000_000_000,
    },
  ],
  dreaming_summary: {
    total_runs: 3,
    completed_runs: 3,
    failed_runs: 0,
    last_run_id: "run-dream-3",
    last_status: "completed",
    last_run_at_ns: 1_700_000_090_000_000_000,
    last_outcome: { disposition: "completed", proposals: [] },
    recent_runs_count: 1,
  },
  desk_workspace: {
    execution_profile: "profile-hash-1",
    runtime_binding: {
      provider: "openai",
      model: "gpt-6-luna",
      revision: "rev-1",
      sandbox_backend: "seatbelt",
      profile_id: "profile-hash-1",
      updated_at_ns: 1_700_000_000_000_000_000,
    },
    desk_manifest_revision: "manifest-rev-2",
    private_draft_count: 2,
    private_draft_notice:
      "Private drafts remain physically isolated on the Agent's Desk and are never disclosed to the Board.",
    published_count: 1,
    work_run_count: 4,
  },
  authority_budget: {
    grant_id: "grant-1",
    grant_scope: "assessment",
    can_hire: false,
    hiring_slots: null,
    hiring_turns: null,
    monthly_budget_minor: 500_000,
    allocated_turns: 20,
    prohibitions: ["Cannot read another Agent's private Desk drafts"],
  },
  tabs: {
    work_activity: [],
    work_items: [
      {
        work_item_id: "wi-retry-1",
        duty_kind: "work.assessment_review",
        state: "due",
        source_kind: "timer",
        source_id: "cond-1",
        payload: {},
        attempt_count: 2,
        next_retry_at_ns: 1_700_000_500_000_000_000,
        updated_at_ns: 1_700_000_050_000_000_000,
      },
    ],
    conditions: [
      {
        condition_id: "cond-1",
        trigger_kind: "timer",
        due_at_ns: 1_700_000_500_000_000_000,
        source_identity: "timer:rev1",
        status: "armed",
        cooldown_ns: 300_000_000_000,
        max_fires: 10,
        fire_count: 3,
      },
    ],
    work_decisions: [
      {
        decision_id: "dec-1",
        work_item_id: "wi-retry-1",
        run_id: "run-1",
        action_id: "action:aabbccdd11223344",
        action_id_redacted: true,
        action_kind: "proceed",
        action_kind_redacted: false,
        evidence_refs: [{ kind: "work_run", id: "run-1" }],
        evidence_digest: "evdigest1234567890",
        // Adversarial canary: must NEVER be rendered!
        rationale: CANARY_DECISION_RATIONALE,
        rationale_redacted: true,
        rationale_digest: "ratdigest9988776655",
        next_condition_id: "cond-1",
        active_snapshot_id: "snap_abcdef1234567890",
        decision_at_ns: 1_700_000_060_000_000_000,
      },
    ],
    agent_episodes: [
      {
        episode_id: "ep-1",
        work_item_id: "wi-retry-1",
        run_id: "run-1",
        duty_kind: "work.assessment_review",
        outcome_status: "completed",
        active_snapshot_id: "snap_abcdef1234567890",
        // Adversarial canary: must NEVER be rendered when summary_redacted is true!
        summary_text: CANARY_EPISODE_NARRATIVE,
        summary_redacted: true,
        summary_digest: "epdigest1122334455",
        tool_calls_count: 4,
        input_tokens: 1200,
        output_tokens: 300,
        cost_minor_units: 125,
        recorded_at_ns: 1_700_000_070_000_000_000,
      },
    ],
    interactions_and_talks: [],
    published_reports: [],
    peer_requests: [
      {
        request_id: "pr-1",
        direction: "outgoing",
        requester_agent_id: "agent-quant",
        requester_name: "Quant Lead",
        recipient_agent_id: "agent-risk",
        recipient_name: "Risk Officer",
        project_id: "proj-alpha",
        question: "Confirm position sizing cap?",
        expected_decision: "approve or cap",
        state: "answered",
        response: { decision: "approved" },
        created_at_ns: 1_700_000_040_000_000_000,
        hop_count: 1,
        chain_depth: 2,
        delegation_path_length: 2,
      },
    ],
    handoffs: [
      {
        offer_id: "ho-1",
        direction: "outgoing",
        sender_id: "agent-quant",
        sender_name: "Quant Lead",
        recipient_id: "agent-risk",
        recipient_name: "Risk Officer",
        asset_id: "asset-1",
        expected_result: "Risk sign-off",
        state: "withdrawn",
        accepted: false,
        expires_at: 1_700_000_900,
        successor_work_item_id: null,
      },
    ],
  },
};

describe("Agent Detail P1 UI rendering and DOM privacy redaction boundaries", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    clearProjectionCache();
  });

  it("renders Always-On runtime binding, skills, decisions, episodes, dreaming, and peer depth while blocking all private canaries from DOM", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () => new Promise(() => undefined)
    );

    const { container } = render(
      <Providers>
        <AgentDeskAuthoritySection data={SAMPLE_DETAIL} runtimeState="available" />
        <AgentMethodsTab
          activeMethods={SAMPLE_DETAIL.active_methods ?? []}
          activeSnapshotId={SAMPLE_DETAIL.active_snapshot_id ?? null}
        />
        <AgentWorkTab
          agentId={SAMPLE_DETAIL.profile.agent_id}
          workItems={SAMPLE_DETAIL.tabs.work_items}
          workActivity={SAMPLE_DETAIL.tabs.work_activity}
          conditions={SAMPLE_DETAIL.tabs.conditions}
          workDecisions={SAMPLE_DETAIL.tabs.work_decisions}
          agentEpisodes={SAMPLE_DETAIL.tabs.agent_episodes}
          responsibilities={SAMPLE_DETAIL.responsibilities}
          dreamingSummary={SAMPLE_DETAIL.dreaming_summary}
        />
        <AgentHandoffsTab
          peerRequests={SAMPLE_DETAIL.tabs.peer_requests}
          handoffs={SAMPLE_DETAIL.tabs.handoffs}
        />
      </Providers>
    );

    const domText = container.textContent ?? "";

    // Positive P1 feature assertions
    expect(domText).toContain("openai · gpt-6-luna (seatbelt)");
    expect(domText).toContain("snap_abcdef1234567890");
    expect(domText).toContain("one_agent · redacted");
    expect(domText).toContain("shared-risk-checklist");
    expect(domText).toContain("Attempts: 2");
    expect(domText).toContain("5m cooldown");
    expect(domText).toContain("Fires 3/10");
    expect(domText).toContain("action:aabbccdd11223344");
    expect(domText).toContain("ratdigest9988776");
    expect(domText).toContain("epdigest11223344");
    expect(domText).toContain("$1.25");
    expect(domText).toContain("3 total (3 completed · 0 failed)");
    expect(domText).toContain("d2 · h1");
    expect(domText).toContain("withdrawn");
    expect(screen.getByText(/1 active \(2 window wakes\)/)).toBeInTheDocument();

    // Strict DOM privacy redaction assertions — ZERO canary leakage allowed
    expect(domText).not.toContain(CANARY_PRIVATE_METHOD_BODY);
    expect(domText).not.toContain(CANARY_DECISION_RATIONALE);
    expect(domText).not.toContain(CANARY_EPISODE_NARRATIVE);
    expect(domText).not.toContain(CANARY_PRIVATE_DRAFT_NAME);
  });
});
