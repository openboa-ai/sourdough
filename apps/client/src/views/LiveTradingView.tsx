import { useEffect, useRef, useState } from "react";
import {
  Badge,
  Button,
  Card,
  DataTable,
  DisclosureRow,
  Inline,
  Notice,
  PageHeader,
  Result,
  Skeleton,
  Stack,
  Status,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatMinorCurrency,
  formatNsClock,
  formatRelativeNs,
  formatSignedPct,
} from "../api/client";
import type { LiveTradingProjection } from "../api/types";
import { PortfolioChart } from "../components/PortfolioChart";
import { useApp } from "../state/store";

export function LiveTradingView() {
  const { navigate, refreshSeq, linkState, reportApiError } = useApp();
  const [data, setData] = useState<LiveTradingProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [freshOrderIds, setFreshOrderIds] = useState<Set<string>>(() => new Set());
  const etagRef = useRef<string | undefined>(undefined);
  const seenOrdersRef = useRef<Set<string>>(new Set());

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
              window.setTimeout(() => setFreshOrderIds(new Set()), 1400);
            }
          }
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

  const tapeColumns: DataColumn[] = [
    { id: "order", label: "Order & Instrument", kind: "identity" },
    { id: "author", label: "Authoring Agent", kind: "role" },
    { id: "qty", label: "Ordered / Filled / Rem", kind: "numeric", align: "end" },
    { id: "price", label: "Limit / Avg / Fees", kind: "numeric", align: "end" },
    { id: "status", label: "Status & Residual Position", kind: "text" },
    { id: "trace", label: "Why?", kind: "actions", align: "end" },
  ];

  const tapeRows: DataRow[] = data.execution_tape.map((o) => {
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
            <Inline gap={6} align="center">
              <Text variant="body">{o.action.toUpperCase()} {o.instrument}</Text>
            </Inline>
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
          <Inline gap={8} align="center">
            <Badge tone="brand" variant="soft" dot={false}>
              Paper Environment
            </Badge>
            <Status tone={data.risk_gate.suspended ? "warning" : "success"}>
              {data.risk_gate.suspended ? "New Orders Paused" : "Risk Gate Open"}
            </Status>
          </Inline>
        }
      />

      {/* 4-KPI 3-Second Executive Band */}
      <div className="sd-kpi-grid">
        <Card variant="outlined" density="compact">
          <Stack gap={6}>
            <Inline justify="between" align="center">
              <Text variant="meta">Paper Portfolio Value</Text>
              <Badge tone="neutral" variant="outline" dot={false}>
                Paper
              </Badge>
            </Inline>
            <Text className="sd-kpi-value">
              {formatMinorCurrency(port.value_minor, currency)}
            </Text>
            <Inline justify="between" align="center">
              <Text
                variant="meta"
                className={
                  port.return_pct === null
                    ? "sd-kpi-delta-muted sd-tabular"
                    : port.return_pct >= 0
                      ? "sd-kpi-delta-pos sd-tabular"
                      : "sd-kpi-delta-neg sd-tabular"
                }
              >
                {formatSignedPct(port.return_pct)}
              </Text>
              <Text variant="meta">{port.source}</Text>
            </Inline>
          </Stack>
        </Card>

        <Card variant="outlined" density="compact">
          <Stack gap={6}>
            <Text variant="meta">Trading BOR P&L</Text>
            <Text
              className={`sd-kpi-value ${
                bor.value_minor === null
                  ? "sd-kpi-delta-muted"
                  : bor.value_minor >= 0
                    ? "sd-kpi-delta-pos"
                    : "sd-kpi-delta-neg"
              }`}
            >
              {formatMinorCurrency(bor.value_minor, currency, { signed: true })}
            </Text>
            <Text variant="meta">{bor.source}</Text>
          </Stack>
        </Card>

        <Card variant="outlined" density="compact">
          <Stack gap={6}>
            <Text variant="meta">Agent LLM & Compute Opex</Text>
            <Text className="sd-kpi-value">
              {formatMinorCurrency(opex.value_minor, currency)}
            </Text>
            <Text variant="meta">{opex.source}</Text>
          </Stack>
        </Card>

        <Card variant="outlined" density="compact">
          <Stack gap={6}>
            <Inline justify="between" align="center">
              <Text variant="meta">Company Net P&L</Text>
              <Status tone={data.risk_gate.suspended ? "warning" : "success"}>
                {data.risk_gate.suspended ? "Paused" : "Active"}
              </Status>
            </Inline>
            <Text
              className={`sd-kpi-value ${
                net.value_minor === null
                  ? "sd-kpi-delta-muted"
                  : net.value_minor >= 0
                    ? "sd-kpi-delta-pos"
                    : "sd-kpi-delta-neg"
              }`}
            >
              {formatMinorCurrency(net.value_minor, currency, { signed: true })}
            </Text>
            <Text variant="meta">{net.source}</Text>
          </Stack>
        </Card>
      </div>

      {/* Single Collapsible Since Last Visit Brief (no redundant sidebar links) */}
      <DisclosureRow
        label={`Since Last Visit · ${data.since_last_visit.new_event_count} recorded events`}
        initiallyOpen={false}
      >
        <Stack gap={8}>
          {data.since_last_visit.highlights.length === 0 ? (
            <Text variant="help">No new company events since your previous visit.</Text>
          ) : (
            data.since_last_visit.highlights.map((ev) => (
              <Inline key={ev.event_seq} justify="between" align="center" wrap>
                <Inline gap={8} align="center">
                  <Badge tone="neutral" variant="outline" dot={false}>
                    {ev.domain}
                  </Badge>
                  <Text variant="body">{ev.event_type}</Text>
                  <Text variant="meta" className="sd-mono">
                    {ev.entity_id}
                  </Text>
                </Inline>
                <Text variant="meta" className="sd-tabular">
                  {formatRelativeNs(ev.recorded_at_ns)} · seq #{ev.event_seq}
                </Text>
              </Inline>
            ))
          )}
        </Stack>
      </DisclosureRow>

      {/* Performance Chart + 5-Step Two-Layer P&L Reconciliation Bridge */}
      <div className="sd-two-col">
        <PortfolioChart
          points={data.cumulative_return_series}
          currency={currency}
          asOfLabel={asOfLabel}
        />

        <Card
          title="Two-Layer P&L Reconciliation"
          description="Trading books of record vs Company operating costs"
          variant="outlined"
        >
          <Stack gap={8}>
            {data.pnl_reconciliation_bridge.map((step) => (
              <div key={step.step} className="sd-bridge-row">
                <Text variant="body">{step.label}</Text>
                <Text
                  variant="body"
                  className={`sd-tabular ${
                    step.status === "unavailable"
                      ? "sd-kpi-delta-muted"
                      : (step.amount_minor ?? 0) > 0
                        ? "sd-kpi-delta-pos"
                        : (step.amount_minor ?? 0) < 0
                          ? "sd-kpi-delta-neg"
                          : ""
                  }`}
                >
                  {step.status === "unavailable"
                    ? "Unavailable"
                    : formatMinorCurrency(step.amount_minor, currency, {
                        signed: step.step !== "agent_llm_opex" && step.step !== "trading_fees",
                      })}
                </Text>
              </div>
            ))}

            {data.open_close_issues && data.open_close_issues.length > 0 ? (
              <Notice title="Open accounting close issue" tone="warning">
                Company Net P&L remains Unavailable until all missing cost records are closed —
                never coerced to $0.00.
              </Notice>
            ) : null}

            {data.adapter_status ? (
              <Text variant="meta">
                Adapter: {data.adapter_status.connector_revision} · Account{" "}
                {data.adapter_status.account_id} · Exposure cap{" "}
                {formatMinorCurrency(
                  data.adapter_status.exposure_limit_minor_units,
                  currency
                )}
              </Text>
            ) : null}
          </Stack>
        </Card>
      </div>

      {/* Live Execution Tape (6-column cancel != flat honesty) */}
      <Card
        title="Live Paper Execution Tape"
        description={
          linkState === "live"
            ? "Coalesced live stream · Grouped causal Order → Fills → Fees → Cancellation → Position"
            : "Snapshot paused while reconnecting"
        }
        variant="outlined"
      >
        {data.execution_tape.length === 0 ? (
          <Text variant="help">No paper orders recorded yet for this company.</Text>
        ) : (
          <DataTable
            caption="Paper execution tape"
            columns={tapeColumns}
            rows={tapeRows}
          />
        )}
      </Card>

      {/* Open Paper Positions */}
      <Card
        title="Open Paper Positions"
        description="Current observed paper exposure across instruments"
        variant="outlined"
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
