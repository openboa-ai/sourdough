import { Card, List, Text, ThreadRow } from "@openboa/yogurt";
import type { AgentEdgeItem, AgentNodeItem } from "../../../api/types";
import { useRouter } from "../../../state/store";
import {
  edgeKindLabel,
  formatRelativeTimestamp,
  safeIsoFromEpoch,
} from "./layout";

export interface AgentTalksPanelProps {
  totalEdgeCount: number;
  filteredEdges: AgentEdgeItem[];
  nodeById: Map<string, AgentNodeItem>;
}

export function AgentTalksPanel({
  totalEdgeCount,
  filteredEdges,
  nodeById,
}: AgentTalksPanelProps) {
  const { navigate } = useRouter();
  const nameFor = (id: string) => nodeById.get(id)?.display_name ?? id;

  return (
    <Card
      title="Live Inter-Agent Interactions"
      description="Click any thread to observe the full read-only channel in Company Messages"
      variant="raised"
      density="compact"
    >
      {totalEdgeCount === 0 ? (
        <Text variant="help">
          No active peer requests or channel discussions yet. Messages never assign work on their
          own.
        </Text>
      ) : filteredEdges.length === 0 ? (
        <Text variant="help">No inter-agent interactions match the current filter.</Text>
      ) : (
        <List label="Live inter-agent interactions" divided>
          {filteredEdges.map((edge) => {
            const srcName = nameFor(edge.source_agent_id);
            const dstName = nameFor(edge.target_agent_id);
            const isoDate = safeIsoFromEpoch(edge.updated_at_ns);
            return (
              <li key={edge.edge_id} className="yg-list-item">
                <ThreadRow
                  thread={{
                    id: edge.edge_id,
                    title: `${srcName} ↔ ${dstName} · ${edgeKindLabel(edge.kind)}`,
                    preview: edge.latest_preview || edge.label,
                    ...(isoDate
                      ? {
                          updatedAt: {
                            dateTime: isoDate,
                            label: formatRelativeTimestamp(edge.updated_at_ns),
                          },
                        }
                      : {}),
                    unread: edge.active === true,
                  }}
                  onOpen={() =>
                    navigate(`/messages?channel=${encodeURIComponent(edge.channel_id)}`)
                  }
                />
              </li>
            );
          })}
        </List>
      )}
    </Card>
  );
}
