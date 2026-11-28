import { describe, expect, it } from "vitest";
import type { AgentEdgeItem, AgentNodeItem } from "../../../api/types";
import {
  buildGraphRows,
  clusterForAgent,
  compactAgentTag,
  computeClusterBoxes,
  computeConnectedNeighbors,
  computeEdgeCurve,
  computeGraphEdgeLayout,
  evalQuadBezier,
  filterEdges,
  filterMatchingAgentIds,
  formatRelativeTimestamp,
  groupEdgesByPair,
  pillDisplayText,
  placeEdgePills,
  rectsIntersect,
  summarizeAgentStatuses,
  tierFromWidth,
  type Rect,
} from "./layout";

function makeNode(overrides: Partial<AgentNodeItem> & { agent_id: string }): AgentNodeItem {
  return {
    display_name: overrides.agent_id,
    role_label: "Operations Specialist",
    is_ceo: false,
    avatar_variant: "wide-oval-top",
    avatar_state: "ready",
    live_status: "ready",
    archived: false,
    current_task: "Standing watch",
    active_run: null,
    monthly_budget_minor: 50_000,
    private_draft_count: 0,
    published_count: 0,
    grant_scope: "operations.standard",
    ...overrides,
  };
}

function makeEdge(
  overrides: Partial<AgentEdgeItem> & {
    edge_id: string;
    source_agent_id: string;
    target_agent_id: string;
  }
): AgentEdgeItem {
  return {
    kind: "channel",
    state: "open",
    active: true,
    label: "Desk sync",
    channel_id: "ch-sync",
    updated_at_ns: 1_700_000_000_000_000_000,
    ...overrides,
  };
}

function makeRect(left: number, top: number, w = 260, h = 140): Rect {
  return {
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

describe("agents/graph/layout", () => {
  describe("empty graph safety", () => {
    it("handles empty node and edge collections without throwing", () => {
      const built = buildGraphRows([], "desktop");
      expect(built.rows).toEqual([]);
      expect(built.navRows).toEqual([]);
      expect(built.clusterMembers.size).toBe(0);

      expect(groupEdgesByPair([])).toEqual([]);
      expect(computeClusterBoxes(0, "desktop", built.clusterMembers, {}, 1200, 800)).toEqual([]);
      expect(placeEdgePills([], new Map(), {}, 0, 0, "desktop")).toEqual({
        placed: [],
        rail: [],
      });
      expect(computeGraphEdgeLayout([], {}, 0, 0, 0, "desktop", [])).toEqual({
        curves: new Map(),
        placedPills: [],
        railGroups: [],
      });
      expect(filterMatchingAgentIds([], "", "all").size).toBe(0);
      expect(filterEdges([], new Set(), "alpha", "working")).toEqual([]);
      expect(computeConnectedNeighbors(null, []).size).toBe(0);
      expect(summarizeAgentStatuses([])).toEqual({
        workingCount: 0,
        waitingCount: 0,
        readyCount: 0,
        blockedCount: 0,
        archivedCount: 0,
      });
    });
  });

  describe("day-0 single-founder layout", () => {
    it("places single Founder CEO in one Executive row without cluster rings", () => {
      const ceo = makeNode({
        agent_id: "agent-ceo",
        display_name: "Founder · CEO",
        role_label: "Founder · CEO",
        is_ceo: true,
        live_status: "working",
      });

      expect(clusterForAgent(ceo)).toBe("Executive");

      const desktop = buildGraphRows([ceo], "desktop");
      expect(desktop.rows).toHaveLength(1);
      expect(desktop.rows[0].id).toBe("r-0");
      expect(desktop.rows[0].cells).toEqual([ceo]);
      expect(desktop.navRows).toEqual([[ceo]]);
      expect(desktop.clusterMembers.get("Executive")).toEqual(["agent-ceo"]);

      const boxes = computeClusterBoxes(
        1,
        "desktop",
        desktop.clusterMembers,
        { "agent-ceo": makeRect(40, 40) },
        960,
        420
      );
      expect(boxes).toEqual([]);
    });
  });

  describe("responsive tier & multi-agent cluster grouping", () => {
    it("maps container widths to mobile, tablet, and desktop tiers", () => {
      expect(tierFromWidth(320)).toBe("mobile");
      expect(tierFromWidth(539)).toBe("mobile");
      expect(tierFromWidth(540)).toBe("tablet");
      expect(tierFromWidth(839)).toBe("tablet");
      expect(tierFromWidth(840)).toBe("desktop");
      expect(tierFromWidth(1280)).toBe("desktop");
    });

    it("groups >= 5 agents on desktop into Executive + functional cluster columns deterministically", () => {
      const ceo = makeNode({
        agent_id: "ceo",
        display_name: "Founder · CEO",
        role_label: "Chief Executive",
        is_ceo: true,
      });
      const q1 = makeNode({
        agent_id: "q1",
        display_name: "Ada Quant",
        role_label: "Quant Researcher",
        grant_scope: "research.alpha",
      });
      const q2 = makeNode({
        agent_id: "q2",
        display_name: "Lin Alpha",
        role_label: "Alpha Strategist",
        grant_scope: "research.signals",
      });
      const risk = makeNode({
        agent_id: "r1",
        display_name: "Sora Risk",
        role_label: "Risk Officer",
        grant_scope: "compliance.limits",
      });
      const ops = makeNode({
        agent_id: "o1",
        display_name: "Mina Ops",
        role_label: "Operations Lead",
        grant_scope: "ops.delivery",
      });

      const nodes = [q1, risk, ceo, ops, q2];
      const firstRun = buildGraphRows(nodes, "desktop");
      const secondRun = buildGraphRows(nodes, "desktop");

      // Deterministic across repeated invocations.
      expect(firstRun.rows).toEqual(secondRun.rows);
      expect(firstRun.navRows).toEqual(secondRun.navRows);

      // Executive row first, then 2 cluster rows across 3 columns (Research, Risk & Settlement, Operations & QA).
      expect(firstRun.rows).toHaveLength(3);
      expect(firstRun.rows[0]).toEqual({ id: "exec-0", cells: [ceo] });
      expect(firstRun.rows[1].clusterCols).toBe(3);
      expect(firstRun.rows[1].cells.map((c) => c?.agent_id ?? null)).toEqual([
        "q1",
        "r1",
        "o1",
      ]);
      expect(firstRun.rows[2].clusterCols).toBe(3);
      expect(firstRun.rows[2].cells.map((c) => c?.agent_id ?? null)).toEqual([
        "q2",
        null,
        null,
      ]);

      // navRows strips null placeholders cleanly.
      expect(firstRun.navRows.map((row) => row.map((n) => n.agent_id))).toEqual([
        ["ceo"],
        ["q1", "r1", "o1"],
        ["q2"],
      ]);

      // Cluster boxes wrap non-overlapping cluster member rects on desktop.
      const nodeRects: Record<string, Rect> = {
        ceo: makeRect(420, 48, 260, 140),
        q1: makeRect(60, 260, 260, 140),
        r1: makeRect(420, 260, 260, 140),
        o1: makeRect(780, 260, 260, 140),
        q2: makeRect(60, 472, 260, 140),
      };
      const boxes = computeClusterBoxes(
        nodes.length,
        "desktop",
        firstRun.clusterMembers,
        nodeRects,
        1120,
        700
      );
      expect(boxes.map((b) => b.label).sort()).toEqual([
        "Executive",
        "Operations & QA",
        "Research",
        "Risk & Settlement",
      ]);

      const researchBox = boxes.find((b) => b.label === "Research")!;
      expect(researchBox.left).toBe(46);
      expect(researchBox.top).toBe(228);
      expect(researchBox.w).toBe(288);
      expect(researchBox.h).toBe(398);

      // Cluster boxes are suppressed on tablet/mobile.
      expect(
        computeClusterBoxes(nodes.length, "tablet", firstRun.clusterMembers, nodeRects, 800, 700)
      ).toEqual([]);
      expect(
        computeClusterBoxes(nodes.length, "mobile", firstRun.clusterMembers, nodeRects, 420, 900)
      ).toEqual([]);
    });

    it("stacks all agents into single-column rows on mobile and pairs on tablet", () => {
      const nodes = Array.from({ length: 5 }, (_, i) =>
        makeNode({
          agent_id: `a${i}`,
          is_ceo: i === 0,
          role_label: i === 0 ? "CEO" : "Quant",
        })
      );
      const mobile = buildGraphRows(nodes, "mobile");
      expect(mobile.rows).toHaveLength(5);
      expect(mobile.rows.every((r) => r.cells.length === 1)).toBe(true);

      const tablet = buildGraphRows(nodes, "tablet");
      expect(tablet.rows[0].cells.map((c) => c?.agent_id)).toEqual(["a0"]);
      expect(tablet.rows[1].cells.map((c) => c?.agent_id)).toEqual(["a1", "a2"]);
      expect(tablet.rows[2].cells.map((c) => c?.agent_id)).toEqual(["a3", "a4"]);
    });
  });

  describe("edge pairing, bezier curves, and pill placement", () => {
    it("groups symmetric edges per pair, excludes self-loops, and ranks active peer requests first", () => {
      const edges: AgentEdgeItem[] = [
        makeEdge({
          edge_id: "self",
          source_agent_id: "ceo",
          target_agent_id: "ceo",
          label: "Self note",
        }),
        makeEdge({
          edge_id: "e-ch",
          source_agent_id: "q1",
          target_agent_id: "ceo",
          kind: "channel",
          label: "Daily sync",
          updated_at_ns: 200,
        }),
        makeEdge({
          edge_id: "e-pr",
          source_agent_id: "ceo",
          target_agent_id: "q1",
          kind: "peer_request",
          label: "Review alpha risk",
          updated_at_ns: 100,
        }),
        makeEdge({
          edge_id: "e-inactive",
          source_agent_id: "ceo",
          target_agent_id: "r1",
          kind: "handoff",
          active: false,
          label: "Archived handoff",
          updated_at_ns: 999,
        }),
      ];

      const groups = groupEdgesByPair(edges);
      expect(groups).toHaveLength(2);
      // Active pair sorts before inactive pair even with lower timestamp.
      expect(groups[0].pairKey).toBe("ceo::q1");
      expect(groups[0].primary.edge_id).toBe("e-pr");
      expect(groups[0].extraCount).toBe(1);
      expect(groups[0].hasActive).toBe(true);

      expect(groups[1].pairKey).toBe("ceo::r1");
      expect(groups[1].hasActive).toBe(false);
    });

    it("computes deterministic quadratic bezier curves for direct, blocked, diagonal, and mobile layouts", () => {
      expect(evalQuadBezier(0, 0, 50, 100, 100, 0, 0)).toEqual({ x: 0, y: 0 });
      expect(evalQuadBezier(0, 0, 50, 100, 100, 0, 1)).toEqual({ x: 100, y: 0 });
      expect(evalQuadBezier(0, 0, 50, 100, 100, 0, 0.5)).toEqual({ x: 50, y: 50 });

      const rects: Record<string, Rect> = {
        top: makeRect(100, 40, 260, 120),
        mid: makeRect(100, 220, 260, 120),
        bot: makeRect(100, 400, 260, 120),
        right: makeRect(500, 40, 260, 120),
        farRight: makeRect(900, 40, 260, 120),
      };

      // Missing endpoint returns null.
      expect(computeEdgeCurve("top", "unknown", rects, "desktop", 0, 1200)).toBeNull();

      // Same column without blocker connects bottom -> top.
      const directCol = computeEdgeCurve("top", "mid", rects, "desktop", 0, 1200)!;
      expect(directCol.y1).toBe(rects.top.bottom);
      expect(directCol.y2).toBe(rects.mid.top);
      expect(directCol.d).toMatch(/^M .+ Q .+ .+$/);

      // Same column with blocker ("mid") routes through outer gutter.
      const blockedCol = computeEdgeCurve("top", "bot", rects, "desktop", 0, 1200)!;
      expect(blockedCol.x1).toBe(rects.top.right);
      expect(blockedCol.x2).toBe(rects.bot.right);
      expect(blockedCol.cx).toBeGreaterThan(rects.top.right);

      // Same row with blocker ("right") arches above or below.
      const blockedRow = computeEdgeCurve("top", "farRight", rects, "desktop", 0, 1200)!;
      expect(blockedRow.x1).toBe(rects.top.cx);
      expect(blockedRow.x2).toBe(rects.farRight.cx);

      // Mobile routes along the left gutter.
      const mobileCurve = computeEdgeCurve("top", "mid", rects, "mobile", 2, 400)!;
      expect(mobileCurve.x1).toBe(rects.top.left);
      expect(mobileCurve.x2).toBe(rects.mid.left);
      expect(mobileCurve.cx).toBeLessThan(rects.top.left);
    });

    it("places edge pills clear of node cards and obstacles, and routes to rail on mobile", () => {
      const rects: Record<string, Rect> = {
        ceo: makeRect(60, 60, 240, 120),
        quant: makeRect(460, 60, 240, 120),
      };
      const edges = [
        makeEdge({
          edge_id: "e1",
          source_agent_id: "ceo",
          target_agent_id: "quant",
          label: "Mandate check",
        }),
      ];
      const groups = groupEdgesByPair(edges);

      const desktopLayout = computeGraphEdgeLayout(
        groups,
        rects,
        900,
        420,
        360,
        "desktop",
        []
      );
      expect(desktopLayout.curves.size).toBe(1);
      expect(desktopLayout.placedPills).toHaveLength(1);
      expect(desktopLayout.railGroups).toHaveLength(0);

      const pill = desktopLayout.placedPills[0];
      const pLeft = pill.cx - pill.w / 2;
      const pRight = pill.cx + pill.w / 2;
      const pTop = pill.cy - pill.h / 2;
      const pBottom = pill.cy + pill.h / 2;

      for (const card of Object.values(rects)) {
        expect(
          rectsIntersect(pLeft, pTop, pRight, pBottom, card.left, card.top, card.right, card.bottom)
        ).toBe(false);
      }

      // Mobile always routes pills to the interaction rail.
      const mobileLayout = computeGraphEdgeLayout(
        groups,
        rects,
        380,
        500,
        460,
        "mobile",
        []
      );
      expect(mobileLayout.placedPills).toHaveLength(0);
      expect(mobileLayout.railGroups).toHaveLength(1);

      // Saturated stage where cards occupy all space routes pills to rail safely.
      const crowdedRects: Record<string, Rect> = {
        ceo: makeRect(0, 0, 200, 120),
        quant: makeRect(200, 0, 200, 120),
      };
      const crowdedLayout = computeGraphEdgeLayout(
        groups,
        crowdedRects,
        400,
        120,
        120,
        "desktop",
        []
      );
      expect(crowdedLayout.placedPills).toHaveLength(0);
      expect(crowdedLayout.railGroups).toHaveLength(1);
    });

    it("lays out 2, 3, and 4 agent teams deterministically on desktop", () => {
      const a0 = makeNode({ agent_id: "ceo", is_ceo: true });
      const a1 = makeNode({ agent_id: "q1", role_label: "Quant" });
      const a2 = makeNode({ agent_id: "r1", role_label: "Risk" });
      const a3 = makeNode({ agent_id: "o1", role_label: "Ops" });

      expect(buildGraphRows([a0, a1], "desktop").rows.map((r) => r.cells.length)).toEqual([2]);
      expect(buildGraphRows([a0, a1, a2], "desktop").rows.map((r) => r.cells.length)).toEqual([
        1, 2,
      ]);
      expect(buildGraphRows([a0, a1, a2, a3], "desktop").rows.map((r) => r.cells.length)).toEqual([
        2, 2,
      ]);
    });
  });

  describe("filtering, labels, and honest formatting", () => {
    it("filters agents and edges accurately and formats missing timestamps honestly", () => {
      const ceo = makeNode({
        agent_id: "ceo",
        display_name: "Founder · CEO",
        role_label: "CEO",
        is_ceo: true,
        live_status: "working",
        current_task: "Reviewing charter",
      });
      const qa = makeNode({
        agent_id: "qa",
        display_name: "Nora Validator",
        role_label: "QA Engineer",
        live_status: "idle",
        current_task: "Waiting on test suite",
      });
      const edges = [
        makeEdge({
          edge_id: "e1",
          source_agent_id: "ceo",
          target_agent_id: "qa",
          label: "Charter verification",
        }),
      ];

      expect(Array.from(filterMatchingAgentIds([ceo, qa], "", "ready"))).toEqual(["qa"]);
      expect(Array.from(filterMatchingAgentIds([ceo, qa], "charter", "all"))).toEqual(["ceo"]);
      expect(filterEdges(edges, new Set(["ceo"]), "charter", "all")).toHaveLength(1);
      expect(Array.from(computeConnectedNeighbors("ceo", edges))).toEqual(["qa"]);
      expect(summarizeAgentStatuses([ceo, qa])).toEqual({
        workingCount: 1,
        waitingCount: 0,
        readyCount: 1,
        blockedCount: 0,
        archivedCount: 0,
      });

      expect(compactAgentTag("Founder · CEO")).toBe("CEO");
      expect(compactAgentTag("Nora Validator")).toBe("Nora");
      expect(compactAgentTag("   ")).toBe("Agent");

      const group = groupEdgesByPair(edges)[0];
      expect(pillDisplayText(group)).toBe("Charter verifica…");
      expect(pillDisplayText(group, "CEO ↔ Nora")).toBe("CEO ↔ Nora · Charter verifica…");

      expect(formatRelativeTimestamp(null)).toBe("Unavailable");
      expect(formatRelativeTimestamp(undefined)).toBe("Unavailable");
      expect(formatRelativeTimestamp(0)).toBe("Unavailable");
    });
  });
});
