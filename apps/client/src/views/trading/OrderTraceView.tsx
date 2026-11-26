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
} from "../../api/client";
import type { OrderTraceProjection } from "../../api/types";
import { SpaBreadcrumb } from "../../components/SpaBreadcrumb";
import { useApp, useEscapeBack } from "../../state/store";

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

function resolveBackMeta(
  backLink: string,
  agentName: string
): {
  label: string;
  breadcrumbItems: Array<{ id: string; label: string; href: string }>;
} {
  if (backLink === "/trading/live") {
    return {
      label: "Back to Live Trading",
      breadcrumbItems: [{ id: "live", label: "Live Trading", href: "/trading/live" }],
    };
  }
  if (backLink.startsWith("/agents/")) {
    return {
      label: `Back to ${agentName || "Agent"}`,
      breadcrumbItems: [
        { id: "agents", label: "Agents", href: "/agents" },
        { id: "agent", label: agentName || "Agent", href: backLink },
      ],
    };
  }
  return {
    label: "Back to Paper Trading",
    breadcrumbItems: [{ id: "paper", label: "Paper Trading", href: "/trading/paper" }],
  };
}

export function OrderTraceView({ orderId }: { orderId: string }) {
  const { navigate, refreshSeq, routeSearchParams, reportApiError, askCeo } = useApp();
  const [data, setData] = useState<OrderTraceProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const prevOrderIdRef = useRef(orderId);

  const envHint = routeSearchParams.get("env");
  const fallbackBackLink = envHint === "live" ? "/trading/live" : "/trading/paper";
  const activeData = data && data.order.order_id === orderId ? data : null;
  const resolvedFocusReturnId = resolveOrderFocusReturnId(
    activeData ? activeData.order.order_id : orderId
  );
  const backLink = activeData?.order.back_link || fallbackBackLink;

  useEscapeBack(backLink, resolvedFocusReturnId);

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
    const fallbackMeta = resolveBackMeta(fallbackBackLink, "Agent");
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
              onClick={() => navigate(fallbackBackLink, { focusReturnId: resolvedFocusReturnId })}
            >
              {fallbackMeta.label}
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
  const currency = order.currency || "USD";
  const focusReturnId = resolvedFocusReturnId;
  const isTerminal = isTerminalOrderStatus(order.status);
  const remainingOpen = getRemainingOpenQty(order);
  const backMeta = resolveBackMeta(backLink, order.agent_name);
  const isCancelled = order.status === "cancelled" || order.cancelled_at_ns !== null;
  const accountLabel =
    order.environment === "live"
      ? "Live Account"
      : order.environment === "agent_sandbox"
        ? "Sandbox Account"
        : "Paper Account";

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
      id: "notional",
      label: "Notional",
      kind: "numeric",
      align: "end",
      sortValue: (row) => fillById.get(row.id)?.notional_minor_units ?? null,
    },
    {
      id: "fee",
      label: "Venue Fee",
      kind: "numeric",
      align: "end",
      sortValue: (row) => fillById.get(row.id)?.fee_minor_units ?? null,
    },
    {
      id: "realized_pnl",
      label: "Realized P&L",
      kind: "numeric",
      align: "end",
      sortValue: (row) => fillById.get(row.id)?.realized_pnl_minor_units ?? null,
    },
    {
      id: "delivery",
      label: "Fee Delivery",
      kind: "role",
      sortValue: (row) => fillById.get(row.id)?.delivery_status ?? "",
    },
    {
      id: "filled_at",
      label: "Filled At",
      kind: "numeric",
      align: "end",
      sortValue: (row) => normalizeToNs(fillById.get(row.id)?.filled_at_ns) ?? null,
    },
  ];

  const fillRows: DataRow[] = order.fills.map((f) => ({
    id: f.fill_id,
    cells: {
      fill_id: <Text className="sd-mono">{f.fill_id}</Text>,
      qty: <Text className="sd-tabular">{f.quantity}</Text>,
      price: (
        <Text className="sd-tabular">{formatMinorCurrency(f.price_minor_units, currency)}</Text>
      ),
      notional: (
        <Text className="sd-tabular">
          {formatMinorCurrency(f.notional_minor_units, currency)}
        </Text>
      ),
      fee: (
        <Text className="sd-tabular">{formatMinorCurrency(f.fee_minor_units, currency)}</Text>
      ),
      realized_pnl: (
        <Text
          className={`sd-tabular ${
            f.realized_pnl_minor_units > 0
              ? "sd-tone-pos"
              : f.realized_pnl_minor_units < 0
                ? "sd-tone-neg"
                : "sd-tone-muted"
          }`}
        >
          {formatMinorCurrency(f.realized_pnl_minor_units, currency, { signed: true })}
        </Text>
      ),
      delivery: (
        <Badge
          tone={
            f.delivery_status === "acknowledged"
              ? "success"
              : f.delivery_status === "pending"
                ? "warning"
                : "neutral"
          }
          variant="soft"
          dot={false}
        >
          {f.delivery_status ?? "none"}
        </Badge>
      ),
      filled_at: (
        <Text className="sd-tabular">{formatTimestamp(f.filled_at_ns, "No reading")}</Text>
      ),
    },
  }));

  return (
    <Stack gap={16}>
      <Navbar
        variant="back"
        label={backMeta.label}
        onBack={() => navigate(backLink, { focusReturnId })}
        action={<CopyButton value={order.order_id} presentation="quiet" />}
      />

      <SpaBreadcrumb
        focusReturnId={focusReturnId}
        items={[
          ...backMeta.breadcrumbItems,
          { id: "order", label: `Order ${order.order_id}`, href: `/orders/${order.order_id}` },
        ]}
      />

      <PageHeader
        title={`Order ${order.order_id}`}
        detail={`${order.side.toUpperCase()} ${order.instrument_id} · ${order.book_name} (${order.environment}) · Author: ${order.agent_name}`}
        action={
          <Inline gap={8} align="center" wrap>
            {order.agent_id ? (
              <Button
                emphasis="soft"
                size="compact"
                shape="pill"
                onClick={() => navigate(`/agents/${encodeURIComponent(order.agent_id!)}`)}
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
                  kind: "trading_order",
                  id: order.order_id,
                  label: `Order ${order.order_id} (${order.side.toUpperCase()} ${order.instrument_id})`,
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
              source="trading_book_order + trading_book_fill"
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
                { id: "env", label: "Environment", value: `${order.environment} (${order.tier})` },
                { id: "book", label: "Book", value: order.book_name },
                { id: "inst", label: "Instrument", value: order.instrument_id },
                {
                  id: "acct",
                  label: accountLabel,
                  value: <Text className="sd-mono">{order.account_id}</Text>,
                },
                ...(order.external_order_id
                  ? [
                      {
                        id: "ext_id",
                        label: "External Order ID",
                        value: <Text className="sd-mono">{order.external_order_id}</Text>,
                      },
                    ]
                  : []),
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
                      <Text className="sd-tabular">{order.residual_position_qty}</Text>
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
                  value: formatMinorCurrency(order.limit_minor_units, currency),
                },
                {
                  id: "mkt",
                  label: "Market Price at Order",
                  value: formatMinorCurrency(order.market_price_minor_units, currency),
                },
                {
                  id: "avg_fill",
                  label: "Average Fill Price",
                  value:
                    order.avg_fill_minor_units === null
                      ? "No reading"
                      : formatMinorCurrency(order.avg_fill_minor_units, currency),
                },
                {
                  id: "fees",
                  label: "Total Venue Fees",
                  value: formatMinorCurrency(order.total_fee_minor_units, currency),
                },
                {
                  id: "mandate",
                  label: "Mandate Revision",
                  value: order.mandate_revision ?? "Unavailable",
                },
                {
                  id: "run_id",
                  label: "Work Run ID",
                  value: order.run_id ? (
                    <Text className="sd-mono">{order.run_id}</Text>
                  ) : (
                    "Direct"
                  ),
                },
                {
                  id: "duty",
                  label: "Duty",
                  value: order.duty_id ? (
                    <Text className="sd-mono">
                      {order.duty_id}
                      {order.duty_kind ? ` (${order.duty_kind})` : ""}
                    </Text>
                  ) : order.duty_kind ? (
                    order.duty_kind
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
                  id: "action_id",
                  label: "Action ID",
                  value: <Text className="sd-mono">{order.action_id}</Text>,
                },
                {
                  id: "created_at",
                  label: "Submitted At",
                  value: formatTimestamp(order.created_at_ns, "Unavailable"),
                },
                {
                  id: "cancelled_at",
                  label: "Cancelled At",
                  value:
                    order.cancelled_at_ns !== null
                      ? formatTimestamp(order.cancelled_at_ns, "No reading")
                      : "Not cancelled",
                },
              ]}
            />
          </div>
        </Card>
      </div>

      <div className={order.external_action ? "sd-split-equal" : undefined}>
        <Card title="Cancellation Details" variant="raised">
          {!isCancelled ? (
            <Text variant="help">Not cancelled — no cancellation record on this order.</Text>
          ) : (
            <div className="sd-facts-wrap">
              <RecordFacts
                source="trading_book_order"
                asOf={`seq #${activeData.event_seq}`}
                facts={[
                  {
                    id: "cancel_reason",
                    label: "Cancel Reason",
                    value: order.cancel_reason ?? "Cancelled",
                  },
                  {
                    id: "cancel_outcome",
                    label: "Position State",
                    value: (
                      <Badge tone={order.cancel_not_flat ? "warning" : "neutral"} dot={false}>
                        {order.cancel_not_flat ? "Open residual exposure" : "Flat"}
                      </Badge>
                    ),
                  },
                  {
                    id: "cancel_at",
                    label: "Cancelled At",
                    value: formatTimestamp(order.cancelled_at_ns, "No reading"),
                  },
                  {
                    id: "cancel_filled_qty",
                    label: "Filled Quantity at Cancel",
                    value: String(order.filled_qty),
                  },
                  {
                    id: "cancel_pos_qty",
                    label: "Observed Position Quantity",
                    value: String(order.residual_position_qty),
                  },
                ]}
              />
            </div>
          )}
        </Card>

        {order.external_action ? (
          <Card title="External Venue Action" variant="raised">
            <div className="sd-facts-wrap">
              <RecordFacts
                source="external_actions"
                asOf={`seq #${activeData.event_seq}`}
                facts={[
                  {
                    id: "ext_key",
                    label: "Idempotency Key",
                    value: <Text className="sd-mono">{order.external_action.idempotency_key}</Text>,
                  },
                  {
                    id: "ext_status",
                    label: "Status",
                    value: (
                      <Badge
                        tone={
                          order.external_action.status === "confirmed"
                            ? "success"
                            : order.external_action.status === "rejected"
                              ? "danger"
                              : "warning"
                        }
                        dot={false}
                      >
                        {order.external_action.status}
                      </Badge>
                    ),
                  },
                  {
                    id: "ext_conn",
                    label: "Connector",
                    value: order.external_action.connector,
                  },
                  {
                    id: "ext_op",
                    label: "Operation",
                    value: order.external_action.operation,
                  },
                  ...(order.external_action.receipt !== null &&
                  order.external_action.receipt !== undefined
                    ? [
                        {
                          id: "ext_receipt",
                          label: "Venue Receipt",
                          value: (
                            <Text className="sd-mono">
                              {typeof order.external_action.receipt === "string"
                                ? order.external_action.receipt
                                : JSON.stringify(order.external_action.receipt)}
                            </Text>
                          ),
                        },
                      ]
                    : []),
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
            onClick={() => navigate("/reports")}
          >
            All Reports
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
                    onOpen={() => navigate("/reports")}
                  />
                  <Inline gap={8} align="center" wrap>
                    <Button
                      emphasis="soft"
                      size="compact"
                      shape="pill"
                      onClick={() => navigate("/reports")}
                    >
                      View in Reports
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
