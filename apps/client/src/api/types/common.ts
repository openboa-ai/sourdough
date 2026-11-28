export type BoardRole = "board_viewer" | "board_control" | null;

export type ExecutionEnvironment = "agent_sandbox" | "paper" | "external_test" | "live";

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

export interface ContextRef {
  kind: string;
  id: string;
  label: string;
}

export type AskContext = ContextRef;

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
