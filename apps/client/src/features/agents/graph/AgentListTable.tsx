import { useMemo } from "react";
import { ThinkingIndicator } from "@openboa/yogurt/motion-thinking-indicator";
import {
  AgentRow,
  Badge,
  Button,
  Card,
  DataTable,
  EmptyResult,
  Inline,
  Stack,
  Status,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import { normalizeAvatarVariant } from "../../../api/avatar";
import type { AgentNodeItem } from "../../../api/types";
import { formatMinorCurrency } from "../../../lib/format";
import { statusToneFor, type StatusFilter } from "./layout";

export interface AgentListTableProps {
  filteredNodes: AgentNodeItem[];
  nodeById: Map<string, AgentNodeItem>;
  query: string;
  statusFilter: StatusFilter;
  prefersReducedMotion: boolean;
  onOpenAgent: (agentId: string) => void;
  onClearFilter: () => void;
}

export function AgentListTable({
  filteredNodes,
  nodeById,
  query,
  statusFilter,
  prefersReducedMotion,
  onOpenAgent,
  onClearFilter,
}: AgentListTableProps) {
  const listCols = useMemo<DataColumn[]>(
    () => [
      {
        id: "agent",
        label: "Agent & Role",
        kind: "identity",
        sortValue: (row) => nodeById.get(row.id)?.display_name ?? "",
      },
      {
        id: "status",
        label: "Status",
        kind: "role",
        sortValue: (row) => nodeById.get(row.id)?.live_status ?? "",
      },
      {
        id: "task",
        label: "Current Task",
        kind: "text",
        sortValue: (row) => nodeById.get(row.id)?.current_task ?? "",
      },
      {
        id: "desk",
        label: "Private Desk & Budget",
        kind: "numeric",
        align: "end",
        sortValue: (row) => nodeById.get(row.id)?.monthly_budget_minor ?? 0,
      },
      { id: "action", label: "Inspect", kind: "actions", align: "end" },
    ],
    [nodeById]
  );

  const listRows = useMemo<DataRow[]>(
    () =>
      filteredNodes.map((node) => {
        const dutyLabel = node.active_run?.duty_kind ?? "Running work turn";
        return {
          id: node.agent_id,
          cells: {
            agent: (
              <AgentRow
                agent={{
                  id: node.agent_id,
                  name: node.display_name,
                  role: node.role_label,
                  state: node.avatar_state,
                  art: normalizeAvatarVariant(node.avatar_variant),
                  preview: node.grant_scope,
                }}
                onSelect={() => onOpenAgent(node.agent_id)}
              />
            ),
            status: (
              <Status tone={statusToneFor(node.live_status)}>{node.live_status}</Status>
            ),
            task: (
              <Stack gap={4}>
                <Text variant="help">{node.current_task}</Text>
                {node.sandbox_activity ? (
                  <Badge tone="neutral" variant="outline" dot={false}>
                    Trading Sandbox ·{" "}
                    {node.sandbox_activity.fill_count > 0
                      ? `${node.sandbox_activity.fill_count} ${node.sandbox_activity.fill_count === 1 ? "fill" : "fills"}`
                      : "active"}
                  </Badge>
                ) : null}
                {node.live_status === "working" ? (
                  <Inline gap={6} align="center" wrap={false}>
                    <ThinkingIndicator active={!prefersReducedMotion} label={dutyLabel} />
                    <Text variant="help" className="sd-graph-duty-label">
                      {dutyLabel}
                    </Text>
                  </Inline>
                ) : null}
              </Stack>
            ),
            desk: (
              <Stack gap={2} align="end">
                <Text variant="help" className="sd-tabular">
                  {node.private_draft_count}{" "}
                  {node.private_draft_count === 1 ? "private draft" : "private drafts"}
                </Text>
                <Text variant="help" className="sd-tabular">
                  Budget {formatMinorCurrency(node.monthly_budget_minor)}/mo
                </Text>
              </Stack>
            ),
            action: (
              <Button
                data-focus-id={node.agent_id}
                emphasis="soft"
                size="compact"
                shape="pill"
                onClick={() => onOpenAgent(node.agent_id)}
              >
                Open Desk
              </Button>
            ),
          },
        };
      }),
    [filteredNodes, prefersReducedMotion, onOpenAgent]
  );

  return (
    <Card title="All Company Agents" variant="raised" className="sd-ledger-card">
      {filteredNodes.length === 0 ? (
        <EmptyResult query={query || statusFilter} onClear={onClearFilter} />
      ) : (
        <DataTable caption="Agents list" columns={listCols} rows={listRows} />
      )}
    </Card>
  );
}
