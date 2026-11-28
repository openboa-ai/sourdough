import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Button,
  Card,
  EmptyResult,
  FilterBar,
  Inline,
  PageHeader,
  Result,
  SegmentedControl,
  Skeleton,
  Stack,
} from "@openboa/yogurt";
import type { AgentsGraphProjection } from "../../../api/types";
import { useProjection } from "../../../api/useProjection";
import { useRouter } from "../../../state/store";
import { AgentGraphCanvas } from "./AgentGraphCanvas";
import { AgentListTable } from "./AgentListTable";
import { AgentTalksPanel } from "./AgentTalksPanel";
import {
  filterEdges,
  filterMatchingAgentIds,
  groupEdgesByPair,
  summarizeAgentStatuses,
  type StatusFilter,
} from "./layout";

export function AgentsActivityView() {
  const { navigate } = useRouter();
  const { data, loading, error, refetch } =
    useProjection<AgentsGraphProjection>("agents-graph");

  const [viewMode, setViewMode] = useState<"graph" | "list">(
    () => data?.default_view_mode ?? "graph"
  );
  const initializedModeRef = useRef(Boolean(data));
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );

  useEffect(() => {
    if (data && !initializedModeRef.current) {
      setViewMode(data.default_view_mode);
      initializedModeRef.current = true;
    }
  }, [data]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setPrefersReducedMotion(mq.matches);
    onChange();
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const nodes = data?.nodes ?? [];
  const edges = data?.edges ?? [];

  const nodeById = useMemo(
    () => new Map(nodes.map((n) => [n.agent_id, n])),
    [nodes]
  );

  const matchingAgentIds = useMemo(
    () => filterMatchingAgentIds(nodes, query, statusFilter),
    [nodes, query, statusFilter]
  );

  const filteredNodes = useMemo(
    () => nodes.filter((n) => matchingAgentIds.has(n.agent_id)),
    [nodes, matchingAgentIds]
  );

  const hasActiveFilter = query.trim().length > 0 || statusFilter !== "all";

  const pairGroups = useMemo(() => groupEdgesByPair(edges), [edges]);

  const filteredEdges = useMemo(
    () => filterEdges(edges, matchingAgentIds, query, statusFilter),
    [edges, matchingAgentIds, query, statusFilter]
  );

  const { workingCount, waitingCount, readyCount, blockedCount, archivedCount } =
    useMemo(() => summarizeAgentStatuses(nodes), [nodes]);

  const openAgent = useCallback(
    (agentId: string) => {
      navigate(`/agents/${encodeURIComponent(agentId)}`, {
        focusReturnId: agentId,
      });
    },
    [navigate]
  );

  const clearFilter = useCallback(() => {
    setQuery("");
    setStatusFilter("all");
  }, []);

  if (loading && !data) {
    return (
      <Stack gap={16}>
        <PageHeader
          title="Agents & Activity"
          detail="Loading live team interaction graph..."
        />
        <Skeleton label="Loading agents" lines={5} />
      </Stack>
    );
  }

  if (!data) {
    return (
      <Result
        title="Agents & Activity unavailable"
        action={
          <Button emphasis="soft" shape="pill" onClick={refetch}>
            Retry
          </Button>
        }
      >
        {error ?? "Could not load agent graph."}
      </Result>
    );
  }

  return (
    <Stack gap={16}>
      <PageHeader
        title="Agents"
        detail={`${data.nodes.length} ${data.nodes.length === 1 ? "agent" : "agents"} · ${data.edges.length} ${data.edges.length === 1 ? "active interaction" : "active interactions"} · Select any agent to inspect Desk, Authority & Activity`}
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

      <Inline gap={8} align="center" wrap aria-label="Agent team live status summary">
        <Badge tone="brand" variant="soft" dot={false}>
          {workingCount} Working
        </Badge>
        <Badge tone="warning" variant="soft" dot={false}>
          {waitingCount} Waiting
        </Badge>
        <Badge tone="success" variant="soft" dot={false}>
          {readyCount} Ready
        </Badge>
        {blockedCount > 0 ? (
          <Badge tone="danger" variant="soft" dot={false}>
            {blockedCount} Blocked
          </Badge>
        ) : null}
        {archivedCount > 0 ? (
          <Badge tone="neutral" variant="outline" dot={false}>
            {archivedCount} Archived
          </Badge>
        ) : null}
        <Badge tone="neutral" variant="soft" dot={false}>
          {pairGroups.length}{" "}
          {pairGroups.length === 1 ? "Interaction Pair" : "Interaction Pairs"}
        </Badge>
      </Inline>

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
              { id: "working", label: `Working (${workingCount})` },
              { id: "waiting", label: `Waiting (${waitingCount})` },
              { id: "ready", label: `Ready (${readyCount})` },
            ]}
          />
        </div>
      </FilterBar>

      {viewMode === "graph" ? (
        filteredNodes.length === 0 ? (
          <Card variant="raised">
            <EmptyResult query={query || statusFilter} onClear={clearFilter} />
          </Card>
        ) : (
          <AgentGraphCanvas
            companyId={data.company_id}
            nodes={data.nodes}
            edges={data.edges}
            pairGroups={pairGroups}
            nodeById={nodeById}
            matchingAgentIds={matchingAgentIds}
            hasActiveFilter={hasActiveFilter}
            prefersReducedMotion={prefersReducedMotion}
            onOpenAgent={openAgent}
          />
        )
      ) : (
        <AgentListTable
          filteredNodes={filteredNodes}
          nodeById={nodeById}
          query={query}
          statusFilter={statusFilter}
          prefersReducedMotion={prefersReducedMotion}
          onOpenAgent={openAgent}
          onClearFilter={clearFilter}
        />
      )}

      <AgentTalksPanel
        totalEdgeCount={data.edges.length}
        filteredEdges={filteredEdges}
        nodeById={nodeById}
      />
    </Stack>
  );
}
