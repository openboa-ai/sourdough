import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { ActivityIsland } from "@openboa/yogurt/motion-activity-island";
import { StorageMeter } from "@openboa/yogurt/motion-storage-meter";
import {
  Badge,
  Button,
  Card,
  cx,
  DataTable,
  DisclosureRow,
  EmptyResult,
  FilterBar,
  Inline,
  Link,
  List,
  Navbar,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  SegmentedControl,
  Skeleton,
  Stack,
  Status,
  Table,
  Text,
  WorkEvent,
  type DataColumn,
  type DataRow,
  type Fact,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatMinorCurrency,
  formatNsClock,
  formatRelativeNs,
  formatSignedPct,
} from "../api/client";
import type {
  ExecutionOrderItem,
  LiveTradingProjection,
  OpenPositionItem,
  ReturnPoint,
} from "../api/types";
import {
  ContextualApprovalList,
  usePendingApprovals,
} from "../components/ContextualApprovalCard";
import { PortfolioChart } from "../components/PortfolioChart";
import { SpaBreadcrumb } from "../components/SpaBreadcrumb";
import { useApp } from "../state/store";

type OrderSnapshot = {
  filled_qty: number;
  status: string;
  remaining_qty: number;
};

type KpiKey = "portfolio" | "bor" | "opex" | "net";

type KpiSignatures = Record<KpiKey, string>;

type KpiPulseState = Record<KpiKey, boolean>;

const EMPTY_KPI_PULSE: KpiPulseState = {
  portfolio: false,
  bor: false,
  opex: false,
  net: false,
};

// Preserves the entry since_seq across intra-SPA Tier 1 navigation while resetting on reload.
const sessionPinnedSinceSeq = new Map<string, number>();

const ORDER_FOCUS_STORAGE_PREFIX = "sd.orderFocusReturn.";

function persistOrderFocusReturn(orderId: string, focusId: string): void {
  try {
    window.sessionStorage.setItem(`${ORDER_FOCUS_STORAGE_PREFIX}${orderId}`, focusId);
  } catch {
    // Ignore storage failures in restricted contexts.
  }
}

function isTerminalOrderStatus(status: string): boolean {
  return (
    status === "filled" ||
    status.startsWith("cancelled") ||
    status === "rejected" ||
    status === "denied" ||
    status === "expired"
  );
}

function getRemainingOpenQty(order: { status: string; remaining_qty: number }): number {
  return isTerminalOrderStatus(order.status) ? 0 : order.remaining_qty;
}

function normalizeToNs(epoch: number | null | undefined): number | null {
  if (epoch === null || epoch === undefined || !Number.isFinite(epoch) || epoch <= 0) {
    return null;
  }
  if (epoch < 1e12) return Math.round(epoch * 1_000_000_000);
  if (epoch < 1e15) return Math.round(epoch * 1_000_000);
  return epoch;
}

function formatTimestamp(epoch: number | null | undefined, emptyLabel: string): string {
  const ns = normalizeToNs(epoch);
  if (ns === null) return emptyLabel;
  return `${formatNsClock(ns)} (${formatRelativeNs(ns)})`;
}

function formatWholeCurrency(major: number, currency: string): string {
  const safeCurrency = /^[A-Z]{3}$/.test(currency) ? currency : "USD";
  const isWhole = Math.abs(major - Math.round(major)) < 0.005;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: safeCurrency,
    minimumFractionDigits: isWhole ? 0 : 2,
    maximumFractionDigits: isWhole ? 0 : 2,
  }).format(major);
}

function buildKpiSignatures(proj: LiveTradingProjection): KpiSignatures {
  const port = proj.kpis.portfolio_value;
  const bor = proj.kpis.trading_bor_pnl;
  const opex = proj.kpis.agent_llm_opex;
  const net = proj.kpis.company_net_pnl;
  return {
    portfolio: `${port.value_minor}|${port.return_pct}|${port.status}`,
    bor: `${bor.value_minor}|${bor.status}`,
    opex: `${opex.value_minor}|${opex.status}|${proj.close_count ?? ""}`,
    net: `${net.value_minor}|${net.status}|${proj.risk_gate.suspended}|${proj.risk_gate.effective_revision}|${proj.adapter_status?.last_quote_minor_units ?? ""}`,
  };
}

function readDisclosureOpen(scopeKey: string | null): boolean {
  if (!scopeKey) return false;
  try {
    return window.localStorage.getItem(`sd.sinceLastVisitOpen.${scopeKey}`) === "true";
  } catch {
    return false;
  }
}

function writeDisclosureOpen(scopeKey: string | null, open: boolean): void {
  if (!scopeKey) return;
  try {
    window.localStorage.setItem(`sd.sinceLastVisitOpen.${scopeKey}`, open ? "true" : "false");
  } catch {
    // Ignore storage failures in restricted contexts.
  }
}

function formatCloseIssue(issue: Record<string, unknown>, idx: number): {
  title: string;
  detail: string;
} {
  const primary =
    issue.reason ??
    issue.detail ??
    issue.message ??
    issue.issue_kind ??
    issue.kind ??
    issue.code ??
    issue.type ??
    `Close issue #${idx + 1}`;
  const title = String(primary);
  const extras = Object.entries(issue)
    .filter(
      ([k, v]) =>
        v !== null &&
        v !== undefined &&
        v !== "" &&
        String(v) !== title &&
        !["reason", "detail", "message"].includes(k)
    )
    .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
  return {
    title,
    detail: extras.length > 0 ? extras.join(" · ") : "Open accounting close discrepancy",
  };
}

function MiniSparkline({ points }: { points: ReturnPoint[] }) {
  const valid = points
    .map((p, idx) => ({ idx, val: p.return_pct }))
    .filter((item): item is { idx: number; val: number } => item.val !== null);
  if (valid.length < 2) {
    return (
      <Text variant="meta" className="sd-tone-muted">
        Sealed valuation series
      </Text>
    );
  }
  const min = Math.min(0, ...valid.map((v) => v.val));
  const max = Math.max(0, ...valid.map((v) => v.val));
  const span = Math.max(max - min, 0.2);
  const w = 180;
  const h = 32;
  const xAt = (idx: number) => 4 + (idx / Math.max(1, points.length - 1)) * (w - 8);
  const yAt = (v: number) => 4 + ((max - v) / span) * (h - 8);
  const zeroY = yAt(0);

  const segments: string[] = [];
  let seg: string[] = [];
  points.forEach((p, idx) => {
    if (p.return_pct === null) {
      if (seg.length > 0) {
        segments.push(seg.join(" "));
        seg = [];
      }
    } else {
      seg.push(`${seg.length === 0 ? "M" : "L"} ${xAt(idx).toFixed(1)} ${yAt(p.return_pct).toFixed(1)}`);
    }
  });
  if (seg.length > 0) segments.push(seg.join(" "));

  return (
    <svg className="sd-kpi-spark" viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
      <line x1={4} y1={zeroY} x2={w - 4} y2={zeroY} className="sd-kpi-spark-zero" />
      {segments.map((d, i) => (
        <path key={i} d={d} className="sd-kpi-spark-line" />
      ))}
    </svg>
  );
}

interface TapeColumnDef {
  id: string;
  label: string;
  kind: "identity" | "text" | "numeric" | "role" | "actions";
  align: "start" | "end";
  sortValue?: (o: ExecutionOrderItem) => string | number | null | undefined;
}

export function LiveTradingView() {
  const {
    navigate,
    refreshSeq,
    linkState,
    reportApiError,
    meta,
    performanceMode,
    getLastSeenSeq,
    markSeenSeq,
    registerEscapeBack,
  } = useApp();

  const [data, setData] = useState<LiveTradingProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [freshOrderIds, setFreshOrderIds] = useState<Set<string>>(() => new Set());
  const [kpiPulse, setKpiPulse] = useState<KpiPulseState>(EMPTY_KPI_PULSE);
  const [tapeQuery, setTapeQuery] = useState("");
  const [tapeFilter, setTapeFilter] = useState<"all" | "working" | "filled" | "cancelled">("all");
  const [tapePresentation, setTapePresentation] = useState<"table" | "feed">("table");
  const [expandedOrderIds, setExpandedOrderIds] = useState<Set<string>>(() => new Set());
  const [tapeSort, setTapeSort] = useState<{ id: string; descending: boolean } | null>(null);
  const [burstLocked, setBurstLocked] = useState(false);
  const [localRefresh, setLocalRefresh] = useState(0);
  const [visitSinceSeq, setVisitSinceSeq] = useState<number | null>(null);

  const livePromotionApprovals = usePendingApprovals(
    (q) =>
      q.kind === "live_promotion" ||
      q.kind === "paper_to_live_migration" ||
      q.kind === "execution_promotion"
  );

  const etagRef = useRef<string | undefined>(undefined);
  const prevProjPathRef = useRef<string | null>(null);
  const prevOrdersRef = useRef<Map<string, OrderSnapshot>>(new Map());
  const prevKpisRef = useRef<KpiSignatures | null>(null);
  const pinnedScopeRef = useRef<string | null>(null);
  const freshTimerRef = useRef<number | null>(null);
  const kpiTimerRef = useRef<number | null>(null);
  const burstTimerRef = useRef<number | null>(null);
  const frozenOrderIdsRef = useRef<string[]>([]);
  const latestSnapshotRef = useRef<{
    companyId: string;
    principalId: string | null;
    seq: number;
  } | null>(null);
  const disclosureWrapRef = useRef<HTMLDivElement | null>(null);

  const scopeKey = meta
    ? `${meta.company_id}.${meta.board_access.principal_id || "unbound"}`
    : null;

  useEffect(() => {
    if (!meta || !scopeKey) return;
    if (pinnedScopeRef.current === scopeKey && visitSinceSeq !== null) return;
    pinnedScopeRef.current = scopeKey;
    const pinned = sessionPinnedSinceSeq.has(scopeKey)
      ? sessionPinnedSinceSeq.get(scopeKey)!
      : getLastSeenSeq(meta.company_id, meta.board_access.principal_id);
    sessionPinnedSinceSeq.set(scopeKey, pinned);
    setVisitSinceSeq(pinned);
  }, [getLastSeenSeq, meta, scopeKey, visitSinceSeq]);

  useEffect(() => {
    if (!performanceMode) return;
    return registerEscapeBack("/trading/paper", "perf-adapter-entry");
  }, [performanceMode, registerEscapeBack]);

  useEffect(() => {
    return () => {
      if (freshTimerRef.current !== null) window.clearTimeout(freshTimerRef.current);
      if (kpiTimerRef.current !== null) window.clearTimeout(kpiTimerRef.current);
      if (burstTimerRef.current !== null) window.clearTimeout(burstTimerRef.current);
    };
  }, []);

  useEffect(() => {
    const onPageHide = () => {
      const snap = latestSnapshotRef.current;
      if (snap) {
        markSeenSeq(snap.companyId, snap.principalId, snap.seq);
      }
    };
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
  }, [markSeenSeq]);

  useEffect(() => {
    const wrap = disclosureWrapRef.current;
    if (!wrap || !scopeKey) return;
    const onToggle = (ev: Event) => {
      const target = ev.target;
      if (target instanceof HTMLDetailsElement) {
        writeDisclosureOpen(scopeKey, target.open);
      }
    };
    wrap.addEventListener("toggle", onToggle, true);
    return () => wrap.removeEventListener("toggle", onToggle, true);
  }, [scopeKey, performanceMode, data?.paper_enabled]);

  useEffect(() => {
    const readyToFetch = scopeKey !== null ? visitSinceSeq !== null : linkState !== "connecting";
    if (!readyToFetch) return;

    const projPath =
      visitSinceSeq !== null && visitSinceSeq > 0
        ? `live-trading?since_seq=${visitSinceSeq}`
        : "live-trading";

    if (prevProjPathRef.current !== projPath) {
      etagRef.current = undefined;
      prevProjPathRef.current = projPath;
    }

    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<LiveTradingProjection>(projPath, {
          etag: etagRef.current,
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.etag) etagRef.current = res.etag;
        if (res.status === 200 && res.data) {
          const nextData = res.data;
          const incomingOrders = new Map<string, OrderSnapshot>(
            nextData.execution_tape.map((o) => [
              o.order_id,
              {
                filled_qty: o.filled_qty,
                status: o.status,
                remaining_qty: o.remaining_qty,
              },
            ])
          );

          if (prevOrdersRef.current.size > 0) {
            const changed = new Set<string>();
            for (const o of nextData.execution_tape) {
              const prev = prevOrdersRef.current.get(o.order_id);
              if (
                !prev ||
                prev.filled_qty !== o.filled_qty ||
                prev.status !== o.status ||
                prev.remaining_qty !== o.remaining_qty
              ) {
                changed.add(o.order_id);
              }
            }
            if (changed.size > 0) {
              if (freshTimerRef.current !== null) window.clearTimeout(freshTimerRef.current);
              setFreshOrderIds(changed);
              freshTimerRef.current = window.setTimeout(() => {
                setFreshOrderIds(new Set());
                freshTimerRef.current = null;
              }, 380);

              if (burstTimerRef.current !== null) window.clearTimeout(burstTimerRef.current);
              setBurstLocked(true);
              burstTimerRef.current = window.setTimeout(() => {
                setBurstLocked(false);
                burstTimerRef.current = null;
              }, 1200);
            }
          }

          const nextSigs = buildKpiSignatures(nextData);
          if (prevKpisRef.current !== null) {
            const prevSigs = prevKpisRef.current;
            const changedKpis: KpiPulseState = {
              portfolio: prevSigs.portfolio !== nextSigs.portfolio,
              bor: prevSigs.bor !== nextSigs.bor,
              opex: prevSigs.opex !== nextSigs.opex,
              net: prevSigs.net !== nextSigs.net,
            };
            if (changedKpis.portfolio || changedKpis.bor || changedKpis.opex || changedKpis.net) {
              if (kpiTimerRef.current !== null) window.clearTimeout(kpiTimerRef.current);
              setKpiPulse(changedKpis);
              kpiTimerRef.current = window.setTimeout(() => {
                setKpiPulse(EMPTY_KPI_PULSE);
                kpiTimerRef.current = null;
              }, 380);
            }
          }

          prevOrdersRef.current = incomingOrders;
          prevKpisRef.current = nextSigs;
          setData(nextData);

          const activePrincipal = meta?.board_access.principal_id ?? null;
          latestSnapshotRef.current = {
            companyId: nextData.company_id,
            principalId: activePrincipal,
            seq: nextData.event_seq,
          };
          markSeenSeq(nextData.company_id, activePrincipal, nextData.event_seq);
        }
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Failed to load live trading");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [
    linkState,
    localRefresh,
    markSeenSeq,
    meta?.board_access.principal_id,
    refreshSeq,
    reportApiError,
    scopeKey,
    visitSinceSeq,
  ]);

  const filteredTape = useMemo(() => {
    const list = data?.execution_tape ?? [];
    const q = tapeQuery.trim().toLowerCase();
    return list.filter((o) => {
      if (tapeFilter === "working" && o.status !== "working" && o.status !== "partial_fill") {
        return false;
      }
      if (tapeFilter === "filled" && o.status !== "filled") {
        return false;
      }
      if (tapeFilter === "cancelled" && !o.cancel_not_flat && o.status !== "cancelled_flat") {
        return false;
      }
      if (!q) return true;
      return `${o.order_id} ${o.instrument} ${o.agent_name} ${o.agent_role} ${o.action}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.execution_tape, tapeQuery, tapeFilter]);

  const tapeColumns: TapeColumnDef[] = useMemo(
    () => [
      {
        id: "order",
        label: "Order & Instrument",
        kind: "identity",
        align: "start",
        sortValue: (o) => `${o.instrument} ${o.order_id}`,
      },
      {
        id: "author",
        label: "Authoring Agent",
        kind: "role",
        align: "start",
        sortValue: (o) => `${o.agent_name} ${o.agent_role}`,
      },
      {
        id: "qty",
        label: "Ordered / Filled / Rem",
        kind: "numeric",
        align: "end",
        sortValue: (o) => o.filled_qty,
      },
      {
        id: "price",
        label: "Limit / Avg / Fees",
        kind: "numeric",
        align: "end",
        sortValue: (o) => o.avg_fill_minor_units ?? o.limit_minor_units,
      },
      {
        id: "status",
        label: "Status & Residual Position",
        kind: "text",
        align: "start",
        sortValue: (o) => (o.cancel_not_flat ? `0-${o.status}` : o.status),
      },
      {
        id: "time",
        label: "Decided",
        kind: "numeric",
        align: "end",
        sortValue: (o) => normalizeToNs(o.decided_at) ?? null,
      },
      {
        id: "trace",
        label: "Details & Why?",
        kind: "actions",
        align: "end",
      },
    ],
    []
  );

  const sortedTape = useMemo(() => {
    const comparatorCol = tapeSort
      ? tapeColumns.find((c) => c.id === tapeSort.id && c.sortValue)
      : undefined;

    if (burstLocked && frozenOrderIdsRef.current.length > 0) {
      const rank = new Map(frozenOrderIdsRef.current.map((id, idx) => [id, idx]));
      const existing: ExecutionOrderItem[] = [];
      const incoming: ExecutionOrderItem[] = [];
      for (const item of filteredTape) {
        if (rank.has(item.order_id)) existing.push(item);
        else incoming.push(item);
      }
      existing.sort((a, b) => (rank.get(a.order_id) ?? 0) - (rank.get(b.order_id) ?? 0));
      return [...existing, ...incoming];
    }

    let ordered = filteredTape;
    if (comparatorCol && comparatorCol.sortValue && tapeSort) {
      const valueOf = comparatorCol.sortValue;
      ordered = [...filteredTape].sort((a, b) => {
        const x = valueOf(a);
        const y = valueOf(b);
        if (x == null) return y == null ? 0 : 1;
        if (y == null) return -1;
        const cmp =
          typeof x === "number" && typeof y === "number"
            ? x - y
            : String(x).localeCompare(String(y), "en", { numeric: true });
        return tapeSort.descending ? -cmp : cmp;
      });
    }
    frozenOrderIdsRef.current = ordered.map((o) => o.order_id);
    return ordered;
  }, [burstLocked, filteredTape, tapeColumns, tapeSort]);

  if (loading && !data) {
    return (
      <Stack gap={16}>
        <PageHeader
          title={performanceMode ? "Paper Performance" : "Paper Trading"}
          detail="Loading authoritative company paper trading books..."
        />
        <Skeleton label="Loading portfolio metrics" lines={4} />
      </Stack>
    );
  }

  if (!data) {
    return (
      <Result
        title="Paper Trading unavailable"
        action={
          <Button
            emphasis="soft"
            shape="pill"
            onClick={() => {
              setLoading(true);
              setError(null);
              setLocalRefresh((n) => n + 1);
            }}
          >
            Retry
          </Button>
        }
      >
        {error ?? "Waiting for local control plane snapshot."}
      </Result>
    );
  }

  const currency = data.kpis.portfolio_value.currency || "USD";
  const asOfShort = formatNsClock(data.as_of_ns);
  const asOfLabel = `As of ${asOfShort} · seq #${data.event_seq}`;

  if (!data.active_domains.includes("paper") || !data.paper_enabled) {
    return (
      <Stack gap={16}>
        <PageHeader
          title="Company Overview"
          detail={`Core-only company · ${asOfLabel}`}
        />
        <Result
          title="Paper profile not enabled on this company"
          action={
            <Inline gap={8} wrap>
              <Button
                emphasis="filled"
                shape="pill"
                onClick={() => navigate("/office")}
              >
                Agents &amp; Activity
              </Button>
              <Button emphasis="soft" shape="pill" onClick={() => navigate("/ceo")}>
                Ask CEO
              </Button>
              <Button
                emphasis="soft"
                shape="pill"
                onClick={() => navigate("/company/approvals")}
              >
                Approvals
              </Button>
            </Inline>
          }
        >
          This company is running core staffing, messaging, and accounting workflows without an
          activated paper trading account. Bind a paper account to inspect live paper executions
          and portfolio performance here.
        </Result>
      </Stack>
    );
  }

  const port = data.kpis.portfolio_value;
  const bor = data.kpis.trading_bor_pnl;
  const opex = data.kpis.agent_llm_opex;
  const net = data.kpis.company_net_pnl;

  const feesBridge = data.pnl_reconciliation_bridge.find((s) => s.step === "trading_fees");
  const fundingBridge = data.pnl_reconciliation_bridge.find(
    (s) =>
      s.step === "funding_settlements" ||
      s.step === "booked_funding" ||
      s.step === "funding"
  );
  const issueList = data.open_close_issues ?? [];
  const issueCount = issueList.length;

  const quoteMinor = data.adapter_status?.last_quote_minor_units ?? null;
  const quoteObsLabel = formatTimestamp(
    data.adapter_status?.last_quote_observed_at,
    ""
  );

  const exposureLimitMajor =
    data.adapter_status?.exposure_limit_minor_units != null
      ? Number((data.adapter_status.exposure_limit_minor_units / 100).toFixed(2))
      : null;

  const exposureCategories = data.open_positions
    .map((pos) => {
      const notionalMinor = Math.abs(pos.quantity) * pos.avg_entry_minor_units;
      const notionalMajor = Number((notionalMinor / 100).toFixed(2));
      return {
        id: pos.instrument,
        label: pos.instrument,
        amount: notionalMajor,
        detail: `${formatWholeCurrency(notionalMajor, currency)} · ${pos.quantity} qty @ ${formatMinorCurrency(pos.avg_entry_minor_units, currency)} · Author: ${pos.agent_name}`,
      };
    })
    .filter((cat) => Number.isFinite(cat.amount) && cat.amount > 0);

  const exposureUsedMajor = Number(
    exposureCategories.reduce((acc, c) => acc + c.amount, 0).toFixed(2)
  );
  const isOverCap =
    exposureLimitMajor !== null && exposureLimitMajor > 0 && exposureUsedMajor > exposureLimitMajor;
  const exposurePct =
    exposureLimitMajor && exposureLimitMajor > 0
      ? Math.round((exposureUsedMajor / exposureLimitMajor) * 100)
      : 0;

  const reconciliationFacts: Fact[] = data.pnl_reconciliation_bridge.map((step) => ({
    id: step.step,
    label: step.label,
    value: (
      <Text
        variant="body"
        className={`sd-tabular ${
          step.status === "unavailable"
            ? "sd-tone-muted"
            : (step.amount_minor ?? 0) > 0
              ? "sd-tone-pos"
              : (step.amount_minor ?? 0) < 0
                ? "sd-tone-neg"
                : ""
        }`}
      >
        {step.status === "unavailable"
          ? "Unavailable"
          : formatMinorCurrency(step.amount_minor, currency, {
              signed: step.step !== "agent_llm_opex" && step.step !== "trading_fees",
            })}
      </Text>
    ),
  }));

  const toggleExpandedOrder = (orderId: string) => {
    setExpandedOrderIds((prev) => {
      const next = new Set(prev);
      if (next.has(orderId)) next.delete(orderId);
      else next.add(orderId);
      return next;
    });
  };

  const renderOrderDetailContent = (o: ExecutionOrderItem) => {
    const isTerminal = isTerminalOrderStatus(o.status);
    const remainingOpen = getRemainingOpenQty(o);
    return (
    <Stack gap={8}>
      <Inline gap={12} align="center" wrap>
        <Text variant="help" className="sd-tabular">
          Remaining Open: {remainingOpen}
          {isTerminal && o.remaining_qty > 0 ? ` (${o.remaining_qty} cancelled)` : ""}
        </Text>
        <Text variant="help" className="sd-tabular">
          Reserved Margin: {formatMinorCurrency(o.reserved_minor_units, currency)}
        </Text>
        <Text variant="help" className="sd-tabular">
          Avg Fill:{" "}
          {o.avg_fill_minor_units === null
            ? "No reading"
            : formatMinorCurrency(o.avg_fill_minor_units, currency)}
        </Text>
        <Text variant="help">Mandate: {o.mandate_revision}</Text>
        {o.rationale ? <Text variant="help">Rationale: {o.rationale}</Text> : null}
      </Inline>

      {o.fills.length === 0 ? (
        <Text variant="meta">No fills recorded for this order.</Text>
      ) : (
        <Stack gap={4}>
          <Text variant="meta">Recorded Fills ({o.fills.length})</Text>
          {o.fills.map((f) => (
            <Inline key={f.fill_id} gap={12} align="center" wrap>
              <Text variant="help" className="sd-mono">
                {f.fill_id}
              </Text>
              <Text variant="help" className="sd-tabular">
                Qty {f.quantity} @ {formatMinorCurrency(f.price_minor_units, currency)}
              </Text>
              <Text variant="help" className="sd-tabular">
                Fee:{" "}
                {f.fee_minor_units === null
                  ? "No reading"
                  : formatMinorCurrency(f.fee_minor_units, currency)}
              </Text>
              <Text variant="help" className="sd-tabular">
                Pos: {f.position_quantity === null ? "No reading" : f.position_quantity}
              </Text>
              <Text variant="meta" className="sd-tabular">
                Observed: {formatTimestamp(f.available_at, "No reading")}
              </Text>
            </Inline>
          ))}
        </Stack>
      )}

      {o.cancellation ? (
        <Inline gap={12} align="center" wrap>
          <Badge tone={o.cancel_not_flat ? "warning" : "neutral"} dot={false}>
            Cancel: {o.cancellation.outcome}
          </Badge>
          <Text variant="help" className="sd-mono">
            {o.cancellation.event_id}
          </Text>
          <Text variant="help">
            Released: {o.cancellation.released ? "Yes" : "No"} · Filled at cancel:{" "}
            {o.cancellation.filled_quantity} · Pos:{" "}
            {o.cancellation.position_quantity === null
              ? "No reading"
              : o.cancellation.position_quantity}
          </Text>
          <Text variant="meta" className="sd-tabular">
            Observed: {formatTimestamp(o.cancellation.available_at, "No reading")}
          </Text>
        </Inline>
      ) : null}

      {o.reconciliation ? (
        <Text variant="meta" className="sd-tabular">
          Reconciliation: {o.reconciliation.attempts} attempt(s)
          {o.reconciliation.fill_id ? ` · Fill ${o.reconciliation.fill_id}` : ""}
        </Text>
      ) : null}
    </Stack>
    );
  };

  const posById = new Map<string, OpenPositionItem>(
    data.open_positions.map((pos) => [`${pos.instrument}-${pos.latest_order_id}`, pos])
  );

  const posColumns: DataColumn[] = [
    {
      id: "instrument",
      label: "Instrument",
      kind: "identity",
      sortValue: (row) => posById.get(row.id)?.instrument ?? row.id,
    },
    {
      id: "qty",
      label: "Net Quantity",
      kind: "numeric",
      align: "end",
      sortValue: (row) => posById.get(row.id)?.quantity ?? null,
    },
    {
      id: "entry",
      label: "Avg Entry",
      kind: "numeric",
      align: "end",
      sortValue: (row) => posById.get(row.id)?.avg_entry_minor_units ?? null,
    },
    {
      id: "owner",
      label: "Latest Author",
      kind: "text",
      sortValue: (row) => posById.get(row.id)?.agent_name ?? "",
    },
    {
      id: "action",
      label: "Trace",
      kind: "actions",
      align: "end",
      sortValue: (row) => posById.get(row.id)?.latest_order_id ?? "",
    },
  ];

  const posRows: DataRow[] = data.open_positions.map((pos) => ({
    id: `${pos.instrument}-${pos.latest_order_id}`,
    cells: {
      instrument: (
        <Inline gap={8} align="center">
          <Text variant="body">{pos.instrument}</Text>
          {pos.from_cancelled_order ? (
            <Badge tone="warning" dot={false}>
              Residual from cancel
            </Badge>
          ) : null}
        </Inline>
      ),
      qty: <Text className="sd-tabular">{pos.quantity}</Text>,
      entry: (
        <Text className="sd-tabular">
          {formatMinorCurrency(pos.avg_entry_minor_units, currency)}
        </Text>
      ),
      owner: pos.agent_name,
      action: (
        <Button
          emphasis="quiet"
          size="compact"
          shape="pill"
          data-focus-id={`pos-order-${pos.latest_order_id}`}
          onClick={() => {
            const focusId = `pos-order-${pos.latest_order_id}`;
            persistOrderFocusReturn(pos.latest_order_id, focusId);
            navigate(`/orders/${encodeURIComponent(pos.latest_order_id)}`, {
              focusReturnId: focusId,
            });
          }}
        >
          Order
        </Button>
      ),
    },
  }));

  const handleMarkCaughtUp = () => {
    if (!meta || !scopeKey) return;
    const seq = data.event_seq;
    sessionPinnedSinceSeq.set(scopeKey, seq);
    markSeenSeq(meta.company_id, meta.board_access.principal_id, seq);
    etagRef.current = undefined;
    setVisitSinceSeq(seq);
    setLocalRefresh((n) => n + 1);
  };

  const domainEntries = Object.entries(data.since_last_visit.by_domain);
  const initialDisclosureOpen = readDisclosureOpen(scopeKey);

  return (
    <Stack gap={16}>
      {performanceMode ? (
        <Fragment>
          <Navbar
            variant="back"
            label="Back to Paper Trading"
            onBack={() => navigate("/trading/paper", { focusReturnId: "perf-adapter-entry" })}
          />
          <SpaBreadcrumb
            focusReturnId="perf-adapter-entry"
            items={[
              { id: "paper", label: "Paper Trading", href: "/trading/paper" },
              { id: "performance", label: "Performance", href: "/performance" },
            ]}
          />
        </Fragment>
      ) : null}

      <PageHeader
        title={performanceMode ? "Paper Performance" : "Paper Trading"}
        detail={asOfLabel}
        action={
          <Inline gap={8} align="center" wrap>
            <ActivityIsland
              label={linkState === "live" ? "Paper Tape" : "Snapshot"}
              summary={`${data.execution_tape.length} orders · #${data.event_seq}`}
            >
              <Stack gap={8}>
                <Text variant="meta">
                  Recent daemon stream · {data.since_last_visit.new_event_count} events since last visit
                </Text>
                {data.since_last_visit.highlights.slice(0, 4).map((ev) => (
                  <Inline key={ev.event_seq} justify="between" align="center" gap={8}>
                    <Text variant="help">
                      {ev.domain}.{ev.event_type}
                    </Text>
                    <Text variant="meta" className="sd-tabular">
                      #{ev.event_seq}
                    </Text>
                  </Inline>
                ))}
              </Stack>
            </ActivityIsland>
            <Status tone={data.risk_gate.suspended ? "warning" : "success"}>
              {data.risk_gate.suspended ? "New Orders Paused" : "Risk Gate Open"}
            </Status>
            {!performanceMode ? (
              <Inline gap={6} align="center">
                <Button
                  emphasis="soft"
                  size="compact"
                  shape="pill"
                  onClick={() => navigate("/")}
                >
                  Live Trading
                </Button>
                <Button
                  emphasis="quiet"
                  size="compact"
                  shape="pill"
                  data-focus-id="perf-adapter-entry"
                  onClick={() =>
                    navigate("/performance", { focusReturnId: "perf-adapter-entry" })
                  }
                >
                  Performance
                </Button>
              </Inline>
            ) : null}
          </Inline>
        }
      />

      <ContextualApprovalList questions={livePromotionApprovals} />

      <div className="sd-kpi-grid">
        <Card
          variant="raised"
          density="compact"
          className="sd-kpi-card"
          data-state={kpiPulse.portfolio ? "pulse" : undefined}
        >
          <Inline justify="between" align="center">
            <Text variant="meta">Paper Portfolio Value</Text>
            <Badge tone="neutral" variant="outline" dot={false}>
              Paper
            </Badge>
          </Inline>
          <Text className="sd-kpi-value">
            {formatMinorCurrency(port.value_minor, currency)}
          </Text>
          <div className="sd-kpi-mid">
            <MiniSparkline points={data.cumulative_return_series} />
          </div>
          <Inline justify="between" align="center">
            <Text
              variant="meta"
              className={`sd-tabular ${
                port.return_pct === null
                  ? "sd-tone-muted"
                  : port.return_pct >= 0
                    ? "sd-tone-pos"
                    : "sd-tone-neg"
              }`}
            >
              {formatSignedPct(port.return_pct)}
            </Text>
            <Text variant="meta">{port.source}</Text>
          </Inline>
        </Card>

        <Card
          variant="raised"
          density="compact"
          className="sd-kpi-card"
          data-state={kpiPulse.bor ? "pulse" : undefined}
        >
          <Inline justify="between" align="center">
            <Text variant="meta">Trading BOR P&amp;L</Text>
            <Badge tone="neutral" variant="soft" dot={false}>
              {currency}
            </Badge>
          </Inline>
          <Text
            className={`sd-kpi-value ${
              bor.value_minor === null
                ? "sd-tone-muted"
                : bor.value_minor >= 0
                  ? "sd-tone-pos"
                  : "sd-tone-neg"
            }`}
          >
            {formatMinorCurrency(bor.value_minor, currency, { signed: true })}
          </Text>
          <div className="sd-kpi-mid">
            <Text variant="meta" className="sd-tabular">
              Fees{" "}
              {feesBridge && feesBridge.status !== "unavailable"
                ? formatMinorCurrency(feesBridge.amount_minor, currency)
                : "Unavailable"}
              {fundingBridge
                ? ` · Funding ${
                    fundingBridge.status === "unavailable"
                      ? "Unavailable"
                      : formatMinorCurrency(fundingBridge.amount_minor, currency, { signed: true })
                  }`
                : ""}
            </Text>
          </div>
          <Inline justify="between" align="center">
            <Text variant="meta">As of {asOfShort}</Text>
            <Text variant="meta">{bor.source}</Text>
          </Inline>
        </Card>

        <Card
          variant="raised"
          density="compact"
          className="sd-kpi-card"
          data-state={kpiPulse.opex ? "pulse" : undefined}
        >
          <Inline justify="between" align="center">
            <Text variant="meta">Agent LLM &amp; Compute Opex</Text>
            <Badge tone={issueCount > 0 ? "warning" : "neutral"} variant="soft" dot={false}>
              {issueCount > 0
                ? `${issueCount} open issue${issueCount === 1 ? "" : "s"}`
                : "Close settled"}
            </Badge>
          </Inline>
          <Text className="sd-kpi-value">
            {formatMinorCurrency(opex.value_minor, currency)}
          </Text>
          <div className="sd-kpi-mid">
            <Text variant="meta" className="sd-tabular">
              {data.close_count !== undefined
                ? `${data.close_count} accounting close${data.close_count === 1 ? "" : "s"}`
                : "General ledger operating cost"}
            </Text>
          </div>
          <Inline justify="between" align="center">
            <Text variant="meta">As of {asOfShort}</Text>
            <Text variant="meta">{opex.source}</Text>
          </Inline>
        </Card>

        <Card
          variant="raised"
          density="compact"
          className="sd-kpi-card"
          data-state={kpiPulse.net ? "pulse" : undefined}
        >
          <Inline justify="between" align="center">
            <Text variant="meta">Company Net P&amp;L</Text>
            <Status tone={data.risk_gate.suspended ? "warning" : "success"}>
              {data.risk_gate.suspended ? "Paused" : "Active"}
            </Status>
          </Inline>
          <Text
            className={`sd-kpi-value ${
              net.value_minor === null
                ? "sd-tone-muted"
                : net.value_minor >= 0
                  ? "sd-tone-pos"
                  : "sd-tone-neg"
            }`}
          >
            {formatMinorCurrency(net.value_minor, currency, { signed: true })}
          </Text>
          <div className="sd-kpi-mid">
            <Text variant="meta" className="sd-tabular">
              {quoteMinor !== null
                ? `Last quote ${formatMinorCurrency(quoteMinor, currency)}${
                    quoteObsLabel ? ` · ${quoteObsLabel}` : ""
                  }`
                : "No live market tick · Sealed valuation"}
            </Text>
          </div>
          <Inline justify="between" align="center">
            <Text variant="meta" className="sd-tabular">
              Gate rev #{data.risk_gate.effective_revision}
            </Text>
            <Text variant="meta">{net.source}</Text>
          </Inline>
        </Card>
      </div>

      {!performanceMode ? (
        <div ref={disclosureWrapRef}>
          <DisclosureRow
            key={`since-visit-${scopeKey ?? "anon"}`}
            label={`Since Last Visit · ${data.since_last_visit.new_event_count} recorded events`}
            initiallyOpen={initialDisclosureOpen}
          >
            <Stack gap={12}>
              <Inline justify="between" align="center" wrap>
                <Inline gap={6} align="center" wrap>
                  {domainEntries.length > 0 ? (
                    domainEntries.map(([domain, count]) => (
                      <Badge key={domain} tone="neutral" variant="soft" dot={false}>
                        {domain}: {count}
                      </Badge>
                    ))
                  ) : (
                    <Text variant="meta">
                      Since seq #{data.since_last_visit.since_seq}
                    </Text>
                  )}
                </Inline>
                <Button
                  emphasis="soft"
                  size="compact"
                  shape="pill"
                  disabled={!meta || data.since_last_visit.new_event_count === 0}
                  onClick={handleMarkCaughtUp}
                >
                  Mark caught up
                </Button>
              </Inline>

              {data.since_last_visit.highlights.length === 0 ? (
                <Text variant="help">No new company events since your previous visit.</Text>
              ) : (
                <List label="Recent company events" divided>
                  {data.since_last_visit.highlights.map((ev) => (
                    <li key={ev.event_seq} className="yg-list-item">
                      <WorkEvent
                        event={{
                          id: String(ev.event_seq),
                          kind:
                            ev.domain === "paper"
                              ? "task"
                              : ev.domain === "ops"
                                ? "routine"
                                : "review",
                          title: `${ev.domain} · ${ev.event_type}`,
                          detail: ev.entity_id,
                          time: `${formatRelativeNs(ev.recorded_at_ns)} · seq #${ev.event_seq}`,
                          state: "done",
                        }}
                      />
                    </li>
                  ))}
                </List>
              )}
            </Stack>
          </DisclosureRow>
        </div>
      ) : null}

      <div className="sd-two-col">
        <PortfolioChart
          points={data.cumulative_return_series}
          currency={currency}
          asOfLabel={asOfLabel}
        />

        <Stack gap={16}>
          <Card
            title="Two-Layer P&L Reconciliation"
            description="Trading books of record vs Company operating costs"
            variant="raised"
          >
            <Stack gap={12}>
              <RecordFacts
                source={
                  data.adapter_status
                    ? `${data.adapter_status.connector_revision} · ${data.adapter_status.account_id}`
                    : "paper_valuation + general_ledger"
                }
                asOf={`seq #${data.event_seq}`}
                facts={reconciliationFacts}
              />

              {issueCount > 0 ? (
                <Notice title="Open accounting close issue" tone="warning">
                  Company Net P&amp;L remains Unavailable until all missing cost records are closed —
                  never coerced to $0.00.
                </Notice>
              ) : null}
            </Stack>
          </Card>

          {exposureLimitMajor !== null && exposureLimitMajor > 0 ? (
            <Card
              title="Paper Exposure Cap Utilization"
              headerAction={
                <Inline gap={8} align="center">
                  {isOverCap ? (
                    <Badge tone="warning" dot={false}>
                      Over cap
                    </Badge>
                  ) : null}
                  <Text variant="meta" className="sd-tabular">
                    {formatWholeCurrency(exposureUsedMajor, currency)} /{" "}
                    {formatWholeCurrency(exposureLimitMajor, currency)} ({exposurePct}%)
                  </Text>
                </Inline>
              }
              variant="raised"
              density="compact"
              className="sd-exposure-meter"
            >
              <Stack gap={8}>
                <StorageMeter
                  label="Paper Exposure Cap Utilization"
                  used={exposureUsedMajor}
                  capacity={exposureLimitMajor}
                  unit={currency}
                  categories={exposureCategories}
                />
                {exposureCategories.length > 0 ? (
                  <Text variant="meta" className="sd-tabular">
                    {exposureCategories
                      .map((c) => `${c.label} ${formatWholeCurrency(c.amount, currency)}`)
                      .join(" · ")}
                  </Text>
                ) : null}
                {isOverCap ? (
                  <Text variant="meta" className="sd-tone-warn sd-tabular">
                    Exposure exceeds cap by{" "}
                    {formatWholeCurrency(exposureUsedMajor - exposureLimitMajor, currency)}
                  </Text>
                ) : null}
              </Stack>
            </Card>
          ) : null}
        </Stack>
      </div>

      {performanceMode ? (
        <div className="sd-split-equal">
          <Card title="Adapter & Binding Telemetry" variant="raised">
            {data.adapter_status ? (
              <div className="sd-facts-wrap">
                <RecordFacts
                  source="paper_binding + venue_adapter"
                  asOf={`seq #${data.event_seq}`}
                  facts={[
                    {
                      id: "env",
                      label: "Environment",
                      value: (
                        <Badge tone="neutral" variant="outline" dot={false}>
                          {data.adapter_status.environment}
                        </Badge>
                      ),
                    },
                    {
                      id: "account",
                      label: "Account ID",
                      value: <Text className="sd-mono">{data.adapter_status.account_id}</Text>,
                    },
                    {
                      id: "destination",
                      label: "Destination ID",
                      value: <Text className="sd-mono">{data.adapter_status.destination_id}</Text>,
                    },
                    {
                      id: "connector",
                      label: "Connector Revision",
                      value: (
                        <Text className="sd-mono">{data.adapter_status.connector_revision}</Text>
                      ),
                    },
                    {
                      id: "mandate",
                      label: "Mandate Revision",
                      value: (
                        <Text className="sd-mono">{data.adapter_status.mandate_revision}</Text>
                      ),
                    },
                    {
                      id: "starting",
                      label: "Starting Capital",
                      value: formatMinorCurrency(
                        data.adapter_status.starting_minor_units,
                        data.adapter_status.currency || currency
                      ),
                    },
                    {
                      id: "limit",
                      label: "Exposure Limit",
                      value: formatMinorCurrency(
                        data.adapter_status.exposure_limit_minor_units,
                        data.adapter_status.currency || currency
                      ),
                    },
                    {
                      id: "quote",
                      label: "Last Market Quote",
                      value:
                        data.adapter_status.last_quote_minor_units === null
                          ? "No live market tick"
                          : formatMinorCurrency(
                              data.adapter_status.last_quote_minor_units,
                              data.adapter_status.currency || currency
                            ),
                    },
                    {
                      id: "quote_at",
                      label: "Last Quote Observed",
                      value: formatTimestamp(
                        data.adapter_status.last_quote_observed_at,
                        "No reading"
                      ),
                    },
                  ]}
                />
              </div>
            ) : (
              <Text variant="help">Adapter binding telemetry unavailable.</Text>
            )}
          </Card>

          <Card
            title="Accounting Close & Open Issues"
            description={`${data.close_count ?? 0} close run(s) recorded`}
            variant="raised"
          >
            {issueList.length === 0 ? (
              <Text variant="help">No open accounting close issues.</Text>
            ) : (
              <Stack gap={12}>
                <Notice title={`${issueCount} open close issue(s)`} tone="warning">
                  Company Net P&amp;L remains Unavailable until these close items are resolved.
                </Notice>
                <List label="Open accounting close issues" divided>
                  {issueList.map((issue, idx) => {
                    const parsed = formatCloseIssue(issue, idx);
                    return (
                      <li key={`close-issue-${idx}`} className="yg-list-item">
                        <WorkEvent
                          event={{
                            id: `close-issue-${idx}`,
                            kind: "review",
                            title: parsed.title,
                            detail: parsed.detail,
                            state: "waiting",
                          }}
                        />
                      </li>
                    );
                  })}
                </List>
              </Stack>
            )}
          </Card>
        </div>
      ) : null}

      <Card
        title="Live Paper Execution Tape"
        description={
          linkState === "live"
            ? "Coalesced live stream · Grouped causal Order → Fills → Fees → Cancellation → Position"
            : "Snapshot paused while reconnecting"
        }
        headerAction={
          <Inline gap={8} align="center" wrap>
            {burstLocked ? (
              <Button
                emphasis="soft"
                size="compact"
                shape="pill"
                onClick={() => {
                  if (burstTimerRef.current !== null) {
                    window.clearTimeout(burstTimerRef.current);
                    burstTimerRef.current = null;
                  }
                  setBurstLocked(false);
                }}
              >
                Sort locked (burst) · Unlock
              </Button>
            ) : null}
            <SegmentedControl
              label="Execution tape layout"
              value={tapePresentation}
              onValueChange={(next) => setTapePresentation(next as "table" | "feed")}
              options={[
                { id: "table", label: "Table" },
                { id: "feed", label: "Causal Feed" },
              ]}
            />
          </Inline>
        }
        variant="raised"
      >
        <Stack gap={12}>
          <FilterBar
            label="Filter execution tape"
            hiddenLabel
            placeholder="Filter by order ID, instrument, or agent..."
            size="compact"
            query={tapeQuery}
            onQueryChange={setTapeQuery}
          >
            <SegmentedControl
              label="Filter by order status"
              value={tapeFilter}
              onValueChange={(next) =>
                setTapeFilter(next as "all" | "working" | "filled" | "cancelled")
              }
              options={[
                { id: "all", label: `All (${data.execution_tape.length})` },
                { id: "working", label: "Working" },
                { id: "filled", label: "Filled" },
                { id: "cancelled", label: "Cancelled" },
              ]}
            />
          </FilterBar>

          {data.execution_tape.length === 0 ? (
            <Text variant="help">No paper orders recorded yet for this company.</Text>
          ) : filteredTape.length === 0 ? (
            <EmptyResult
              query={tapeQuery || tapeFilter}
              onClear={() => {
                setTapeQuery("");
                setTapeFilter("all");
              }}
            />
          ) : tapePresentation === "table" ? (
            <Table caption="Paper execution tape">
              <thead>
                <tr>
                  {tapeColumns.map((col) => {
                    const isNumeric = col.kind === "numeric";
                    const isSorted = tapeSort?.id === col.id && Boolean(col.sortValue);
                    return (
                      <th
                        key={col.id}
                        scope="col"
                        data-kind={col.kind}
                        data-align={col.align}
                        data-numeric={isNumeric || undefined}
                        aria-sort={
                          isSorted
                            ? tapeSort!.descending
                              ? "descending"
                              : "ascending"
                            : undefined
                        }
                      >
                        {col.sortValue ? (
                          <button
                            type="button"
                            className="yg-table-sort"
                            onClick={() => {
                              if (burstTimerRef.current !== null) {
                                window.clearTimeout(burstTimerRef.current);
                                burstTimerRef.current = null;
                              }
                              setBurstLocked(false);
                              setTapeSort((prev) => ({
                                id: col.id,
                                descending: prev?.id === col.id && !prev.descending,
                              }));
                            }}
                          >
                            {col.label}
                            <span aria-hidden="true">
                              {isSorted ? (tapeSort!.descending ? "↓" : "↑") : "↕"}
                            </span>
                          </button>
                        ) : (
                          col.label
                        )}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {sortedTape.map((o) => {
                  const isFresh = freshOrderIds.has(o.order_id);
                  const isExpanded = expandedOrderIds.has(o.order_id);
                  const focusId = `order-${o.order_id}`;
                  const statusBadge = o.cancel_not_flat ? (
                    <Stack gap={2}>
                      <Badge tone="warning" dot={false}>
                        Cancelled · Open {o.residual_position_qty}
                      </Badge>
                      <Text variant="meta">Cancel did not flatten filled exposure</Text>
                    </Stack>
                  ) : o.status === "filled" ? (
                    <Badge tone="success" dot={false}>
                      Filled · Pos {o.residual_position_qty ?? o.filled_qty}
                    </Badge>
                  ) : o.status === "partial_fill" ? (
                    <Badge tone="brand" dot={false}>
                      Partial · Pos {o.residual_position_qty ?? o.filled_qty}
                    </Badge>
                  ) : o.status === "cancelled_flat" ? (
                    <Badge tone="neutral" dot={false}>
                      Cancelled · Flat (0)
                    </Badge>
                  ) : (
                    <Badge tone="neutral" dot={false}>
                      Working
                    </Badge>
                  );

                  return (
                    <Fragment key={o.order_id}>
                      <tr
                        data-fresh={isFresh ? "true" : undefined}
                        className={isFresh ? "sd-live-enter sd-live-flash" : undefined}
                        onClick={(event) => {
                          const target = event.target;
                          if (
                            target instanceof HTMLElement &&
                            target.closest("button, a, input, summary, [role='button']")
                          ) {
                            return;
                          }
                          toggleExpandedOrder(o.order_id);
                        }}
                      >
                        <td data-kind="identity" data-align="start">
                          <Stack gap={2}>
                            <Link
                              href={`/orders/${encodeURIComponent(o.order_id)}`}
                              className="sd-tape-order-link"
                              onClick={(e) => {
                                e.preventDefault();
                                persistOrderFocusReturn(o.order_id, focusId);
                                navigate(`/orders/${encodeURIComponent(o.order_id)}`, {
                                  focusReturnId: focusId,
                                });
                              }}
                            >
                              {o.action.toUpperCase()} {o.instrument}
                            </Link>
                            <Text variant="meta" className="sd-mono">
                              {o.order_id}
                            </Text>
                          </Stack>
                        </td>
                        <td data-kind="role" data-align="start">
                          <Stack gap={2}>
                            <Text variant="body">{o.agent_name}</Text>
                            <Text variant="meta">{o.agent_role}</Text>
                          </Stack>
                        </td>
                        <td data-kind="numeric" data-align="end" data-numeric="true">
                          <Text variant="body" className="sd-tabular">
                            {o.ordered_qty} / {o.filled_qty} / {getRemainingOpenQty(o)}
                          </Text>
                        </td>
                        <td data-kind="numeric" data-align="end" data-numeric="true">
                          <Stack gap={2} align="end">
                            <Text variant="body" className="sd-tabular">
                              {formatMinorCurrency(
                                o.avg_fill_minor_units ?? o.limit_minor_units,
                                currency
                              )}
                            </Text>
                            <Text variant="meta" className="sd-tabular">
                              Fee {formatMinorCurrency(o.total_fee_minor_units, currency)}
                            </Text>
                          </Stack>
                        </td>
                        <td data-kind="text" data-align="start">
                          {statusBadge}
                        </td>
                        <td data-kind="numeric" data-align="end" data-numeric="true">
                          <Text variant="meta" className="sd-tabular">
                            {formatTimestamp(o.decided_at, "Unavailable")}
                          </Text>
                        </td>
                        <td data-kind="actions" data-align="end">
                          <Inline gap={6} align="center" justify="end">
                            <Button
                              emphasis="quiet"
                              size="compact"
                              shape="pill"
                              aria-expanded={isExpanded}
                              onClick={() => toggleExpandedOrder(o.order_id)}
                            >
                              {isExpanded ? "Hide" : `Fills (${o.fills.length})`}
                            </Button>
                            <Button
                              emphasis="quiet"
                              size="compact"
                              shape="pill"
                              data-focus-id={focusId}
                              onClick={() => {
                                persistOrderFocusReturn(o.order_id, focusId);
                                navigate(`/orders/${encodeURIComponent(o.order_id)}`, {
                                  focusReturnId: focusId,
                                });
                              }}
                            >
                              Why?
                            </Button>
                          </Inline>
                        </td>
                      </tr>
                      {isExpanded ? (
                        <tr className="sd-tape-detail-row">
                          <td colSpan={tapeColumns.length}>{renderOrderDetailContent(o)}</td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </Table>
          ) : (
            <List label="Live execution causal feed" divided>
              {sortedTape.map((o) => {
                const isFresh = freshOrderIds.has(o.order_id);
                const isExpanded = expandedOrderIds.has(o.order_id);
                const focusId = `order-${o.order_id}`;
                const isTerminal = isTerminalOrderStatus(o.status);
                const remainingOpen = getRemainingOpenQty(o);
                return (
                  <li
                    key={o.order_id}
                    data-fresh={isFresh ? "true" : undefined}
                    className={cx("yg-list-item", isFresh && "sd-live-enter sd-live-flash")}
                  >
                    <Stack gap={8}>
                      <WorkEvent
                        event={{
                          id: o.order_id,
                          kind: o.cancel_not_flat ? "review" : "task",
                          title: `${o.action.toUpperCase()} ${o.instrument} (${o.filled_qty}/${o.ordered_qty})`,
                          detail: `${o.agent_name} · Rem open ${remainingOpen}${
                            isTerminal && o.remaining_qty > 0
                              ? ` (${o.remaining_qty} cancelled)`
                              : ""
                          } · Avg ${formatMinorCurrency(
                            o.avg_fill_minor_units ?? o.limit_minor_units,
                            currency
                          )} · Fee ${formatMinorCurrency(o.total_fee_minor_units, currency)}${
                            o.cancel_not_flat
                              ? ` · Residual open ${o.residual_position_qty} (cancel did not flatten)`
                              : ""
                          }`,
                          time: formatTimestamp(o.decided_at, "Unavailable"),
                          state: o.cancel_not_flat
                            ? "waiting"
                            : o.status === "filled"
                              ? "ready"
                              : "running",
                        }}
                        inlineMeta={
                          <Badge
                            tone={
                              o.cancel_not_flat
                                ? "warning"
                                : o.status === "filled"
                                  ? "success"
                                  : "brand"
                            }
                            dot={false}
                          >
                            {o.cancel_not_flat
                              ? `Cancelled · Open ${o.residual_position_qty}`
                              : o.status}
                          </Badge>
                        }
                        hideStateBadge
                        actions={
                          <Inline gap={6} align="center">
                            <Button
                              emphasis="quiet"
                              size="compact"
                              shape="pill"
                              aria-expanded={isExpanded}
                              onClick={() => toggleExpandedOrder(o.order_id)}
                            >
                              {isExpanded ? "Hide" : `Fills (${o.fills.length})`}
                            </Button>
                            <Button
                              emphasis="quiet"
                              size="compact"
                              shape="pill"
                              data-focus-id={focusId}
                              onClick={() => {
                                persistOrderFocusReturn(o.order_id, focusId);
                                navigate(`/orders/${encodeURIComponent(o.order_id)}`, {
                                  focusReturnId: focusId,
                                });
                              }}
                            >
                              Why?
                            </Button>
                          </Inline>
                        }
                      />
                      {isExpanded ? renderOrderDetailContent(o) : null}
                    </Stack>
                  </li>
                );
              })}
            </List>
          )}
        </Stack>
      </Card>

      <Card
        title="Open Paper Positions"
        description="Current observed paper exposure across instruments"
        variant="raised"
      >
        {data.open_positions.length === 0 ? (
          <Text variant="help">No open paper positions (flat).</Text>
        ) : (
          <DataTable
            caption="Open paper positions"
            columns={posColumns}
            rows={posRows}
          />
        )}
      </Card>
    </Stack>
  );
}
