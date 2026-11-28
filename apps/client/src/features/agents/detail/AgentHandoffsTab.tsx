import { useMemo } from "react";
import {
  Badge,
  Button,
  Card,
  DataTable,
  DisclosureRow,
  Inline,
  List,
  Stack,
  Surface,
  Text,
  WorkEvent,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type { AgentDetailProjection } from "../../../api/types";
import { formatNsWithClock } from "../../../lib/format";
import { useRouter } from "../../../state/store";
import {
  handoffStateTone,
  resolveHandoffState,
  workEventStateForHandoff,
} from "./helpers";

type PeerRequestEntry = AgentDetailProjection["tabs"]["peer_requests"][number];

export interface AgentHandoffsTabProps {
  peerRequests: AgentDetailProjection["tabs"]["peer_requests"];
  handoffs: AgentDetailProjection["tabs"]["handoffs"];
}

export function AgentHandoffsTab({
  peerRequests,
  handoffs,
}: AgentHandoffsTabProps) {
  const { navigate, askCeo } = useRouter();

  const peerById = useMemo(
    () => new Map<string, PeerRequestEntry>(peerRequests.map((pr) => [pr.request_id, pr])),
    [peerRequests]
  );

  const peerCols = useMemo<DataColumn[]>(
    () => [
      {
        id: "dir",
        label: "Direction",
        kind: "role",
        sortValue: (row) => peerById.get(row.id)?.direction ?? "",
      },
      {
        id: "peer",
        label: "Counterparty",
        kind: "identity",
        sortValue: (row) => {
          const pr = peerById.get(row.id);
          if (!pr) return "";
          return pr.direction === "outgoing" ? pr.recipient_name : pr.requester_name;
        },
      },
      {
        id: "project",
        label: "Channel",
        kind: "text",
        sortValue: (row) => peerById.get(row.id)?.project_id ?? "",
      },
      {
        id: "question",
        label: "Question & Expected Decision",
        kind: "text",
        sortValue: (row) => peerById.get(row.id)?.question ?? "",
      },
      {
        id: "state",
        label: "State",
        kind: "role",
        sortValue: (row) => peerById.get(row.id)?.state ?? "",
      },
      {
        id: "created",
        label: "Created",
        kind: "numeric",
        align: "end",
        sortValue: (row) => peerById.get(row.id)?.created_at_ns ?? 0,
      },
    ],
    [peerById]
  );

  const peerRows = useMemo<DataRow[]>(
    () =>
      peerRequests.map((pr) => ({
        id: pr.request_id,
        cells: {
          dir: (
            <Badge tone="neutral" variant="outline" dot={false}>
              {pr.direction}
            </Badge>
          ),
          peer: pr.direction === "outgoing" ? pr.recipient_name : pr.requester_name,
          project: pr.project_id ? (
            <Button
              emphasis="quiet"
              size="compact"
              shape="pill"
              onClick={() =>
                navigate(`/messages?channel=${encodeURIComponent(pr.project_id!)}`)
              }
            >
              #{pr.project_id}
            </Button>
          ) : (
            <Text variant="help">Direct</Text>
          ),
          question: (
            <Stack gap={4}>
              <Text variant="body">{pr.question}</Text>
              <Text variant="help">Expected: {pr.expected_decision}</Text>
              {pr.response ? (
                <DisclosureRow label="Response payload" initiallyOpen={false}>
                  <Surface variant="surface" density="compact">
                    <Text as="p" className="sd-mono sd-agent-pre">
                      {JSON.stringify(pr.response, null, 2)}
                    </Text>
                  </Surface>
                </DisclosureRow>
              ) : (
                <Text variant="help">Response: Pending</Text>
              )}
            </Stack>
          ),
          state: (
            <Badge
              tone={
                pr.state === "open"
                  ? "warning"
                  : pr.state === "answered"
                    ? "success"
                    : "neutral"
              }
              dot={false}
            >
              {pr.state}
            </Badge>
          ),
          created: (
            <Text variant="help" className="sd-tabular">
              {formatNsWithClock(pr.created_at_ns, "Unavailable")}
            </Text>
          ),
        },
      })),
    [peerRequests, navigate]
  );

  return (
    <Stack gap={12}>
      <Card title="Blocking Peer Requests" variant="raised" className="sd-ledger-card">
        {peerRows.length === 0 ? (
          <Text variant="help">No peer requests recorded.</Text>
        ) : (
          <DataTable caption="Peer requests" columns={peerCols} rows={peerRows} />
        )}
      </Card>
      <Card title="Work Handoff Offers" variant="raised">
        {handoffs.length === 0 ? (
          <Text variant="help">No work handoffs recorded.</Text>
        ) : (
          <List label="Work handoff offers" divided>
            {handoffs.map((h) => {
              const handoffState = resolveHandoffState(h.state, h.accepted);
              return (
                <li key={h.offer_id} className="yg-list-item">
                  <WorkEvent
                    event={{
                      id: h.offer_id,
                      kind: "handoff",
                      title: `${h.sender_name} → ${h.recipient_name}`,
                      detail: h.expected_result,
                      state: workEventStateForHandoff(h.state, h.accepted),
                    }}
                    inlineMeta={
                      <Inline gap={6} align="center" wrap>
                        <Badge
                          tone={handoffStateTone(h.state, h.accepted)}
                          variant="soft"
                          dot={false}
                        >
                          {handoffState}
                        </Badge>
                        <Badge tone="neutral" variant="outline" dot={false}>
                          {h.direction}
                        </Badge>
                        <Button
                          emphasis="quiet"
                          size="compact"
                          shape="pill"
                          onClick={() => navigate("/reports")}
                        >
                          Asset {h.asset_id}
                        </Button>
                      </Inline>
                    }
                    actions={
                      <Button
                        emphasis="soft"
                        size="compact"
                        shape="pill"
                        onClick={() =>
                          askCeo({
                            kind: "shared_publication",
                            id: h.asset_id,
                            label: `Handoff ${h.offer_id} (${h.asset_id})`,
                          })
                        }
                      >
                        Ask CEO
                      </Button>
                    }
                  />
                </li>
              );
            })}
          </List>
        )}
      </Card>
    </Stack>
  );
}
