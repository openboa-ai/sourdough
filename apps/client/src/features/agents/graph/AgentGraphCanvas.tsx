import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { ThinkingIndicator } from "@openboa/yogurt/motion-thinking-indicator";
import {
  AgentAvatar,
  Badge,
  Button,
  Divider,
  Inline,
  Popover,
  PopoverDescription,
  PopoverTitle,
  ScrollArea,
  Stack,
  Status,
  Surface,
  Text,
} from "@openboa/yogurt";
import { normalizeAvatarVariant } from "../../../api/avatar";
import type { AgentEdgeItem, AgentNodeItem } from "../../../api/types";
import { formatMinorCurrency } from "../../../lib/format";
import { useRouter } from "../../../state/store";
import {
  buildGraphRows,
  compactAgentTag,
  computeClusterBoxes,
  computeConnectedNeighbors,
  computeGraphEdgeLayout,
  edgeKindLabel,
  edgeKindTone,
  formatRelativeTimestamp,
  pillDisplayText,
  statusToneFor,
  tierFromWidth,
  type LayoutTier,
  type PairEdgeGroup,
  type Rect,
} from "./layout";

export interface AgentGraphCanvasProps {
  companyId: string;
  nodes: AgentNodeItem[];
  edges: AgentEdgeItem[];
  pairGroups: PairEdgeGroup[];
  nodeById: Map<string, AgentNodeItem>;
  matchingAgentIds: Set<string>;
  hasActiveFilter: boolean;
  prefersReducedMotion: boolean;
  onOpenAgent: (agentId: string) => void;
}

export function AgentGraphCanvas({
  companyId,
  nodes,
  edges,
  pairGroups,
  nodeById,
  matchingAgentIds,
  hasActiveFilter,
  prefersReducedMotion,
  onOpenAgent,
}: AgentGraphCanvasProps) {
  const { navigate, focusReturnId, clearFocusReturnId, askCeo } = useRouter();
  const [hoveredAgent, setHoveredAgent] = useState<string | null>(null);
  const [focusedAgent, setFocusedAgent] = useState<string | null>(null);
  const [activeCellId, setActiveCellId] = useState<string | null>(null);
  const [geometry, setGeometry] = useState<{
    canvasW: number;
    canvasH: number;
    stageBottom: number;
    tier: LayoutTier;
    nodeRects: Record<string, Rect>;
  }>(() => ({
    canvasW: 0,
    canvasH: 0,
    stageBottom: 0,
    tier:
      typeof window !== "undefined"
        ? window.innerWidth < 680
          ? "mobile"
          : window.innerWidth < 1100
            ? "tablet"
            : "desktop"
        : "desktop",
    nodeRects: {},
  }));

  const canvasRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const gridRef = useRef<HTMLDivElement | null>(null);
  const cardRefs = useRef<Record<string, HTMLDivElement | null>>({});

  const { rows, navRows, clusterMembers } = useMemo(
    () => buildGraphRows(nodes, geometry.tier),
    [nodes, geometry.tier]
  );

  useEffect(() => {
    const flat = navRows.flat();
    if (flat.length === 0) return;
    if (focusReturnId && flat.some((n) => n.agent_id === focusReturnId)) {
      setActiveCellId(focusReturnId);
      return;
    }
    setActiveCellId((prev) =>
      prev && flat.some((n) => n.agent_id === prev) ? prev : flat[0].agent_id
    );
  }, [navRows, focusReturnId]);

  const focusGridCell = useCallback((agentId: string): boolean => {
    const escaped =
      typeof CSS !== "undefined" && typeof CSS.escape === "function"
        ? CSS.escape(agentId)
        : agentId.replace(/"/g, '\\"');
    const el = gridRef.current?.querySelector<HTMLElement>(`[data-focus-id="${escaped}"]`);
    if (!el) return false;
    el.focus();
    return true;
  }, []);

  useEffect(() => {
    if (focusReturnId && nodes.length > 0) {
      if (focusGridCell(focusReturnId)) {
        clearFocusReturnId();
      }
    }
  }, [focusReturnId, nodes.length, clearFocusReturnId, focusGridCell]);

  const isClustered = nodes.length >= 5 && geometry.tier === "desktop";

  const measureGeometry = useCallback(() => {
    const canvasEl = canvasRef.current;
    if (!canvasEl) return;
    const canvasW = canvasEl.clientWidth;
    const canvasH = canvasEl.clientHeight;
    if (canvasW === 0 || canvasH === 0) return;
    const canvasRect = canvasEl.getBoundingClientRect();
    const originX = canvasRect.left + canvasEl.clientLeft;
    const originY = canvasRect.top + canvasEl.clientTop;
    const contentW = Math.max(0, canvasRect.width - canvasEl.clientLeft * 2);
    const tier = tierFromWidth(contentW);
    const stageEl = stageRef.current;
    const stageBottom = stageEl
      ? Math.round(stageEl.getBoundingClientRect().bottom - originY)
      : canvasH;
    const nextRects: Record<string, Rect> = {};

    for (const node of nodes) {
      const el = cardRefs.current[node.agent_id];
      if (!el) continue;
      const r = el.getBoundingClientRect();
      const left = Math.round((r.left - originX) * 2) / 2;
      const top = Math.round((r.top - originY) * 2) / 2;
      const w = Math.round(r.width * 2) / 2;
      const h = Math.round(r.height * 2) / 2;
      nextRects[node.agent_id] = {
        left,
        top,
        right: left + w,
        bottom: top + h,
        w,
        h,
        cx: left + w / 2,
        cy: top + h / 2,
      };
    }

    setGeometry((prev) => {
      if (
        prev.canvasW === canvasW &&
        prev.canvasH === canvasH &&
        prev.stageBottom === stageBottom &&
        prev.tier === tier
      ) {
        const prevKeys = Object.keys(prev.nodeRects);
        const nextKeys = Object.keys(nextRects);
        if (
          prevKeys.length === nextKeys.length &&
          nextKeys.every((k) => {
            const a = prev.nodeRects[k];
            const b = nextRects[k];
            return (
              a &&
              a.left === b.left &&
              a.top === b.top &&
              a.w === b.w &&
              a.h === b.h
            );
          })
        ) {
          return prev;
        }
      }
      return { canvasW, canvasH, stageBottom, tier, nodeRects: nextRects };
    });
  }, [nodes]);

  useLayoutEffect(() => {
    measureGeometry();
    const canvasEl = canvasRef.current;
    if (!canvasEl) return;
    const observer =
      typeof ResizeObserver !== "undefined"
        ? new ResizeObserver(() => measureGeometry())
        : null;
    if (observer) {
      observer.observe(canvasEl);
      for (const node of nodes) {
        const el = cardRefs.current[node.agent_id];
        if (el) observer.observe(el);
      }
    }
    let cancelled = false;
    const onFonts = () => {
      if (!cancelled) measureGeometry();
    };
    const fontSet = typeof document !== "undefined" ? document.fonts : undefined;
    void fontSet?.ready?.then(onFonts);
    fontSet?.addEventListener?.("loadingdone", onFonts);
    return () => {
      cancelled = true;
      observer?.disconnect();
      fontSet?.removeEventListener?.("loadingdone", onFonts);
    };
  }, [nodes, rows, isClustered, measureGeometry]);

  const clusterBoxes = useMemo(
    () =>
      computeClusterBoxes(
        nodes.length,
        geometry.tier,
        clusterMembers,
        geometry.nodeRects,
        geometry.canvasW,
        geometry.canvasH
      ),
    [nodes.length, geometry, clusterMembers]
  );

  const { curves, placedPills, railGroups } = useMemo(
    () =>
      computeGraphEdgeLayout(
        pairGroups,
        geometry.nodeRects,
        geometry.canvasW,
        geometry.canvasH,
        geometry.stageBottom,
        geometry.tier,
        clusterBoxes
      ),
    [pairGroups, geometry, clusterBoxes]
  );

  const activeAgentId = focusedAgent ?? hoveredAgent;

  const connectedNeighbors = useMemo(
    () => computeConnectedNeighbors(activeAgentId, edges),
    [activeAgentId, edges]
  );

  const animateAvatars = nodes.length < 10 && !prefersReducedMotion;
  const nameFor = (id: string) => nodeById.get(id)?.display_name ?? id;

  const handleCellKeyDown = (
    event: KeyboardEvent<HTMLElement>,
    rowIdx: number,
    colIdx: number,
    nodeId: string
  ) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpenAgent(nodeId);
      return;
    }

    const isRtl =
      gridRef.current !== null &&
      typeof window !== "undefined" &&
      window.getComputedStyle(gridRef.current).direction === "rtl";

    let target: AgentNodeItem | undefined;
    const curRow = navRows[rowIdx] ?? [];

    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      const step = (event.key === "ArrowRight" ? 1 : -1) * (isRtl ? -1 : 1);
      const nextCol = colIdx + step;
      if (nextCol >= 0 && nextCol < curRow.length) {
        target = curRow[nextCol];
      } else if (step > 0 && rowIdx + 1 < navRows.length) {
        target = navRows[rowIdx + 1][0];
      } else if (step < 0 && rowIdx - 1 >= 0) {
        const prevRow = navRows[rowIdx - 1];
        target = prevRow[prevRow.length - 1];
      }
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const nextRowIdx = rowIdx + (event.key === "ArrowDown" ? 1 : -1);
      if (nextRowIdx >= 0 && nextRowIdx < navRows.length) {
        const targetRow = navRows[nextRowIdx];
        const nextCol =
          curRow.length === 1
            ? Math.floor(targetRow.length / 2)
            : Math.min(
                targetRow.length - 1,
                Math.round(
                  (colIdx / Math.max(1, curRow.length - 1)) * (targetRow.length - 1)
                )
              );
        target = targetRow[nextCol];
      }
    } else if (event.key === "Home") {
      if (event.ctrlKey || event.metaKey) {
        target = navRows[0]?.[0];
      } else {
        target = curRow[0];
      }
    } else if (event.key === "End") {
      if (event.ctrlKey || event.metaKey) {
        const lastRow = navRows[navRows.length - 1];
        target = lastRow?.[lastRow.length - 1];
      } else {
        target = curRow[curRow.length - 1];
      }
    }

    if (target) {
      event.preventDefault();
      setActiveCellId(target.agent_id);
      focusGridCell(target.agent_id);
    }
  };

  const renderEdgePopover = (group: PairEdgeGroup, inRail = false) => {
    const srcName = nameFor(group.agentA);
    const dstName = nameFor(group.agentB);
    const basePillText = pillDisplayText(group);
    const pillText = inRail
      ? pillDisplayText(group, `${compactAgentTag(srcName)} ↔ ${compactAgentTag(dstName)}`)
      : basePillText;

    const threadList = (
      <Stack gap={8}>
        {group.edges.map((edge, idx) => (
          <Fragment key={edge.edge_id}>
            {idx > 0 ? <Divider /> : null}
            <Stack gap={6}>
              <Inline justify="between" align="center" gap={8}>
                <Badge tone={edgeKindTone(edge.kind)} variant="soft" dot={false}>
                  {edgeKindLabel(edge.kind)}
                </Badge>
                {edge.message_count ? (
                  <Text variant="help" className="sd-tabular">
                    {edge.message_count} {edge.message_count === 1 ? "msg" : "msgs"}
                  </Text>
                ) : null}
              </Inline>
              <Text variant="help" className="sd-graph-card-name">
                {edge.label}
              </Text>
              {edge.latest_preview && edge.latest_preview !== edge.label ? (
                <Text variant="help">
                  {edge.latest_sender_name ? `${edge.latest_sender_name}: ` : ""}
                  {edge.latest_preview}
                </Text>
              ) : edge.latest_sender_name ? (
                <Text variant="help">{edge.latest_sender_name}</Text>
              ) : null}
              <Inline justify="between" align="center" gap={8}>
                <Text variant="help" className="sd-tabular">
                  {formatRelativeTimestamp(edge.updated_at_ns)}
                </Text>
                <Button
                  emphasis="filled"
                  size="compact"
                  shape="pill"
                  onClick={() =>
                    navigate(`/messages?channel=${encodeURIComponent(edge.channel_id)}`)
                  }
                >
                  Open Thread →
                </Button>
              </Inline>
            </Stack>
          </Fragment>
        ))}
      </Stack>
    );

    return (
      <Popover
        label={`Preview interactions between ${srcName} and ${dstName}`}
        align="center"
        trigger={
          <Button
            emphasis="soft"
            size="compact"
            shape="pill"
            className="sd-graph-edge-pill-btn"
            aria-label={`${basePillText}, ${srcName} and ${dstName}, ${group.edges.length} ${group.edges.length === 1 ? "thread" : "threads"}`}
          >
            {pillText}
          </Button>
        }
      >
        <Stack gap={8}>
          <PopoverTitle>
            {srcName} ↔ {dstName}
          </PopoverTitle>
          <PopoverDescription>
            {group.edges.length}{" "}
            {group.edges.length === 1 ? "active interaction" : "interactions between agents"}
          </PopoverDescription>
          {group.edges.length > 1 ? (
            <ScrollArea
              label={`Threads between ${srcName} and ${dstName}`}
              className="sd-graph-popover-scroll"
            >
              {threadList}
            </ScrollArea>
          ) : (
            threadList
          )}
        </Stack>
      </Popover>
    );
  };

  const effectiveActiveCellId =
    (focusReturnId && navRows.flat().some((n) => n.agent_id === focusReturnId)
      ? focusReturnId
      : activeCellId) ??
    navRows[0]?.[0]?.agent_id ??
    null;

  const renderGridNodeCard = (node: AgentNodeItem, rowIdx: number, colIdx: number) => {
    const isFocused = activeAgentId === node.agent_id;
    const isDimmed =
      (hasActiveFilter && !matchingAgentIds.has(node.agent_id)) ||
      (activeAgentId !== null &&
        node.agent_id !== activeAgentId &&
        !connectedNeighbors.has(node.agent_id));
    const dutyLabel = node.active_run?.duty_kind ?? "Running work turn";

    return (
      <div
        key={node.agent_id}
        ref={(el) => {
          cardRefs.current[node.agent_id] = el;
        }}
        className="sd-graph-node-wrap"
        data-dense={nodes.length >= 5 ? "true" : "false"}
      >
        <Surface
          variant="raised"
          role="gridcell"
          tabIndex={effectiveActiveCellId === node.agent_id ? 0 : -1}
          data-focus-id={node.agent_id}
          data-focused={isFocused ? "true" : "false"}
          data-dimmed={isDimmed ? "true" : "false"}
          className="yg-card sd-graph-node-card"
          aria-label={`${node.display_name}, ${node.role_label}, ${node.live_status}, ${node.current_task}`}
          onClick={() => onOpenAgent(node.agent_id)}
          onKeyDown={(event) => handleCellKeyDown(event, rowIdx, colIdx, node.agent_id)}
          onMouseEnter={() => setHoveredAgent(node.agent_id)}
          onMouseLeave={() =>
            setHoveredAgent((prev) => (prev === node.agent_id ? null : prev))
          }
          onFocus={() => {
            setActiveCellId(node.agent_id);
            setFocusedAgent(node.agent_id);
          }}
          onBlur={() =>
            setFocusedAgent((prev) => (prev === node.agent_id ? null : prev))
          }
        >
          <Stack gap={8}>
            <div className="sd-graph-card-head">
              <div className="sd-graph-card-identity">
                <AgentAvatar
                  agent={normalizeAvatarVariant(node.avatar_variant)}
                  state={node.avatar_state}
                  animated={animateAvatars}
                  size={34}
                  label={`${node.display_name} (${node.live_status})`}
                />
                <div className="sd-graph-card-titles">
                  <Text variant="body" className="sd-graph-card-name">
                    {node.display_name}
                  </Text>
                  <Badge
                    tone={node.is_ceo ? "brand" : "neutral"}
                    variant="soft"
                    dot={false}
                  >
                    {node.role_label}
                  </Badge>
                </div>
              </div>
              <Status tone={statusToneFor(node.live_status)}>{node.live_status}</Status>
            </div>

            <Text variant="help">{node.current_task}</Text>

            {node.sandbox_activity ? (
              <Inline gap={6} align="center" wrap>
                <Badge tone="neutral" variant="outline" dot={false}>
                  Trading Sandbox ·{" "}
                  {node.sandbox_activity.fill_count > 0
                    ? `${node.sandbox_activity.fill_count} ${node.sandbox_activity.fill_count === 1 ? "fill" : "fills"}`
                    : "active"}
                </Badge>
              </Inline>
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

          <div className="sd-graph-card-footer">
            <Stack gap={2}>
              <Text variant="help" className="sd-tabular">
                {node.private_draft_count}{" "}
                {node.private_draft_count === 1 ? "private draft" : "private drafts"}
              </Text>
              <Text variant="help" className="sd-tabular">
                Budget {formatMinorCurrency(node.monthly_budget_minor)}/mo
              </Text>
            </Stack>
            <Text variant="help" className="sd-graph-inspect-cue" aria-hidden="true">
              Inspect →
            </Text>
          </div>
        </Surface>
      </div>
    );
  };

  const graphGrid = (
    <div
      ref={gridRef}
      role="grid"
      aria-label="Agent interaction grid"
      className="sd-graph-nodes-grid"
    >
      {rows.map((row) => {
        const navRowIdx = navRows.findIndex((nr) =>
          row.cells.some((c) => c !== null && nr.includes(c))
        );
        let nonNullCol = 0;
        return (
          <div
            key={row.id}
            role="row"
            className="sd-graph-row"
            data-cluster-cols={row.clusterCols}
          >
            {row.cells.map((cell, idx) => {
              if (!cell) {
                return (
                  <div
                    key={`${row.id}-empty-${idx}`}
                    className="sd-graph-slot-spacer"
                    aria-hidden="true"
                  />
                );
              }
              const colIdx = nonNullCol;
              nonNullCol += 1;
              return renderGridNodeCard(cell, Math.max(0, navRowIdx), colIdx);
            })}
          </div>
        );
      })}
    </div>
  );

  return (
    <div
      ref={canvasRef}
      className="sd-graph-canvas"
      role="region"
      aria-label="Agent interaction network graph"
    >
      {geometry.canvasW > 0 && geometry.canvasH > 0 ? (
        <svg
          className="sd-graph-edges-svg"
          viewBox={`0 0 ${geometry.canvasW} ${geometry.canvasH}`}
          width={geometry.canvasW}
          height={geometry.canvasH}
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
          <rect
            width={geometry.canvasW}
            height={geometry.canvasH}
            fill="url(#sd-dot-pattern)"
          />

          {clusterBoxes.map((box) => (
            <g key={box.id}>
              <rect
                x={box.left}
                y={box.top}
                width={box.w}
                height={box.h}
                rx={16}
                className="sd-graph-cluster-ring"
              />
              <text
                x={box.left + 12}
                y={box.top + 18}
                className="sd-graph-cluster-label"
              >
                {box.label}
              </text>
            </g>
          ))}

          {pairGroups.map((group) => {
            const curve = curves.get(group.pairKey);
            if (!curve) return null;
            const focused =
              activeAgentId !== null &&
              (group.agentA === activeAgentId || group.agentB === activeAgentId);
            const dimmed =
              (hasActiveFilter &&
                !matchingAgentIds.has(group.agentA) &&
                !matchingAgentIds.has(group.agentB)) ||
              (activeAgentId !== null &&
                group.agentA !== activeAgentId &&
                group.agentB !== activeAgentId);
            return (
              <g key={group.pairKey}>
                <path
                  d={curve.d}
                  className="sd-graph-edge-base"
                  data-focused={focused ? "true" : "false"}
                  data-dimmed={dimmed ? "true" : "false"}
                />
                {group.hasActive ? (
                  <path
                    d={curve.d}
                    className="sd-graph-edge-pulse"
                    data-focused={focused ? "true" : "false"}
                    data-dimmed={dimmed ? "true" : "false"}
                  />
                ) : null}
              </g>
            );
          })}

          {placedPills.map((pill) => {
            if (!pill.leader) return null;
            const dimmed =
              (hasActiveFilter &&
                !matchingAgentIds.has(pill.group.agentA) &&
                !matchingAgentIds.has(pill.group.agentB)) ||
              (activeAgentId !== null &&
                pill.group.agentA !== activeAgentId &&
                pill.group.agentB !== activeAgentId);
            return (
              <line
                key={`leader-${pill.group.pairKey}`}
                x1={pill.leader.x1}
                y1={pill.leader.y1}
                x2={pill.leader.x2}
                y2={pill.leader.y2}
                className="sd-graph-edge-leader"
                data-dimmed={dimmed ? "true" : "false"}
              />
            );
          })}
        </svg>
      ) : null}

      {placedPills.map((pill) => {
        const isFocused =
          activeAgentId !== null &&
          (pill.group.agentA === activeAgentId || pill.group.agentB === activeAgentId);
        const isDimmed =
          (hasActiveFilter &&
            !matchingAgentIds.has(pill.group.agentA) &&
            !matchingAgentIds.has(pill.group.agentB)) ||
          (activeAgentId !== null &&
            pill.group.agentA !== activeAgentId &&
            pill.group.agentB !== activeAgentId);
        return (
          <div
            key={`pill-${pill.group.pairKey}`}
            className="sd-graph-edge-pill-wrap"
            data-focused={isFocused ? "true" : "false"}
            data-dimmed={isDimmed ? "true" : "false"}
            style={{ left: `${pill.cx}px`, top: `${pill.cy}px` }}
          >
            {renderEdgePopover(pill.group)}
          </div>
        );
      })}

      <div
        ref={stageRef}
        className="sd-graph-stage"
        data-clustered={isClustered ? "true" : "false"}
      >
        {nodes.length <= 1 ? (
          <div className="sd-graph-day0-split">
            {graphGrid}
            <Surface variant="outlined" className="sd-graph-day0-bay">
              <Stack gap={8}>
                <Badge tone="neutral" variant="outline" dot={false}>
                  Day 0 Hiring Bay
                </Badge>
                <Text variant="body" className="sd-graph-card-name">
                  Ready for initial specialist hires
                </Text>
                <Text variant="help">
                  Only Founder · CEO is active. Discuss initial team composition with the
                  CEO or review existing mandate and organization records.
                </Text>
              </Stack>
              <Inline gap={8} wrap>
                <Button
                  emphasis="filled"
                  size="compact"
                  shape="pill"
                  onClick={() =>
                    askCeo({
                      kind: "company",
                      id: companyId,
                      label: "First hire & initial team composition",
                    })
                  }
                >
                  Ask CEO about first hire
                </Button>
                <Button
                  emphasis="soft"
                  size="compact"
                  shape="pill"
                  onClick={() => navigate("/reports")}
                >
                  View Reports
                </Button>
              </Inline>
            </Surface>
          </div>
        ) : (
          graphGrid
        )}
      </div>

      {railGroups.length > 0 ? (
        <div
          className="sd-graph-pill-rail"
          aria-label="Interaction threads between agents"
        >
          <Text variant="help">Interactions:</Text>
          {railGroups.map((group) => {
            const isFocused =
              activeAgentId !== null &&
              (group.agentA === activeAgentId || group.agentB === activeAgentId);
            const isDimmed =
              (hasActiveFilter &&
                !matchingAgentIds.has(group.agentA) &&
                !matchingAgentIds.has(group.agentB)) ||
              (activeAgentId !== null &&
                group.agentA !== activeAgentId &&
                group.agentB !== activeAgentId);
            return (
              <div
                key={`rail-${group.pairKey}`}
                className="sd-graph-edge-pill-wrap"
                data-in-rail="true"
                data-focused={isFocused ? "true" : "false"}
                data-dimmed={isDimmed ? "true" : "false"}
              >
                {renderEdgePopover(group, true)}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
