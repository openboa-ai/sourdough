import { useEffect, useMemo, useRef, useState } from "react";
import {
  AgentAvatar,
  Badge,
  Button,
  Card,
  DataTable,
  Inline,
  PageHeader,
  Result,
  SegmentedControl,
  Skeleton,
  Stack,
  Status,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import { fetchProjection, formatMinorCurrency } from "../api/client";
import type { AgentNodeItem, AgentsGraphProjection } from "../api/types";
import { useApp } from "../state/store";

function statusToneFor(status: string): "success" | "warning" | "danger" | "neutral" | "brand" {
  if (status === "working") return "brand";
  if (status === "ready") return "success";
  if (status === "waiting") return "warning";
  if (status === "blocked") return "danger";
  return "neutral";
}

function layoutPositions(nodes: AgentNodeItem[]): Record<string, { x: number; y: number }> {
  // Deterministic non-overlapping hub-and-ring layout (percentages of canvas).
  const positions: Record<string, { x: number; y: number }> = {};
  if (nodes.length === 0) return positions;
  if (nodes.length === 1) {
    positions[nodes[0].agent_id] = { x: 50, y: 50 };
    return positions;
  }
  if (nodes.length === 2) {
    positions[nodes[0].agent_id] = { x: 28, y: 50 };
    positions[nodes[1].agent_id] = { x: 72, y: 50 };
    return positions;
  }
  if (nodes.length === 3) {
    positions[nodes[0].agent_id] = { x: 50, y: 24 };
    positions[nodes[1].agent_id] = { x: 24, y: 74 };
    positions[nodes[2].agent_id] = { x: 76, y: 74 };
    return positions;
  }
  if (nodes.length === 4) {
    positions[nodes[0].agent_id] = { x: 24, y: 26 };
    positions[nodes[1].agent_id] = { x: 76, y: 26 };
    positions[nodes[2].agent_id] = { x: 24, y: 76 };
    positions[nodes[3].agent_id] = { x: 76, y: 76 };
    return positions;
  }
  // 5..8 agents: CEO top-center, others distributed evenly on an ellipse
  const ceo = nodes.find((n) => n.is_ceo) ?? nodes[0];
  const others = nodes.filter((n) => n.agent_id !== ceo.agent_id);
  positions[ceo.agent_id] = { x: 50, y: 22 };
  others.forEach((node, idx) => {
    const angle = (Math.PI * (idx + 0.5)) / others.length;
    const x = 50 - Math.cos(angle) * 34;
    const y = 44 + Math.sin(angle) * 38;
    positions[node.agent_id] = { x: Math.round(x), y: Math.round(y) };
  });
  return positions;
}

export function AgentsActivityView() {
  const { navigate, refreshSeq, focusReturnId, clearFocusReturnId, reportApiError } = useApp();
  const [data, setData] = useState<AgentsGraphProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [viewMode, setViewMode] = useState<"graph" | "list">("graph");
  const [hoveredAgent, setHoveredAgent] = useState<string | null>(null);
  const initializedModeRef = useRef(false);
  const nodeButtonRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<AgentsGraphProjection>("agents-graph", {
          signal: controller.signal,
        });
        if (res.data) {
          setData(res.data);
          if (!initializedModeRef.current) {
            setViewMode(res.data.default_view_mode);
            initializedModeRef.current = true;
          }
        }
      } catch (err) {
        if (!controller.signal.aborted) reportApiError(err);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [refreshSeq, reportApiError]);

  // Deterministic focus recovery when returning from full-screen Agent Detail.
  useEffect(() => {
    if (focusReturnId && data) {
      const target = nodeButtonRefs.current[focusReturnId];
      if (target) {
        target.focus();
        clearFocusReturnId();
      }
    }
  }, [focusReturnId, data, clearFocusReturnId]);

  const positions = useMemo(
    () => layoutPositions(data?.nodes ?? []),
    [data?.nodes]
  );

  // Deduplicate graph edges by unordered agent pair (preferring peer_request > handoff > channel)
  // so every connecting line and clickable pill is crisp and never overlaps.
  const graphEdges = useMemo(() => {
    const raw = data?.edges ?? [];
    const priority = { peer_request: 0, handoff: 1, channel: 2 } as const;
    const byPair = new Map<string, (typeof raw)[number]>();
    for (const edge of raw) {
      const key =
        edge.source_agent_id < edge.target_agent_id
          ? `${edge.source_agent_id}::${edge.target_agent_id}`
          : `${edge.target_agent_id}::${edge.source_agent_id}`;
      const existing = byPair.get(key);
      if (!existing || priority[edge.kind] < priority[existing.kind]) {
        byPair.set(key, edge);
      }
    }
    return Array.from(byPair.values()).slice(0, 6);
  }, [data?.edges]);

  if (loading && !data) {
    return (
      <Stack gap={16}>
        <PageHeader title="Agents & Activity" detail="Loading live team interaction graph..." />
        <Skeleton label="Loading agents" lines={5} />
      </Stack>
    );
  }

  if (!data) {
    return <Result title="Agents & Activity unavailable">Could not load agent graph.</Result>;
  }

  const animateAvatars = data.nodes.length < 10;

  const listCols: DataColumn[] = [
    { id: "agent", label: "Agent & Role", kind: "identity" },
    { id: "status", label: "Status", kind: "role" },
    { id: "task", label: "Current Task", kind: "text" },
    { id: "desk", label: "Private Desk & Budget", kind: "numeric", align: "end" },
    { id: "action", label: "Inspect", kind: "actions", align: "end" },
  ];

  const listRows: DataRow[] = data.nodes.map((node) => ({
    id: node.agent_id,
    cells: {
      agent: (
        <Inline gap={12} align="center">
          <AgentAvatar
            agent={node.avatar_variant}
            state={node.avatar_state}
            animated={animateAvatars}
            size={30}
            label={`${node.display_name} (${node.live_status})`}
          />
          <Stack gap={2}>
            <Text variant="body">{node.display_name}</Text>
            <Badge tone={node.is_ceo ? "brand" : "neutral"} dot={false}>
              {node.role_label}
            </Badge>
          </Stack>
        </Inline>
      ),
      status: <Status tone={statusToneFor(node.live_status)}>{node.live_status}</Status>,
      task: node.current_task,
      desk: (
        <Stack gap={2} align="end">
          <Text variant="meta" className="sd-tabular">
            {node.private_draft_count} private drafts
          </Text>
          <Text variant="meta" className="sd-tabular">
            Budget {formatMinorCurrency(node.monthly_budget_minor)}/mo
          </Text>
        </Stack>
      ),
      action: (
        <Button
          emphasis="soft"
          size="compact"
          shape="pill"
          onClick={() =>
            navigate(`/office/agents/${encodeURIComponent(node.agent_id)}`, {
              focusReturnId: node.agent_id,
            })
          }
        >
          Open Desk
        </Button>
      ),
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Agents & Activity"
        detail={`${data.nodes.length} agents · ${data.edges.length} active interactions · Click any agent for Desk, Authority & Activity`}
        action={
          <SegmentedControl
            label="Agents view mode"
            value={viewMode}
            onValueChange={(next) => setViewMode(next as "graph" | "list")}
            options={[
              { id: "graph", label: "Graph" },
              { id: "list", label: "List" },
            ]}
          />
        }
      />

      {viewMode === "graph" ? (
        <div
          className="sd-graph-canvas"
          role="region"
          aria-label="Agent interaction network graph"
        >
          {/* SVG flat dot grid + curved connector edges (zero CSS gradients) */}
          <svg
            className="sd-graph-edges-svg"
            viewBox="0 0 1000 520"
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            <defs>
              <pattern
                id="sd-dot-pattern"
                width="24"
                height="24"
                patternUnits="userSpaceOnUse"
              >
                <circle cx="12" cy="12" r="1" fill="var(--yg-border-strong, #c5c4bd)" opacity="0.45" />
              </pattern>
            </defs>
            <rect width="1000" height="520" fill="url(#sd-dot-pattern)" />

            {graphEdges.map((edge, idx) => {
              const src = positions[edge.source_agent_id];
              const dst = positions[edge.target_agent_id];
              if (!src || !dst) return null;
              const x1 = src.x * 10;
              const y1 = src.y * 5.2;
              const x2 = dst.x * 10;
              const y2 = dst.y * 5.2;
              const mx = (x1 + x2) / 2;
              const my = (y1 + y2) / 2;
              const bend = idx % 2 === 0 ? -24 : 24;
              const cx = mx;
              const cy = my + bend;
              const active =
                hoveredAgent === edge.source_agent_id ||
                hoveredAgent === edge.target_agent_id;
              return (
                <path
                  key={edge.edge_id}
                  d={`M ${x1} ${y1} Q ${cx} ${cy} ${x2} ${y2}`}
                  className="sd-graph-edge-path"
                  data-active={active ? "true" : "false"}
                />
              );
            })}
          </svg>

          {/* Clickable Interaction Edge Pills jumping to /messages */}
          {graphEdges.map((edge, idx) => {
            const src = positions[edge.source_agent_id];
            const dst = positions[edge.target_agent_id];
            if (!src || !dst) return null;
            const leftPct = (src.x + dst.x) / 2;
            const bendPct = idx % 2 === 0 ? -2.5 : 2.5;
            const topPct = (src.y + dst.y) / 2 + bendPct;
            const shortPill =
              edge.label.length > 26 ? `${edge.label.slice(0, 24)}…` : edge.label;
            return (
              <div
                key={`pill-${edge.edge_id}`}
                className="sd-graph-edge-pill-wrap"
                style={{ left: `${leftPct}%`, top: `${topPct}%` }}
              >
                <Button
                  emphasis="soft"
                  size="compact"
                  shape="pill"
                  onClick={() =>
                    navigate(
                      `/messages?channel=${encodeURIComponent(edge.channel_id)}`
                    )
                  }
                >
                  {shortPill}
                </Button>
              </div>
            );
          })}

          {/* Compact Agent Node Cards */}
          {data.nodes.map((node) => {
            const pos = positions[node.agent_id] ?? { x: 50, y: 50 };
            return (
              <div
                key={node.agent_id}
                className="sd-graph-node-wrap"
                style={{ left: `${pos.x}%`, top: `${pos.y}%` }}
                onMouseEnter={() => setHoveredAgent(node.agent_id)}
                onMouseLeave={() => setHoveredAgent(null)}
              >
                <div
                  className="sd-graph-node-card"
                  data-focused={hoveredAgent === node.agent_id ? "true" : "false"}
                >
                  <Inline justify="between" align="center" gap={8}>
                    <Inline gap={8} align="center">
                      <AgentAvatar
                        agent={node.avatar_variant}
                        state={node.avatar_state}
                        animated={animateAvatars}
                        size={34}
                        label={`${node.display_name} (${node.live_status})`}
                      />
                      <Stack gap={2}>
                        <Text variant="body">{node.display_name}</Text>
                        <Badge
                          tone={node.is_ceo ? "brand" : "neutral"}
                          variant="soft"
                          dot={false}
                        >
                          {node.role_label}
                        </Badge>
                      </Stack>
                    </Inline>
                    <Status tone={statusToneFor(node.live_status)}>
                      {node.live_status}
                    </Status>
                  </Inline>

                  <Text variant="help">{node.current_task}</Text>

                  <Inline justify="between" align="center">
                    <Text variant="meta" className="sd-tabular">
                      {node.private_draft_count} private drafts · {node.published_count} reports
                    </Text>
                    <Button
                      ref={(el) => {
                        nodeButtonRefs.current[node.agent_id] = el;
                      }}
                      emphasis="quiet"
                      size="compact"
                      shape="pill"
                      onClick={() =>
                        navigate(
                          `/office/agents/${encodeURIComponent(node.agent_id)}`,
                          { focusReturnId: node.agent_id }
                        )
                      }
                    >
                      Inspect
                    </Button>
                  </Inline>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <Card title="All Company Agents" variant="outlined">
          <DataTable caption="Agents list" columns={listCols} rows={listRows} />
        </Card>
      )}

      {/* Active Interactions Summary Strip */}
      <Card
        title="Live Inter-Agent Interactions"
        description="Click any thread to observe the full read-only channel in Company Messages"
        variant="outlined"
        density="compact"
      >
        {data.edges.length === 0 ? (
          <Text variant="help">
            No active peer requests or channel discussions yet. Messages never assign work on their own.
          </Text>
        ) : (
          <Stack gap={8}>
            {data.edges.map((edge) => {
              const srcName =
                data.nodes.find((n) => n.agent_id === edge.source_agent_id)?.display_name ??
                edge.source_agent_id;
              const dstName =
                data.nodes.find((n) => n.agent_id === edge.target_agent_id)?.display_name ??
                edge.target_agent_id;
              return (
                <Inline key={edge.edge_id} justify="between" align="center" wrap>
                  <Inline gap={8} align="center" wrap>
                    <Badge tone="neutral" variant="outline" dot={false}>
                      {edge.kind.replace("_", " ")}
                    </Badge>
                    <Text variant="body">
                      {srcName} ↔ {dstName}
                    </Text>
                    <Text variant="meta">{edge.label}</Text>
                  </Inline>
                  <Button
                    emphasis="quiet"
                    size="compact"
                    shape="pill"
                    onClick={() =>
                      navigate(`/messages?channel=${encodeURIComponent(edge.channel_id)}`)
                    }
                  >
                    Open Thread
                  </Button>
                </Inline>
              );
            })}
          </Stack>
        )}
      </Card>
    </Stack>
  );
}
