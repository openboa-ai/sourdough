import type { ReactNode } from "react";
import {
  Badge,
  Button,
  Card,
  DataTable,
  Inline,
  Notice,
  PageHeader,
  Stack,
  Status,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type {
  ReturnPoint,
  TradingDeskBookSummary,
  TradingDeskProjection,
} from "../../../api/types";
import { formatMinorCurrency, formatSignedPct, newCommandId } from "../../../lib/format";
import {
  useControlPlane,
  useOutbox,
  usePreferences,
  useRouter,
} from "../../../state/store";

export interface BookOverviewStripProps {
  mode: "live" | "paper";
  data: TradingDeskProjection;
  currency: string;
  charterRevision: string;
  asOfLabel: string;
  selectedBook: TradingDeskBookSummary | null;
  onSelectBook: (bindingId: string) => void;
  newBookOpen: boolean;
  onToggleNewBook: () => void;
  approvalsSlot?: ReactNode;
  newBookSlot?: ReactNode;
  activeLiveSlot?: ReactNode;
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

export function BookOverviewStrip({
  mode,
  data,
  currency,
  charterRevision,
  asOfLabel,
  selectedBook,
  onSelectBook,
  newBookOpen,
  onToggleNewBook,
  approvalsSlot,
  newBookSlot,
  activeLiveSlot,
}: BookOverviewStripProps) {
  const { navigate } = useRouter();
  const { meta } = useControlPlane();
  const { canWrite, outbox, dispatchCommand } = useOutbox();
  const { pushToast } = usePreferences();

  const isLive = mode === "live";
  const liveActive = isLive && data.governance.company_live !== null;

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

  const port = data.kpis.portfolio_value;
  const pnlKpi = data.kpis.trading_pnl;
  const opexKpi = data.kpis.agent_llm_opex;
  const netKpi = data.kpis.company_net_pnl;

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

  const bookRows: DataRow[] = data.books.map((book: TradingDeskBookSummary) => {
    const isSelected = selectedBook?.binding_id === book.binding_id;
    const roleMeta = roleBadgeProps(book.role);
    const bookReturnPct: number | null | undefined = book.return_pct;
    const returnTone =
      bookReturnPct === null || bookReturnPct === undefined || !Number.isFinite(bookReturnPct)
        ? "sd-tone-muted"
        : bookReturnPct > 0
          ? "sd-tone-pos"
          : bookReturnPct < 0
            ? "sd-tone-neg"
            : "sd-tone-muted";

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
            <Text variant="meta" className={`sd-tabular ${returnTone}`}>
              {formatSignedPct(bookReturnPct)}
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
            onClick={() => onSelectBook(book.binding_id)}
          >
            {isSelected ? "Selected" : "Inspect"}
          </Button>
        ),
      },
    };
  });

  return (
    <>
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
              <Status tone={data.risk_gate.suspended ? "warning" : "success"}>
                {data.risk_gate.suspended ? "New Orders Paused" : "Risk Gate Open"}
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

      {approvalsSlot}
      {newBookSlot}

      {!isLive ? (
        <Card
          title="Company Paper Books"
          description="Select any concurrent paper book to inspect its live equity curve, reconciliation bridge, positions, and execution tape"
          variant="raised"
          className="sd-ledger-card"
          headerAction={
            <Inline gap={6} align="center" wrap>
              <Badge tone="neutral" variant="soft" dot={false}>
                {data.books.length} {data.books.length === 1 ? "book" : "books"}
              </Badge>
              <Badge tone="success" variant="soft" dot={false}>
                {data.governance.soak_ready_books_count} soak ready
              </Badge>
              {supportsOpenPaperBook ? (
                <Button
                  emphasis={newBookOpen ? "quiet" : "soft"}
                  size="compact"
                  shape="pill"
                  disabled={!canOpenPaperBook}
                  onClick={onToggleNewBook}
                >
                  {newBookOpen ? "Close New Book" : "New Paper Book"}
                </Button>
              ) : null}
            </Inline>
          }
        >
          {data.books.length > 0 ? (
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

      {activeLiveSlot}

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
              points={data.cumulative_return_series}
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
              {formatSignedPct(port.return_pct)}
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
              : opexKpi.value_minor === null
                ? "Unavailable"
                : "$0.00"}
          </Text>
          <div className="sd-kpi-mid">
            <Text variant="meta" className="sd-tone-muted">
              Shared company operating expense
            </Text>
          </div>
          <Inline justify="between" align="center">
            <Text variant="meta">{opexKpi.source}</Text>
            <Text variant="meta">{data.close_count} closes</Text>
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
    </>
  );
}
