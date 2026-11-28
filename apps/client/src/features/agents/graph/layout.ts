import type { AgentEdgeItem, AgentNodeItem } from "../../../api/types";
import { formatRelativeNs, normalizeToNs } from "../../../lib/format";

export type StatusFilter = "all" | "working" | "waiting" | "ready";
export type LayoutTier = "desktop" | "tablet" | "mobile";

export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
  w: number;
  h: number;
  cx: number;
  cy: number;
}

export interface ClusterBox {
  id: string;
  label: string;
  left: number;
  top: number;
  w: number;
  h: number;
}

export interface PairEdgeGroup {
  pairKey: string;
  agentA: string;
  agentB: string;
  primary: AgentEdgeItem;
  edges: AgentEdgeItem[];
  extraCount: number;
  hasActive: boolean;
}

export interface GraphRowSpec {
  id: string;
  clusterCols?: number;
  cells: Array<AgentNodeItem | null>;
}

export interface EdgeCurve {
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

export interface BoxBounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface PlacedPill {
  group: PairEdgeGroup;
  cx: number;
  cy: number;
  w: number;
  h: number;
  leader?: { x1: number; y1: number; x2: number; y2: number };
}

export interface AgentStatusSummary {
  workingCount: number;
  waitingCount: number;
  readyCount: number;
  blockedCount: number;
  archivedCount: number;
}

export const NODE_MARGIN = 10;
export const PILL_MARGIN = 6;
export const EDGE_PADDING = 8;

export function formatRelativeTimestamp(epoch: number | null | undefined): string {
  const ns = normalizeToNs(epoch);
  if (ns === null || ns <= 0) return "Unavailable";
  return formatRelativeNs(ns);
}

export function safeIsoFromEpoch(epoch: number | null | undefined): string | undefined {
  const ns = normalizeToNs(epoch);
  if (ns === null || ns <= 0) return undefined;
  const d = new Date(Math.floor(ns / 1_000_000));
  if (Number.isNaN(d.getTime())) return undefined;
  return d.toISOString();
}

export function statusToneFor(
  status: string
): "success" | "warning" | "danger" | "neutral" | "brand" {
  if (status === "working") return "brand";
  if (status === "ready") return "success";
  if (status === "waiting") return "warning";
  if (status === "blocked") return "danger";
  return "neutral";
}

export function clusterForAgent(node: AgentNodeItem): string {
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

export function edgeKindLabel(kind: AgentEdgeItem["kind"]): string {
  if (kind === "peer_request") return "Peer Request";
  if (kind === "handoff") return "Work Handoff";
  return "Internal Channel";
}

export function edgeKindTone(
  kind: AgentEdgeItem["kind"]
): "warning" | "brand" | "neutral" {
  if (kind === "peer_request") return "warning";
  if (kind === "channel") return "brand";
  return "neutral";
}

export function tierFromWidth(width: number): LayoutTier {
  if (width < 540) return "mobile";
  if (width < 840) return "tablet";
  return "desktop";
}

export function compactAgentTag(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "Agent";
  if (/\bCEO\b/i.test(trimmed)) return "CEO";
  const first = trimmed.split(/\s+/)[0] ?? trimmed;
  return first.length > 10 ? `${first.slice(0, 9)}…` : first;
}

export function pillDisplayText(group: PairEdgeGroup, pairPrefix?: string): string {
  const raw = group.primary.label;
  const short = raw.length > 18 ? `${raw.slice(0, 16)}…` : raw;
  const base = group.extraCount > 0 ? `${short} · +${group.extraCount}` : short;
  return pairPrefix ? `${pairPrefix} · ${base}` : base;
}

export function filterMatchingAgentIds(
  nodes: AgentNodeItem[],
  query: string,
  statusFilter: StatusFilter
): Set<string> {
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
}

export function filterEdges(
  edges: AgentEdgeItem[],
  matchingAgentIds: Set<string>,
  query: string,
  statusFilter: StatusFilter
): AgentEdgeItem[] {
  const q = query.trim().toLowerCase();
  const hasActiveFilter = q.length > 0 || statusFilter !== "all";
  if (!hasActiveFilter) return edges;
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
}

export function groupEdgesByPair(edges: AgentEdgeItem[]): PairEdgeGroup[] {
  const priority = { peer_request: 0, handoff: 1, channel: 2 } as const;
  const map = new Map<string, AgentEdgeItem[]>();
  for (const edge of edges) {
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
}

export function computeConnectedNeighbors(
  activeAgentId: string | null,
  edges: AgentEdgeItem[]
): Set<string> {
  const set = new Set<string>();
  if (!activeAgentId) return set;
  for (const edge of edges) {
    if (edge.source_agent_id === activeAgentId) set.add(edge.target_agent_id);
    if (edge.target_agent_id === activeAgentId) set.add(edge.source_agent_id);
  }
  return set;
}

export function summarizeAgentStatuses(nodes: AgentNodeItem[]): AgentStatusSummary {
  let workingCount = 0;
  let waitingCount = 0;
  let readyCount = 0;
  let blockedCount = 0;
  let archivedCount = 0;
  for (const n of nodes) {
    if (n.live_status === "working") workingCount += 1;
    else if (n.live_status === "waiting") waitingCount += 1;
    else if (n.live_status === "ready" || n.live_status === "idle") readyCount += 1;
    else if (n.live_status === "blocked") blockedCount += 1;
    if (n.archived || n.live_status === "archived") archivedCount += 1;
  }
  return { workingCount, waitingCount, readyCount, blockedCount, archivedCount };
}

export function buildGraphRows(
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

export function rectsIntersect(
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

export function evalQuadBezier(
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

export function computeEdgeCurve(
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

export function computeClusterBoxes(
  nodesCount: number,
  tier: LayoutTier,
  clusterMembers: Map<string, string[]>,
  nodeRects: Record<string, Rect>,
  canvasW: number,
  canvasH: number
): ClusterBox[] {
  if (nodesCount < 5 || tier !== "desktop") return [];
  const boxes: ClusterBox[] = [];
  for (const [cluster, memberIds] of clusterMembers.entries()) {
    const memberRects = memberIds
      .map((id) => nodeRects[id])
      .filter((r): r is Rect => Boolean(r));
    if (memberRects.length === 0) continue;
    const left = Math.max(8, Math.min(...memberRects.map((r) => r.left)) - 14);
    const top = Math.max(8, Math.min(...memberRects.map((r) => r.top)) - 32);
    const right = Math.min(
      canvasW - 8,
      Math.max(...memberRects.map((r) => r.right)) + 14
    );
    const bottom = Math.min(
      canvasH - 8,
      Math.max(...memberRects.map((r) => r.bottom)) + 14
    );
    const memberSet = new Set(memberIds);
    const overlapsOther = Object.entries(nodeRects).some(([id, r]) => {
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
}

export function placeEdgePills(
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

export function computeGraphEdgeLayout(
  pairGroups: PairEdgeGroup[],
  nodeRects: Record<string, Rect>,
  canvasW: number,
  canvasH: number,
  stageBottom: number,
  tier: LayoutTier,
  clusterBoxes: ClusterBox[]
): {
  curves: Map<string, EdgeCurve>;
  placedPills: PlacedPill[];
  railGroups: PairEdgeGroup[];
} {
  const curves = new Map<string, EdgeCurve>();
  if (canvasW > 0 && canvasH > 0) {
    pairGroups.forEach((group, idx) => {
      const curve = computeEdgeCurve(
        group.agentA,
        group.agentB,
        nodeRects,
        tier,
        idx,
        canvasW
      );
      if (curve) curves.set(group.pairKey, curve);
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
    curves,
    nodeRects,
    canvasW,
    stageBottom,
    tier,
    clusterLabelObstacles
  );
  return { curves, placedPills: placed, railGroups: rail };
}
