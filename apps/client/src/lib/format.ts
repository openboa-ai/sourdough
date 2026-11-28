/* Honest numeric & temporal formatters: NEVER coerce null/undefined to 0. */

export function normalizeToNs(epoch: number | null | undefined): number | null {
  if (!epoch || !Number.isFinite(epoch)) return null;
  if (epoch < 1e12) return epoch * 1_000_000_000;
  if (epoch < 1e15) return epoch * 1_000_000;
  return epoch;
}

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
  const normalized = normalizeToNs(ns);
  if (!normalized) return "Not scheduled";
  const ms = normalized / 1_000_000;
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
  const normalized = normalizeToNs(ns);
  if (!normalized) return "No reading";
  const d = new Date(normalized / 1_000_000);
  if (Number.isNaN(d.getTime())) return "No reading";
  return d.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function formatNsWithClock(
  ns: number | null | undefined,
  emptyLabel = "No reading"
): string {
  const normalized = normalizeToNs(ns);
  if (!normalized) return emptyLabel;
  return `${formatRelativeNs(normalized)} · ${formatNsClock(normalized)}`;
}

export function formatTimestamp(
  epoch: number | null | undefined,
  emptyLabel = "—"
): string {
  const normalized = normalizeToNs(epoch);
  if (!normalized) return emptyLabel;
  const d = new Date(normalized / 1_000_000);
  if (Number.isNaN(d.getTime())) return emptyLabel;
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function formatDeadlineNs(ns: number | null | undefined): string {
  const normalized = normalizeToNs(ns);
  if (!normalized) return "No deadline";
  const deltaSec = Math.round((normalized / 1_000_000 - Date.now()) / 1000);
  if (deltaSec <= 0) {
    const ago = Math.abs(deltaSec);
    if (ago < 60) return "Expired just now";
    const mins = Math.floor(ago / 60);
    if (mins < 60) return `Expired ${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `Expired ${hrs}h ago`;
    return `Expired ${Math.floor(hrs / 24)}d ago`;
  }
  if (deltaSec < 60) return `in ${deltaSec}s`;
  const mins = Math.floor(deltaSec / 60);
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `in ${hrs}h`;
  return `in ${Math.floor(hrs / 24)}d`;
}

export function newCommandId(prefix = "cmd"): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${prefix}-${Date.now()}-${hex}`;
}
