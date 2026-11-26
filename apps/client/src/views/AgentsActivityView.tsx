import { useEffect, useMemo, useRef, useState } from "react";
import { ThinkingIndicator } from "@openboa/yogurt/motion-thinking-indicator";
import {
  AgentAvatar,
  AgentRow,
  Badge,
  Button,
  Card,
  DataTable,
  EmptyResult,
  FilterBar,
  Inline,
  List,
  PageHeader,
  Popover,
  PopoverDescription,
  PopoverTitle,
  Result,
  SegmentedControl,
  Skeleton,
  Stack,
  Status,
  Text,
  ThreadRow,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import { fetchProjection, formatMinorCurrency, formatRelativeNs } from "../api/client";
import type { AgentEdgeItem, AgentNodeItem, AgentsGraphProjection } from "../api/types";
import { useApp } from "../state/store";

type StatusFilter = "all" | "working" | "waiting" | "ready";

interface NodePos {
  x: number;
  y: number;
  cluster: string;
}

interface ClusterBox {
  id: string;
  label: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

function statusToneFor(status: string): "success" | "warning" | "danger" | "neutral" | "brand" {
  if (status === "working") return "brand";
  if (status === "ready") return "success";
  if (status === "waiting") return "warning";
  if (status === "blocked") return "danger";
  return "neutral";
}

function clusterForAgent(node: AgentNodeItem): string {
  if (node.is_ceo) return "Executive";
  const text = `${node.role_label} ${node.grant_scope}`.toLowerCase();
  if (text.includes("quant") || text.includes("research") || text.includes("alpha")) {
    return "Research";
  }
  if (text.includes("risk") || text.includes("settle") || text.includes("compliance")) {
    return "Risk & Settlement";
  }
  return "Operations & QA";
}

function layoutClusteredGraph(nodes: AgentNodeItem[]): {
  positions: Record<string, NodePos>;
  clusters: ClusterBox[];
  canvasHeight: number;
} {
  const positions: Record<string, NodePos> = {};
  const clusters: ClusterBox[] = [];
  if (nodes.length === 0) return { positions, clusters, canvasHeight: 560 };

  if (nodes.length <= 4) {
    const coords =
      nodes.length === 1
        ? [{ x: 50, y: 50 }]
        : nodes.length === 2
          ? [
              { x: 26, y: 50 },
              { x: 74, y: 50 },
            ]
          : nodes.length === 3
            ? [
                { x: 50, y: 24 },
                { x: 24, y: 74 },
                { x: 76, y: 74 },
              ]
            : [
                { x: 24, y: 26 },
                { x: 76, y: 26 },
                { x: 24, y: 75 },
                { x: 76, y: 75 },
              ];
    nodes.forEach((node, idx) => {
      positions[node.agent_id] = {
        x: coords[idx].x,
        y: coords[idx].y,
        cluster: clusterForAgent(node),
      };
    });
    return { positions, clusters, canvasHeight: 560 };
  }

  // 5+ agents: semantic hub-and-cluster layout grouped by functional cluster.
  const grouped = new Map<string, AgentNodeItem[]>();
  for (const node of nodes) {
    const key = clusterForAgent(node);
    const list = grouped.get(key) ?? [];
    list.push(node);
    grouped.set(key, list);
  }

  const execGroup = grouped.get("Executive") ?? [];
  const otherKeys = Array.from(grouped.keys()).filter((k) => k !== "Executive");
  const maxMembers = Math.max(
    1,
    ...otherKeys.map((k) => grouped.get(k)?.length ?? 0)
  );
  const canvasHeight = Math.min(920, 760 + Math.max(0, maxMembers - 2) * 150);

  if (execGroup.length > 0) {
    execGroup.forEach((node, idx) => {
      const span = execGroup.length === 1 ? 0 : (idx - (execGroup.length - 1) / 2) * 26;
      positions[node.agent_id] = { x: 50 + span, y: 20, cluster: "Executive" };
    });
    clusters.push({
      id: "cluster-exec",
      label: "Executive",
      x: 345,
      y: 12,
      w: 310,
      h: Math.round(canvasHeight * 0.33),
    });
  }

  const colCount = Math.max(1, otherKeys.length);
  otherKeys.forEach((key, colIdx) => {
    const members = grouped.get(key) ?? [];
    const colCenterX =
      colCount === 3
        ? [17, 50, 83][colIdx]
        : colCount === 2
          ? [25, 75][colIdx]
          : ((colIdx + 0.5) / colCount) * 100;
    const clampedX = Math.max(16, Math.min(84, colCenterX));
    const startY = execGroup.length > 0 ? 58 : 26;
    const endY = 87;
    members.forEach((node, rowIdx) => {
      const y =
        members.length === 1
          ? (startY + endY) / 2
          : startY + (rowIdx / (members.length - 1)) * (endY - startY);
      positions[node.agent_id] = { x: clampedX, y, cluster: key };
    });

    const boxW = colCount === 3 ? 270 : Math.floor(920 / colCount) - 24;
    const boxX = Math.round(clampedX * 10 - boxW / 2);
    const boxY = Math.round(canvasHeight * (execGroup.length > 0 ? 0.38 : 0.06));
    const boxH = Math.round(canvasHeight * (execGroup.length > 0 ? 0.60 : 0.88));
    clusters.push({
      id: `cluster-${key}`,
      label: key,
      x: Math.max(14, boxX),
      y: boxY,
      w: Math.max(240, boxW),
      h: boxH,
    });
  });

  return { positions, clusters, canvasHeight };
}

function edgeKindLabel(kind: AgentEdgeItem["kind"]): string {
  if (kind === "peer_request") return "Peer Request";
  if (kind === "handoff") return "Work Handoff";
  return "Internal Channel";
}

export function AgentsActivityView() {
  const { navigate, refreshSeq, focusReturnId, clearFocusReturnId, reportApiError } = useApp();
  const [data, setData] = useState<AgentsGraphProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [viewMode, setViewMode] = useState<"graph" | "list">("graph");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
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

  useEffect(() => {
    if (focusReturnId && data) {
      const target = nodeButtonRefs.current[focusReturnId];
      if (target) {
        target.focus();
        clearFocusReturnId();
      }
    }
  }, [focusReturnId, data, clearFocusReturnId]);

  const matchingAgentIds = useMemo(() => {
    const nodes = data?.nodes ?? [];
    const q = query.trim().toLowerCase();
    const matched = new Set<string>();
    for (const node of nodes) {
      const statusOk =
        statusFilter === "all" ||
        (statusFilter === "ready"
          ? node.live_status === "ready" || node.live_status === "idle"
          : node.live_status === statusFilter);
      const queryOk =
        !q ||
        `${node.display_name} ${node.role_label} ${node.current_task} ${node.grant_scope}`
          .toLowerCase()
          .includes(q);
      if (statusOk && queryOk) matched.add(node.agent_id);
    }
    return matched;
  }, [data?.nodes, query, statusFilter]);

  const filteredNodes = useMemo(
    () => (data?.nodes ?? []).filter((n) => matchingAgentIds.has(n.agent_id)),
    [data?.nodes, matchingAgentIds]
  );

  const hasActiveFilter = query.trim().length > 0 || statusFilter !== "all";

  const { positions, clusters, canvasHeight } = useMemo(
    () => layoutClusteredGraph(data?.nodes ?? []),
    [data?.nodes]
  );

  const graphEdges = useMemo(() => {
    const raw = data?.edges ?? [];
    const priority = { peer_request: 0, handoff: 1, channel: 2 } as const;
    const byPair = new Map<string, AgentEdgeItem>();
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
    return Array.from(byPair.values()).slice(0, 8);
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

  const listRows: DataRow[] = filteredNodes.map((node) => ({
    id: node.agent_id,
    cells: {
      agent: (
        <AgentRow
          agent={{
            id: node.agent_id,
            name: node.display_name,
            role: node.role_label,
            state: node.avatar_state,
            art: node.avatar_variant,
            preview: node.grant_scope,
          }}
          onSelect={() =>
            navigate(`/office/agents/${encodeURIComponent(node.agent_id)}`, {
              focusReturnId: node.agent_id,
            })
          }
        />
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

  const nameFor = (id: string) =>
    data.nodes.find((n) => n.agent_id === id)?.display_name ?? id;

  return (
    <Stack gap={16}>
      <PageHeader
        title="Agents & Activity"
        detail={`${data.nodes.length} agents · ${data.edges.length} active interactions · Click any agent for Desk, Authority & Activity`}
        action={
          <div className="sd-intrinsic">
            <SegmentedControl
              label="Agents view mode"
              value={viewMode}
              onValueChange={(next) => setViewMode(next as "graph" | "list")}
              options={[
                { id: "graph", label: "Graph" },
                { id: "list", label: "List" },
              ]}
            />
          </div>
        }
      />

      <FilterBar
        label="Filter agents"
        hiddenLabel
        placeholder="Filter agents by name, role, or current task..."
        size="compact"
        query={query}
        onQueryChange={setQuery}
      >
        <div className="sd-intrinsic">
          <SegmentedControl
            label="Filter agents by live status"
            value={statusFilter}
            onValueChange={(next) => setStatusFilter(next as StatusFilter)}
            options={[
              { id: "all", label: `All (${data.nodes.length})` },
              {
                id: "working",
                label: `Working (${data.nodes.filter((n) => n.live_status === "working").length})`,
              },
              {
                id: "waiting",
                label: `Waiting (${data.nodes.filter((n) => n.live_status === "waiting").length})`,
              },
              {
                id: "ready",
                label: `Ready (${data.nodes.filter((n) => n.live_status === "ready" || n.live_status === "idle").length})`,
              },
            ]}
          />
        </div>
      </FilterBar>

      {viewMode === "graph" ? (
        <div
          className="sd-graph-canvas"
          style={{ minBlockSize: `${canvasHeight}px` }}
          role="region"
          aria-label="Agent interaction network graph"
        >
          <svg
            className="sd-graph-edges-svg"
            viewBox={`0 0 1000 ${canvasHeight}`}
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
                <circle cx="12" cy="12" r="1" fill="var(--yg-border-chrome)" />
              </pattern>
            </defs>
            <rect width="1000" height={canvasHeight} fill="url(#sd-dot-pattern)" />

            {clusters.map((box) => (
              <g key={box.id}>
                <rect
                  x={box.x}
                  y={box.y}
                  width={box.w}
                  height={box.h}
                  rx={16}
                  className="sd-graph-cluster-ring"
                />
                <text x={box.x + 14} y={box.y + 20} className="sd-graph-cluster-label">
                  {box.label}
                </text>
              </g>
            ))}

            {graphEdges.map((edge, idx) => {
              const src = positions[edge.source_agent_id];
              const dst = positions[edge.target_agent_id];
              if (!src || !dst) return null;
              const x1 = src.x * 10;
              const y1 = (src.y / 100) * canvasHeight;
              const x2 = dst.x * 10;
              const y2 = (dst.y / 100) * canvasHeight;
              const mx = (x1 + x2) / 2;
              const my = (y1 + y2) / 2;
              const bend = idx % 2 === 0 ? -28 : 28;
              const cx = mx;
              const cy = my + bend;
              const pathD = `M ${x1} ${y1} Q ${cx} ${cy} ${x2} ${y2}`;
              const focused =
                hoveredAgent === edge.source_agent_id ||
                hoveredAgent === edge.target_agent_id;
              const isActive = edge.active !== false;
              return (
                <g key={edge.edge_id}>
                  <path
                    d={pathD}
                    className="sd-graph-edge-base"
                    data-focused={focused ? "true" : "false"}
                  />
                  {isActive ? (
                    <path
                      d={pathD}
                      className="sd-graph-edge-pulse"
                      data-focused={focused ? "true" : "false"}
                    />
                  ) : null}
                </g>
              );
            })}
          </svg>

          {graphEdges.map((edge, idx) => {
            const src = positions[edge.source_agent_id];
            const dst = positions[edge.target_agent_id];
            if (!src || !dst) return null;
            const leftPct = (src.x + dst.x) / 2;
            const bendPct = idx % 2 === 0 ? -2.6 : 2.6;
            const topPct = (src.y + dst.y) / 2 + bendPct;
            const shortPill =
              edge.label.length > 26 ? `${edge.label.slice(0, 24)}…` : edge.label;
            const srcName = nameFor(edge.source_agent_id);
            const dstName = nameFor(edge.target_agent_id);
            return (
              <div
                key={`pill-${edge.edge_id}`}
                className="sd-graph-edge-pill-wrap"
                style={{ left: `${leftPct}%`, top: `${topPct}%` }}
              >
                <Popover
                  label={`Preview interaction between ${srcName} and ${dstName}`}
                  align="center"
                  trigger={
                    <Button emphasis="soft" size="compact" shape="pill">
                      {shortPill}
                    </Button>
                  }
                >
                  <Stack gap={8}>
                    <Inline justify="between" align="center" gap={8}>
                      <Badge
                        tone={edge.kind === "peer_request" ? "warning" : "brand"}
                        variant="soft"
                        dot={false}
                      >
                        {edgeKindLabel(edge.kind)}
                      </Badge>
                      {edge.message_count ? (
                        <Text variant="meta" className="sd-tabular">
                          {edge.message_count} {edge.message_count === 1 ? "msg" : "msgs"}
                        </Text>
                      ) : null}
                    </Inline>
                    <PopoverTitle>
                      {srcName} ↔ {dstName}
                    </PopoverTitle>
                    <PopoverDescription>
                      {edge.latest_sender_name ? `${edge.latest_sender_name}: ` : ""}
                      {edge.latest_preview || edge.label}
                    </PopoverDescription>
                    <Inline justify="between" align="center" gap={8}>
                      <Text variant="meta" className="sd-tabular">
                        {formatRelativeNs(edge.updated_at_ns)}
                      </Text>
                      <Button
                        emphasis="filled"
                        tone="brand"
                        size="compact"
                        shape="pill"
                        onClick={() =>
                          navigate(
                            `/messages?channel=${encodeURIComponent(edge.channel_id)}`
                          )
                        }
                      >
                        Open Thread →
                      </Button>
                    </Inline>
                  </Stack>
                </Popover>
              </div>
            );
          })}

          {data.nodes.map((node) => {
            const pos = positions[node.agent_id] ?? { x: 50, y: 50, cluster: "Operations" };
            const dimmed = hasActiveFilter && !matchingAgentIds.has(node.agent_id);
            return (
              <div
                key={node.agent_id}
                className="sd-graph-node-wrap"
                data-dense={data.nodes.length >= 5 ? "true" : "false"}
                style={{ left: `${pos.x}%`, top: `${pos.y}%` }}
                onMouseEnter={() => setHoveredAgent(node.agent_id)}
                onMouseLeave={() => setHoveredAgent(null)}
              >
                <article
                  className="sd-graph-node-card"
                  data-focused={hoveredAgent === node.agent_id ? "true" : "false"}
                  data-dimmed={dimmed ? "true" : "false"}
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

                  {node.live_status === "working" ? (
                    <ThinkingIndicator
                      active
                      label={node.active_run?.duty_kind ?? "Running work turn"}
                    />
                  ) : null}

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
                </article>
              </div>
            );
          })}
        </div>
      ) : (
        <Card title="All Company Agents" variant="raised">
          {filteredNodes.length === 0 ? (
            <EmptyResult
              query={query || statusFilter}
              onClear={() => {
                setQuery("");
                setStatusFilter("all");
              }}
            />
          ) : (
            <DataTable caption="Agents list" columns={listCols} rows={listRows} />
          )}
        </Card>
      )}

      <Card
        title="Live Inter-Agent Interactions"
        description="Click any thread to observe the full read-only channel in Company Messages"
        variant="raised"
        density="compact"
      >
        {data.edges.length === 0 ? (
          <Text variant="help">
            No active peer requests or channel discussions yet. Messages never assign work on their own.
          </Text>
        ) : (
          <List label="Live inter-agent interactions" divided>
            {data.edges.map((edge) => {
              const srcName = nameFor(edge.source_agent_id);
              const dstName = nameFor(edge.target_agent_id);
              return (
                <ThreadRow
                  key={edge.edge_id}
                  thread={{
                    id: edge.edge_id,
                    title: `${srcName} ↔ ${dstName} · ${edgeKindLabel(edge.kind)}`,
                    preview: edge.latest_preview || edge.label,
                    updatedAt: {
                      dateTime: new Date(Math.floor(edge.updated_at_ns / 1_000_000)).toISOString(),
                      label: formatRelativeNs(edge.updated_at_ns),
                    },
                    unread: edge.active === true,
                  }}
                  onOpen={() =>
                    navigate(`/messages?channel=${encodeURIComponent(edge.channel_id)}`)
                  }
                />
              );
            })}
          </List>
        )}
      </Card>
    </Stack>
  );
}
