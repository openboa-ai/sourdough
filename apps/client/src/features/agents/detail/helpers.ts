export function detailStatusTone(
  archived: boolean,
  activeRunStatus: string | undefined,
  onboardingState: string | null
): "neutral" | "brand" | "warning" | "danger" | "success" {
  if (archived) return "neutral";
  if (activeRunStatus === "running") return "brand";
  if (activeRunStatus === "waiting") return "warning";
  if (onboardingState === "blocked") return "danger";
  return "success";
}

export function workEventKindForRun(status: string): "task" | "routine" | "review" {
  if (status === "completed") return "task";
  if (status === "running" || status === "queued" || status === "waiting") return "routine";
  return "review";
}

export function workEventStateForRun(
  status: string
): "running" | "ready" | "waiting" | "failed" | "done" {
  if (status === "running") return "running";
  if (status === "completed") return "ready";
  if (status === "waiting" || status === "queued") return "waiting";
  if (status === "failed" || status === "cancelled" || status === "fenced") return "failed";
  return "done";
}

export function resolveHandoffState(
  state: string | undefined,
  accepted: boolean
): string {
  return state || (accepted ? "accepted" : "offered");
}

export function workEventStateForHandoff(
  state: string | undefined,
  accepted: boolean
): "ready" | "waiting" | "failed" | "done" {
  const effective = resolveHandoffState(state, accepted);
  if (effective === "accepted") return "ready";
  if (effective === "offered") return "waiting";
  if (effective === "declined" || effective === "expired") return "failed";
  return "done";
}

export function handoffStateTone(
  state: string | undefined,
  accepted: boolean
): "success" | "warning" | "danger" | "neutral" {
  const effective = resolveHandoffState(state, accepted);
  if (effective === "accepted") return "success";
  if (effective === "offered") return "warning";
  if (effective === "declined" || effective === "expired") return "danger";
  return "neutral";
}

export function workItemStateTone(
  state: string
): "brand" | "warning" | "success" | "danger" | "neutral" {
  if (state === "running") return "brand";
  if (state === "due" || state === "scheduled" || state === "waiting") return "warning";
  if (state === "settled") return "success";
  if (state === "blocked" || state === "cancelled") return "danger";
  return "neutral";
}

export function workItemSummary(payload: Record<string, unknown>): string | null {
  for (const key of ["summary", "title", "reason", "task"]) {
    const val = payload[key];
    if (typeof val === "string" && val.trim().length > 0) {
      return val.trim();
    }
  }
  return null;
}
