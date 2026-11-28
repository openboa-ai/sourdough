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

const TRANSIENT_HTTP_STATUSES = new Set([502, 503, 504]);
const PROJECTION_RETRY_DELAYS_MS = [250, 600, 1200] as const;
const META_RETRY_DELAYS_MS = [250, 600] as const;

function sleepWithAbort(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) {
    return Promise.reject(new DOMException("Aborted", "AbortError"));
  }
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function isTransientNetworkError(err: unknown, signal?: AbortSignal): boolean {
  if (signal?.aborted) return false;
  if (err instanceof DOMException && err.name === "AbortError") return false;
  return err instanceof TypeError;
}

export async function fetchMeta(
  signal?: AbortSignal,
  opts?: { retryTransientNetwork?: boolean }
): Promise<ControlPlaneMeta> {
  for (let attempt = 0; ; attempt += 1) {
    if (signal?.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    try {
      const res = await fetch("/api/v1/meta", {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        signal,
      });
      if (
        TRANSIENT_HTTP_STATUSES.has(res.status) &&
        attempt < META_RETRY_DELAYS_MS.length
      ) {
        void res.body?.cancel().catch(() => undefined);
        await sleepWithAbort(META_RETRY_DELAYS_MS[attempt], signal);
        continue;
      }
      return (await parseJsonOrThrow(res)) as ControlPlaneMeta;
    } catch (err) {
      if (
        opts?.retryTransientNetwork &&
        isTransientNetworkError(err, signal) &&
        attempt < META_RETRY_DELAYS_MS.length
      ) {
        await sleepWithAbort(META_RETRY_DELAYS_MS[attempt], signal);
        continue;
      }
      throw err;
    }
  }
}

export async function fetchProjection<T>(
  path: string,
  opts?: { etag?: string; signal?: AbortSignal }
): Promise<{ status: 200 | 304; etag: string | null; data: T | null }> {
  const headers: Record<string, string> = {};
  if (opts?.etag) {
    headers["If-None-Match"] = opts.etag;
  }
  const url = `/api/v1/projections/${path}`;
  for (let attempt = 0; ; attempt += 1) {
    if (opts?.signal?.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    try {
      const res = await fetch(url, {
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
      if (
        TRANSIENT_HTTP_STATUSES.has(res.status) &&
        attempt < PROJECTION_RETRY_DELAYS_MS.length
      ) {
        void res.body?.cancel().catch(() => undefined);
        await sleepWithAbort(PROJECTION_RETRY_DELAYS_MS[attempt], opts?.signal);
        continue;
      }
      const data = (await parseJsonOrThrow(res)) as T;
      return { status: 200, etag, data };
    } catch (err) {
      if (
        isTransientNetworkError(err, opts?.signal) &&
        attempt < PROJECTION_RETRY_DELAYS_MS.length
      ) {
        await sleepWithAbort(PROJECTION_RETRY_DELAYS_MS[attempt], opts?.signal);
        continue;
      }
      throw err;
    }
  }
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
    let data: Partial<CommandReceiptResponse> = {};
    try {
      const text = await res.text();
      if (text) {
        const parsed = JSON.parse(text) as unknown;
        if (typeof parsed === "object" && parsed !== null) {
          data = parsed as Partial<CommandReceiptResponse>;
        }
      }
    } catch {
      // Malformed or empty 404 body normalizes to not_found.
    }
    if (!data.state && data.status !== "found") {
      return {
        ...data,
        status: "not_found",
        company_id: typeof data.company_id === "string" ? data.company_id : "",
        command_id: typeof data.command_id === "string" ? data.command_id : commandId,
        command_type: typeof data.command_type === "string" ? data.command_type : operation,
      };
    }
    return data as CommandReceiptResponse;
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

export {
  formatDeadlineNs,
  formatMinorCurrency,
  formatNsClock,
  formatNsWithClock,
  formatRelativeNs,
  formatSignedPct,
  formatTimestamp,
  newCommandId,
  normalizeToNs,
} from "../lib/format";
