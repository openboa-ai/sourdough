import { useEffect, useMemo, useState } from "react";
import { StorageMeter } from "@openboa/yogurt/motion-storage-meter";
import {
  Badge,
  Button,
  Card,
  DataTable,
  FilterBar,
  Inline,
  Link,
  Notice,
  RecordFacts,
  SegmentedControl,
  Stack,
  Text,
  type DataColumn,
  type DataRow,
  type Fact,
} from "@openboa/yogurt";
import type { TradingDeskProjection } from "../../../api/types";
import { PortfolioChart } from "../../../components/PortfolioChart";
import {
  formatMinorCurrency,
  formatNsClock,
  formatRelativeNs,
  normalizeToNs,
} from "../../../lib/format";
import { useRouter } from "../../../state/store";

const ORDER_FOCUS_STORAGE_PREFIX = "sd.orderFocusReturn.";

function persistOrderFocusReturn(orderId: string, focusId: string): void {
  try {
    window.sessionStorage.setItem(`${ORDER_FOCUS_STORAGE_PREFIX}${orderId}`, focusId);
  } catch {
    // Ignore storage failures in restricted contexts.
  }
}

function formatTs(ns: number | null | undefined): string {
  const normalized = normalizeToNs(ns);
  if (!normalized) return "Unavailable";
  return `${formatNsClock(normalized)} (${formatRelativeNs(normalized)})`;
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

export interface PositionsOrdersTabsProps {
  mode: "live" | "paper";
  data: TradingDeskProjection;
  currency: string;
  asOfLabel: string;
}

export function PositionsOrdersTabs({
  mode,
  data,
  currency,
  asOfLabel,
}: PositionsOrdersTabsProps) {
  const { navigate } = useRouter();
  const [tapeQuery, setTapeQuery] = useState("");
  const [tapeStatus, setTapeStatus] = useState<"all" | "filled" | "open">("all");

  useEffect(() => {
    setTapeQuery("");
    setTapeStatus("all");
  }, [mode]);

  const isLive = mode === "live";
  const selectedBook = data.selected_book;
  const liveActive = isLive && data.governance.company_live !== null;
  const shadow = isLive ? data.shadow_benchmark : null;

  const filteredTape = useMemo(() => {
    const q = tapeQuery.trim().toLowerCase();
    return data.execution_tape.filter((o) => {
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
  }, [data.execution_tape, tapeQuery, tapeStatus]);

  // Exposure Cap Meter (identical in Live & Paper)
  const exposureLimitMajor =
    data.exposure_cap.limit_minor_units != null
      ? Number((data.exposure_cap.limit_minor_units / 100).toFixed(2))
      : null;
  const exposureCategories = data.open_positions
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
  const exposureUsedMajor = Number((data.exposure_cap.used_minor_units / 100).toFixed(2));
  const isOverCap =
    exposureLimitMajor !== null &&
    exposureLimitMajor > 0 &&
    exposureUsedMajor > exposureLimitMajor;
  const exposurePct =
    exposureLimitMajor && exposureLimitMajor > 0
      ? Math.round((exposureUsedMajor / exposureLimitMajor) * 100)
      : 0;

  // Two-Layer P&L Reconciliation Facts
  const reconciliationFacts: Fact[] = data.pnl_reconciliation_bridge.map((step) => ({
    id: step.step,
    label: step.label,
    value: (
      <Text
        variant="body"
        className={`sd-tabular ${
          step.status === "unavailable" || step.amount_minor === null
            ? "sd-tone-muted"
            : step.amount_minor > 0
              ? "sd-tone-pos"
              : step.amount_minor < 0
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

  // Open Positions Table (Identical in Live & Paper)
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

  const positionRows: DataRow[] = data.open_positions.map((pos) => ({
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

  // Execution Tape Table (Identical structure, adds External ID in Live mode)
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

  return (
    <>
      {/* Main Performance Chart (left) + Two-Layer Reconciliation & Exposure Cap (right) */}
      <div className="sd-two-col">
        <PortfolioChart
          points={data.cumulative_return_series}
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
                  asOf={`seq #${data.event_seq}`}
                  facts={reconciliationFacts}
                />
                {data.open_close_issues.length > 0 ? (
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
                  ? `${data.governance.live_candidates.length} candidate venue(s) registered. Exposure cap enforces hard notional limits once activated.`
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
          {data.execution_tape.length > 0 ? (
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
              {data.execution_tape.length === 0
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
    </>
  );
}
