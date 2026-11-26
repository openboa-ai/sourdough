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
  AgentRow,
  Badge,
  Button,
  Card,
  DataTable,
  Divider,
  EmptyResult,
  FilterBar,
  Inline,
  List,
  PageHeader,
  Popover,
  PopoverDescription,
  PopoverTitle,
  Result,
  ScrollArea,
  SegmentedControl,
  Skeleton,
  Stack,
  Status,
  Surface,
  Text,
  ThreadRow,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import { fetchProjection, formatMinorCurrency, formatRelativeNs } from "../api/client";
import type { AgentEdgeItem, AgentNodeItem, AgentsGraphProjection } from "../api/types";
import { useApp } from "../state/store";

type StatusFilter = "all" | "working" | "waiting" | "ready";
type LayoutTier = "desktop" | "tablet" | "mobile";

interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
  w: number;
  h: number;
  cx: number;
  cy: number;
}

interface ClusterBox {
  id: string;
  label: string;
  left: number;
  top: number;
  w: number;
  h: number;
}

interface PairEdgeGroup {
  pairKey: string;
  agentA: string;
  agentB: string;
  primary: AgentEdgeItem;
  edges: AgentEdgeItem[];
  extraCount: number;
  hasActive: boolean;
}

interface GraphRowSpec {
  id: string;
  clusterCols?: number;
  cells: Array<AgentNodeItem | null>;
}

interface EdgeCurve {
  x1: number;
  y1: number;
  cx: number;
  cy: number;
  x2: number;
  y2: number;
  midX: number;
  midY: number;
  d: string;
}

interface BoxBounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

interface PlacedPill {
  group: PairEdgeGroup;
  cx: number;
  cy: number;
  w: number;
  h: number;
  leader?: { x1: number; y1: number; x2: number; y2: number };
}

const NODE_MARGIN = 10;
const PILL_MARGIN = 6;
const EDGE_PADDING = 8;

function normalizeToNs(epoch: number | null | undefined): number | null {
  if (epoch === null || epoch === undefined || !Number.isFinite(epoch) || epoch <= 0) {
    return null;
  }
  if (epoch < 1e12) return Math.round(epoch * 1_000_000_000);
  if (epoch < 1e15) return Math.round(epoch * 1_000_000);
  return epoch;
}

function formatRelativeTimestamp(epoch: number | null | undefined): string {
  const ns = normalizeToNs(epoch);
  if (ns === null) return "Unavailable";
  return formatRelativeNs(ns);
}

function safeIsoFromEpoch(epoch: number | null | undefined): string | undefined {
  const ns = normalizeToNs(epoch);
  if (ns === null) return undefined;
  const d = new Date(Math.floor(ns / 1_000_000));
  if (Number.isNaN(d.getTime())) return undefined;
  return d.toISOString();
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

function edgeKindLabel(kind: AgentEdgeItem["kind"]): string {
  if (kind === "peer_request") return "Peer Request";
  if (kind === "handoff") return "Work Handoff";
  return "Internal Channel";
}

function edgeKindTone(kind: AgentEdgeItem["kind"]): "warning" | "brand" | "neutral" {
  if (kind === "peer_request") return "warning";
  if (kind === "channel") return "brand";
  return "neutral";
}

function tierFromWidth(width: number): LayoutTier {
  if (width < 540) return "mobile";
  if (width < 840) return "tablet";
  return "desktop";
}

function buildGraphRows(
  nodes: AgentNodeItem[],
  tier: LayoutTier
): {
  rows: GraphRowSpec[];
  navRows: AgentNodeItem[][];
  clusterMembers: Map<string, string[]>;
} {
  const rows: GraphRowSpec[] = [];
  const clusterMembers = new Map<string, string[]>();

  if (nodes.length === 0) {
    return { rows, navRows: [], clusterMembers };
  }

  for (const node of nodes) {
    const cluster = clusterForAgent(node);
    const list = clusterMembers.get(cluster) ?? [];
    list.push(node.agent_id);
    clusterMembers.set(cluster, list);
  }

  const execNodes = nodes.filter((n) => clusterForAgent(n) === "Executive");
  const otherNodes = nodes.filter((n) => clusterForAgent(n) !== "Executive");
  const orderedAll = [...execNodes, ...otherNodes];

  if (tier === "mobile") {
    orderedAll.forEach((node, idx) => {
      rows.push({ id: `m-${idx}-${node.agent_id}`, cells: [node] });
    });
  } else if (nodes.length <= 4) {
    if (orderedAll.length === 1) {
      rows.push({ id: "r-0", cells: [orderedAll[0]] });
    } else if (orderedAll.length === 2) {
      rows.push({ id: "r-0", cells: [orderedAll[0], orderedAll[1]] });
    } else if (orderedAll.length === 3) {
      rows.push({ id: "r-0", cells: [orderedAll[0]] });
      rows.push({ id: "r-1", cells: [orderedAll[1], orderedAll[2]] });
    } else {
      rows.push({ id: "r-0", cells: [orderedAll[0], orderedAll[1]] });
      rows.push({ id: "r-1", cells: [orderedAll[2], orderedAll[3]] });
    }
  } else if (tier === "tablet") {
    for (let i = 0; i < execNodes.length; i += 2) {
      rows.push({ id: `exec-${i}`, cells: execNodes.slice(i, i + 2) });
    }
    const groupedOthers = new Map<string, AgentNodeItem[]>();
    for (const node of otherNodes) {
      const k = clusterForAgent(node);
      const list = groupedOthers.get(k) ?? [];
      list.push(node);
      groupedOthers.set(k, list);
    }
    const orderedOthers = Array.from(groupedOthers.values()).flat();
    for (let i = 0; i < orderedOthers.length; i += 2) {
      rows.push({ id: `tab-${i}`, cells: orderedOthers.slice(i, i + 2) });
    }
  } else {
    for (let i = 0; i < execNodes.length; i += 3) {
      rows.push({ id: `exec-${i}`, cells: execNodes.slice(i, i + 3) });
    }
    const groupedOthers = new Map<string, AgentNodeItem[]>();
    for (const node of otherNodes) {
      const k = clusterForAgent(node);
      const list = groupedOthers.get(k) ?? [];
      list.push(node);
      groupedOthers.set(k, list);
    }
    const clusterKeys = Array.from(groupedOthers.keys());
    if (clusterKeys.length > 0 && clusterKeys.length <= 3) {
      const maxMembers = Math.max(
        ...clusterKeys.map((k) => groupedOthers.get(k)?.length ?? 0)
      );
      for (let r = 0; r < maxMembers; r += 1) {
        const cells = clusterKeys.map((k) => groupedOthers.get(k)?.[r] ?? null);
        rows.push({
          id: `cluster-row-${r}`,
          clusterCols: clusterKeys.length > 1 ? clusterKeys.length : undefined,
          cells,
        });
      }
    } else {
      const flatOthers = Array.from(groupedOthers.values()).flat();
      for (let i = 0; i < flatOthers.length; i += 3) {
        rows.push({ id: `desk-${i}`, cells: flatOthers.slice(i, i + 3) });
      }
    }
  }

  const navRows = rows
    .map((r) => r.cells.filter((c): c is AgentNodeItem => c !== null))
    .filter((r) => r.length > 0);

  return { rows, navRows, clusterMembers };
}

function rectsIntersect(
  aLeft: number,
  aTop: number,
  aRight: number,
  aBottom: number,
  bLeft: number,
  bTop: number,
  bRight: number,
  bBottom: number
): boolean {
  return !(aRight <= bLeft || bRight <= aLeft || aBottom <= bTop || bBottom <= aTop);
}

function evalQuadBezier(
  x1: number,
  y1: number,
  cx: number,
  cy: number,
  x2: number,
  y2: number,
  t: number
): { x: number; y: number } {
  const mt = 1 - t;
  return {
    x: mt * mt * x1 + 2 * mt * t * cx + t * t * x2,
    y: mt * mt * y1 + 2 * mt * t * cy + t * t * y2,
  };
}

function computeEdgeCurve(
  srcId: string,
  dstId: string,
  nodeRects: Record<string, Rect>,
  tier: LayoutTier,
  pairIndex: number,
  canvasW: number
): EdgeCurve | null {
  const src = nodeRects[srcId];
  const dst = nodeRects[dstId];
  if (!src || !dst) return null;

  if (tier === "mobile") {
    const x1 = src.left;
    const y1 = src.cy;
    const x2 = dst.left;
    const y2 = dst.cy;
    const laneOffset = 6 + (pairIndex % 4) * 5;
    const cx = Math.max(4, Math.min(x1, x2) - laneOffset);
    const cy = (y1 + y2) / 2;
    const mid = evalQuadBezier(x1, y1, cx, cy, x2, y2, 0.5);
    return {
      x1,
      y1,
      cx,
      cy,
      x2,
      y2,
      midX: mid.x,
      midY: mid.y,
      d: `M ${x1.toFixed(1)} ${y1.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${x2.toFixed(1)} ${y2.toFixed(1)}`,
    };
  }

  const dx = dst.cx - src.cx;
  const dy = dst.cy - src.cy;
  const sameCol = Math.abs(dx) < Math.min(src.w, dst.w) * 0.45;
  const sameRow = Math.abs(dy) < Math.min(src.h, dst.h) * 0.45;
  const others = Object.entries(nodeRects)
    .filter(([id]) => id !== srcId && id !== dstId)
    .map(([, rect]) => rect);

  let x1 = src.cx;
  let y1 = src.cy;
  let x2 = dst.cx;
  let y2 = dst.cy;
  let cx = (x1 + x2) / 2;
  let cy = (y1 + y2) / 2;

  if (sameCol) {
    const upper = src.cy <= dst.cy ? src : dst;
    const lower = src.cy <= dst.cy ? dst : src;
    const hasBlocker = others.some(
      (r) =>
        r.bottom > upper.bottom + 4 &&
        r.top < lower.top - 4 &&
        r.right > Math.min(upper.left, lower.left) + 8 &&
        r.left < Math.max(upper.right, lower.right) - 8
    );
    if (hasBlocker) {
      const useLeftGutter = upper.right + 48 > canvasW - 12 && upper.left > 48;
      if (useLeftGutter) {
        x1 = upper.left;
        y1 = upper.cy;
        x2 = lower.left;
        y2 = lower.cy;
        cx = Math.max(12, Math.min(x1, x2) - 40 - (pairIndex % 2) * 14);
        cy = (y1 + y2) / 2;
      } else {
        x1 = upper.right;
        y1 = upper.cy;
        x2 = lower.right;
        y2 = lower.cy;
        cx = Math.min(canvasW - 12, Math.max(x1, x2) + 40 + (pairIndex % 2) * 14);
        cy = (y1 + y2) / 2;
      }
    } else {
      x1 = upper.cx;
      y1 = upper.bottom;
      x2 = lower.cx;
      y2 = lower.top;
      cx = (x1 + x2) / 2 + (pairIndex % 2 === 0 ? -18 : 18);
      cy = (y1 + y2) / 2;
    }
  } else if (sameRow) {
    const leftNode = src.cx <= dst.cx ? src : dst;
    const rightNode = src.cx <= dst.cx ? dst : src;
    const hasBlocker = others.some(
      (r) =>
        r.right > leftNode.right + 4 &&
        r.left < rightNode.left - 4 &&
        r.bottom > Math.min(leftNode.top, rightNode.top) + 8 &&
        r.top < Math.max(leftNode.bottom, rightNode.bottom) - 8
    );
    if (hasBlocker) {
      const aboveTop = Math.min(leftNode.top, rightNode.top);
      if (aboveTop >= 52) {
        x1 = leftNode.cx;
        y1 = leftNode.top;
        x2 = rightNode.cx;
        y2 = rightNode.top;
        cx = (x1 + x2) / 2;
        cy = Math.max(12, aboveTop - 42 - (pairIndex % 2) * 12);
      } else {
        x1 = leftNode.cx;
        y1 = leftNode.bottom;
        x2 = rightNode.cx;
        y2 = rightNode.bottom;
        cx = (x1 + x2) / 2;
        cy = Math.max(leftNode.bottom, rightNode.bottom) + 42 + (pairIndex % 2) * 12;
      }
    } else {
      x1 = leftNode.right;
      y1 = leftNode.cy;
      x2 = rightNode.left;
      y2 = rightNode.cy;
      cx = (x1 + x2) / 2;
      cy = (y1 + y2) / 2 + (pairIndex % 2 === 0 ? -14 : 14);
    }
  } else if (Math.abs(dy) >= Math.abs(dx) * 0.7) {
    const upper = src.cy <= dst.cy ? src : dst;
    const lower = src.cy <= dst.cy ? dst : src;
    x1 = upper.cx;
    y1 = upper.bottom;
    x2 = lower.cx;
    y2 = lower.top;
    const len = Math.hypot(x2 - x1, y2 - y1) || 1;
    const nx = -(y2 - y1) / len;
    const ny = (x2 - x1) / len;
    const sign = pairIndex % 2 === 0 ? 1 : -1;
    cx = (x1 + x2) / 2 + nx * 24 * sign;
    cy = (y1 + y2) / 2 + ny * 24 * sign;
  } else {
    const leftNode = src.cx <= dst.cx ? src : dst;
    const rightNode = src.cx <= dst.cx ? dst : src;
    x1 = leftNode.right;
    y1 = leftNode.cy;
    x2 = rightNode.left;
    y2 = rightNode.cy;
    const len = Math.hypot(x2 - x1, y2 - y1) || 1;
    const nx = -(y2 - y1) / len;
    const ny = (x2 - x1) / len;
    const sign = pairIndex % 2 === 0 ? 1 : -1;
    cx = (x1 + x2) / 2 + nx * 24 * sign;
    cy = (y1 + y2) / 2 + ny * 24 * sign;
  }

  const mid = evalQuadBezier(x1, y1, cx, cy, x2, y2, 0.5);
  return {
    x1,
    y1,
    cx,
    cy,
    x2,
    y2,
    midX: mid.x,
    midY: mid.y,
    d: `M ${x1.toFixed(1)} ${y1.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${x2.toFixed(1)} ${y2.toFixed(1)}`,
  };
}

function compactAgentTag(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "Agent";
  if (/\bCEO\b/i.test(trimmed)) return "CEO";
  const first = trimmed.split(/\s+/)[0] ?? trimmed;
  return first.length > 10 ? `${first.slice(0, 9)}…` : first;
}

function pillDisplayText(group: PairEdgeGroup, pairPrefix?: string): string {
  const raw = group.primary.label;
  const short = raw.length > 18 ? `${raw.slice(0, 16)}…` : raw;
  const base = group.extraCount > 0 ? `${short} · +${group.extraCount}` : short;
  return pairPrefix ? `${pairPrefix} · ${base}` : base;
}

function placeEdgePills(
  groups: PairEdgeGroup[],
  curves: Map<string, EdgeCurve>,
  nodeRects: Record<string, Rect>,
  canvasW: number,
  maxStageBottom: number,
  tier: LayoutTier,
  obstacles: BoxBounds[] = []
): { placed: PlacedPill[]; rail: PairEdgeGroup[] } {
  if (canvasW <= 0 || maxStageBottom <= 0) {
    return { placed: [], rail: [] };
  }
  if (tier === "mobile") {
    return { placed: [], rail: groups };
  }

  const placed: PlacedPill[] = [];
  const rail: PairEdgeGroup[] = [];
  const cardList = Object.values(nodeRects);
  const placedBoxes: BoxBounds[] = [];

  const isFree = (cx: number, cy: number, w: number, h: number): boolean => {
    const left = cx - w / 2;
    const top = cy - h / 2;
    const right = cx + w / 2;
    const bottom = cy + h / 2;
    if (
      left < EDGE_PADDING ||
      right > canvasW - EDGE_PADDING ||
      top < EDGE_PADDING ||
      bottom > maxStageBottom - EDGE_PADDING
    ) {
      return false;
    }
    for (const card of cardList) {
      if (
        rectsIntersect(
          left,
          top,
          right,
          bottom,
          card.left - NODE_MARGIN,
          card.top - NODE_MARGIN,
          card.right + NODE_MARGIN,
          card.bottom + NODE_MARGIN
        )
      ) {
        return false;
      }
    }
    for (const obs of obstacles) {
      if (
        rectsIntersect(
          left,
          top,
          right,
          bottom,
          obs.left - PILL_MARGIN,
          obs.top - PILL_MARGIN,
          obs.right + PILL_MARGIN,
          obs.bottom + PILL_MARGIN
        )
      ) {
        return false;
      }
    }
    for (const box of placedBoxes) {
      if (
        rectsIntersect(
          left,
          top,
          right,
          bottom,
          box.left - PILL_MARGIN,
          box.top - PILL_MARGIN,
          box.right + PILL_MARGIN,
          box.bottom + PILL_MARGIN
        )
      ) {
        return false;
      }
    }
    return true;
  };

  const tSteps = [0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74];
  const nOffsets = [0, 22, -22, 40, -40, 58, -58];

  for (const group of groups) {
    const curve = curves.get(group.pairKey);
    if (!curve) {
      rail.push(group);
      continue;
    }
    const label = pillDisplayText(group);
    const w = Math.min(236, Math.max(96, Math.ceil(label.length * 7.8 + 34)));
    const h = 34;

    const minCx = w / 2 + EDGE_PADDING;
    const maxCx = canvasW - w / 2 - EDGE_PADDING;
    const minCy = h / 2 + EDGE_PADDING;
    const maxCy = maxStageBottom - h / 2 - EDGE_PADDING;
    if (minCx > maxCx || minCy > maxCy) {
      rail.push(group);
      continue;
    }

    const chordLen = Math.hypot(curve.x2 - curve.x1, curve.y2 - curve.y1) || 1;
    const nx = -(curve.y2 - curve.y1) / chordLen;
    const ny = (curve.x2 - curve.x1) / chordLen;

    let chosen: { cx: number; cy: number } | null = null;

    for (const t of tSteps) {
      const pt = evalQuadBezier(
        curve.x1,
        curve.y1,
        curve.cx,
        curve.cy,
        curve.x2,
        curve.y2,
        t
      );
      for (const offset of nOffsets) {
        const cx = Math.max(minCx, Math.min(maxCx, pt.x + nx * offset));
        const cy = Math.max(minCy, Math.min(maxCy, pt.y + ny * offset));
        if (isFree(cx, cy, w, h)) {
          chosen = { cx, cy };
          break;
        }
      }
      if (chosen) break;
    }

    if (!chosen) {
      let bestDist = Infinity;
      for (let cy = minCy; cy <= maxCy; cy += 16) {
        for (let cx = minCx; cx <= maxCx; cx += 16) {
          if (!isFree(cx, cy, w, h)) continue;
          const dist = Math.hypot(cx - curve.midX, cy - curve.midY);
          if (dist < bestDist) {
            bestDist = dist;
            chosen = { cx, cy };
          }
        }
      }
    }

    if (chosen) {
      placedBoxes.push({
        left: chosen.cx - w / 2,
        top: chosen.cy - h / 2,
        right: chosen.cx + w / 2,
        bottom: chosen.cy + h / 2,
      });
      const displaced = Math.hypot(chosen.cx - curve.midX, chosen.cy - curve.midY);
      placed.push({
        group,
        cx: chosen.cx,
        cy: chosen.cy,
        w,
        h,
        leader:
          displaced > 36
            ? { x1: curve.midX, y1: curve.midY, x2: chosen.cx, y2: chosen.cy }
            : undefined,
      });
    } else {
      rail.push(group);
    }
  }

  return { placed, rail };
}

export function AgentsActivityView() {
  const {
    navigate,
    refreshSeq,
    focusReturnId,
    clearFocusReturnId,
    askCeo,
    reportApiError,
  } = useApp();
  const [data, setData] = useState<AgentsGraphProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [viewMode, setViewMode] = useState<"graph" | "list">("graph");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [hoveredAgent, setHoveredAgent] = useState<string | null>(null);
  const [focusedAgent, setFocusedAgent] = useState<string | null>(null);
  const [activeCellId, setActiveCellId] = useState<string | null>(null);
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(() =>
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
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

  const initializedModeRef = useRef(false);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const gridRef = useRef<HTMLDivElement | null>(null);
  const cardRefs = useRef<Record<string, HTMLDivElement | null>>({});

  useEffect(() => {
    if (typeof window === "undefined") return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setPrefersReducedMotion(mq.matches);
    onChange();
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<AgentsGraphProjection>("agents-graph", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) {
          setData(res.data);
          if (!initializedModeRef.current) {
            setViewMode(res.data.default_view_mode);
            initializedModeRef.current = true;
          }
        }
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load agent graph.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

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

  const pairGroups = useMemo<PairEdgeGroup[]>(() => {
    const raw = data?.edges ?? [];
    const priority = { peer_request: 0, handoff: 1, channel: 2 } as const;
    const map = new Map<string, AgentEdgeItem[]>();
    for (const edge of raw) {
      if (edge.source_agent_id === edge.target_agent_id) continue;
      const a =
        edge.source_agent_id < edge.target_agent_id
          ? edge.source_agent_id
          : edge.target_agent_id;
      const b =
        edge.source_agent_id < edge.target_agent_id
          ? edge.target_agent_id
          : edge.source_agent_id;
      const pairKey = `${a}::${b}`;
      const list = map.get(pairKey) ?? [];
      list.push(edge);
      map.set(pairKey, list);
    }
    const groups: PairEdgeGroup[] = [];
    for (const [pairKey, list] of map.entries()) {
      const sorted = [...list].sort((x, y) => {
        const ax = x.active !== false ? 0 : 1;
        const ay = y.active !== false ? 0 : 1;
        if (ax !== ay) return ax - ay;
        const px = priority[x.kind];
        const py = priority[y.kind];
        if (px !== py) return px - py;
        return (y.updated_at_ns || 0) - (x.updated_at_ns || 0);
      });
      const [agentA, agentB] = pairKey.split("::");
      groups.push({
        pairKey,
        agentA,
        agentB,
        primary: sorted[0],
        edges: sorted,
        extraCount: sorted.length - 1,
        hasActive: sorted.some((e) => e.active !== false),
      });
    }
    groups.sort((x, y) => {
      if (x.hasActive !== y.hasActive) return x.hasActive ? -1 : 1;
      return (y.primary.updated_at_ns || 0) - (x.primary.updated_at_ns || 0);
    });
    return groups;
  }, [data?.edges]);

  const { rows, navRows, clusterMembers } = useMemo(
    () => buildGraphRows(data?.nodes ?? [], geometry.tier),
    [data?.nodes, geometry.tier]
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
    if (focusReturnId && data && viewMode === "graph") {
      if (focusGridCell(focusReturnId)) {
        clearFocusReturnId();
      }
    }
  }, [focusReturnId, data, viewMode, clearFocusReturnId, focusGridCell]);

  const isClustered = (data?.nodes.length ?? 0) >= 5 && geometry.tier === "desktop";

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

    for (const node of data?.nodes ?? []) {
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
  }, [data?.nodes]);

  useLayoutEffect(() => {
    if (viewMode !== "graph" || !data || filteredNodes.length === 0) return;
    measureGeometry();
    const canvasEl = canvasRef.current;
    if (!canvasEl) return;
    const observer =
      typeof ResizeObserver !== "undefined"
        ? new ResizeObserver(() => measureGeometry())
        : null;
    if (observer) {
      observer.observe(canvasEl);
      for (const node of data.nodes) {
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
  }, [viewMode, data, filteredNodes.length, rows, isClustered, measureGeometry]);

  const clusterBoxes = useMemo<ClusterBox[]>(() => {
    if (!data || data.nodes.length < 5 || geometry.tier !== "desktop") return [];
    const boxes: ClusterBox[] = [];
    for (const [cluster, memberIds] of clusterMembers.entries()) {
      const memberRects = memberIds
        .map((id) => geometry.nodeRects[id])
        .filter((r): r is Rect => Boolean(r));
      if (memberRects.length === 0) continue;
      const left = Math.max(8, Math.min(...memberRects.map((r) => r.left)) - 14);
      const top = Math.max(8, Math.min(...memberRects.map((r) => r.top)) - 32);
      const right = Math.min(
        geometry.canvasW - 8,
        Math.max(...memberRects.map((r) => r.right)) + 14
      );
      const bottom = Math.min(
        geometry.canvasH - 8,
        Math.max(...memberRects.map((r) => r.bottom)) + 14
      );
      const memberSet = new Set(memberIds);
      const overlapsOther = Object.entries(geometry.nodeRects).some(([id, r]) => {
        if (memberSet.has(id)) return false;
        return rectsIntersect(left, top, right, bottom, r.left, r.top, r.right, r.bottom);
      });
      if (!overlapsOther && right > left && bottom > top) {
        boxes.push({
          id: `cluster-${cluster}`,
          label: cluster,
          left,
          top,
          w: right - left,
          h: bottom - top,
        });
      }
    }
    return boxes;
  }, [data, geometry, clusterMembers]);

  const { curves, placedPills, railGroups } = useMemo(() => {
    const nextCurves = new Map<string, EdgeCurve>();
    if (geometry.canvasW > 0 && geometry.canvasH > 0) {
      pairGroups.forEach((group, idx) => {
        const curve = computeEdgeCurve(
          group.agentA,
          group.agentB,
          geometry.nodeRects,
          geometry.tier,
          idx,
          geometry.canvasW
        );
        if (curve) nextCurves.set(group.pairKey, curve);
      });
    }
    const clusterLabelObstacles: BoxBounds[] = clusterBoxes.map((box) => ({
      left: box.left + 4,
      top: box.top,
      right: box.left + Math.max(120, Math.ceil(box.label.length * 9.5 + 32)),
      bottom: box.top + 28,
    }));
    const { placed, rail } = placeEdgePills(
      pairGroups,
      nextCurves,
      geometry.nodeRects,
      geometry.canvasW,
      geometry.stageBottom,
      geometry.tier,
      clusterLabelObstacles
    );
    return { curves: nextCurves, placedPills: placed, railGroups: rail };
  }, [pairGroups, geometry, clusterBoxes]);

  const activeAgentId = focusedAgent ?? hoveredAgent;

  const connectedNeighbors = useMemo(() => {
    const set = new Set<string>();
    if (!activeAgentId) return set;
    for (const edge of data?.edges ?? []) {
      if (edge.source_agent_id === activeAgentId) set.add(edge.target_agent_id);
      if (edge.target_agent_id === activeAgentId) set.add(edge.source_agent_id);
    }
    return set;
  }, [activeAgentId, data?.edges]);

  const filteredEdges = useMemo(() => {
    const edges = data?.edges ?? [];
    if (!hasActiveFilter) return edges;
    const q = query.trim().toLowerCase();
    return edges.filter((edge) => {
      const endpointMatch =
        matchingAgentIds.has(edge.source_agent_id) ||
        matchingAgentIds.has(edge.target_agent_id);
      if (statusFilter !== "all") return endpointMatch;
      if (!q) return endpointMatch;
      return (
        endpointMatch ||
        `${edge.label} ${edge.latest_preview ?? ""} ${edge.channel_id}`
          .toLowerCase()
          .includes(q)
      );
    });
  }, [data?.edges, hasActiveFilter, matchingAgentIds, query, statusFilter]);

  if (loading && !data) {
    return (
      <Stack gap={16}>
        <PageHeader title="Agents & Activity" detail="Loading live team interaction graph..." />
        <Skeleton label="Loading agents" lines={5} />
      </Stack>
    );
  }

  if (!data) {
    return (
      <Result
        title="Agents & Activity unavailable"
        action={
          <Button
            emphasis="soft"
            shape="pill"
            onClick={() => {
              setLoading(true);
              setError(null);
              setRetrySeq((n) => n + 1);
            }}
          >
            Retry
          </Button>
        }
      >
        {error ?? "Could not load agent graph."}
      </Result>
    );
  }

  const animateAvatars = data.nodes.length < 10 && !prefersReducedMotion;
  const nodeById = new Map(data.nodes.map((n) => [n.agent_id, n]));
  const nameFor = (id: string) => nodeById.get(id)?.display_name ?? id;

  const openAgent = (agentId: string) => {
    navigate(`/agents/${encodeURIComponent(agentId)}`, {
      focusReturnId: agentId,
    });
  };

  const handleCellKeyDown = (
    event: KeyboardEvent<HTMLElement>,
    rowIdx: number,
    colIdx: number,
    nodeId: string
  ) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openAgent(nodeId);
      return;
    }

    const isRtl =
      gridRef.current !== null &&
      typeof window !== "undefined" &&
      window.getComputedStyle(gridRef.current).direction === "rtl";

    let target: AgentNodeItem | undefined;
    const curRow = navRows[rowIdx] ?? [];

    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      const step =
        (event.key === "ArrowRight" ? 1 : -1) * (isRtl ? -1 : 1);
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
                  (colIdx / Math.max(1, curRow.length - 1)) *
                    (targetRow.length - 1)
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

  const workingCount = data.nodes.filter((n) => n.live_status === "working").length;
  const waitingCount = data.nodes.filter((n) => n.live_status === "waiting").length;
  const readyCount = data.nodes.filter(
    (n) => n.live_status === "ready" || n.live_status === "idle"
  ).length;
  const blockedCount = data.nodes.filter((n) => n.live_status === "blocked").length;
  const archivedCount = data.nodes.filter((n) => n.archived || n.live_status === "archived").length;

  const listCols: DataColumn[] = [
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
  ];

  const listRows: DataRow[] = filteredNodes.map((node) => {
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
              art: node.avatar_variant,
              preview: node.grant_scope,
            }}
            onSelect={() => openAgent(node.agent_id)}
          />
        ),
        status: <Status tone={statusToneFor(node.live_status)}>{node.live_status}</Status>,
        task: (
          <Stack gap={4}>
            <Text variant="help">{node.current_task}</Text>
            {node.sandbox_activity ? (
              <Badge tone="neutral" variant="outline" dot={false}>
                Paper Sandbox ·{" "}
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
            onClick={() => openAgent(node.agent_id)}
          >
            Open Desk
          </Button>
        ),
      },
    };
  });

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
        data-dense={data.nodes.length >= 5 ? "true" : "false"}
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
          onClick={() => openAgent(node.agent_id)}
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
                  agent={node.avatar_variant}
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
                  Paper Sandbox ·{" "}
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
          {pairGroups.length} {pairGroups.length === 1 ? "Interaction Pair" : "Interaction Pairs"}
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
            <EmptyResult
              query={query || statusFilter}
              onClear={() => {
                setQuery("");
                setStatusFilter("all");
              }}
            />
          </Card>
        ) : (
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
                (pill.group.agentA === activeAgentId ||
                  pill.group.agentB === activeAgentId);
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
              {data.nodes.length <= 1 ? (
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
                            id: data.company_id,
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
                        onClick={() => navigate("/company/org")}
                      >
                        View Org
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
        )
      ) : (
        <Card title="All Company Agents" variant="raised" className="sd-ledger-card">
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
    </Stack>
  );
}
