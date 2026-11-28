import type { AuditEventItem, RestrictionItem } from "./common";

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

export interface MethodDeltaProjectionItem {
  delta_id: string;
  method_id: string;
  method_id_redacted: boolean;
  revision_sha256: string;
  delta_kind: string;
  item_kind: string;
  audience_scope: string;
  audience_target_id: string | null;
  author_agent_id: string;
  parent_method_ids: string[];
  active_snapshot_id: string | null;
  superseded_revision: string | null;
  redaction_passed: boolean;
  status: string;
  created_at_ns: number;
}

export interface MethodQualificationProjectionItem {
  qualification_id: string;
  method_id: string;
  method_id_redacted: boolean;
  revision_sha256: string;
  delta_id: string;
  baseline_revision_sha256: string | null;
  stage1_passed: boolean;
  stage2_passed: boolean;
  stage3_passed: boolean;
  trial_count: number;
  metrics: Record<string, unknown>;
  evaluator_revision: string;
  passed: boolean;
  recorded_at_ns: number;
}

export interface MethodActivationProjectionItem {
  request_id: string;
  operation: string;
  method_id: string;
  method_id_redacted: boolean;
  revision_sha256: string;
  delta_id: string;
  qualification_id: string;
  audience_scope: string;
  audience_target_id: string | null;
  activation_seq: number;
  principal_id: string;
  grant_id: string;
  quarantined: boolean;
  quarantine_reason: string | null;
  recorded_at_ns: number;
  blast_radius_descendants: string[];
  fallback_revision: string | null;
}

export interface RsiTransferSummaryItem {
  suite_id: string;
  revision: string;
  verdict: string;
  reasons: string[];
  analysis_plan_digest?: string;
  planned_pairs: number;
  complete_pairs: number;
  invalidated_slots?: number;
  candidate_pairwise_wins?: number;
  incumbent_pairwise_wins?: number;
  pairwise_ties?: number;
  pairwise_noise?: number;
  candidate_judgment_passes?: number;
  incumbent_judgment_passes?: number;
  candidate_authority_violations?: number;
  candidate_service_incorrect?: number;
  distinct_transfer_families?: number;
  lcb_alpha?: number;
  ucb_alpha?: number;
  mean_family_delta?: number;
  perm_p?: number;
  alpha?: number;
  min_worthwhile_margin?: number;
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
  method_deltas?: MethodDeltaProjectionItem[];
  method_qualifications?: MethodQualificationProjectionItem[];
  method_activations?: MethodActivationProjectionItem[];
  rsi_transfer_summaries?: RsiTransferSummaryItem[];
  offline_evaluator: {
    connected: boolean;
    isolated_notice: string;
    suites: Array<{
      revision: string;
      suite_id?: string;
      purpose?: string;
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
