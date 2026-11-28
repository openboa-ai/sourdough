import type { ExecutionEnvironment } from "./common";

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
  fee_minor_units: number;
  notional_minor_units: number;
  realized_pnl_minor_units: number;
  filled_at_ns: number;
  delivery_status: "acknowledged" | "pending" | null;
}

export interface OrderTraceExternalAction {
  idempotency_key: string;
  status: string;
  connector: string;
  operation: string;
  receipt: unknown;
}

export interface OrderTraceOrderItem {
  order_id: string;
  binding_id: string;
  book_name: string;
  tier: "agent_sandbox" | "company_paper" | "company_live" | string;
  environment: ExecutionEnvironment;
  account_id: string;
  destination_id: string;
  currency: string;
  instrument: string;
  instrument_id: string;
  side: string;
  action: string;
  actor_id: string;
  agent_id: string | null;
  agent_name: string;
  agent_role: string;
  run_id: string | null;
  duty_id: string | null;
  duty_kind: string | null;
  decision_id: string | null;
  mandate_revision: string | null;
  action_id: string;
  external_order_id: string | null;
  ordered_qty: number;
  filled_qty: number;
  remaining_qty: number;
  limit_minor_units: number;
  market_price_minor_units: number;
  avg_fill_minor_units: number | null;
  total_fee_minor_units: number;
  status: string;
  cancelled_at_ns: number | null;
  cancel_reason: string | null;
  residual_position_qty: number;
  cancel_not_flat: boolean;
  fills: OrderFillItem[];
  external_action: OrderTraceExternalAction | null;
  created_at_ns: number;
  updated_at_ns: number;
  back_link: string;
}

export interface OrderTraceProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  order: OrderTraceOrderItem;
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

export interface StrategiesProjection {
  projection: string;
  company_id: string;
  as_of_ns: number;
  event_seq: number;
  trading_enabled: boolean;
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
    binding_id: string;
    binding_revision: string;
    account_id: string;
    connector_revision: string;
    exposure_limit_minor_units: number;
    currency: string;
    starting_minor_units: number;
    environment: ExecutionEnvironment;
    destination_id: string;
  } | null;
}

export interface ExecutionTierBindingItem {
  binding_id: string;
  company_id: string;
  tier: "agent_sandbox" | "company_paper" | "company_live";
  environment: ExecutionEnvironment;
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
  binding_revision?: string;
  book_name?: string | null;
  strategy_release_id?: string | null;
  book_role?: string | null;
  shadow_of_live_binding_id?: string | null;
  nominated_question_id?: string | null;
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

export interface PaperLiveMigrationItem {
  migration_id: string;
  question_id: string | null;
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

export interface TradingRiskRestrictionItem {
  restriction_id: string;
  cause_category: string;
  scope_ref: string;
  restriction_revision: number;
  operator_releasable: boolean;
  reason: string;
  recorded_at_ns: number;
}

export interface TradingDeskProjection {
  projection: string;
  company_id: string;
  charter_revision: string;
  as_of_ns: number;
  event_seq: number;
  active_domains: string[];
  trading_enabled: boolean;
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
    active_restrictions: TradingRiskRestrictionItem[];
  };
  governance: {
    company_live: ExecutionTierBindingItem | null;
    live_candidates: LiveCandidateBindingItem[];
    qualification_receipts: QualificationReceiptItem[];
    promotion_history: EnvironmentPromotionRecordItem[];
    migration_history: PaperLiveMigrationItem[];
    paper_soak_ready: boolean;
    paper_books_count: number;
    soak_ready_books_count: number;
    current_core_digest: string;
  };
}
