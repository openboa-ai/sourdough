import { useEffect, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Breadcrumb,
  Button,
  Card,
  DataTable,
  Inline,
  Navbar,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Skeleton,
  Stack,
  Text,
  Trace,
  type DataColumn,
  type DataRow,
  type TraceEvent,
} from "@openboa/yogurt";
import { fetchProjection, formatMinorCurrency } from "../api/client";
import type { OrderTraceProjection } from "../api/types";
import { useApp } from "../state/store";

export function OrderTraceView({ orderId }: { orderId: string }) {
  const { navigate, refreshSeq, reportApiError } = useApp();
  const [data, setData] = useState<OrderTraceProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<OrderTraceProjection>(
          `orders/${encodeURIComponent(orderId)}`,
          { signal: controller.signal }
        );
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!controller.signal.aborted) {
          reportApiError(err);
          setError(err instanceof Error ? err.message : "Order trace not found");
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [orderId, refreshSeq, reportApiError]);

  if (loading && !data) {
    return <Skeleton label="Loading order causal lineage" lines={5} />;
  }

  if (!data) {
    return (
      <Result
        title="Order trace not found"
        action={
          <Button emphasis="soft" shape="pill" onClick={() => navigate("/")}>
            Back to Live Trading
          </Button>
        }
      >
        {error ?? `Order ${orderId} does not exist in this company store.`}
      </Result>
    );
  }

  const order = data.order;
  const traceEvents: TraceEvent[] = data.causal_steps.map((step, idx) => ({
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

  const fillCols: DataColumn[] = [
    { id: "fill_id", label: "Fill ID", kind: "identity" },
    { id: "qty", label: "Quantity", kind: "numeric", align: "end" },
    { id: "price", label: "Fill Price", kind: "numeric", align: "end" },
    { id: "fee", label: "Venue Fee", kind: "numeric", align: "end" },
    { id: "pos", label: "Observed Position", kind: "numeric", align: "end" },
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
    },
  }));

  return (
    <Stack gap={16}>
      <Navbar
        variant="back"
        label="Back to Live Trading"
        onBack={() => navigate("/")}
        action={<CopyButton value={order.order_id} presentation="quiet" />}
      />

      <Breadcrumb
        items={[
          { id: "home", label: "Live Trading", href: "/" },
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
                  navigate(`/office/agents/${encodeURIComponent(order.agent_id!)}`)
                }
              >
                Inspect {order.agent_name}
              </Button>
            ) : null}
            <Button
              emphasis="filled"
              tone="brand"
              size="compact"
              shape="pill"
              onClick={() =>
                navigate("/messages", {
                  contextRef: {
                    kind: "paper_order",
                    id: order.order_id,
                    label: `Order ${order.order_id} (${order.action.toUpperCase()} ${order.instrument})`,
                  },
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
          <RecordFacts
            source="paper_order + paper_fill + paper_cancellation"
            asOf={`seq #${data.event_seq}`}
            facts={[
              { id: "inst", label: "Instrument", value: order.instrument },
              {
                id: "qty",
                label: "Ordered / Filled / Remaining",
                value: `${order.ordered_qty} / ${order.filled_qty} / ${order.remaining_qty}`,
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
                id: "fees",
                label: "Total Venue Fees",
                value: formatMinorCurrency(order.total_fee_minor_units),
              },
              { id: "mandate", label: "Mandate Revision", value: order.mandate_revision },
            ]}
          />
        </Card>
      </div>

      <Card title="Recorded Fills & Fees" variant="raised">
        {order.fills.length === 0 ? (
          <Text variant="help">No fills recorded for this order.</Text>
        ) : (
          <DataTable caption="Order fills" columns={fillCols} rows={fillRows} />
        )}
      </Card>
    </Stack>
  );
}
