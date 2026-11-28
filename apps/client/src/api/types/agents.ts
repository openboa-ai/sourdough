import type { AgentVariant, GazeState } from "@openboa/yogurt";
import type { AgentSandboxPortfolioDetail } from "./trading";

export interface AgentSandboxActivitySummary {
  account_id: string;
  currency: string;
  fill_count: number;
  open_positions_count: number;
  cash_minor_units: number;
  gross_exposure_minor_units: number;
}

export interface AgentNodeItem {
  agent_id: string;
  display_name: string;
  role_label: string;
  is_ceo: boolean;
  avatar_variant: AgentVariant;
  avatar_state: GazeState;
  live_status: string;
  archived: boolean;
  current_task: string;
  active_run: {
    run_id: string;
    work_item_id: string;
    duty_kind: string;
    status: string;
    fencing_token: number;
    budget_ceiling_minor: number;
    updated_at_ns: number;
  } | null;
  monthly_budget_minor: number;
  private_draft_count: number;
  published_count: number;
  grant_scope: string;
  sandbox_authorized?: boolean;
  sandbox_activity?: AgentSandboxActivitySummary | null;
}

export interface AgentEdgeItem {
  edge_id: string;
  source_agent_id: string;
  target_agent_id: string;
  kind: "peer_request" | "handoff" | "channel";
  state: string;
  active?: boolean;
  label: string;
  latest_preview?: string;
  latest_sender_name?: string;
  message_count?: number;
  channel_id: string;
  updated_at_ns: number;
}

export interface AgentsGraphProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  default_view_mode: "graph" | "list";
  nodes: AgentNodeItem[];
  edges: AgentEdgeItem[];
}

export interface AgentDetailProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  profile: AgentNodeItem & {
    execution_profile: string;
    grant_id: string | null;
    archive_reason: string | null;
    onboarding_state: string | null;
    onboarding_purpose: string | null;
    onboarding_attempts: number;
    allocated_turns: number | null;
  };
  sandbox_authorized?: boolean;
  agent_sandbox?: AgentSandboxPortfolioDetail | null;
  desk_workspace: {
    execution_profile: string;
    desk_manifest_revision: string | null;
    private_draft_count: number;
    private_draft_notice: string;
    published_count: number;
    work_run_count: number;
  };
  authority_budget: {
    grant_id: string | null;
    grant_scope: string;
    can_hire: boolean;
    hiring_slots: number | null;
    hiring_turns: number | null;
    monthly_budget_minor: number;
    allocated_turns: number | null;
    prohibitions: string[];
  };
  tabs: {
    work_activity: Array<{
      run_id: string;
      work_item_id: string;
      duty_kind: string;
      status: string;
      fencing_token: number;
      budget_ceiling_minor: number;
      outcome: Record<string, unknown> | null;
      created_at_ns: number;
      updated_at_ns: number;
    }>;
    work_items: Array<{
      work_item_id: string;
      duty_kind: string;
      state: string;
      source_kind: string;
      source_id: string;
      payload: Record<string, unknown>;
      updated_at_ns: number;
    }>;
    conditions: Array<{
      condition_id: string;
      trigger_kind: string;
      due_at_ns: number | null;
      source_identity: string | null;
      status: string;
    }>;
    interactions_and_talks: Array<{
      message_id: string;
      project_id: string;
      sender_id: string;
      sender_name: string;
      sender_role: string;
      body: string;
      asset_id: string | null;
      audience: string[];
    }>;
    published_reports: Array<{
      asset_id: string;
      project_id: string;
      work_item_id: string;
      audience: string[];
      content: string;
    }>;
    peer_requests: Array<{
      request_id: string;
      direction: string;
      requester_agent_id: string;
      requester_name: string;
      recipient_agent_id: string;
      recipient_name: string;
      project_id: string | null;
      question: string;
      expected_decision: string;
      state: string;
      response: Record<string, unknown> | null;
      created_at_ns: number;
    }>;
    handoffs: Array<{
      offer_id: string;
      direction: string;
      sender_id: string;
      sender_name: string;
      recipient_id: string;
      recipient_name: string;
      asset_id: string;
      expected_result: string;
      state: "offered" | "accepted" | "declined" | "expired" | "withdrawn" | string;
      accepted: boolean;
      expires_at: number;
      successor_work_item_id: string | null;
    }>;
  };
}
