import { useMemo } from "react";
import {
  Badge,
  Button,
  Card,
  DataTable,
  Notice,
  RecordFacts,
  Stack,
  Text,
  type DataColumn,
  type DataRow,
  type Fact,
} from "@openboa/yogurt";
import type { AgentSandboxPortfolioDetail } from "../../../api/types";
import { formatMinorCurrency } from "../../../lib/format";
import { useRouter } from "../../../state/store";

export interface AgentSandboxTabProps {
  agentDisplayName: string;
  eventSeq: number;
  sandbox: AgentSandboxPortfolioDetail;
}

const SANDBOX_POS_COLS: DataColumn[] = [
  { id: "instrument", label: "Instrument", kind: "identity" },
  { id: "qty", label: "Quantity", kind: "numeric", align: "end" },
  { id: "entry", label: "Avg Entry", kind: "numeric", align: "end" },
  { id: "pnl", label: "Realized P&L", kind: "numeric", align: "end" },
  { id: "fees", label: "Fees", kind: "numeric", align: "end" },
];

const SANDBOX_ORDER_COLS: DataColumn[] = [
  { id: "order", label: "Order & Instrument", kind: "identity" },
  { id: "side", label: "Side / Qty", kind: "numeric", align: "end" },
  { id: "price", label: "Fill / Limit", kind: "numeric", align: "end" },
  { id: "fee", label: "Fee", kind: "numeric", align: "end" },
  { id: "status", label: "Status", kind: "role" },
];

export function AgentSandboxTab({
  agentDisplayName,
  eventSeq,
  sandbox,
}: AgentSandboxTabProps) {
  const { navigate } = useRouter();
  const sandboxCurrency = sandbox.binding.currency || "USD";

  const sandboxFacts = useMemo<Fact[]>(
    () => [
      {
        id: "sbx-acct",
        label: "Sandbox Account",
        value: <Text className="sd-mono">{sandbox.binding.account_id}</Text>,
      },
      {
        id: "sbx-cash",
        label: "Virtual Cash",
        value: (
          <Text className="sd-tabular">
            {formatMinorCurrency(sandbox.cash_minor_units, sandboxCurrency)}
          </Text>
        ),
      },
      {
        id: "sbx-exp",
        label: "Gross Exposure / Cap",
        value: (
          <Text className="sd-tabular">
            {formatMinorCurrency(sandbox.gross_exposure_minor_units, sandboxCurrency)} /{" "}
            {formatMinorCurrency(sandbox.exposure_limit_minor_units, sandboxCurrency)}
          </Text>
        ),
      },
      {
        id: "sbx-pnl",
        label: "Realized P&L",
        value: (
          <Text
            className={`sd-tabular ${
              sandbox.realized_pnl_minor_units > 0
                ? "sd-tone-pos"
                : sandbox.realized_pnl_minor_units < 0
                  ? "sd-tone-neg"
                  : ""
            }`}
          >
            {formatMinorCurrency(sandbox.realized_pnl_minor_units, sandboxCurrency, {
              signed: true,
            })}
          </Text>
        ),
      },
      {
        id: "sbx-fees",
        label: "Simulated Fees & Slippage",
        value: (
          <Text className="sd-tabular">
            {formatMinorCurrency(sandbox.total_fees_minor_units, sandboxCurrency)} (
            {sandbox.binding.fee_bps} bps fee · {sandbox.binding.slippage_bps} bps slip)
          </Text>
        ),
      },
    ],
    [sandbox, sandboxCurrency]
  );

  const sandboxPosRows = useMemo<DataRow[]>(
    () =>
      sandbox.positions.map((p) => ({
        id: p.instrument_id,
        cells: {
          instrument: <Text variant="body">{p.instrument_id}</Text>,
          qty: <Text className="sd-tabular">{p.quantity}</Text>,
          entry: (
            <Text className="sd-tabular">
              {formatMinorCurrency(p.avg_entry_minor_units, sandboxCurrency)}
            </Text>
          ),
          pnl: (
            <Text className="sd-tabular">
              {formatMinorCurrency(p.realized_pnl_minor_units, sandboxCurrency, {
                signed: true,
              })}
            </Text>
          ),
          fees: (
            <Text className="sd-tabular">
              {formatMinorCurrency(p.total_fees_minor_units, sandboxCurrency)}
            </Text>
          ),
        },
      })),
    [sandbox.positions, sandboxCurrency]
  );

  const sandboxOrderRows = useMemo<DataRow[]>(
    () =>
      sandbox.orders.map((o) => ({
        id: o.order_id,
        cells: {
          order: (
            <Stack gap={2}>
              <Text variant="body">{o.instrument_id}</Text>
              <Text variant="meta" className="sd-mono">
                {o.order_id}
              </Text>
            </Stack>
          ),
          side: (
            <Text className="sd-tabular">
              {o.side.toUpperCase()} {o.filled_quantity}/{o.quantity}
            </Text>
          ),
          price: (
            <Text className="sd-tabular">
              {formatMinorCurrency(
                o.fill_price_minor_units ?? o.limit_minor_units,
                sandboxCurrency
              )}
            </Text>
          ),
          fee: (
            <Text className="sd-tabular">
              {formatMinorCurrency(o.fee_minor_units, sandboxCurrency)}
            </Text>
          ),
          status: (
            <Badge
              tone={
                o.status === "filled" ? "success" : o.status === "open" ? "brand" : "neutral"
              }
              dot={false}
            >
              {o.status}
            </Badge>
          ),
        },
      })),
    [sandbox.orders, sandboxCurrency]
  );

  return (
    <Stack gap={12}>
      <Notice title="Isolated personal trading sandbox" tone="neutral">
        This virtual account belongs exclusively to {agentDisplayName} for strategy testing.
        Orders here never touch shared company paper books or live venues.
      </Notice>
      <Stack gap={12}>
        <Card
          title="Sandbox Portfolio Summary"
          description={`Binding ${sandbox.binding.binding_id} · Quote source ${sandbox.binding.quote_source}`}
          variant="raised"
          headerAction={
            <Button
              emphasis="quiet"
              size="compact"
              shape="pill"
              onClick={() => navigate("/trading/paper")}
            >
              Paper Trading
            </Button>
          }
        >
          <div className="sd-facts-wrap">
            <RecordFacts
              source="execution_environment_binding + trading_book"
              asOf={`seq #${eventSeq}`}
              facts={sandboxFacts}
            />
          </div>
        </Card>
        <div className="sd-split-equal">
          <Card title="Sandbox Positions" variant="raised">
            {sandboxPosRows.length === 0 ? (
              <Text variant="help">No sandbox positions recorded yet.</Text>
            ) : (
              <DataTable
                caption="Agent personal trading sandbox positions"
                columns={SANDBOX_POS_COLS}
                rows={sandboxPosRows}
              />
            )}
          </Card>
          <Card title="Recent Sandbox Orders" variant="raised">
            {sandboxOrderRows.length === 0 ? (
              <Text variant="help">No sandbox orders recorded yet.</Text>
            ) : (
              <DataTable
                caption="Agent personal trading sandbox orders"
                columns={SANDBOX_ORDER_COLS}
                rows={sandboxOrderRows}
              />
            )}
          </Card>
        </div>
      </Stack>
    </Stack>
  );
}
