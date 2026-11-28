export type BadgeTone =
  | "neutral"
  | "brand"
  | "success"
  | "warning"
  | "danger";

export type SemanticTone = BadgeTone | "info";

export function agentStateTone(state: string | null | undefined): BadgeTone {
  switch ((state ?? "").toLowerCase()) {
    case "running":
    case "active":
    case "working":
    case "awake":
      return "success";
    case "waiting":
    case "blocked":
    case "due":
    case "queued":
      return "warning";
    case "failed":
    case "error":
    case "restricted":
    case "fenced":
      return "danger";
    case "retired":
    case "archived":
    case "sleeping":
    case "idle":
    default:
      return "neutral";
  }
}

export function handoffStateTone(state: string | null | undefined): BadgeTone {
  switch ((state ?? "").toLowerCase()) {
    case "accepted":
      return "success";
    case "offered":
    case "pending":
      return "warning";
    case "declined":
    case "expired":
      return "danger";
    case "withdrawn":
    default:
      return "neutral";
  }
}

export function orderStatusTone(status: string | null | undefined): BadgeTone {
  switch ((status ?? "").toLowerCase()) {
    case "filled":
    case "completed":
    case "applied":
      return "success";
    case "open":
    case "accepted":
    case "working":
    case "partial":
    case "submitting":
    case "pending":
      return "warning";
    case "rejected":
    case "denied":
    case "failed":
    case "expired":
      return "danger";
    case "cancelled":
    case "canceled":
    default:
      return "neutral";
  }
}

export function verdictTone(verdict: string | null | undefined): BadgeTone {
  switch ((verdict ?? "").toLowerCase()) {
    case "pass":
    case "passed":
    case "qualified":
    case "active":
    case "promote":
    case "approved":
      return "success";
    case "warn":
    case "inconclusive":
    case "candidate":
    case "shadow":
      return "warning";
    case "fail":
    case "failed":
    case "quarantined":
    case "rejected":
    case "regress":
      return "danger";
    default:
      return "neutral";
  }
}

export function environmentTone(env: string | null | undefined): SemanticTone {
  switch ((env ?? "").toLowerCase()) {
    case "live":
    case "company_live":
      return "brand";
    case "paper":
    case "company_paper":
      return "info";
    case "external_test":
      return "warning";
    case "agent_sandbox":
    default:
      return "neutral";
  }
}
