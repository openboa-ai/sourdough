import { useEffect, useMemo, useRef, useState } from "react";
import { ActivityIsland } from "@openboa/yogurt/motion-activity-island";
import { StorageMeter } from "@openboa/yogurt/motion-storage-meter";
import {
  Badge,
  Button,
  Card,
  DataTable,
  DisclosureRow,
  EmptyResult,
  FilterBar,
  Inline,
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
import type { LiveTradingProjection, ReturnPoint } from "../api/types";
import { PortfolioChart } from "../components/PortfolioChart";
import { useApp } from "../state/store";

function MiniSparkline({ points }: { points: ReturnPoint[] }) {
  const valid = points
    .map((p, idx) => ({ idx, val: p.return_pct }))
    .filter((item): item is { idx: number; val: number } => item.val !== null);
  if (valid.length < 2) return null;
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

export function LiveTradingView() {
  const { navigate, refreshSeq, linkState, reportApiError } = useApp();
  const [data, setData] = useState<LiveTradingProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [freshOrderIds, setFreshOrderIds] = useState<Set<string>>(() => new Set());
  const [kpiPulsing, setKpiPulsing] = useState(false);
  const [tapeQuery, setTapeQuery] = useState("");
  const [tapeFilter, setTapeFilter] = useState<"all" | "working" | "filled" | "cancelled">("all");
  const [tapePresentation, setTapePresentation] = useState<"table" | "feed">("table");
  const etagRef = useRef<string | undefined>(undefined);
  const seenOrdersRef = useRef<Set<string>>(new Set());
  const prevSeqRef = useRef<number | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<LiveTradingProjection>("live-trading", {
          etag: etagRef.current,
          signal: controller.signal,
        });
        if (res.etag) etagRef.current = res.etag;
        if (res.status === 200 && res.data) {
          const incoming = new Set(res.data.execution_tape.map((o) => o.order_id));
          if (seenOrdersRef.current.size > 0) {
            const newlyArrived = new Set<string>();
            for (const oid of incoming) {
              if (!seenOrdersRef.current.has(oid)) newlyArrived.add(oid);
            }
            if (newlyArrived.size > 0) {
              setFreshOrderIds(newlyArrived);
              window.setTimeout(() => setFreshOrderIds(new Set()), 1800);
            }
          }
          if (prevSeqRef.current !== null && res.data.event_seq !== prevSeqRef.current) {
            setKpiPulsing(true);
            window.setTimeout(() => setKpiPulsing(false), 600);
          }
          prevSeqRef.current = res.data.event_seq;
          seenOrdersRef.current = incoming;
          setData(res.data);
        }
        setError(null);
      } catch (err) {
        if (!controller.signal.aborted) {
          reportApiError(err);
          setError(err instanceof Error ? err.message : "Failed to load live trading");
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [refreshSeq, reportApiError]);

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

  if (loading && !data) {
    return (
      <Stack gap={16}>
        <PageHeader title="Live Trading & Portfolio" detail="Loading authoritative paper state..." />
        <Skeleton label="Loading portfolio metrics" lines={4} />
      </Stack>
    );
  }

  if (!data) {
    return (
      <Result title="Live trading view unavailable">
        {error ?? "Waiting for local control plane snapshot."}
      </Result>
    );
  }

  const currency = data.kpis.portfolio_value.currency || "USD";
  const asOfLabel = `As of ${formatNsClock(data.as_of_ns)} · seq #${data.event_seq}`;

  if (!data.paper_enabled) {
    return (
      <Stack gap={16}>
        <PageHeader
          title="Company Overview"
          detail={`Core-only company · ${asOfLabel}`}
        />
        <Result
          title="Paper trading domain is not active yet"
          action={
            <Button
              emphasis="filled"
              tone="brand"
              shape="pill"
              onClick={() => navigate("/office")}
            >
              Inspect Agents & Activity
            </Button>
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

  const exposureLimitMajor =
    data.adapter_status?.exposure_limit_minor_units != null
      ? Math.round(data.adapter_status.exposure_limit_minor_units / 100)
      : null;

  const exposureCategories = data.open_positions.map((pos) => {
    const notionalMajor = Math.max(
      1,
      Math.round((Math.abs(pos.quantity) * pos.avg_entry_minor_units) / 100)
    );
    return {
      id: pos.instrument,
      label: pos.instrument,
      amount: notionalMajor,
      detail: `${pos.quantity} qty @ ${formatMinorCurrency(pos.avg_entry_minor_units, currency)} · Author: ${pos.agent_name}`,
    };
  });

  const exposureUsedMajor = exposureCategories.reduce((acc, c) => acc + c.amount, 0);

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

  const tapeColumns: DataColumn[] = [
    {
      id: "order",
      label: "Order & Instrument",
      kind: "identity",
      sortValue: (row) => row.id,
    },
    { id: "author", label: "Authoring Agent", kind: "role" },
    { id: "qty", label: "Ordered / Filled / Rem", kind: "numeric", align: "end" },
    { id: "price", label: "Limit / Avg / Fees", kind: "numeric", align: "end" },
    { id: "status", label: "Status & Residual Position", kind: "text" },
    { id: "trace", label: "Why?", kind: "actions", align: "end" },
  ];

  const tapeRows: DataRow[] = filteredTape.map((o) => {
    const isFresh = freshOrderIds.has(o.order_id);
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

    return {
      id: o.order_id,
      cells: {
        order: (
          <Stack gap={2} className={isFresh ? "sd-live-enter sd-live-flash" : undefined}>
            <Text variant="body">
              {o.action.toUpperCase()} {o.instrument}
            </Text>
            <Text variant="meta" className="sd-mono">
              {o.order_id}
            </Text>
          </Stack>
        ),
        author: (
          <Stack gap={2}>
            <Text variant="body">{o.agent_name}</Text>
            <Text variant="meta">{o.agent_role}</Text>
          </Stack>
        ),
        qty: (
          <Text variant="body" className="sd-tabular">
            {o.ordered_qty} / {o.filled_qty} / {o.remaining_qty}
          </Text>
        ),
        price: (
          <Stack gap={2} align="end">
            <Text variant="body" className="sd-tabular">
              {formatMinorCurrency(o.avg_fill_minor_units ?? o.limit_minor_units, currency)}
            </Text>
            <Text variant="meta" className="sd-tabular">
              Fee {formatMinorCurrency(o.total_fee_minor_units, currency)}
            </Text>
          </Stack>
        ),
        status: statusBadge,
        trace: (
          <Button
            emphasis="quiet"
            size="compact"
            shape="pill"
            onClick={() => navigate(`/orders/${encodeURIComponent(o.order_id)}`)}
          >
            Why?
          </Button>
        ),
      },
    };
  });

  const posColumns: DataColumn[] = [
    { id: "instrument", label: "Instrument", kind: "identity" },
    { id: "qty", label: "Net Quantity", kind: "numeric", align: "end" },
    { id: "entry", label: "Avg Entry", kind: "numeric", align: "end" },
    { id: "owner", label: "Latest Author", kind: "text" },
    { id: "action", label: "Trace", kind: "actions", align: "end" },
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
          onClick={() =>
            navigate(`/orders/${encodeURIComponent(pos.latest_order_id)}`)
          }
        >
          Order
        </Button>
      ),
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Live Trading & Portfolio"
        detail={asOfLabel}
        action={
          <Inline gap={8} align="center" wrap>
            <ActivityIsland
              label={linkState === "live" ? "Live Tape" : "Snapshot"}
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
          </Inline>
        }
      />

      <div className="sd-kpi-grid">
        <section className="sd-kpi-card" data-pulse={kpiPulsing ? "true" : "false"}>
          <Inline justify="between" align="center">
            <Text variant="meta">Paper Portfolio Value</Text>
            <Badge tone="neutral" variant="outline" dot={false}>
              Paper
            </Badge>
          </Inline>
          <Text className="sd-kpi-value">
            {formatMinorCurrency(port.value_minor, currency)}
          </Text>
          <MiniSparkline points={data.cumulative_return_series} />
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
        </section>

        <section className="sd-kpi-card" data-pulse={kpiPulsing ? "true" : "false"}>
          <Text variant="meta">Trading BOR P&amp;L</Text>
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
          <Text variant="meta">{bor.source}</Text>
        </section>

        <section className="sd-kpi-card" data-pulse={kpiPulsing ? "true" : "false"}>
          <Text variant="meta">Agent LLM &amp; Compute Opex</Text>
          <Text className="sd-kpi-value">
            {formatMinorCurrency(opex.value_minor, currency)}
          </Text>
          <Text variant="meta">{opex.source}</Text>
        </section>

        <section className="sd-kpi-card" data-pulse={kpiPulsing ? "true" : "false"}>
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
          <Text variant="meta">{net.source}</Text>
        </section>
      </div>

      <DisclosureRow
        label={`Since Last Visit · ${data.since_last_visit.new_event_count} recorded events`}
        initiallyOpen={false}
      >
        <Stack gap={8}>
          {data.since_last_visit.highlights.length === 0 ? (
            <Text variant="help">No new company events since your previous visit.</Text>
          ) : (
            <List label="Recent company events" divided>
              {data.since_last_visit.highlights.map((ev) => (
                <WorkEvent
                  key={ev.event_seq}
                  event={{
                    id: String(ev.event_seq),
                    kind: ev.domain === "paper" ? "task" : ev.domain === "ops" ? "routine" : "review",
                    title: `${ev.domain} · ${ev.event_type}`,
                    detail: ev.entity_id,
                    time: `${formatRelativeNs(ev.recorded_at_ns)} · seq #${ev.event_seq}`,
                    state: "done",
                  }}
                />
              ))}
            </List>
          )}
        </Stack>
      </DisclosureRow>

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

              {data.open_close_issues && data.open_close_issues.length > 0 ? (
                <Notice title="Open accounting close issue" tone="warning">
                  Company Net P&amp;L remains Unavailable until all missing cost records are closed —
                  never coerced to $0.00.
                </Notice>
              ) : null}
            </Stack>
          </Card>

          {exposureLimitMajor && exposureLimitMajor > 0 ? (
            <Card variant="raised" density="compact">
              <StorageMeter
                label="Paper Exposure Cap Utilization"
                used={exposureUsedMajor}
                capacity={exposureLimitMajor}
                unit={currency}
                categories={exposureCategories}
              />
            </Card>
          ) : null}
        </Stack>
      </div>

      <Card
        title="Live Paper Execution Tape"
        description={
          linkState === "live"
            ? "Coalesced live stream · Grouped causal Order → Fills → Fees → Cancellation → Position"
            : "Snapshot paused while reconnecting"
        }
        headerAction={
          <SegmentedControl
            label="Execution tape layout"
            value={tapePresentation}
            onValueChange={(next) => setTapePresentation(next as "table" | "feed")}
            options={[
              { id: "table", label: "Table" },
              { id: "feed", label: "Causal Feed" },
            ]}
          />
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
            <DataTable
              caption="Paper execution tape"
              columns={tapeColumns}
              rows={tapeRows}
            />
          ) : (
            <List label="Live execution causal feed" divided>
              {filteredTape.map((o) => (
                <WorkEvent
                  key={o.order_id}
                  event={{
                    id: o.order_id,
                    kind: o.cancel_not_flat ? "review" : "task",
                    title: `${o.action.toUpperCase()} ${o.instrument} (${o.filled_qty}/${o.ordered_qty})`,
                    detail: `${o.agent_name} · Avg ${formatMinorCurrency(
                      o.avg_fill_minor_units ?? o.limit_minor_units,
                      currency
                    )} · Fee ${formatMinorCurrency(o.total_fee_minor_units, currency)}${
                      o.cancel_not_flat
                        ? ` · Residual open ${o.residual_position_qty} (cancel did not flatten)`
                        : ""
                    }`,
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
                  onOpen={() => navigate(`/orders/${encodeURIComponent(o.order_id)}`)}
                />
              ))}
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
