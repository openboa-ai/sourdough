import { Fragment, useEffect, useRef, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Button,
  Card,
  DataTable,
  Divider,
  Inline,
  Navbar,
  Notice,
  PageHeader,
  RecordFacts,
  ResourceRow,
  Result,
  Skeleton,
  Stack,
  Text,
  Trace,
  type DataColumn,
  type DataRow,
  type TraceEvent,
} from "@openboa/yogurt";
import {
  ApiError,
  fetchProjection,
  formatMinorCurrency,
  formatNsClock,
  formatRelativeNs,
} from "../api/client";
import type { OrderTraceProjection } from "../api/types";
import { SpaBreadcrumb } from "../components/SpaBreadcrumb";
import { useApp, useEscapeBack } from "../state/store";

type RelatedPublicationItem = OrderTraceProjection["related_publications"][number];

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

const ORDER_FOCUS_STORAGE_PREFIX = "sd.orderFocusReturn.";

function resolveOrderFocusReturnId(orderId: string): string {
  try {
    const stored = window.sessionStorage.getItem(`${ORDER_FOCUS_STORAGE_PREFIX}${orderId}`);
    if (stored && (stored.startsWith("order-") || stored.startsWith("pos-order-"))) {
      return stored;
    }
  } catch {
    // Ignore storage failures in restricted contexts.
  }
  return `order-${orderId}`;
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

export function OrderTraceView({ orderId }: { orderId: string }) {
  const { navigate, refreshSeq, reportApiError, askCeo } = useApp();
  const [data, setData] = useState<OrderTraceProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const prevOrderIdRef = useRef(orderId);

  const activeData = data && data.order.order_id === orderId ? data : null;
  const resolvedFocusReturnId = resolveOrderFocusReturnId(
    activeData ? activeData.order.order_id : orderId
  );

  useEscapeBack("/trading/paper", resolvedFocusReturnId);

  useEffect(() => {
    if (prevOrderIdRef.current !== orderId) {
      prevOrderIdRef.current = orderId;
      setLoading(true);
      setData(null);
      setError(null);
      setErrorStatus(null);
    }
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<OrderTraceProjection>(
          `orders/${encodeURIComponent(orderId)}`,
          { signal: controller.signal }
        );
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
        setErrorStatus(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setErrorStatus(err instanceof ApiError ? err.status : null);
        setError(err instanceof Error ? err.message : "Failed to load order trace");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [orderId, refreshSeq, retrySeq, reportApiError]);

  if (loading && !activeData) {
    return <Skeleton label="Loading order causal lineage" lines={5} />;
  }

  if (!activeData) {
    const isNotFound = errorStatus === 404 || error === null;
    return (
      <Result
        title={isNotFound ? "Order trace not found" : "Order trace unavailable"}
        action={
          <Inline gap={8} wrap>
            {!isNotFound ? (
              <Button
                emphasis="soft"
                shape="pill"
                onClick={() => {
                  setLoading(true);
                  setError(null);
                  setErrorStatus(null);
                  setRetrySeq((n) => n + 1);
                }}
              >
                Retry
              </Button>
            ) : null}
            <Button
              emphasis="soft"
              shape="pill"
              onClick={() => navigate("/trading/paper", { focusReturnId: resolvedFocusReturnId })}
            >
              Back to Paper Trading
            </Button>
          </Inline>
        }
      >
        {isNotFound
          ? error ?? `Order ${orderId} does not exist in this company store.`
          : error ?? "Control plane temporarily unavailable while loading order trace."}
      </Result>
    );
  }

  const order = activeData.order;
  const focusReturnId = resolvedFocusReturnId;
  const isTerminal = isTerminalOrderStatus(order.status);
  const remainingOpen = getRemainingOpenQty(order);

  const traceEvents: TraceEvent[] = activeData.causal_steps.map((step, idx) => ({
    id: `${step.stage}-${idx}`,
    index: String(idx + 1).padStart(2, "0"),
    title: `${step.stage} · ${step.id}`,
    detail: step.detail,
    state:
      step.status === "warning"
        ? "flagged"
        : step.status === "complete"
          ? "complete"
          : "working",
  }));

  const fillById = new Map(order.fills.map((f) => [f.fill_id, f]));

  const fillCols: DataColumn[] = [
    {
      id: "fill_id",
      label: "Fill ID",
      kind: "identity",
      sortValue: (row) => row.id,
    },
    {
      id: "qty",
      label: "Quantity",
      kind: "numeric",
      align: "end",
      sortValue: (row) => fillById.get(row.id)?.quantity ?? null,
    },
    {
      id: "price",
      label: "Fill Price",
      kind: "numeric",
      align: "end",
      sortValue: (row) => fillById.get(row.id)?.price_minor_units ?? null,
    },
    {
      id: "fee",
      label: "Venue Fee",
      kind: "numeric",
      align: "end",
      sortValue: (row) => fillById.get(row.id)?.fee_minor_units ?? null,
    },
    {
      id: "pos",
      label: "Observed Position",
      kind: "numeric",
      align: "end",
      sortValue: (row) => fillById.get(row.id)?.position_quantity ?? null,
    },
    {
      id: "available_at",
      label: "Observed At",
      kind: "numeric",
      align: "end",
      sortValue: (row) => normalizeToNs(fillById.get(row.id)?.available_at) ?? null,
    },
  ];

  const fillRows: DataRow[] = order.fills.map((f) => ({
    id: f.fill_id,
    cells: {
      fill_id: <Text className="sd-mono">{f.fill_id}</Text>,
      qty: <Text className="sd-tabular">{f.quantity}</Text>,
      price: (
        <Text className="sd-tabular">{formatMinorCurrency(f.price_minor_units)}</Text>
      ),
      fee: (
        <Text className="sd-tabular">
          {f.fee_minor_units === null ? "No reading" : formatMinorCurrency(f.fee_minor_units)}
        </Text>
      ),
      pos: (
        <Text className="sd-tabular">
          {f.position_quantity === null ? "No reading" : f.position_quantity}
        </Text>
      ),
      available_at: (
        <Text className="sd-tabular">{formatTimestamp(f.available_at, "No reading")}</Text>
      ),
    },
  }));

  return (
    <Stack gap={16}>
      <Navbar
        variant="back"
        label="Back to Live Trading"
        onBack={() => navigate("/", { focusReturnId })}
        action={<CopyButton value={order.order_id} presentation="quiet" />}
      />

      <SpaBreadcrumb
        focusReturnId={focusReturnId}
        items={[
          { id: "paper", label: "Paper Trading", href: "/trading/paper" },
          { id: "order", label: `Order ${order.order_id}`, href: `/orders/${order.order_id}` },
        ]}
      />

      <PageHeader
        title={`Order ${order.order_id}`}
        detail={`${order.action.toUpperCase()} ${order.instrument} · Author: ${order.agent_name}`}
        action={
          <Inline gap={8} align="center" wrap>
            {order.agent_id ? (
              <Button
                emphasis="soft"
                size="compact"
                shape="pill"
                onClick={() =>
                  navigate(`/agents/${encodeURIComponent(order.agent_id!)}`)
                }
              >
                Inspect {order.agent_name}
              </Button>
            ) : null}
            <Button
              emphasis="filled"
              size="compact"
              shape="pill"
              onClick={() =>
                askCeo({
                  kind: "paper_order",
                  id: order.order_id,
                  label: `Order ${order.order_id} (${order.action.toUpperCase()} ${order.instrument})`,
                })
              }
            >
              Ask CEO about this order
            </Button>
          </Inline>
        }
      />

      {order.cancel_not_flat ? (
        <Notice title="Cancelled with open residual exposure" tone="warning" urgent>
          This order was cancelled after partial fills executed. Residual net position remains{" "}
          {order.residual_position_qty} units — cancelling an order never auto-flattens already
          filled exposure.
        </Notice>
      ) : null}

      <div className="sd-two-col">
        <Card title="Causal Execution Lineage" variant="raised">
          <Trace label="Order execution lineage" events={traceEvents} />
        </Card>

        <Card title="Order & Exposure Summary" variant="raised">
          <div className="sd-facts-wrap">
            <RecordFacts
              source="paper_order + paper_fill + paper_cancellation"
              asOf={`seq #${activeData.event_seq}`}
              facts={[
                {
                  id: "status",
                  label: "Order Status",
                  value: (
                    <Badge
                      tone={
                        order.cancel_not_flat
                          ? "warning"
                          : order.status === "filled"
                            ? "success"
                            : "neutral"
                      }
                      dot={false}
                    >
                      {order.status}
                    </Badge>
                  ),
                },
                { id: "inst", label: "Instrument", value: order.instrument },
                {
                  id: "acct",
                  label: "Paper Account",
                  value: <Text className="sd-mono">{order.account_id}</Text>,
                },
                {
                  id: "qty",
                  label: "Ordered / Filled / Remaining Open",
                  value: `${order.ordered_qty} / ${order.filled_qty} / ${remainingOpen}${
                    isTerminal && order.remaining_qty > 0
                      ? ` (${order.remaining_qty} cancelled)`
                      : ""
                  }`,
                },
                {
                  id: "pos",
                  label: "Residual Net Position",
                  value: (
                    <Inline gap={6} align="center">
                      <Text className="sd-tabular">
                        {order.residual_position_qty ?? "No reading"}
                      </Text>
                      {order.cancel_not_flat ? (
                        <Badge tone="warning" dot={false}>
                          Open after cancel
                        </Badge>
                      ) : null}
                    </Inline>
                  ),
                },
                {
                  id: "lim",
                  label: "Limit Price",
                  value: formatMinorCurrency(order.limit_minor_units),
                },
                {
                  id: "avg_fill",
                  label: "Average Fill Price",
                  value:
                    order.avg_fill_minor_units === null
                      ? "No reading"
                      : formatMinorCurrency(order.avg_fill_minor_units),
                },
                {
                  id: "reserved",
                  label: "Reserved Margin",
                  value: formatMinorCurrency(order.reserved_minor_units),
                },
                {
                  id: "fees",
                  label: "Total Venue Fees",
                  value:
                    order.total_fee_minor_units === null
                      ? "No reading"
                      : formatMinorCurrency(order.total_fee_minor_units),
                },
                { id: "mandate", label: "Mandate Revision", value: order.mandate_revision },
                {
                  id: "run_id",
                  label: "Work Run ID",
                  value: <Text className="sd-mono">{order.run_id}</Text>,
                },
                {
                  id: "duty_id",
                  label: "Duty ID",
                  value: order.duty_id ? (
                    <Text className="sd-mono">{order.duty_id}</Text>
                  ) : (
                    "Unavailable"
                  ),
                },
                {
                  id: "decision_id",
                  label: "Decision ID",
                  value: order.decision_id ? (
                    <Text className="sd-mono">{order.decision_id}</Text>
                  ) : (
                    "Unavailable"
                  ),
                },
                {
                  id: "decided_at",
                  label: "Decided At",
                  value: formatTimestamp(order.decided_at, "Unavailable"),
                },
                {
                  id: "rationale",
                  label: "Decision Rationale",
                  value: order.rationale ?? "Unavailable",
                },
                {
                  id: "cancelled_at",
                  label: "Cancelled At",
                  value: order.cancellation
                    ? formatTimestamp(order.cancellation.available_at, "No reading")
                    : "Not cancelled",
                },
              ]}
            />
          </div>
        </Card>
      </div>

      <div className={order.reconciliation ? "sd-split-equal" : undefined}>
        <Card title="Cancellation Details" variant="raised">
          {order.cancellation === null ? (
            <Text variant="help">Not cancelled — no cancellation record on this order.</Text>
          ) : (
            <div className="sd-facts-wrap">
              <RecordFacts
                source="paper_cancellation"
                asOf={`seq #${activeData.event_seq}`}
                facts={[
                  {
                    id: "cancel_event",
                    label: "Cancellation Event ID",
                    value: <Text className="sd-mono">{order.cancellation.event_id}</Text>,
                  },
                  {
                    id: "cancel_outcome",
                    label: "Outcome",
                    value: (
                      <Badge
                        tone={order.cancel_not_flat ? "warning" : "neutral"}
                        dot={false}
                      >
                        {order.cancellation.outcome}
                      </Badge>
                    ),
                  },
                  {
                    id: "cancel_released",
                    label: "Reservation Released",
                    value: order.cancellation.released
                      ? "Yes (remaining reserve released)"
                      : "No (reserve held)",
                  },
                  {
                    id: "cancel_available_at",
                    label: "Observed At",
                    value: formatTimestamp(order.cancellation.available_at, "No reading"),
                  },
                  {
                    id: "cancel_filled_qty",
                    label: "Filled Quantity at Cancel",
                    value: String(order.cancellation.filled_quantity),
                  },
                  {
                    id: "cancel_pos_qty",
                    label: "Position Quantity at Cancel",
                    value:
                      order.cancellation.position_quantity === null
                        ? "No reading"
                        : String(order.cancellation.position_quantity),
                  },
                ]}
              />
            </div>
          )}
        </Card>

        {order.reconciliation ? (
          <Card title="Venue Reconciliation" variant="raised">
            <div className="sd-facts-wrap">
              <RecordFacts
                source="paper_reconciliation"
                asOf={`seq #${activeData.event_seq}`}
                facts={[
                  {
                    id: "recon_attempts",
                    label: "Query Attempts",
                    value: String(order.reconciliation.attempts),
                  },
                  {
                    id: "recon_next",
                    label: "Next Query At",
                    value: formatTimestamp(order.reconciliation.next_query_at, "Not scheduled"),
                  },
                  {
                    id: "recon_fill",
                    label: "Reconciled Fill ID",
                    value: order.reconciliation.fill_id ? (
                      <Text className="sd-mono">{order.reconciliation.fill_id}</Text>
                    ) : (
                      "No reading"
                    ),
                  },
                  {
                    id: "recon_sources",
                    label: "Evidence Sources",
                    value:
                      Array.isArray(order.reconciliation.sources) &&
                      order.reconciliation.sources.length > 0
                        ? order.reconciliation.sources.map((s) => String(s)).join(", ")
                        : "None",
                  },
                ]}
              />
            </div>
          </Card>
        ) : null}
      </div>

      <Card title="Recorded Fills & Fees" variant="raised" className="sd-ledger-card">
        {order.fills.length === 0 ? (
          <Text variant="help">No fills recorded for this order.</Text>
        ) : (
          <DataTable caption="Order fills" columns={fillCols} rows={fillRows} />
        )}
      </Card>

      <Card
        title="Related Publications"
        description="Shared company reports authored by this order's agent"
        headerAction={
          <Button
            emphasis="quiet"
            size="compact"
            shape="pill"
            onClick={() => navigate("/company/org")}
          >
            All Org Reports
          </Button>
        }
        variant="raised"
      >
        {activeData.related_publications.length === 0 ? (
          <Text variant="help">No related publications from {order.agent_name}.</Text>
        ) : (
          <Stack gap={12}>
            {activeData.related_publications.map((pub: RelatedPublicationItem, idx: number) => (
              <Fragment key={pub.asset_id}>
                {idx > 0 ? <Divider /> : null}
                <Stack gap={8}>
                  <ResourceRow
                    resource={{
                      id: pub.asset_id,
                      kind: "output",
                      title: `${pub.asset_id} · #${pub.project_id}`,
                      detail: pub.excerpt,
                    }}
                    onOpen={() => navigate("/company/org")}
                  />
                  <Inline gap={8} align="center" wrap>
                    <Button
                      emphasis="soft"
                      size="compact"
                      shape="pill"
                      onClick={() => navigate("/company/org")}
                    >
                      View in Org &amp; Reports
                    </Button>
                    <Button
                      emphasis="quiet"
                      size="compact"
                      shape="pill"
                      onClick={() =>
                        askCeo({
                          kind: "shared_publication",
                          id: pub.asset_id,
                          label: `Publication ${pub.asset_id} (#${pub.project_id})`,
                        })
                      }
                    >
                      Ask CEO about this publication
                    </Button>
                  </Inline>
                </Stack>
              </Fragment>
            ))}
          </Stack>
        )}
      </Card>
    </Stack>
  );
}
