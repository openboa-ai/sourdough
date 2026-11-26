import type { AgentVariant, GazeState } from "@openboa/yogurt";

export type BoardRole = "board_viewer" | "board_control" | null;

export interface BoardAccess {
  bootstrap_locked: boolean;
  role: BoardRole;
  principal_id: string | null;
  binding_revision: number;
  bootstrap_challenge?: string;
}

export interface ControlPlaneMeta {
  control_plane_version: string;
  company_id: string;
  charter_revision: string;
  founder_id: string | null;
  store_key: string;
  service_incarnation_id: string;
  schema_version: number;
  active_domains: string[];
  daemon_pid: number;
  event_seq: number;
  board_access: BoardAccess;
  pending_approvals_count: number;
  active_restrictions_count: number;
  paper_risk_suspended: boolean;
  paper_risk_effective_revision: number;
  supported_projections: string[];
  supported_commands: Record<string, string[]>;
}

export interface AuditEventItem {
  event_seq: number;
  company_id?: string;
  domain: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  recorded_at_ns: number;
}

export interface RestrictionItem {
  restriction_id: string;
  cause_category: string;
  scope_ref: string;
  restriction_revision: number;
  active: boolean;
  operator_releasable: boolean;
  reason: string;
  recorded_at_ns: number;
  cleared_at_ns: number | null;
  last_command_id: string | null;
}

export interface ReturnPoint {
  id: string;
  label: string;
  return_pct: number | null;
  pnl_minor: number | null;
  nav_minor: number | null;
  source: string;
  observed_at_ns?: number | null;
}

export interface PnlBridgeStep {
  step: string;
  label: string;
  amount_minor: number | null;
  status: "ok" | "unavailable";
}

export interface OrderFillItem {
  fill_id: string;
  quantity: number;
  price_minor_units: number;
  fee_minor_units: number | null;
  position_quantity: number | null;
  available_at: number | null;
}

export interface OrderCancellationItem {
  event_id: string;
  filled_quantity: number;
  released: boolean;
  position_quantity: number | null;
  available_at: number | null;
  outcome: string;
}

export interface ExecutionOrderItem {
  order_id: string;
  run_id: string;
  duty_id: string | null;
  decision_id: string | null;
  rationale: string | null;
  action: string;
  decided_at: number | null;
  account_id: string;
  instrument: string;
  mandate_revision: string;
  agent_id: string | null;
  agent_name: string;
  agent_role: string;
  ordered_qty: number;
  filled_qty: number;
  remaining_qty: number;
  limit_minor_units: number;
  avg_fill_minor_units: number | null;
  reserved_minor_units: number;
  total_fee_minor_units: number | null;
  status: string;
  cancellation: OrderCancellationItem | null;
  residual_position_qty: number | null;
  cancel_not_flat: boolean;
  fills: OrderFillItem[];
  reconciliation?: {
    attempts: number;
    next_query_at: number | null;
    fill_id: string | null;
    sources: unknown[];
  } | null;
}

export interface OpenPositionItem {
  instrument: string;
  account_id: string;
  quantity: number;
  avg_entry_minor_units: number;
  latest_order_id: string;
  agent_id: string | null;
  agent_name: string;
  from_cancelled_order: boolean;
}

export interface LiveTradingProjection {
  projection: string;
  company_id: string;
  charter_revision: string;
  as_of_ns: number;
  event_seq: number;
  active_domains: string[];
  paper_enabled: boolean;
  environment: string;
  board_role: BoardRole;
  risk_gate: {
    suspended: boolean;
    paper_risk_suspended?: boolean;
    effective_revision: number;
    active_restriction_count: number;
  };
  kpis: {
    portfolio_value: {
      value_minor: number | null;
      starting_minor?: number | null;
      return_pct: number | null;
      currency: string;
      status: string;
      source: string;
    };
    trading_bor_pnl: {
      value_minor: number | null;
      currency: string;
      status: string;
      source: string;
    };
    agent_llm_opex: {
      value_minor: number | null;
      currency: string;
      status: string;
      source: string;
    };
    company_net_pnl: {
      value_minor: number | null;
      currency: string;
      status: string;
      source: string;
    };
  };
  since_last_visit: {
    since_seq: number;
    new_event_count: number;
    by_domain: Record<string, number>;
    highlights: AuditEventItem[];
  };
  cumulative_return_series: ReturnPoint[];
  pnl_reconciliation_bridge: PnlBridgeStep[];
  execution_tape: ExecutionOrderItem[];
  open_positions: OpenPositionItem[];
  adapter_status: {
    environment: string;
    account_id: string;
    connector_revision: string;
    destination_id: string;
    mandate_revision: string;
    exposure_limit_minor_units: number | null;
    starting_minor_units: number | null;
    currency: string;
    last_quote_minor_units: number | null;
    last_quote_observed_at: number | null;
  } | null;
  active_restrictions: RestrictionItem[];
  open_close_issues?: Array<Record<string, unknown>>;
  close_count?: number;
}

export interface OrderTraceProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  order: ExecutionOrderItem;
  causal_steps: Array<{
    stage: string;
    id: string;
    detail: string;
    status: string;
  }>;
  related_publications: Array<{
    asset_id: string;
    project_id: string;
    author_id: string;
    excerpt: string;
  }>;
}

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
      accepted: boolean;
    }>;
  };
}

export interface ContextRef {
  kind: string;
  id: string;
  label: string;
}

export type AskContext = ContextRef;

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

export interface StrategiesProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  paper_enabled: boolean;
  active_deployment: {
    deployment_id: string;
    release_id: string;
    program_identity: string;
    binding_revision: string;
    comparison_id: string | null;
    status: string;
    command_id: string;
    activated_at_ns: number;
  } | null;
  deployments: Array<{
    deployment_id: string;
    release_id: string;
    program_identity: string;
    binding_revision: string;
    comparison_id: string | null;
    status: string;
    command_id: string;
    activated_at_ns: number;
    retired_at_ns: number | null;
  }>;
  releases: Array<{
    release_id: string;
    owner_agent_id: string;
    owner_name: string;
    parent_release_id: string | null;
    parameters: Record<string, unknown>;
    pit_manifest_id: string;
    research_trial_count_n: number;
    bundle_sha256: string;
    recorded_at_ns: number;
  }>;
  trials: Array<{
    comparison_id: string;
    incumbent_release_id: string;
    challenger_release_id: string;
    baseline_statement_revision: string;
    status: string;
    verdict: string | null;
    verdict_detail: Record<string, unknown> | null;
    invalidation_reason: string | null;
    registered_at_ns: number;
    evaluated_at_ns: number | null;
  }>;
  adapter: {
    binding_revision: string;
    account_id: string;
    connector_revision: string;
    exposure_limit_minor_units: number;
    currency: string;
    starting_minor_units: number;
    environment: string;
    destination_id: string;
  } | null;
}

export interface FinanceProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  paper_enabled: boolean;
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
  paper_risk: {
    suspended: boolean;
    effective_revision: number;
    unresolved_orders_count: number;
    pending_binding_change: boolean;
    settled_company_ok: boolean;
    last_quote_observed_at: number | null;
    server_freshness_note: string;
  } | null;
}

export interface SystemSafetyProjection {
  projection: string;
  company_id: string;
  charter_revision: string;
  as_of_ns: number;
  event_seq: number;
  checkpoints: Array<{
    archive_name: string;
    size_bytes: number;
    modified_ns: number;
  }>;
  restore_receipts: Array<{
    request_revision: string;
    checkpoint_sha256: string;
    watermark: Record<string, unknown>;
    restored_at_ns: number;
  }>;
  store_retired: {
    request_revision: string;
    replacement_sha256: string;
    retired_at_ns: number;
  } | null;
  safe_custody_hold: Array<{
    id: string;
    label: string;
    status: "done" | "active" | "pending";
  }>;
  active_restrictions: RestrictionItem[];
}

export interface SystemEvalsProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  qualifications: Array<{
    receipt_id: string;
    candidate_asset_id: string;
    baseline_asset_id: string | null;
    parent_asset_id: string | null;
    passed: boolean;
    trial_count: number;
    recorded_at_ns: number;
  }>;
  activations: Array<{
    request_id: string;
    operation: string;
    candidate_id: string;
    qualification_id: string;
    parent_asset_id: string | null;
    revision: number;
    quarantined: boolean;
    quarantine_reason: string | null;
    recorded_at_ns: number;
    blast_radius_descendants: string[];
    fallback_candidate_id: string | null;
  }>;
  offline_evaluator: {
    connected: boolean;
    isolated_notice: string;
    suites: Array<{
      revision: string;
      sealed_at_ns: number;
      observation_count: number;
      grade_count: number;
      pairwise_grade_count: number;
    }>;
  };
}

export interface SystemAuditProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  events: AuditEventItem[];
  conditions: Array<{
    condition_id: string;
    owner_agent_id: string;
    trigger_kind: string;
    due_at_ns: number | null;
    source_identity: string | null;
    status: string;
  }>;
  active_fences: Array<{
    run_id: string;
    agent_id: string;
    duty_kind: string;
    status: string;
    fencing_token: number;
    lease_owner: string | null;
    lease_expires_at_ns: number | null;
  }>;
  board_bindings: Array<{
    principal_id: string;
    role_name: string;
    scope_ref: string;
    binding_revision: number;
    active: boolean;
  }>;
}

export type CommandLifecycle =
  | "idle"
  | "draft"
  | "submitting"
  | "unknown"
  | "accepted"
  | "applied"
  | "denied"
  | "not_found_retry_choice";

export interface LocalCommandIntent {
  company_id: string;
  principal_id?: string;
  command_id: string;
  command_type: string;
  contract_version: string;
  preconditions: Record<string, unknown>;
  payload: Record<string, unknown>;
  label: string;
  sent_ns: number;
}

export interface CommandReceiptResponse {
  status: "found" | "not_found";
  company_id: string;
  command_id: string;
  command_type?: string;
  state?: "accepted" | "applied" | "denied";
  disposition?: string;
  denial_code?: string | null;
  effective_revision?: number;
  recorded_at_ns?: number;
  peer_pid?: number;
  result?: Record<string, unknown>;
}

export interface ExecutionTierBindingItem {
  binding_id: string;
  company_id: string;
  tier: "agent_sandbox" | "company_paper" | "company_live";
  environment: "paper" | "external_test" | "live";
  owner_agent_id: string | null;
  account_id: string;
  destination_id: string;
  connector_revision: string;
  instrument_rules_digest: string;
  currency: string;
  starting_minor_units: number;
  exposure_limit_minor_units: number;
  fee_bps: number;
  slippage_bps: number;
  quote_source: string;
  credential_handle_ref: string | null;
  status: string;
  created_at_ns?: number;
  updated_at_ns?: number;
}

export interface LiveCandidateBindingItem extends ExecutionTierBindingItem {
  matching_qualification_receipt_id: string | null;
  promotion_payload_hash: string | null;
}

export interface AgentSandboxSummaryItem {
  agent_id: string;
  agent_display_name?: string;
  agent_role_label?: string;
  binding_id: string;
  account_id: string;
  currency: string;
  fee_bps: number;
  slippage_bps: number;
  quote_source: string;
  cash_minor_units: number;
  gross_exposure_minor_units: number;
  realized_pnl_minor_units: number;
  total_fees_minor_units: number;
  open_positions_count: number;
  fill_count: number;
}

export interface AgentSandboxPortfolioDetail {
  binding: ExecutionTierBindingItem;
  cash_minor_units: number;
  gross_exposure_minor_units: number;
  exposure_limit_minor_units: number;
  realized_pnl_minor_units: number;
  total_fees_minor_units: number;
  fill_count: number;
  positions: Array<{
    instrument_id: string;
    quantity: number;
    avg_entry_minor_units: number;
    realized_pnl_minor_units: number;
    total_fees_minor_units: number;
    updated_at_ns: number;
  }>;
  orders: Array<{
    order_id: string;
    action_id: string;
    instrument_id: string;
    side: string;
    quantity: number;
    limit_minor_units: number;
    status: string;
    filled_quantity: number;
    fill_price_minor_units: number | null;
    fee_minor_units: number;
    created_at_ns: number;
  }>;
}

export interface QualificationReceiptItem {
  receipt_id: string;
  adapter_revision: string;
  environment: string;
  destination_id: string;
  instrument_rules_digest: string;
  core_digest: string;
  verdict: string;
  cases_passed: number;
  cases_failed: number;
  cases_unrun: number;
  evidence_sha256: string;
  recorded_at_ns: number;
}

export interface EnvironmentPromotionRecordItem {
  promotion_id: string;
  command_id: string;
  action: string;
  from_binding_id: string | null;
  to_binding_id: string;
  qualification_receipt_id: string | null;
  payload_hash: string;
  reason: string;
  recorded_at_ns: number;
}

export interface CompanyPaperBookItem {
  binding_id: string;
  account_id: string;
  book_name: string;
  strategy_release_id: string | null;
  role: "active" | "nominated" | "shadow" | "retired";
  status: string;
  shadow_of_live_binding_id: string | null;
  nominated_question_id: string | null;
  currency: string;
  starting_minor_units: number;
  exposure_limit_minor_units: number;
  fee_bps: number;
  slippage_bps: number;
  quote_source: string;
  cash_minor_units: number;
  gross_exposure_minor_units: number;
  realized_pnl_minor_units: number;
  total_fees_minor_units: number;
  fill_count: number;
  open_positions_count: number;
  open_orders_count: number;
  soak_ready: boolean;
  positions: Array<{
    instrument_id: string;
    quantity: number;
    avg_entry_minor_units: number;
    realized_pnl_minor_units: number;
    total_fees_minor_units: number;
    updated_at_ns: number;
  }>;
  orders: Array<{
    order_id: string;
    action_id: string;
    instrument_id: string;
    side: string;
    quantity: number;
    limit_minor_units: number;
    status: string;
    filled_quantity: number;
    fill_price_minor_units: number | null;
    fee_minor_units: number;
    created_at_ns: number;
  }>;
}

export interface PaperLiveMigrationItem {
  migration_id: string;
  question_id: string | null;
  source_paper_binding_id: string;
  source_book_name: string;
  target_live_binding_id: string;
  target_account_id: string;
  replaced_live_binding_id: string | null;
  qualification_receipt_id: string;
  migration_mode: "clean_start" | "drain_incumbent";
  source_paper_disposition: "keep_shadow" | "retire_source";
  positions_copied: number;
  pre_check_summary: Record<string, unknown>;
  reason: string;
  recorded_at_ns: number;
}

export interface ExecutionTiersProjection {
  projection: string;
  company_id: string;
  charter_revision: string;
  as_of_ns: number;
  event_seq: number;
  paper_domain_installed: boolean;
  company_paper: ExecutionTierBindingItem | null;
  company_paper_books?: CompanyPaperBookItem[];
  company_paper_soak_ready: boolean;
  company_live: ExecutionTierBindingItem | null;
  live_candidates: LiveCandidateBindingItem[];
  agent_sandboxes: AgentSandboxSummaryItem[];
  qualification_receipts: QualificationReceiptItem[];
  promotion_history: EnvironmentPromotionRecordItem[];
  migration_history?: PaperLiveMigrationItem[];
  current_core_digest: string;
}

export interface TradingDeskBookSummary {
  binding_id: string;
  account_id: string;
  book_name: string;
  environment: "live" | "paper";
  tier: "company_live" | "company_paper";
  destination_id: string;
  connector_revision: string;
  strategy_release_id: string | null;
  role: string;
  status: string;
  source_paper_binding_id: string | null;
  shadow_of_live_binding_id: string | null;
  nominated_question_id: string | null;
  currency: string;
  starting_minor_units: number;
  exposure_limit_minor_units: number;
  fee_bps: number;
  slippage_bps: number;
  quote_source: string;
  nav_minor_units: number;
  pnl_minor_units: number;
  return_pct: number;
  cash_minor_units: number;
  gross_exposure_minor_units: number;
  realized_pnl_minor_units: number;
  unrealized_pnl_minor_units: number;
  total_fees_minor_units: number;
  fill_count: number;
  open_positions_count: number;
  open_orders_count: number;
  soak_ready: boolean;
}

export interface TradingDeskPositionItem {
  instrument_id: string;
  quantity: number;
  avg_entry_minor_units: number;
  mark_price_minor_units: number;
  notional_minor_units: number;
  unrealized_pnl_minor_units: number;
  realized_pnl_minor_units: number;
  total_fees_minor_units: number;
  updated_at_ns: number;
}

export interface TradingDeskOrderItem {
  order_id: string;
  action_id: string;
  actor_id: string;
  actor_name: string;
  actor_role: string;
  book_name: string;
  binding_id: string;
  external_order_id: string | null;
  instrument_id: string;
  side: "buy" | "sell" | string;
  quantity: number;
  limit_minor_units: number;
  status: string;
  filled_quantity: number;
  fill_price_minor_units: number | null;
  fee_minor_units: number;
  created_at_ns: number;
}

export interface ShadowBenchmarkData {
  binding_id: string;
  book_name: string;
  strategy_release_id: string | null;
  role: string;
  return_pct: number;
  post_launch_return_pct: number;
  execution_drift_pp: number;
  nav_minor_units: number;
  pnl_minor_units: number;
  series: ReturnPoint[];
}

export interface TradingDeskProjection {
  projection: string;
  company_id: string;
  charter_revision: string;
  as_of_ns: number;
  event_seq: number;
  active_domains: string[];
  paper_enabled: boolean;
  environment: "live" | "paper";
  selected_book_id: string | null;
  selected_book: TradingDeskBookSummary | null;
  books: TradingDeskBookSummary[];
  kpis: {
    portfolio_value: {
      value_minor: number | null;
      return_pct: number | null;
      currency: string;
      status: string;
      source: string;
    };
    trading_pnl: {
      value_minor: number | null;
      realized_minor: number | null;
      unrealized_minor: number | null;
      fees_minor: number | null;
      currency: string;
      status: string;
      source: string;
    };
    agent_llm_opex: {
      value_minor: number | null;
      currency: string;
      status: string;
      source: string;
    };
    company_net_pnl: {
      value_minor: number | null;
      currency: string;
      status: string;
      source: string;
    };
  };
  cumulative_return_series: ReturnPoint[];
  shadow_benchmark: ShadowBenchmarkData | null;
  open_positions: TradingDeskPositionItem[];
  execution_tape: TradingDeskOrderItem[];
  pnl_reconciliation_bridge: PnlBridgeStep[];
  exposure_cap: {
    used_minor_units: number;
    limit_minor_units: number | null;
    currency: string;
  };
  close_count: number;
  open_close_issues: Array<Record<string, unknown>>;
  risk_gate: {
    suspended: boolean;
    effective_revision: number;
    active_restriction_count: number;
  };
  governance: {
    company_live: ExecutionTierBindingItem | null;
    live_candidates: LiveCandidateBindingItem[];
    qualification_receipts: QualificationReceiptItem[];
    promotion_history: EnvironmentPromotionRecordItem[];
    migration_history: PaperLiveMigrationItem[];
    company_paper_soak_ready: boolean;
    paper_books_count: number;
    soak_ready_books_count: number;
    current_core_digest: string;
  };
}
