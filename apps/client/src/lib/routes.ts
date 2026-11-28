import type { ContextRef } from "../api/types";

export const ALLOWED_ASK_CONTEXT_KINDS = new Set([
  "trading_order",
  "agent",
  "shared_publication",
  "decision_question",
  "strategy_release",
  "challenger_trial",
  "strategy_trial",
  "restriction",
  "restriction_cause",
  "work_run",
  "company",
  "account_balance",
  "audit_event",
]);

export function splitRoute(raw: string): { pathname: string; search: string } {
  const hashIdx = raw.indexOf("#");
  const noHash = hashIdx === -1 ? raw : raw.slice(0, hashIdx);
  const qIdx = noHash.indexOf("?");
  const rawPath = qIdx === -1 ? noHash : noHash.slice(0, qIdx);
  const rawSearch = qIdx === -1 ? "" : noHash.slice(qIdx);
  let pathname = rawPath || "/";
  if (!pathname.startsWith("/")) {
    pathname = `/${pathname}`;
  }
  while (pathname.length > 1 && pathname.endsWith("/")) {
    pathname = pathname.slice(0, -1);
  }
  const search = rawSearch === "?" ? "" : rawSearch;
  return { pathname, search };
}

export function normalizeRoute(raw: string): string {
  const { pathname, search } = splitRoute(raw);
  return `${pathname}${search}`;
}

export function decodeSingleSegment(pathname: string, prefix: string): string | null {
  if (!pathname.startsWith(prefix)) return null;
  const rawSegment = pathname.slice(prefix.length);
  if (!rawSegment || rawSegment.includes("/")) return null;
  try {
    const decoded = decodeURIComponent(rawSegment);
    return decoded && !decoded.includes("/") ? decoded : null;
  } catch {
    return null;
  }
}

export function sanitizeAskContext(
  raw: { kind?: unknown; id?: unknown; label?: unknown } | null | undefined
): ContextRef | null {
  if (!raw || typeof raw.kind !== "string" || typeof raw.id !== "string") return null;
  const kind = raw.kind.trim();
  const id = raw.id.trim();
  if (
    !kind ||
    kind.length > 64 ||
    !ALLOWED_ASK_CONTEXT_KINDS.has(kind) ||
    !id ||
    id.length > 128 ||
    /[\u0000-\u001f\u007f]/.test(id)
  ) {
    return null;
  }
  const rawLabel = typeof raw.label === "string" ? raw.label.trim() : "";
  const label = (rawLabel || `${kind.replace(/_/g, " ")} · ${id}`).slice(0, 240);
  return { kind, id, label };
}

export function parseAskContextFromSearch(search: string): ContextRef | null {
  if (!search) return null;
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const kind = params.get("context_kind");
  const id = params.get("context_id");
  if (!kind || !id) return null;
  return sanitizeAskContext({ kind, id, label: params.get("context_label") });
}

export function isMessagesPathname(pathname: string): boolean {
  return pathname === "/messages";
}

export function isTier1Path(pathname: string): boolean {
  return (
    pathname.startsWith("/orders/") ||
    pathname.startsWith("/agents/") ||
    pathname.startsWith("/approvals/")
  );
}

export function defaultEscapeBackTarget(pathname: string): string | null {
  if (pathname.startsWith("/orders/")) return "/trading/paper";
  if (pathname.startsWith("/agents/")) return "/agents";
  if (pathname.startsWith("/approvals/")) return "/approvals";
  return null;
}
