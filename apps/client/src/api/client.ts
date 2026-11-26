import type {
  AuditEventItem,
  CommandReceiptResponse,
  ControlPlaneMeta,
  LocalCommandIntent,
} from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function parseJsonOrThrow(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    if (!response.ok) {
      throw new ApiError(response.status, `HTTP ${response.status}`);
    }
    return {};
  }
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    throw new ApiError(response.status, "Control plane returned malformed JSON");
  }
  if (!response.ok) {
    const detail =
      typeof data === "object" && data !== null && "error" in data
        ? String((data as { error: unknown }).error)
        : `HTTP ${response.status}`;
    throw new ApiError(response.status, detail);
  }
  return data;
}

export async function fetchMeta(signal?: AbortSignal): Promise<ControlPlaneMeta> {
  const res = await fetch("/api/v1/meta", {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    signal,
  });
  return (await parseJsonOrThrow(res)) as ControlPlaneMeta;
}

export async function fetchProjection<T>(
  path: string,
  opts?: { etag?: string; signal?: AbortSignal }
): Promise<{ status: 200 | 304; etag: string | null; data: T | null }> {
  const headers: Record<string, string> = {};
  if (opts?.etag) {
    headers["If-None-Match"] = opts.etag;
  }
  const res = await fetch(`/api/v1/projections/${path}`, {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers,
    signal: opts?.signal,
  });
  const etag = res.headers.get("etag");
  if (res.status === 304) {
    return { status: 304, etag, data: null };
  }
  const data = (await parseJsonOrThrow(res)) as T;
  return { status: 200, etag, data };
}

export async function fetchEventsNdjson(
  afterSeq: number,
  waitMs = 3500,
  signal?: AbortSignal
): Promise<AuditEventItem[]> {
  const res = await fetch(
    `/api/v1/events?after_seq=${encodeURIComponent(String(afterSeq))}&limit=100&wait_ms=${encodeURIComponent(String(waitMs))}`,
    {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      signal,
    }
  );
  if (!res.ok) {
    await parseJsonOrThrow(res);
  }
  const text = await res.text();
  const events: AuditEventItem[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const parsed = JSON.parse(trimmed) as AuditEventItem;
      if (parsed && typeof parsed.event_seq === "number") {
        events.push(parsed);
      }
    } catch {
      // Skip malformed trailing line cleanly.
    }
  }
  return events;
}

export async function submitCommandApi(
  intent: LocalCommandIntent
): Promise<CommandReceiptResponse> {
  const res = await fetch("/api/v1/commands", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": intent.command_id,
    },
    body: JSON.stringify({
      company_id: intent.company_id,
      command_id: intent.command_id,
      command_type: intent.command_type,
      contract_version: intent.contract_version,
      preconditions: intent.preconditions,
      payload: intent.payload,
    }),
  });
  return (await parseJsonOrThrow(res)) as CommandReceiptResponse;
}

export async function lookupCommandReceiptApi(
  commandId: string,
  operation: string
): Promise<CommandReceiptResponse> {
  const res = await fetch(
    `/api/v1/commands/${encodeURIComponent(commandId)}?operation=${encodeURIComponent(operation)}`,
    {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
    }
  );
  if (res.status === 404) {
    return (await res.json()) as CommandReceiptResponse;
  }
  return (await parseJsonOrThrow(res)) as CommandReceiptResponse;
}

export async function readPendingOutboxApi(): Promise<{
  pending: LocalCommandIntent | null;
  receipt?: CommandReceiptResponse | null;
  quarantined_stale?: boolean;
  notice?: string;
}> {
  const res = await fetch("/api/local/pending", {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
  });
  return (await parseJsonOrThrow(res)) as {
    pending: LocalCommandIntent | null;
    receipt?: CommandReceiptResponse | null;
    quarantined_stale?: boolean;
    notice?: string;
  };
}

export async function writePendingOutboxApi(intent: LocalCommandIntent): Promise<void> {
  const res = await fetch("/api/local/pending", {
    method: "PUT",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(intent),
  });
  await parseJsonOrThrow(res);
}

export async function deletePendingOutboxApi(): Promise<void> {
  const res = await fetch("/api/local/pending", {
    method: "DELETE",
    credentials: "same-origin",
    cache: "no-store",
  });
  await parseJsonOrThrow(res);
}

/* Honest numeric & temporal formatters: NEVER coerce null/undefined to 0. */

export function formatMinorCurrency(
  minor: number | null | undefined,
  currency = "USD",
  opts?: { signed?: boolean }
): string {
  if (minor === null || minor === undefined || !Number.isFinite(minor)) {
    return "Unavailable";
  }
  const major = minor / 100;
  const safeCurrency = /^[A-Z]{3}$/.test(currency) ? currency : "USD";
  const absFormatted = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: safeCurrency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Math.abs(major));
  if (minor < 0) return `-${absFormatted}`;
  if (opts?.signed && minor > 0) return `+${absFormatted}`;
  return absFormatted;
}

export function formatSignedPct(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) {
    return "Unavailable";
  }
  const prefix = pct > 0 ? "+" : "";
  return `${prefix}${pct.toFixed(2)}%`;
}

export function formatRelativeNs(ns: number | null | undefined): string {
  if (!ns || !Number.isFinite(ns)) return "Not scheduled";
  const ms = ns / 1_000_000;
  const deltaSec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (deltaSec < 5) return "just now";
  if (deltaSec < 60) return `${deltaSec}s ago`;
  const mins = Math.floor(deltaSec / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export function formatNsClock(ns: number | null | undefined): string {
  if (!ns || !Number.isFinite(ns)) return "No reading";
  const d = new Date(ns / 1_000_000);
  if (Number.isNaN(d.getTime())) return "No reading";
  return d.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function newCommandId(prefix = "cmd"): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${prefix}-${Date.now()}-${hex}`;
}
