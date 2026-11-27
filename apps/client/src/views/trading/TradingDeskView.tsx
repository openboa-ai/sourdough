import { Fragment, useEffect, useMemo, useState } from "react";
import { StorageMeter } from "@openboa/yogurt/motion-storage-meter";
import {
  Badge,
  Button,
  Card,
  DataTable,
  Divider,
  FilterBar,
  Inline,
  Link,
  List,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  SegmentedControl,
  Skeleton,
  Stack,
  Status,
  Text,
  TextField,
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
  newCommandId,
} from "../../api/client";
import type {
  LiveCandidateBindingItem,
  ReturnPoint,
  TradingDeskBookSummary,
  TradingDeskProjection,
} from "../../api/types";
import {
  ContextualApprovalList,
  usePendingApprovals,
} from "../../components/ContextualApprovalCard";
import { ControlledInlineConfirm } from "../../components/ControlledInlineConfirm";
import { PortfolioChart } from "../../components/PortfolioChart";
import { useApp } from "../../state/store";

const ORDER_FOCUS_STORAGE_PREFIX = "sd.orderFocusReturn.";

function persistOrderFocusReturn(orderId: string, focusId: string): void {
  try {
    window.sessionStorage.setItem(`${ORDER_FOCUS_STORAGE_PREFIX}${orderId}`, focusId);
  } catch {
    // Ignore storage failures in restricted contexts.
  }
}

function formatShortHash(digest: string | null | undefined): string {
  if (!digest) return "Unavailable";
  return digest.length > 16 ? `${digest.slice(0, 12)}…` : digest;
}

function formatTs(ns: number | null | undefined): string {
  if (!ns || !Number.isFinite(ns)) return "Unavailable";
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

function MiniSparkline({ points, label }: { points: ReturnPoint[]; label: string }) {
  const valid = points
    .map((p, idx) => ({ idx, val: p.return_pct }))
    .filter((item): item is { idx: number; val: number } => item.val !== null);
  if (valid.length < 2) {
    return (
      <Text variant="meta" className="sd-tone-muted">
        {label}
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
      seg.push(
        `${seg.length === 0 ? "M" : "L"} ${xAt(idx).toFixed(1)} ${yAt(p.return_pct).toFixed(1)}`
      );
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

function roleBadgeProps(role: string): {
  tone: "brand" | "success" | "warning" | "neutral";
  label: string;
} {
  if (role === "nominated") return { tone: "brand", label: "Recommended for Live" };
  if (role === "shadow") return { tone: "success", label: "Shadow Benchmark" };
  if (role === "retired") return { tone: "neutral", label: "Retired" };
  return { tone: "neutral", label: "Active" };
}

export function TradingDeskView({ mode }: { mode: "live" | "paper" }) {
  const {
    navigate,
    refreshSeq,
    meta,
    canWrite,
    outbox,
    dispatchCommand,
    reportApiError,
    pushToast,
  } = useApp();

  const [selectedBookId, setSelectedBookId] = useState<string | null>(null);
  const [data, setData] = useState<TradingDeskProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [tapeQuery, setTapeQuery] = useState("");
  const [tapeStatus, setTapeStatus] = useState<"all" | "filled" | "open">("all");
  const [promoteTargetId, setPromoteTargetId] = useState<string | null>(null);
  const [demoteAction, setDemoteAction] = useState<"demote_to_paper" | "suspend_live" | null>(
    null
  );
  const [newBookOpen, setNewBookOpen] = useState(false);
  const [newBookName, setNewBookName] = useState("");
  const [newAccountId, setNewAccountId] = useState("");
  const [newAccountTouched, setNewAccountTouched] = useState(false);
  const [newStartingUsd, setNewStartingUsd] = useState("100000");
  const [newExposureUsd, setNewExposureUsd] = useState("50000");
  const [newStrategyRelease, setNewStrategyRelease] = useState("");
  const [newFeeBps, setNewFeeBps] = useState("4");
  const [newSlippageBps, setNewSlippageBps] = useState("2");

  // Reset book selection cleanly when switching between Live Trading and Paper Trading.
  useEffect(() => {
    setSelectedBookId(null);
    setTapeQuery("");
    setTapeStatus("all");
    setPromoteTargetId(null);
    setDemoteAction(null);
    setNewBookOpen(false);
  }, [mode]);

  const promotionApprovals = usePendingApprovals(
    (q) =>
      q.kind === "live_promotion" ||
      q.kind === "paper_to_live_migration" ||
      q.kind === "execution_promotion"
  );

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const qs = new URLSearchParams({ environment: mode });
    if (selectedBookId) {
      qs.set("book_id", selectedBookId);
    }
    void (async () => {
      try {
        const res = await fetchProjection<TradingDeskProjection>(
          `trading-desk?${qs.toString()}`,
          { signal: controller.signal }
        );
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(
          err instanceof Error
            ? err.message
            : `Failed to load ${mode === "live" ? "live" : "paper"} trading desk`
        );
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [mode, selectedBookId, refreshSeq, retrySeq, reportApiError]);

  const activeData = data && data.environment === mode ? data : null;
  const isLive = mode === "live";

  const filteredTape = useMemo(() => {
    if (!activeData) return [];
    const q = tapeQuery.trim().toLowerCase();
    return activeData.execution_tape.filter((o) => {
      if (tapeStatus === "filled" && o.status !== "filled") return false;
      if (tapeStatus === "open" && o.status !== "open") return false;
      if (!q) return true;
      return (
        o.order_id.toLowerCase().includes(q) ||
        o.instrument_id.toLowerCase().includes(q) ||
        o.actor_name.toLowerCase().includes(q) ||
        o.side.toLowerCase().includes(q) ||
        (o.external_order_id ?? "").toLowerCase().includes(q)
      );
    });
  }, [activeData, tapeQuery, tapeStatus]);

  if (loading && !activeData) {
    return (
      <Stack gap={16}>
        <PageHeader
          title={isLive ? "Live Trading" : "Paper Trading"}
          detail={
            isLive
              ? "Loading live capital KPIs, performance chart, positions, and venue gates..."
              : "Loading company paper books, simulated equity curves, positions, and execution tape..."
          }
        />
        <Skeleton label="Loading trading desk" lines={6} />
      </Stack>
    );
  }

  if (!activeData) {
    return (
      <Result
        title={isLive ? "Live Trading unavailable" : "Paper Trading unavailable"}
        action={
          <Button
            emphasis="soft"
            shape="pill"
            onClick={() => {
              setLoading(true);
              setError(null);
              setRetrySeq((n) => n + 1);
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

  if (!activeData.trading_enabled) {
    return (
      <Stack gap={16}>
        <PageHeader
          title={isLive ? "Live Trading" : "Paper Trading"}
          detail="Trading domain not installed"
        />
        <Result title="Trading module inactive">
          Activate the Trading domain module to access paper books, live venues, and execution controls.
        </Result>
      </Stack>
    );
  }

  const currency = activeData.kpis.portfolio_value.currency || "USD";
  const charterRevision = activeData.charter_revision || meta?.charter_revision || "";
  const asOfLabel = `As of ${formatNsClock(activeData.as_of_ns)} · seq #${activeData.event_seq}`;
  const selectedBook = activeData.selected_book;
  const liveActive = isLive && activeData.governance.company_live !== null;
  const shadow = isLive ? activeData.shadow_benchmark : null;

  const canPromoteCommand =
    canWrite &&
    Boolean(
      meta?.supported_commands?.promote_execution_environment?.includes(
        "execution-tier-command-v1"
      )
    );
  const canDemoteCommand =
    canWrite &&
    Boolean(
      meta?.supported_commands?.demote_live_environment?.includes("execution-tier-command-v1")
    );
  const supportsOpenPaperBook = Boolean(
    meta?.supported_commands?.open_company_paper_book?.includes("execution-tier-command-v1")
  );
  const supportsReconcile = Boolean(
    meta?.supported_commands?.reconcile_trading_books?.includes("execution-tier-command-v1")
  );
  const canOpenPaperBook = canWrite && supportsOpenPaperBook && Boolean(charterRevision);
  const canReconcileCommand = canWrite && supportsReconcile && Boolean(charterRevision);
  const isReconciling =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    outbox.intent?.command_type === "reconcile_trading_books";
  const isOpeningBook =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    outbox.intent?.command_type === "open_company_paper_book";

  const trimmedBookName = newBookName.trim();
  const trimmedAccountId = newAccountId.trim();
  const parsedStartingUsd = Number(newStartingUsd);
  const parsedExposureUsd = Number(newExposureUsd);
  const parsedFeeBps = Number.parseInt(newFeeBps, 10);
  const parsedSlippageBps = Number.parseInt(newSlippageBps, 10);

  const accountIdError =
    trimmedAccountId && !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(trimmedAccountId)
      ? "Use letters, digits, '.', '_', ':', or '-'."
      : undefined;
  const startingError =
    newStartingUsd && (!Number.isFinite(parsedStartingUsd) || parsedStartingUsd <= 0)
      ? "Enter a positive dollar amount."
      : undefined;
  const exposureError =
    newExposureUsd &&
    (!Number.isFinite(parsedExposureUsd) ||
      parsedExposureUsd <= 0 ||
      (Number.isFinite(parsedStartingUsd) && parsedExposureUsd > parsedStartingUsd))
      ? "Exposure ceiling must be positive and cannot exceed starting capital."
      : undefined;
  const feeError =
    newFeeBps && (!Number.isInteger(parsedFeeBps) || parsedFeeBps < 0 || parsedFeeBps > 1000)
      ? "0 to 1000 bps."
      : undefined;
  const slippageError =
    newSlippageBps &&
    (!Number.isInteger(parsedSlippageBps) || parsedSlippageBps < 0 || parsedSlippageBps > 500)
      ? "0 to 500 bps."
      : undefined;
  const canSubmitNewBook =
    canOpenPaperBook &&
    !isOpeningBook &&
    trimmedBookName.length > 0 &&
    trimmedAccountId.length > 0 &&
    !accountIdError &&
    !startingError &&
    !exposureError &&
    !feeError &&
    !slippageError &&
    Number.isFinite(parsedStartingUsd) &&
    parsedStartingUsd > 0 &&
    Number.isFinite(parsedExposureUsd) &&
    parsedExposureUsd > 0 &&
    parsedExposureUsd <= parsedStartingUsd;

  const handleBookNameChange = (value: string) => {
    setNewBookName(value);
    if (!newAccountTouched) {
      const slug = value
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 40);
      setNewAccountId(slug ? `paper-${slug}` : "");
    }
  };

  const handleCreatePaperBook = async () => {
    if (!meta || !canSubmitNewBook) return;
    const startingMinor = Math.round(parsedStartingUsd * 100);
    const exposureMinor = Math.round(parsedExposureUsd * 100);
    const lifecycle = await dispatchCommand({
      company_id: meta.company_id,
      command_id: newCommandId("open-paper-book"),
      command_type: "open_company_paper_book",
      contract_version: "execution-tier-command-v1",
      preconditions: { charter_revision: charterRevision },
      payload: {
        account_id: trimmedAccountId,
        book_name: trimmedBookName,
        starting_minor_units: startingMinor,
        exposure_limit_minor_units: exposureMinor,
        currency,
        strategy_release_id: newStrategyRelease.trim() || null,
        fee_bps: Number.isInteger(parsedFeeBps) ? parsedFeeBps : 4,
        slippage_bps: Number.isInteger(parsedSlippageBps) ? parsedSlippageBps : 2,
        reason: `Board opened paper book '${trimmedBookName}'`,
      },
      label: `Open paper book ${trimmedBookName}`,
      sent_ns: Date.now() * 1_000_000,
    });
    if (lifecycle === "applied") {
      pushToast(`Opened paper book "${trimmedBookName}".`);
      setNewBookOpen(false);
      setNewBookName("");
      setNewAccountId("");
      setNewAccountTouched(false);
      setNewStrategyRelease("");
    }
  };

  const handleReconcileNow = async () => {
    if (!meta || !canReconcileCommand || isReconciling) return;
    const lifecycle = await dispatchCommand({
      company_id: meta.company_id,
      command_id: newCommandId("reconcile-desk"),
      command_type: "reconcile_trading_books",
      contract_version: "execution-tier-command-v1",
      preconditions: { charter_revision: charterRevision },
      payload: {
        max_quote_age_seconds: 60,
        reason: `Board on-demand reconcile (${isLive ? "live" : "paper"} desk)`,
      },
      label: "Sync quotes & reconcile trading books",
      sent_ns: Date.now() * 1_000_000,
    });
    if (lifecycle === "applied") {
      pushToast("Synced market quotes and reconciled active trading books.");
    }
  };

  const activeLiveMigration =
    isLive && activeData.governance.company_live
      ? activeData.governance.migration_history.find(
          (m) => m.target_live_binding_id === activeData.governance.company_live?.binding_id
        ) ?? null
      : null;

  const port = activeData.kpis.portfolio_value;
  const pnlKpi = activeData.kpis.trading_pnl;
  const opexKpi = activeData.kpis.agent_llm_opex;
  const netKpi = activeData.kpis.company_net_pnl;

  // Exposure Cap Meter (identical in Live & Paper!)
  const exposureLimitMajor =
    activeData.exposure_cap.limit_minor_units != null
      ? Number((activeData.exposure_cap.limit_minor_units / 100).toFixed(2))
      : null;
  const exposureCategories = activeData.open_positions
    .map((pos) => {
      const notionalMajor = Number((pos.notional_minor_units / 100).toFixed(2));
      return {
        id: pos.instrument_id,
        label: pos.instrument_id,
        amount: notionalMajor,
        detail: `${formatWholeCurrency(notionalMajor, currency)} · ${pos.quantity} qty @ mark ${formatMinorCurrency(pos.mark_price_minor_units, currency)}`,
      };
    })
    .filter((c) => Number.isFinite(c.amount) && c.amount > 0);
  const exposureUsedMajor = Number(
    (activeData.exposure_cap.used_minor_units / 100).toFixed(2)
  );
  const isOverCap =
    exposureLimitMajor !== null &&
    exposureLimitMajor > 0 &&
    exposureUsedMajor > exposureLimitMajor;
  const exposurePct =
    exposureLimitMajor && exposureLimitMajor > 0
      ? Math.round((exposureUsedMajor / exposureLimitMajor) * 100)
      : 0;

  // Two-Layer P&L Reconciliation Facts
  const reconciliationFacts: Fact[] = activeData.pnl_reconciliation_bridge.map((step) => ({
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

  // Company Paper Books interactive switcher table (Paper Trading mode)
  const bookCols: DataColumn[] = [
    { id: "book", label: "Paper Book & Strategy", kind: "identity" },
    { id: "role", label: "Status", kind: "role" },
    { id: "nav", label: "NAV & Return", kind: "numeric", align: "end" },
    { id: "pnl", label: "Total P&L", kind: "numeric", align: "end" },
    { id: "exposure", label: "Exposure / Cap", kind: "numeric", align: "end" },
    { id: "soak", label: "Fills & Soak", kind: "numeric", align: "end" },
    { id: "action", label: "Desk View", kind: "actions", align: "end" },
  ];

  const bookRows: DataRow[] = activeData.books.map((book: TradingDeskBookSummary) => {
    const isSelected = selectedBook?.binding_id === book.binding_id;
    const roleMeta = roleBadgeProps(book.role);
    return {
      id: book.binding_id,
      cells: {
        book: (
          <Stack gap={2}>
            <Inline gap={6} align="center" wrap>
              <Text variant="body">{book.book_name}</Text>
              {isSelected ? (
                <Badge tone="brand" variant="soft" dot={false}>
                  Inspecting
                </Badge>
              ) : null}
            </Inline>
            <Text variant="meta" className="sd-mono">
              {book.account_id}
              {book.strategy_release_id ? ` · ${book.strategy_release_id}` : ""}
            </Text>
          </Stack>
        ),
        role: (
          <Badge tone={roleMeta.tone} variant="soft" dot={false}>
            {roleMeta.label}
          </Badge>
        ),
        nav: (
          <Stack gap={2}>
            <Text className="sd-tabular">
              {formatMinorCurrency(book.nav_minor_units, book.currency)}
            </Text>
            <Text
              variant="meta"
              className={`sd-tabular ${
                book.return_pct > 0
                  ? "sd-tone-pos"
                  : book.return_pct < 0
                    ? "sd-tone-neg"
                    : "sd-tone-muted"
              }`}
            >
              {formatSignedPct(book.return_pct)}
            </Text>
          </Stack>
        ),
        pnl: (
          <Text
            className={`sd-tabular ${
              book.pnl_minor_units > 0
                ? "sd-tone-pos"
                : book.pnl_minor_units < 0
                  ? "sd-tone-neg"
                  : "sd-tone-muted"
            }`}
          >
            {formatMinorCurrency(book.pnl_minor_units, book.currency, { signed: true })}
          </Text>
        ),
        exposure: (
          <Text className="sd-tabular">
            {formatMinorCurrency(book.gross_exposure_minor_units, book.currency)} /{" "}
            {formatMinorCurrency(book.exposure_limit_minor_units, book.currency)}
          </Text>
        ),
        soak: (
          <Inline gap={6} align="center" justify="end">
            <Text variant="meta" className="sd-tabular">
              {book.fill_count} fills
            </Text>
            <Badge
              tone={book.soak_ready ? "success" : "warning"}
              variant="outline"
              dot={false}
            >
              {book.soak_ready ? "Soak Ready" : "Soak Pending"}
            </Badge>
          </Inline>
        ),
        action: (
          <Button
            emphasis={isSelected ? "filled" : "soft"}
            tone={isSelected ? "brand" : "neutral"}
            size="compact"
            shape="pill"
            onClick={() => setSelectedBookId(book.binding_id)}
          >
            {isSelected ? "Selected" : "Inspect"}
          </Button>
        ),
      },
    };
  });

  // Open Positions Table (Identical in Live & Paper!)
  const positionCols: DataColumn[] = [
    { id: "instrument", label: "Instrument", kind: "identity" },
    { id: "qty", label: "Position Qty", kind: "numeric", align: "end" },
    { id: "entry", label: "Avg Entry", kind: "numeric", align: "end" },
    { id: "mark", label: "Mark Price", kind: "numeric", align: "end" },
    { id: "notional", label: "Notional", kind: "numeric", align: "end" },
    { id: "unrealized", label: "Unrealized P&L", kind: "numeric", align: "end" },
    { id: "realized", label: "Realized P&L", kind: "numeric", align: "end" },
    { id: "fees", label: "Fees", kind: "numeric", align: "end" },
  ];

  const positionRows: DataRow[] = activeData.open_positions.map((pos) => ({
    id: pos.instrument_id,
    cells: {
      instrument: <Text className="sd-mono">{pos.instrument_id}</Text>,
      qty: (
        <Text className="sd-tabular">
          {pos.quantity > 0 ? `+${pos.quantity}` : String(pos.quantity)}
        </Text>
      ),
      entry: (
        <Text className="sd-tabular">
          {formatMinorCurrency(pos.avg_entry_minor_units, currency)}
        </Text>
      ),
      mark: (
        <Text className="sd-tabular">
          {formatMinorCurrency(pos.mark_price_minor_units, currency)}
        </Text>
      ),
      notional: (
        <Text className="sd-tabular">
          {formatMinorCurrency(pos.notional_minor_units, currency)}
        </Text>
      ),
      unrealized: (
        <Text
          className={`sd-tabular ${
            pos.unrealized_pnl_minor_units > 0
              ? "sd-tone-pos"
              : pos.unrealized_pnl_minor_units < 0
                ? "sd-tone-neg"
                : "sd-tone-muted"
          }`}
        >
          {formatMinorCurrency(pos.unrealized_pnl_minor_units, currency, { signed: true })}
        </Text>
      ),
      realized: (
        <Text
          className={`sd-tabular ${
            pos.realized_pnl_minor_units > 0
              ? "sd-tone-pos"
              : pos.realized_pnl_minor_units < 0
                ? "sd-tone-neg"
                : "sd-tone-muted"
          }`}
        >
          {formatMinorCurrency(pos.realized_pnl_minor_units, currency, { signed: true })}
        </Text>
      ),
      fees: (
        <Text variant="meta" className="sd-tabular">
          {formatMinorCurrency(pos.total_fees_minor_units, currency)}
        </Text>
      ),
    },
  }));

  // Execution Tape Table (Identical structure, adds External ID in Live mode!)
  const tapeCols: DataColumn[] = [
    { id: "order", label: isLive ? "Order / Venue ID" : "Order ID", kind: "identity" },
    { id: "instrument", label: "Instrument", kind: "text" },
    { id: "side", label: "Side & Qty", kind: "numeric", align: "end" },
    { id: "price", label: "Fill / Limit", kind: "numeric", align: "end" },
    { id: "fee", label: "Fee", kind: "numeric", align: "end" },
    { id: "actor", label: "Actor", kind: "text" },
    { id: "status", label: "Status", kind: "role" },
    { id: "recorded", label: "Recorded", kind: "numeric", align: "end" },
  ];

  const tapeRows: DataRow[] = filteredTape.map((o) => ({
    id: o.order_id,
    cells: {
      order: (
        <Stack gap={2}>
          <Link
            href={`/orders/${encodeURIComponent(o.order_id)}?env=${mode}`}
            className="sd-tape-order-link sd-mono"
            data-focus-id={`order-${o.order_id}`}
            onClick={(e) => {
              e.preventDefault();
              persistOrderFocusReturn(o.order_id, `order-${o.order_id}`);
              navigate(`/orders/${encodeURIComponent(o.order_id)}?env=${mode}`, {
                focusReturnId: `order-${o.order_id}`,
              });
            }}
          >
            {o.order_id}
          </Link>
          {o.external_order_id ? (
            <Text variant="meta" className="sd-mono">
              {o.external_order_id}
            </Text>
          ) : null}
        </Stack>
      ),
      instrument: <Text className="sd-mono">{o.instrument_id}</Text>,
      side: (
        <Inline gap={6} align="center" justify="end">
          <Badge
            tone={o.side === "buy" ? "success" : "warning"}
            variant="soft"
            dot={false}
          >
            {o.side.toUpperCase()}
          </Badge>
          <Text className="sd-tabular">
            {o.filled_quantity}/{o.quantity}
          </Text>
        </Inline>
      ),
      price: (
        <Text className="sd-tabular">
          {o.fill_price_minor_units !== null
            ? formatMinorCurrency(o.fill_price_minor_units, currency)
            : formatMinorCurrency(o.limit_minor_units, currency)}
        </Text>
      ),
      fee: (
        <Text variant="meta" className="sd-tabular">
          {formatMinorCurrency(o.fee_minor_units, currency)}
        </Text>
      ),
      actor: (
        <Stack gap={2}>
          <Text variant="body">{o.actor_name}</Text>
          <Text variant="meta">{o.actor_role}</Text>
        </Stack>
      ),
      status: (
        <Badge
          tone={
            o.status === "filled"
              ? "success"
              : o.status === "open"
                ? "brand"
                : "neutral"
          }
          variant="soft"
          dot={false}
        >
          {o.status}
        </Badge>
      ),
      recorded: (
        <Text variant="meta" className="sd-tabular">
          {formatTs(o.created_at_ns)}
        </Text>
      ),
    },
  }));

  // Live-only Qualification Receipts table
  const qualCols: DataColumn[] = [
    { id: "receipt", label: "Receipt ID", kind: "identity" },
    { id: "adapter", label: "Adapter & Destination", kind: "text" },
    { id: "verdict", label: "Verdict", kind: "role" },
    { id: "cases", label: "Cases (Pass / Fail / Unrun)", kind: "numeric", align: "end" },
    { id: "core", label: "Core Digest", kind: "text" },
    { id: "recorded", label: "Recorded", kind: "numeric", align: "end" },
  ];

  const qualRows: DataRow[] = activeData.governance.qualification_receipts.map((q) => {
    const coreMatches = q.core_digest === activeData.governance.current_core_digest;
    const isPass = q.verdict === "PASS" && q.cases_failed === 0 && q.cases_unrun === 0;
    return {
      id: q.receipt_id,
      cells: {
        receipt: <Text className="sd-mono">{q.receipt_id}</Text>,
        adapter: (
          <Stack gap={2}>
            <Text variant="body">{q.adapter_revision}</Text>
            <Text variant="meta">
              {q.environment} · {q.destination_id}
            </Text>
          </Stack>
        ),
        verdict: (
          <Badge tone={isPass ? "success" : "danger"} dot={false}>
            {q.verdict}
          </Badge>
        ),
        cases: (
          <Text className="sd-tabular">
            {q.cases_passed} / {q.cases_failed} / {q.cases_unrun}
          </Text>
        ),
        core: (
          <Inline gap={6} align="center">
            <Badge tone={coreMatches ? "success" : "warning"} variant="soft" dot={false}>
              {coreMatches ? "Current Core" : "Stale Core"}
            </Badge>
            <Text variant="meta" className="sd-mono">
              {formatShortHash(q.core_digest)}
            </Text>
          </Inline>
        ),
        recorded: (
          <Text variant="meta" className="sd-tabular">
            {formatTs(q.recorded_at_ns)}
          </Text>
        ),
      },
    };
  });

  const renderCandidateRow = (cand: LiveCandidateBindingItem, idx: number) => {
    const hasQual = Boolean(cand.matching_qualification_receipt_id && cand.promotion_payload_hash);
    const allGatesGreen =
      activeData.governance.paper_soak_ready &&
      hasQual &&
      !liveActive &&
      canPromoteCommand;
    const isConfirmOpen = promoteTargetId === cand.binding_id;
    const isSubmitting =
      (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
      outbox.intent?.command_type === "promote_execution_environment" &&
      outbox.intent?.preconditions?.target_binding_id === cand.binding_id;

    const prereqFacts: Fact[] = [
      {
        id: "soak",
        label: "Paper Soak Readiness",
        value: (
          <Badge
            tone={activeData.governance.paper_soak_ready ? "success" : "warning"}
            dot={false}
          >
            {activeData.governance.paper_soak_ready
              ? "Verified"
              : "Pending paper fills"}
          </Badge>
        ),
      },
      {
        id: "qual",
        label: "Venue Qualification",
        value: hasQual ? (
          <Badge tone="success" dot={false}>
            {cand.matching_qualification_receipt_id}
          </Badge>
        ) : (
          <Badge tone="warning" dot={false}>
            No PASS receipt for current core
          </Badge>
        ),
      },
      {
        id: "cred",
        label: "Host Credential Handle",
        value: <Text className="sd-mono">{cand.credential_handle_ref ?? "Unbound"}</Text>,
      },
      {
        id: "cap",
        label: "Exposure Ceiling",
        value: (
          <Text className="sd-tabular">
            {formatMinorCurrency(cand.exposure_limit_minor_units, cand.currency)}
          </Text>
        ),
      },
    ];

    return (
      <Fragment key={cand.binding_id}>
        {idx > 0 ? <Divider /> : null}
        <Stack gap={12}>
          <Inline justify="between" align="center" gap={8} wrap>
            <Stack gap={2}>
              <Inline gap={8} align="center" wrap>
                <Text variant="body">{cand.account_id}</Text>
                <Badge tone="neutral" variant="outline" dot={false}>
                  {cand.status}
                </Badge>
                <Text variant="meta" className="sd-mono">
                  {cand.binding_id}
                </Text>
              </Inline>
              <Text variant="help">
                {cand.connector_revision} · {cand.destination_id} · Rules{" "}
                {cand.instrument_rules_digest}
              </Text>
            </Stack>
            <Button
              emphasis={allGatesGreen ? "filled" : "soft"}
              tone={allGatesGreen ? "brand" : "neutral"}
              size="compact"
              shape="pill"
              onClick={() =>
                setPromoteTargetId((cur) => (cur === cand.binding_id ? null : cand.binding_id))
              }
            >
              {isConfirmOpen ? "Close Promotion Gate" : "Review Live Promotion"}
            </Button>
          </Inline>

          <div className="sd-facts-wrap">
            <RecordFacts
              source="execution_environment_binding"
              asOf={`seq #${activeData.event_seq}`}
              facts={prereqFacts}
            />
          </div>

          {meta ? (
            <ControlledInlineConfirm
              open={isConfirmOpen}
              surface="inline"
              pending={isSubmitting}
              onCancel={() => setPromoteTargetId(null)}
              decision={{
                id: `promote-${cand.binding_id}`,
                revision: `${cand.binding_id}:${cand.matching_qualification_receipt_id ?? "none"}:${cand.promotion_payload_hash ?? "none"}`,
                title: `Promote ${cand.account_id} to Live Trading`,
                consequence:
                  "Activates real-capital external venue execution under this binding's exposure ceiling and host credential handle. Starts from a clean 0% live baseline with 0 virtual positions copied.",
                confirmLabel: "Confirm Promote to Live",
                destructive: false,
                available: allGatesGreen,
                unavailableNotice: !allGatesGreen ? (
                  <Notice title="Live promotion gate locked" tone="warning">
                    {liveActive
                      ? "Another live venue binding is already active. Demote the current live binding before promoting a replacement."
                      : !activeData.governance.paper_soak_ready
                        ? "Company paper trading must record fills or valuations before promoting to live."
                        : !hasQual
                          ? "A passing venue qualification receipt matching this connector revision, instrument rules, and current trading core digest is required."
                          : "Board Control authority is required to activate live trading."}
                  </Notice>
                ) : undefined,
                requireReason: true,
                defaultReason: `Board approved live promotion for ${cand.account_id}`,
                whatChanges: [
                  {
                    label: "Active Live Venue",
                    value: `${cand.account_id} (${cand.destination_id}) transitions to active`,
                  },
                  {
                    label: "Exposure Cap",
                    value: formatMinorCurrency(cand.exposure_limit_minor_units, cand.currency),
                  },
                ],
                whatStaysUntouched: [
                  {
                    label: "Paper Books & Agent Sandboxes",
                    value: "Simulated accounts remain strictly isolated from real funds",
                  },
                  {
                    label: "Host API Secrets",
                    value: `Resolved only in memory via ${cand.credential_handle_ref ?? "host handle"}`,
                  },
                ],
                technicalDetails: [
                  { label: "command_type", value: "promote_execution_environment" },
                  { label: "target_binding_id", value: cand.binding_id },
                  {
                    label: "qualification_receipt_id",
                    value: cand.matching_qualification_receipt_id ?? "Unavailable",
                  },
                  {
                    label: "expected_payload_hash",
                    value: cand.promotion_payload_hash ?? "Unavailable",
                  },
                  { label: "charter_revision", value: charterRevision || "Unavailable" },
                ],
              }}
              onConfirm={async ({ reason }) => {
                if (!cand.matching_qualification_receipt_id || !cand.promotion_payload_hash) {
                  return;
                }
                const lifecycle = await dispatchCommand({
                  company_id: meta.company_id,
                  command_id: newCommandId("promote-live"),
                  command_type: "promote_execution_environment",
                  contract_version: "execution-tier-command-v1",
                  preconditions: {
                    charter_revision: charterRevision,
                    target_binding_id: cand.binding_id,
                    qualification_receipt_id: cand.matching_qualification_receipt_id,
                    expected_payload_hash: cand.promotion_payload_hash,
                  },
                  payload: { reason },
                  label: `Promote ${cand.account_id} to live`,
                  sent_ns: Date.now() * 1_000_000,
                });
                if (lifecycle === "applied") {
                  setPromoteTargetId(null);
                  pushToast(`Promoted ${cand.account_id} to Live Trading.`);
                }
              }}
            />
          ) : null}
        </Stack>
      </Fragment>
    );
  };

  return (
    <Stack gap={16}>
      <PageHeader
        title={isLive ? "Live Trading" : "Paper Trading"}
        detail={
          isLive
            ? `External venue production capital · Charter ${charterRevision || "unknown"} · ${asOfLabel}`
            : `Simulated company books · Charter ${charterRevision || "unknown"} · ${asOfLabel}`
        }
        action={
          <Inline gap={8} align="center" wrap>
            {isLive ? (
              <Status tone={liveActive ? "brand" : "success"}>
                {liveActive ? "Live Active" : "Paper Standby"}
              </Status>
            ) : (
              <Status tone={activeData.risk_gate.suspended ? "warning" : "success"}>
                {activeData.risk_gate.suspended ? "New Orders Paused" : "Risk Gate Open"}
              </Status>
            )}
            {supportsReconcile ? (
              <Button
                emphasis="soft"
                size="compact"
                shape="pill"
                disabled={!canReconcileCommand || isReconciling}
                onClick={() => void handleReconcileNow()}
              >
                {isReconciling ? "Syncing…" : "Sync Quotes"}
              </Button>
            ) : null}
            <Button
              emphasis="soft"
              size="compact"
              shape="pill"
              onClick={() => navigate(isLive ? "/trading/paper" : "/trading/live")}
            >
              {isLive ? "Paper Trading" : "Live Trading"}
            </Button>
            <Button
              emphasis="quiet"
              size="compact"
              shape="pill"
              onClick={() => navigate("/trading/strategies")}
            >
              Strategies
            </Button>
          </Inline>
        }
      />

      <ContextualApprovalList questions={promotionApprovals} />

      {!isLive && newBookOpen ? (
        <Card
          title="Open New Company Paper Book"
          description="Allocate an isolated simulated capital pool with independent positions, exposure ceiling, and equity curve"
          variant="raised"
        >
          <Stack gap={12}>
            <div className="sd-split-equal">
              <TextField
                label="Book Name"
                value={newBookName}
                onChange={(e) => handleBookNameChange(e.target.value)}
                placeholder="ETH Mean Reversion"
              />
              <TextField
                label="Account ID"
                value={newAccountId}
                onChange={(e) => {
                  setNewAccountTouched(true);
                  setNewAccountId(e.target.value);
                }}
                placeholder="paper-eth-mean-reversion"
                error={accountIdError}
              />
            </div>
            <div className="sd-split-equal">
              <TextField
                label={`Starting Capital (${currency})`}
                value={newStartingUsd}
                onChange={(e) => setNewStartingUsd(e.target.value)}
                placeholder="100000"
                error={startingError}
              />
              <TextField
                label={`Exposure Ceiling (${currency})`}
                value={newExposureUsd}
                onChange={(e) => setNewExposureUsd(e.target.value)}
                placeholder="50000"
                error={exposureError}
              />
            </div>
            <div className="sd-split-equal">
              <TextField
                label="Strategy Release ID (optional)"
                value={newStrategyRelease}
                onChange={(e) => setNewStrategyRelease(e.target.value)}
                placeholder="rel-eth-mr-v1"
              />
              <div className="sd-split-equal">
                <TextField
                  label="Fee (bps)"
                  value={newFeeBps}
                  onChange={(e) => setNewFeeBps(e.target.value)}
                  placeholder="4"
                  error={feeError}
                />
                <TextField
                  label="Slippage (bps)"
                  value={newSlippageBps}
                  onChange={(e) => setNewSlippageBps(e.target.value)}
                  placeholder="2"
                  error={slippageError}
                />
              </div>
            </div>
            <Inline justify="end" gap={8}>
              <Button
                emphasis="quiet"
                size="compact"
                shape="pill"
                onClick={() => setNewBookOpen(false)}
              >
                Cancel
              </Button>
              <Button
                emphasis="filled"
                tone="brand"
                size="compact"
                shape="pill"
                disabled={!canSubmitNewBook}
                onClick={() => void handleCreatePaperBook()}
              >
                {isOpeningBook ? "Creating…" : "Create Paper Book"}
              </Button>
            </Inline>
          </Stack>
        </Card>
      ) : null}

      {!isLive ? (
        <Card
          title="Company Paper Books"
          description="Select any concurrent paper book to inspect its live equity curve, reconciliation bridge, positions, and execution tape"
          variant="raised"
          className="sd-ledger-card"
          headerAction={
            <Inline gap={6} align="center" wrap>
              <Badge tone="neutral" variant="soft" dot={false}>
                {activeData.books.length}{" "}
                {activeData.books.length === 1 ? "book" : "books"}
              </Badge>
              <Badge tone="success" variant="soft" dot={false}>
                {activeData.governance.soak_ready_books_count} soak ready
              </Badge>
              {supportsOpenPaperBook ? (
                <Button
                  emphasis={newBookOpen ? "quiet" : "soft"}
                  size="compact"
                  shape="pill"
                  disabled={!canOpenPaperBook}
                  onClick={() => setNewBookOpen((open) => !open)}
                >
                  {newBookOpen ? "Close New Book" : "New Paper Book"}
                </Button>
              ) : null}
            </Inline>
          }
        >
          {activeData.books.length > 0 ? (
            <DataTable
              caption="Concurrent company paper books"
              columns={bookCols}
              rows={bookRows}
            />
          ) : (
            <Notice title="No active company paper books" tone="neutral">
              Open a paper book above or run{" "}
              <span className="sd-mono">sourdough open-paper-book</span> to start
              concurrent simulated trading.
            </Notice>
          )}
        </Card>
      ) : null}

      {isLive && activeData.governance.company_live ? (
        <Card
          title={`Active Live Venue · ${activeData.governance.company_live.account_id}`}
          description={`${activeData.governance.company_live.destination_id} · ${activeData.governance.company_live.connector_revision} · Credential ${activeData.governance.company_live.credential_handle_ref ?? "host-managed"}`}
          variant="raised"
          density="compact"
          headerAction={
            <Inline gap={8} align="center" wrap>
              {shadow ? (
                <Badge tone="success" variant="soft" dot={false}>
                  Shadow Benchmark: {shadow.book_name}
                </Badge>
              ) : null}
              <Button
                emphasis="soft"
                tone="danger"
                size="compact"
                shape="pill"
                disabled={!canDemoteCommand}
                onClick={() =>
                  setDemoteAction((cur) =>
                    cur === "demote_to_paper" ? null : "demote_to_paper"
                  )
                }
              >
                Demote to Paper
              </Button>
              <Button
                emphasis="quiet"
                tone="danger"
                size="compact"
                shape="pill"
                disabled={!canDemoteCommand}
                onClick={() =>
                  setDemoteAction((cur) =>
                    cur === "suspend_live" ? null : "suspend_live"
                  )
                }
              >
                Suspend Live
              </Button>
            </Inline>
          }
        >
          <Stack gap={8}>
            {activeLiveMigration ? (
              <Inline gap={8} align="center" wrap>
                <Text variant="help">
                  Migrated from {activeLiveMigration.source_book_name} (
                  {activeLiveMigration.migration_mode} · 0 virtual positions copied)
                </Text>
                {shadow ? (
                  <Badge
                    tone={Math.abs(shadow.execution_drift_pp) <= 0.25 ? "success" : "warning"}
                    variant="outline"
                    dot={false}
                  >
                    Live vs Shadow Drift {shadow.execution_drift_pp > 0 ? "+" : ""}
                    {shadow.execution_drift_pp.toFixed(2)} pp
                  </Badge>
                ) : null}
              </Inline>
            ) : null}

            {meta && demoteAction !== null ? (
              <ControlledInlineConfirm
                open
                surface="inline"
                pending={
                  (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
                  outbox.intent?.command_type === "demote_live_environment"
                }
                onCancel={() => setDemoteAction(null)}
                decision={{
                  id: `demote-${activeData.governance.company_live.binding_id}-${demoteAction}`,
                  revision: `${activeData.governance.company_live.binding_id}:${demoteAction}`,
                  title:
                    demoteAction === "demote_to_paper"
                      ? `Demote ${activeData.governance.company_live.account_id} back to Paper Mode`
                      : `Emergency Suspend ${activeData.governance.company_live.account_id}`,
                  consequence:
                    demoteAction === "demote_to_paper"
                      ? "Returns the live venue binding to registered standby and halts external real-capital execution while keeping Paper Trading running."
                      : "Immediately suspends live venue execution and records an operator safety hold.",
                  confirmLabel:
                    demoteAction === "demote_to_paper"
                      ? "Confirm Demote to Paper"
                      : "Confirm Emergency Suspend",
                  destructive: true,
                  available: canDemoteCommand,
                  requireReason: true,
                  defaultReason:
                    demoteAction === "demote_to_paper"
                      ? "Board returning execution to paper standby mode"
                      : "Emergency Board suspension of live venue execution",
                  whatChanges: [
                    {
                      label: "Live Venue Status",
                      value:
                        demoteAction === "demote_to_paper"
                          ? "active → registered (paper standby)"
                          : "active → suspended (safety hold)",
                    },
                  ],
                  whatStaysUntouched: [
                    {
                      label: "Paper Books & Audit History",
                      value: "Company paper books and historical live fills remain intact",
                    },
                  ],
                  technicalDetails: [
                    { label: "command_type", value: "demote_live_environment" },
                    {
                      label: "target_binding_id",
                      value: activeData.governance.company_live.binding_id,
                    },
                    { label: "action", value: demoteAction },
                    { label: "charter_revision", value: charterRevision || "Unavailable" },
                  ],
                }}
                onConfirm={async ({ reason }) => {
                  const targetLive = activeData.governance.company_live;
                  if (!targetLive) return;
                  const lifecycle = await dispatchCommand({
                    company_id: meta.company_id,
                    command_id: newCommandId("demote-live"),
                    command_type: "demote_live_environment",
                    contract_version: "execution-tier-command-v1",
                    preconditions: {
                      charter_revision: charterRevision,
                      target_binding_id: targetLive.binding_id,
                    },
                    payload: {
                      action: demoteAction,
                      reason,
                    },
                    label:
                      demoteAction === "demote_to_paper"
                        ? `Demote ${targetLive.account_id} to paper`
                        : `Suspend ${targetLive.account_id}`,
                    sent_ns: Date.now() * 1_000_000,
                  });
                  if (lifecycle === "applied") {
                    setDemoteAction(null);
                    pushToast(
                      demoteAction === "demote_to_paper"
                        ? "Returned execution to Paper Standby Mode."
                        : "Suspended live venue execution."
                    );
                  }
                }}
              />
            ) : null}
          </Stack>
        </Card>
      ) : null}

      {/* 4 Flagship KPI Cards — 100% Symmetric across Live Trading & Paper Trading */}
      <div className="sd-kpi-grid">
        <Card variant="raised" density="compact" className="sd-kpi-card">
          <Inline justify="between" align="center">
            <Text variant="meta">
              {isLive ? "Live Portfolio Value" : "Paper Portfolio Value"}
            </Text>
            <Badge
              tone={isLive ? (liveActive ? "brand" : "neutral") : "neutral"}
              variant="outline"
              dot={false}
            >
              {isLive ? (liveActive ? "Live" : "Standby") : "Paper"}
            </Badge>
          </Inline>
          <Text className="sd-kpi-value sd-tabular">
            {port.value_minor !== null
              ? formatMinorCurrency(port.value_minor, currency)
              : "Standby"}
          </Text>
          <div className="sd-kpi-mid">
            <MiniSparkline
              points={activeData.cumulative_return_series}
              label={isLive ? "0% live baseline" : "Sealed valuation series"}
            />
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
              {port.return_pct === null ? "0.00%" : formatSignedPct(port.return_pct)}
            </Text>
            <Text variant="meta">{port.source}</Text>
          </Inline>
        </Card>

        <Card variant="raised" density="compact" className="sd-kpi-card">
          <Inline justify="between" align="center">
            <Text variant="meta">
              {isLive ? "Live Trading P&L" : "Paper Trading P&L"}
            </Text>
            <Badge tone="neutral" variant="soft" dot={false}>
              BOR
            </Badge>
          </Inline>
          <Text
            className={`sd-kpi-value sd-tabular ${
              pnlKpi.value_minor === null
                ? "sd-tone-muted"
                : pnlKpi.value_minor >= 0
                  ? "sd-tone-pos"
                  : "sd-tone-neg"
            }`}
          >
            {pnlKpi.value_minor !== null
              ? formatMinorCurrency(pnlKpi.value_minor, currency, { signed: true })
              : "Gated"}
          </Text>
          <div className="sd-kpi-mid">
            <Text variant="meta" className="sd-tabular sd-tone-muted">
              {pnlKpi.realized_minor !== null && pnlKpi.unrealized_minor !== null
                ? `Realized ${formatMinorCurrency(pnlKpi.realized_minor, currency, { signed: true })} · Unrl ${formatMinorCurrency(pnlKpi.unrealized_minor, currency, { signed: true })}`
                : "Awaiting live fills"}
            </Text>
          </div>
          <Inline justify="between" align="center">
            <Text variant="meta" className="sd-tabular">
              {pnlKpi.fees_minor !== null
                ? `Fees ${formatMinorCurrency(pnlKpi.fees_minor, currency)}`
                : "No fees"}
            </Text>
            <Text variant="meta">{pnlKpi.source}</Text>
          </Inline>
        </Card>

        <Card variant="raised" density="compact" className="sd-kpi-card">
          <Inline justify="between" align="center">
            <Text variant="meta">Agent LLM &amp; Compute Opex</Text>
            <Badge tone="neutral" variant="soft" dot={false}>
              GL
            </Badge>
          </Inline>
          <Text className="sd-kpi-value sd-tabular">
            {opexKpi.status === "ok"
              ? formatMinorCurrency(opexKpi.value_minor, currency)
              : "$0.00"}
          </Text>
          <div className="sd-kpi-mid">
            <Text variant="meta" className="sd-tone-muted">
              Shared company operating expense
            </Text>
          </div>
          <Inline justify="between" align="center">
            <Text variant="meta">{opexKpi.source}</Text>
            <Text variant="meta">{activeData.close_count} closes</Text>
          </Inline>
        </Card>

        <Card variant="raised" density="compact" className="sd-kpi-card">
          <Inline justify="between" align="center">
            <Text variant="meta">{isLive ? "Live Net P&L" : "Paper Net P&L"}</Text>
            <Badge
              tone={
                netKpi.value_minor === null
                  ? "neutral"
                  : netKpi.value_minor >= 0
                    ? "success"
                    : "danger"
              }
              variant="soft"
              dot={false}
            >
              {netKpi.value_minor === null ? "Standby" : "Reconciled"}
            </Badge>
          </Inline>
          <Text
            className={`sd-kpi-value sd-tabular ${
              netKpi.value_minor === null
                ? "sd-tone-muted"
                : netKpi.value_minor >= 0
                  ? "sd-tone-pos"
                  : "sd-tone-neg"
            }`}
          >
            {netKpi.value_minor !== null
              ? formatMinorCurrency(netKpi.value_minor, currency, { signed: true })
              : "Standby"}
          </Text>
          <div className="sd-kpi-mid">
            <Text variant="meta" className="sd-tone-muted">
              {selectedBook
                ? `Cash ${formatMinorCurrency(selectedBook.cash_minor_units, currency)}`
                : "No capital deployed"}
            </Text>
          </div>
          <Inline justify="between" align="center">
            <Text variant="meta">{netKpi.source}</Text>
            <Text variant="meta" className="sd-tabular">
              {selectedBook ? `${selectedBook.fill_count} fills` : "0 fills"}
            </Text>
          </Inline>
        </Card>
      </div>

      {/* Main Performance Chart (left) + Two-Layer Reconciliation & Exposure Cap (right) */}
      <div className="sd-two-col">
        <PortfolioChart
          points={activeData.cumulative_return_series}
          currency={currency}
          asOfLabel={asOfLabel}
          title={
            isLive
              ? "Live Cumulative Performance"
              : selectedBook
                ? `Paper Cumulative Performance · ${selectedBook.book_name}`
                : "Paper Cumulative Performance"
          }
          subtitlePct={
            isLive
              ? shadow
                ? `0% Live Launch baseline vs Shadow Paper Benchmark · ${asOfLabel}`
                : `0% baseline · Reconciled live venue returns · ${asOfLabel}`
              : `0% baseline · Service-projected paper returns · ${asOfLabel}`
          }
          subtitlePnl={
            isLive
              ? `$0 baseline · Reconciled live cumulative P&L · ${asOfLabel}`
              : `$0 baseline · Service-projected paper P&L · ${asOfLabel}`
          }
          navColumnLabel={isLive ? "Live Book NAV" : "Paper Book NAV"}
          primarySeriesLabel={
            isLive ? selectedBook?.account_id ?? "Live" : selectedBook?.book_name ?? "Paper"
          }
          benchmarkLabel={shadow ? `Shadow: ${shadow.book_name}` : undefined}
          benchmarkPoints={shadow?.series}
          benchmarkDriftPp={shadow?.execution_drift_pp ?? null}
          emptyMessage={
            isLive
              ? "Live trading is in Paper Standby (0% capital deployed). Review a recommended Paper Book or promote a qualified candidate venue below to start plotting live performance."
              : "No valuation cuts recorded for this paper book yet. Once fills or marks land, cumulative performance will plot here on a 0% baseline."
          }
          emptyAction={
            isLive ? (
              <Button
                emphasis="soft"
                size="compact"
                shape="pill"
                onClick={() => navigate("/trading/paper")}
              >
                Inspect Paper Books
              </Button>
            ) : undefined
          }
        />

        <Stack gap={16}>
          <Card
            title="Two-Layer P&L Reconciliation"
            description={
              isLive
                ? "Live venue fills & marks vs Company operating costs"
                : "Simulated paper book vs Company operating costs"
            }
            variant="raised"
          >
            {reconciliationFacts.length > 0 ? (
              <Stack gap={12}>
                <RecordFacts
                  source={
                    selectedBook
                      ? `${selectedBook.connector_revision} · ${selectedBook.account_id}`
                      : "trading_book + general_ledger"
                  }
                  asOf={`seq #${activeData.event_seq}`}
                  facts={reconciliationFacts}
                />
                {activeData.open_close_issues.length > 0 ? (
                  <Notice title="Open accounting close issue" tone="warning">
                    Company Net P&amp;L reflects open close discrepancies until settled.
                  </Notice>
                ) : null}
              </Stack>
            ) : (
              <Text variant="help">
                {isLive
                  ? "No live venue active yet. Reconciliation activates automatically once live execution starts."
                  : "Select or register a company paper book to view reconciliation."}
              </Text>
            )}
          </Card>

          <Card
            title={isLive ? "Live Exposure Cap Utilization" : "Paper Exposure Cap Utilization"}
            headerAction={
              exposureLimitMajor !== null && exposureLimitMajor > 0 ? (
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
              ) : (
                <Badge tone="neutral" variant="outline" dot={false}>
                  Standby
                </Badge>
              )
            }
            variant="raised"
            density="compact"
            className="sd-exposure-meter"
          >
            {exposureLimitMajor !== null && exposureLimitMajor > 0 ? (
              <Stack gap={8}>
                <StorageMeter
                  label={
                    isLive ? "Live Exposure Cap Utilization" : "Paper Exposure Cap Utilization"
                  }
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
                ) : (
                  <Text variant="meta">0% utilized · All positions flat</Text>
                )}
              </Stack>
            ) : (
              <Text variant="help">
                {isLive
                  ? `${activeData.governance.live_candidates.length} candidate venue(s) registered. Exposure cap enforces hard notional limits once activated.`
                  : "No active exposure limit bound."}
              </Text>
            )}
          </Card>
        </Stack>
      </div>

      {/* Open Positions — Identical across Live & Paper */}
      <Card
        title={isLive ? "Live Open Positions" : "Paper Open Positions"}
        description={
          selectedBook
            ? `Active inventory on ${selectedBook.book_name} (${selectedBook.account_id}) marked to latest observed prices`
            : isLive
              ? "No live positions — Paper Standby active"
              : "No paper book selected"
        }
        variant="raised"
        className="sd-ledger-card"
        headerAction={
          <Badge tone="neutral" variant="soft" dot={false}>
            {positionRows.length} {positionRows.length === 1 ? "position" : "positions"}
          </Badge>
        }
      >
        {positionRows.length === 0 ? (
          <Text variant="help">
            {isLive && !liveActive
              ? "No live positions open. Virtual paper positions are never carried over into live capital."
              : "No open positions — book is currently flat in cash."}
          </Text>
        ) : (
          <DataTable
            caption={isLive ? "Live open positions" : "Paper open positions"}
            columns={positionCols}
            rows={positionRows}
          />
        )}
      </Card>

      {/* Execution Tape — Identical across Live & Paper */}
      <Card
        title={isLive ? "Live Execution Tape" : "Paper Execution Tape"}
        description={
          selectedBook
            ? `Authoritative order and fill log for ${selectedBook.book_name}`
            : isLive
              ? "External venue fills reconcile here once live trading is activated"
              : "Simulated fills reconcile here as paper orders execute"
        }
        variant="raised"
        className="sd-ledger-card"
        headerAction={
          <Inline gap={8} align="center" wrap>
            <SegmentedControl
              label="Filter tape status"
              value={tapeStatus}
              onValueChange={(next) => setTapeStatus(next as "all" | "filled" | "open")}
              options={[
                { id: "all", label: "All" },
                { id: "filled", label: "Filled" },
                { id: "open", label: "Open" },
              ]}
            />
            <Badge tone="neutral" variant="soft" dot={false}>
              {tapeRows.length} {tapeRows.length === 1 ? "order" : "orders"}
            </Badge>
          </Inline>
        }
      >
        <Stack gap={12}>
          {activeData.execution_tape.length > 0 ? (
            <FilterBar
              query={tapeQuery}
              onQueryChange={setTapeQuery}
              placeholder={
                isLive
                  ? "Filter by order ID, venue ID, instrument, or actor..."
                  : "Filter by order ID, instrument, side, or actor..."
              }
            />
          ) : null}
          {tapeRows.length === 0 ? (
            <Text variant="help">
              {activeData.execution_tape.length === 0
                ? isLive
                  ? "No live orders submitted yet."
                  : "No paper orders recorded for this book yet."
                : "No orders match the current filter."}
            </Text>
          ) : (
            <DataTable
              caption={isLive ? "Live execution tape" : "Paper execution tape"}
              columns={tapeCols}
              rows={tapeRows}
            />
          )}
        </Stack>
      </Card>

      {/* Live-Only Governance Surface: Venue Candidates, Qualification Receipts, Migration & Promotion Ledgers */}
      {isLive ? (
        <Stack gap={16}>
          <Card
            title="Live Venue Candidates & Promotion Gate"
            description="Fail-closed prerequisites checked server-side before activating real-capital external execution"
            variant="raised"
          >
            {activeData.governance.live_candidates.length === 0 ? (
              <Text variant="help">
                No candidate live venue bindings registered yet. Register a live candidate with a
                host credential handle and run venue qualification first.
              </Text>
            ) : (
              <Stack gap={12}>
                {activeData.governance.live_candidates.map((cand, idx) =>
                  renderCandidateRow(cand, idx)
                )}
              </Stack>
            )}
          </Card>

          <Card
            title="Venue Qualification Receipts"
            description="Signed wire-harness verification receipts tied to connector revision, instrument rules, and trading core digest"
            variant="raised"
            className="sd-ledger-card"
            headerAction={
              <Badge tone="neutral" variant="soft" dot={false}>
                {qualRows.length} {qualRows.length === 1 ? "receipt" : "receipts"}
              </Badge>
            }
          >
            {qualRows.length === 0 ? (
              <Text variant="help">No venue qualification receipts recorded yet.</Text>
            ) : (
              <DataTable
                caption="Venue qualification receipts"
                columns={qualCols}
                rows={qualRows}
              />
            )}
          </Card>

          {activeData.governance.migration_history.length > 0 ? (
            <Card
              title="Paper-to-Live Migration Ledger"
              description="Immutable audit trail of Board-approved migrations from Company Paper Books to Live venues (always 0 virtual positions copied)"
              variant="raised"
            >
              <List label="Paper-to-Live migrations" divided>
                {activeData.governance.migration_history.map((mig) => (
                  <li key={mig.migration_id} className="yg-list-item">
                    <WorkEvent
                      event={{
                        id: mig.migration_id,
                        kind: "review",
                        title: `${mig.source_book_name} → ${mig.target_account_id} (${mig.migration_mode})`,
                        detail: `${mig.reason} · source ${mig.source_paper_disposition} · ${mig.positions_copied} virtual positions copied · receipt ${mig.qualification_receipt_id}`,
                        time: formatTs(mig.recorded_at_ns),
                        state: "done",
                      }}
                    />
                  </li>
                ))}
              </List>
            </Card>
          ) : null}

          <Card
            title="Promotion & Demotion Ledger"
            description="Immutable Board decisions for live environment activation, demotion, and emergency suspension"
            variant="raised"
          >
            {activeData.governance.promotion_history.length === 0 ? (
              <Text variant="help">
                No environment promotions or demotions recorded yet. The company remains in
                paper standby mode.
              </Text>
            ) : (
              <List label="Environment promotion ledger" divided>
                {activeData.governance.promotion_history.map((rec) => (
                  <li key={rec.promotion_id} className="yg-list-item">
                    <WorkEvent
                      event={{
                        id: rec.promotion_id,
                        kind: "review",
                        title: `${rec.action} · ${rec.to_binding_id}`,
                        detail: `${rec.reason}${rec.qualification_receipt_id ? ` · receipt ${rec.qualification_receipt_id}` : ""} · hash ${formatShortHash(rec.payload_hash)}`,
                        time: formatTs(rec.recorded_at_ns),
                        state: rec.action === "promote_live" ? "done" : "failed",
                      }}
                    />
                  </li>
                ))}
              </List>
            )}
          </Card>
        </Stack>
      ) : null}
    </Stack>
  );
}
