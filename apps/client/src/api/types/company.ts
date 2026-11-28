import type { AgentVariant } from "@openboa/yogurt";
import type { AgentNodeItem } from "./agents";
import type { ContextRef, RestrictionItem } from "./common";

export interface MessagesProjection {
  projection: string;
  company_id: string;
  charter_revision: string;
  as_of_ns: number;
  event_seq: number;
  ceo_thread: {
    channel_id: "ceo-direct";
    ceo_agent_id: string;
    ceo_display_name: string;
    ceo_role_label: string;
    ceo_avatar_variant: AgentVariant;
    writable: boolean;
    advisory_notice: string;
    messages: Array<{
      message_id: string;
      sender_kind: "board" | "ceo";
      sender_id: string;
      sender_name: string;
      body: string;
      context_ref: ContextRef | null;
      command_id: string | null;
      delivery_state: string;
      recorded_at_ns: number;
    }>;
  };
  internal_channels: Array<{
    channel_id: string;
    name: string;
    revision: string;
    read_only: true;
    members: Array<{
      agent_id: string;
      display_name: string;
      role_label: string;
      avatar_variant: AgentVariant;
    }>;
    message_count: number;
    messages: Array<{
      message_id: string;
      sender_id: string;
      sender_name: string;
      sender_role: string;
      avatar_variant: AgentVariant;
      body: string;
      asset_id: string | null;
      audience: string[];
      delivery_count: number;
      read_count: number;
    }>;
  }>;
}

export interface ApprovalImpact {
  what_changes?: Array<{ label: string; value: string }>;
  what_stays_untouched?: Array<{ label: string; value: string }>;
  consequence_note?: string;
  confirm_verb?: string;
  destructive?: boolean;
}

export interface ApprovalQuestionItem {
  question_id: string;
  kind: string;
  title: string;
  summary: string;
  impact: ApprovalImpact;
  options: string[];
  revision: number;
  payload_hash: string;
  state: "open" | "applied" | "declined" | "expired";
  respond_by_ns: number | null;
  created_by_agent_id: string;
  created_by_name: string;
  created_by_role: string;
  created_at_ns: number;
  resolved_at_ns: number | null;
  resolved_command_id: string | null;
  resolution_choice: string | null;
  resolution_reason: string | null;
  application_status: string;
  delivery_status: string;
}

export interface ApprovalsProjection {
  projection: string;
  company_id: string;
  charter_revision: string;
  as_of_ns: number;
  event_seq: number;
  pending_count: number;
  pending: ApprovalQuestionItem[];
  history: ApprovalQuestionItem[];
  promote_candidates: Array<{
    comparison_id: string;
    incumbent_release_id: string;
    challenger_release_id: string;
    status: string;
    verdict: string;
    verdict_detail: Record<string, unknown>;
    evaluated_at_ns: number | null;
  }>;
}

export interface FinanceProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  trading_enabled: boolean;
  account_balances: Array<{ account: string; balance_minor: number }>;
  journals: Array<{
    entry_id: string;
    source: string;
    source_event: string;
    effective_date: string;
    currency: string;
    lines: Array<{ ordinal: number; account: string; amount_minor: number }>;
  }>;
  accounting_closes: Array<{
    request_id: string;
    scope: string;
    supersedes: string | null;
    status: string;
    assessed_ns: number | null;
    issues: Array<Record<string, unknown>>;
    period: string | null;
  }>;
  open_close_issues: Array<Record<string, unknown>>;
  funding_settlements: Array<{
    settlement_id: string;
    instrument_id: string;
    interval_end_ns: number;
    signed_position_lots: number;
    funding_cashflow_atoms: number | null;
    status: string;
    unavailable_reason: string | null;
  }>;
  retained_statements: Array<{
    request_id: string;
    window_id: string;
    revision: string;
    payload: Record<string, unknown>;
  }>;
}

export interface OrgProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  roster: Array<AgentNodeItem & { execution_profile: string; onboarding_state: string | null; onboarding_attempts: number }>;
  publications: Array<{
    asset_id: string;
    project_id: string;
    author_id: string;
    author_name: string;
    author_role: string;
    work_item_id: string;
    audience: string[];
    content: string;
  }>;
  delegated_hr_custody: {
    board_can_retire: false;
    policy_notice: string;
    archived_employees: Array<{
      cutoff: number;
      agent_id: string;
      agent_name: string;
      manager_id: string;
      manager_name: string;
      employment_revision: string;
      reason: string;
      previous_scope: string;
    }>;
  };
}

export interface RestrictionsProjection {
  projection: string;
  company_id: string;
  charter_revision: string;
  as_of_ns: number;
  event_seq: number;
  active_causes: RestrictionItem[];
  history: RestrictionItem[];
  trading_risk: {
    paper_suspended: boolean;
    live_suspended: boolean;
    effective_revision: number;
    unresolved_orders_count: number;
    unresolved_external_actions_count: number;
    settled_company_ok: boolean;
    server_freshness_note: string;
  } | null;
}
