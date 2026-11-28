import { describe, expect, it } from "vitest";
import type { ReturnPoint } from "../../api/types";
import {
  buildSvgPath,
  buildValueSegments,
  clampScrubberIndex,
  computeChartGeometry,
  computeDeltaLabel,
  computeDriftDisplay,
  computeSlotMetrics,
  computeTimeframeSlices,
  computeXTicks,
  computeYDomain,
  computeYTicks,
  DAY_MS,
  extractPointTimeMs,
  extractSeriesValues,
  filterVisibleBenchmarkPoints,
  findFallbackIndex,
  findNearestBenchmarkPoint,
  findPreviousValidIndex,
  formatAxisTick,
  formatScrubberText,
  formatXAxisTick,
  snapPointerToIndex,
  truncateTick,
  xAtIndex,
  xForBenchmarkIndex,
  yAtValue,
  type TimestampedReturnPoint,
} from "./math";

function makePoint(overrides: Partial<TimestampedReturnPoint> = {}): TimestampedReturnPoint {
  return {
    id: overrides.id ?? "pt-1",
    label: overrides.label ?? "2026-01-01",
    return_pct: overrides.return_pct !== undefined ? overrides.return_pct : 1.5,
    pnl_minor: overrides.pnl_minor !== undefined ? overrides.pnl_minor : 15000,
    nav_minor: overrides.nav_minor !== undefined ? overrides.nav_minor : 1015000,
    source: overrides.source ?? "sealed",
    ...overrides,
  };
}

describe("chart/math — domain bounds & series extraction", () => {
  it("preserves null readings without coercing to zero", () => {
    const pts: ReturnPoint[] = [
      makePoint({ id: "a", return_pct: 2.5, pnl_minor: 25000 }),
      makePoint({ id: "b", return_pct: null, pnl_minor: null }),
      makePoint({ id: "c", return_pct: 0, pnl_minor: 0 }),
    ];

    expect(extractSeriesValues(pts, "pct")).toEqual([2.5, null, 0]);
    expect(extractSeriesValues(pts, "pnl")).toEqual([250, null, 0]);
  });

  it("computes standby fallback domain when series is empty or all null", () => {
    const empty = computeYDomain([]);
    expect(empty.hasAny).toBe(false);
    expect(empty.hasBenchmark).toBe(false);
    expect(empty.domain.minVal).toBe(-1);
    expect(empty.domain.maxVal).toBe(1);
    expect(empty.domain.yMin).toBeCloseTo(-1.32, 6);
    expect(empty.domain.yMax).toBeCloseTo(1.32, 6);

    const allNull = computeYDomain([null, Number.NaN, Number.POSITIVE_INFINITY]);
    expect(allNull.hasAny).toBe(false);
    expect(allNull.domain.yMin).toBeCloseTo(-1.32, 6);
    expect(allNull.domain.yMax).toBeCloseTo(1.32, 6);
  });

  it("anchors domain at zero, enforces minimum span of 0.5, and incorporates benchmark bounds", () => {
    // Tiny positive single point -> minVal anchored to 0, span clamped to 0.5, 16% padding = 0.08
    const singleTiny = computeYDomain([0.1]);
    expect(singleTiny.hasAny).toBe(true);
    expect(singleTiny.domain.minVal).toBe(0);
    expect(singleTiny.domain.maxVal).toBe(0.1);
    expect(singleTiny.domain.yMin).toBeCloseTo(-0.08, 6);
    expect(singleTiny.domain.yMax).toBeCloseTo(0.18, 6);

    // Benchmark extends negative bound below primary
    const withBench = computeYDomain([2, 5], [-4, 1]);
    expect(withBench.hasAny).toBe(true);
    expect(withBench.hasBenchmark).toBe(true);
    expect(withBench.domain.minVal).toBe(-4);
    expect(withBench.domain.maxVal).toBe(5);
    expect(withBench.domain.yMin).toBeCloseTo(-4 - 9 * 0.16, 6);
    expect(withBench.domain.yMax).toBeCloseTo(5 + 9 * 0.16, 6);
  });
});

describe("chart/math — geometry, scaling & interpolation", () => {
  it("computes geometry with clamped minimum inner width", () => {
    const normal = computeChartGeometry(560, 200, 12, 12);
    expect(normal.innerW).toBe(536);
    expect(normal.innerH).toBe(176);
    expect(normal.topY).toBe(12);
    expect(normal.bottomY).toBe(188);

    const cramped = computeChartGeometry(30, 200, 12, 12);
    expect(cramped.innerW).toBe(40);
  });

  it("scales x and y linearly and centers single-point plots", () => {
    const geom = computeChartGeometry(224, 200, 12, 12); // innerW = 200, innerH = 176
    expect(xAtIndex(0, 1, geom)).toBe(112);
    expect(xAtIndex(0, 0, geom)).toBe(112);
    expect(xAtIndex(0, 5, geom)).toBe(12);
    expect(xAtIndex(2, 5, geom)).toBe(112);
    expect(xAtIndex(4, 5, geom)).toBe(212);

    const domain = { minVal: -10, maxVal: 10, yMin: -10, yMax: 10 };
    expect(yAtValue(10, domain, geom)).toBeCloseTo(geom.topY, 6);
    expect(yAtValue(-10, domain, geom)).toBeCloseTo(geom.bottomY, 6);
    expect(yAtValue(0, domain, geom)).toBeCloseTo(geom.padY + geom.innerH / 2, 6);
  });

  it("interpolates benchmark x coordinates by timestamp and handles equal/single/unwindowed cases", () => {
    const geom = computeChartGeometry(224, 200, 12, 12); // padX=12, innerW=200
    const t0 = Date.UTC(2026, 0, 1);
    const primary: ReturnPoint[] = [
      makePoint({ id: "p0", label: "2026-01-01", observed_at_ns: t0 * 1_000_000 }),
      makePoint({ id: "p1", label: "2026-01-03", observed_at_ns: (t0 + 2 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "p2", label: "2026-01-05", observed_at_ns: (t0 + 4 * DAY_MS) * 1_000_000 }),
    ];
    const visTimes = primary.map((p) => extractPointTimeMs(p));

    // Single benchmark point centers horizontally
    const singleBench = [makePoint({ id: "b0", observed_at_ns: (t0 + DAY_MS) * 1_000_000 })];
    expect(xForBenchmarkIndex(0, singleBench, primary, visTimes, geom)).toBe(112);

    // Unequal length benchmark with mid-interval timestamp interpolates between p0 (x=12) and p1 (x=112)
    const bench: ReturnPoint[] = [
      makePoint({ id: "b0", observed_at_ns: (t0 - DAY_MS) * 1_000_000 }), // before start -> clamps to xAt(0)
      makePoint({ id: "b1", observed_at_ns: (t0 + DAY_MS) * 1_000_000 }), // halfway between p0 and p1 -> x=62
      makePoint({ id: "b2", observed_at_ns: (t0 + 3 * DAY_MS) * 1_000_000 }), // halfway between p1 and p2 -> x=162
      makePoint({ id: "b3", observed_at_ns: (t0 + 9 * DAY_MS) * 1_000_000 }), // after end -> clamps to xAt(2)=212
    ];
    expect(xForBenchmarkIndex(0, bench, primary, visTimes, geom)).toBeCloseTo(12, 6);
    expect(xForBenchmarkIndex(1, bench, primary, visTimes, geom)).toBeCloseTo(62, 6);
    expect(xForBenchmarkIndex(2, bench, primary, visTimes, geom)).toBeCloseTo(162, 6);
    expect(xForBenchmarkIndex(3, bench, primary, visTimes, geom)).toBeCloseTo(212, 6);

    // Equal length benchmark aligns directly by index
    expect(xForBenchmarkIndex(1, primary, primary, visTimes, geom)).toBeCloseTo(112, 6);

    // Unwindowed benchmark falls back to proportional index distribution
    const unwindowedPrimary = [
      makePoint({ id: "u0", label: "Cut 1", observed_at_ns: null }),
      makePoint({ id: "u1", label: "Cut 2", observed_at_ns: null }),
    ];
    const unwindowedBench = [
      makePoint({ id: "ub0", label: "B1", observed_at_ns: null }),
      makePoint({ id: "ub1", label: "B2", observed_at_ns: null }),
      makePoint({ id: "ub2", label: "B3", observed_at_ns: null }),
    ];
    expect(
      xForBenchmarkIndex(1, unwindowedBench, unwindowedPrimary, [null, null], geom)
    ).toBeCloseTo(112, 6);
  });

  it("splits polyline segments at missing intervals without bridging gaps", () => {
    const segs = buildValueSegments(
      [1, 2, null, 4, Number.NaN, 5],
      (idx) => idx * 10,
      (v) => 100 - v * 10
    );
    expect(segs).toHaveLength(3);
    expect(segs[0].map((p) => p.idx)).toEqual([0, 1]);
    expect(segs[1].map((p) => p.idx)).toEqual([3]);
    expect(segs[2].map((p) => p.idx)).toEqual([5]);

    expect(buildSvgPath(segs[0])).toBe("M 0.0 90.0 L 10.0 80.0");
    expect(buildSvgPath(segs[1])).toBe("M 30.0 60.0");
    expect(buildValueSegments([], () => 0, () => 0)).toEqual([]);
  });

  it("computes hit slot and gap widths cleanly for single and dense series", () => {
    expect(computeSlotMetrics(1, 400)).toEqual({ slotWidth: 400, gapWidth: 28 });
    expect(computeSlotMetrics(5, 400)).toEqual({ slotWidth: 100, gapWidth: 28 });
    expect(computeSlotMetrics(101, 400)).toEqual({ slotWidth: 4, gapWidth: 8 });
  });
});

describe("chart/math — tick generation & axis formatting", () => {
  it("formats X-axis labels cleanly in full and compact modes", () => {
    expect(formatXAxisTick("2026-04-05T14:30:00Z")).toBe("04-05 14:30");
    expect(formatXAxisTick("2026-04-05T14:30:00Z", true)).toBe("04-05");
    expect(formatXAxisTick("2026-04-05", false)).toBe("2026-04-05");
    expect(formatXAxisTick("2026-04-05", true)).toBe("04-05");
    expect(formatXAxisTick("  Cut #4  ")).toBe("Cut #4");
  });

  it("truncates tick text safely", () => {
    expect(truncateTick("abcdef", 6)).toBe("abcdef");
    expect(truncateTick("abcdef", 4)).toBe("abc…");
    expect(truncateTick("abcdef", 1)).toBe("a");
  });

  it("formats Y-axis ticks for pct and currency units", () => {
    expect(formatAxisTick(0, "pct", "USD")).toBe("0.0%");
    expect(formatAxisTick(3.25, "pct", "USD")).toBe("+3.3%");
    expect(formatAxisTick(-1.2, "pct", "USD")).toBe("-1.2%");

    expect(formatAxisTick(0, "pnl", "USD")).toBe("$0.00");
    expect(formatAxisTick(250, "pnl", "USD")).toBe("+$250");
    expect(formatAxisTick(-42.5, "pnl", "USD")).toBe("-$42.50");
    // Falls back safely on invalid currency code
    expect(formatAxisTick(120, "pnl", "INVALID")).toBe("+$120");
  });

  it("generates Y-axis ticks respecting minimum vertical spacing", () => {
    const geom = computeChartGeometry(560, 200, 12, 12);
    const domain = { minVal: -5, maxVal: 5, yMin: -6.6, yMax: 6.6 };
    const layout = computeYTicks(domain, geom, "pct", "USD");

    expect(layout.showMidPos).toBe(true);
    expect(layout.showMidNeg).toBe(true);
    expect(layout.ticks.map((t) => t.key)).toEqual(["zero", "max", "min", "midPos", "midNeg"]);
    expect(layout.ticks.find((t) => t.key === "zero")?.label).toBe("0.0%");

    // Very short plot suppresses colliding mid ticks
    const shortGeom = computeChartGeometry(560, 60, 12, 12); // innerH = 36
    const shortLayout = computeYTicks(domain, shortGeom, "pct", "USD");
    expect(shortLayout.showMidPos).toBe(false);
    expect(shortLayout.showMidNeg).toBe(false);
  });

  it("generates non-overlapping X-axis ticks across empty, single, wide, and cramped widths", () => {
    const wideGeom = computeChartGeometry(600, 200, 12, 12);
    const fixedMeasure = (text: string) => text.length * 8;

    // Empty
    expect(computeXTicks([], wideGeom, fixedMeasure)).toEqual([]);

    // Single point
    const onePt = [makePoint({ id: "only", label: "2026-04-01T09:00:00Z" })];
    expect(computeXTicks(onePt, wideGeom, fixedMeasure)).toEqual([
      { idx: 0, label: "04-01 09:00", edge: "mid" },
    ]);

    // Multiple points on wide plot keeps start, mids, and end without collisions
    const manyPts: ReturnPoint[] = Array.from({ length: 6 }, (_, i) =>
      makePoint({ id: `p${i}`, label: `2026-04-0${i + 1}` })
    );
    const wideTicks = computeXTicks(manyPts, wideGeom, fixedMeasure);
    expect(wideTicks[0]).toEqual({ idx: 0, label: "2026-04-01", edge: "start" });
    expect(wideTicks[wideTicks.length - 1]).toEqual({
      idx: 5,
      label: "2026-04-06",
      edge: "end",
    });
    expect(wideTicks.length).toBeGreaterThan(2);

    // Ultra-narrow plot collapses to a single start tick instead of overlapping
    const ultraNarrow = computeChartGeometry(60, 200, 12, 12); // innerW = 40 -> maxEndPx = 13 < 32
    const collapsed = computeXTicks(manyPts, ultraNarrow, fixedMeasure);
    expect(collapsed).toHaveLength(1);
    expect(collapsed[0].idx).toBe(0);
    expect(collapsed[0].edge).toBe("start");
  });
});

describe("chart/math — timestamps & timeframe slicing", () => {
  it("extracts timestamps from ns, cut_at_ns, numeric observed_at, ISO string, or label", () => {
    const ms = Date.UTC(2026, 2, 15, 12, 0, 0);
    expect(extractPointTimeMs(makePoint({ observed_at_ns: ms * 1_000_000 }))).toBe(ms);
    expect(extractPointTimeMs(makePoint({ observed_at_ns: null, cut_at_ns: ms * 1_000_000 }))).toBe(
      ms
    );
    expect(extractPointTimeMs(makePoint({ observed_at_ns: null, observed_at: ms }))).toBe(ms);
    expect(
      extractPointTimeMs(makePoint({ observed_at_ns: null, observed_at: Math.floor(ms / 1000) }))
    ).toBe(ms);
    expect(
      extractPointTimeMs(
        makePoint({ observed_at_ns: null, observed_at: "2026-03-15T12:00:00Z" })
      )
    ).toBe(ms);
    expect(
      extractPointTimeMs(makePoint({ observed_at_ns: null, label: "2026-03-15T12:00:00Z" }))
    ).toBe(ms);
    expect(extractPointTimeMs(makePoint({ observed_at_ns: null, label: "Cut A" }))).toBeNull();
  });

  it("computes timeframe availability and slices for multi-week timestamped series", () => {
    const end = Date.UTC(2026, 3, 20, 12, 0, 0);
    const pts: ReturnPoint[] = [
      makePoint({ id: "d-40", observed_at_ns: (end - 40 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "d-15", observed_at_ns: (end - 15 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "d-4", observed_at_ns: (end - 4 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "d-0.5", observed_at_ns: (end - 0.5 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "d-0", observed_at_ns: end * 1_000_000 }),
    ];

    const info = computeTimeframeSlices(pts);
    expect(info.hasTimestamps).toBe(true);
    expect(info.availability).toEqual({ "1d": true, "7d": true, "30d": true, all: true });
    expect(info.slices["1d"].map((p) => p.id)).toEqual(["d-0.5", "d-0"]);
    expect(info.slices["7d"].map((p) => p.id)).toEqual(["d-4", "d-0.5", "d-0"]);
    expect(info.slices["30d"].map((p) => p.id)).toEqual(["d-15", "d-4", "d-0.5", "d-0"]);
    expect(info.slices.all).toHaveLength(5);

    // Single point disables windowed timeframes
    const singleInfo = computeTimeframeSlices([pts[0]]);
    expect(singleInfo.hasTimestamps).toBe(false);
    expect(singleInfo.availability).toEqual({ "1d": false, "7d": false, "30d": false, all: true });
  });

  it("filters benchmark points to visible primary timeframe", () => {
    const t0 = Date.UTC(2026, 0, 1);
    const vis = [
      makePoint({ id: "v1", observed_at_ns: (t0 + 5 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "v2", observed_at_ns: (t0 + 10 * DAY_MS) * 1_000_000 }),
    ];
    const bench = [
      makePoint({ id: "b0", observed_at_ns: t0 * 1_000_000 }),
      makePoint({ id: "b1", observed_at_ns: (t0 + 6 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "b2", observed_at_ns: (t0 + 9 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "b3", observed_at_ns: (t0 + 20 * DAY_MS) * 1_000_000 }),
    ];
    expect(filterVisibleBenchmarkPoints(bench, vis).map((p) => p.id)).toEqual(["b1", "b2"]);
    expect(filterVisibleBenchmarkPoints(undefined, vis)).toEqual([]);
  });
});

describe("chart/math — nearest-point lookup, scrubber snapping & readout formatting", () => {
  it("snaps pointer coordinates to nearest index with boundary clamping", () => {
    const geom = computeChartGeometry(224, 200, 12, 12); // padX=12, innerW=200
    expect(snapPointerToIndex(100, 0, geom, 0)).toBeNull();
    expect(snapPointerToIndex(100, 0, geom, 1)).toBe(0);

    // 5 points -> indices 0,1,2,3,4 at x = 12, 62, 112, 162, 212
    expect(snapPointerToIndex(0, 0, geom, 5)).toBe(0);
    expect(snapPointerToIndex(60, 0, geom, 5)).toBe(1);
    expect(snapPointerToIndex(112, 0, geom, 5)).toBe(2);
    expect(snapPointerToIndex(500, 0, geom, 5)).toBe(4);

    expect(clampScrubberIndex(-3, 5)).toBe(0);
    expect(clampScrubberIndex(2.6, 5)).toBe(3);
    expect(clampScrubberIndex(99, 5)).toBe(4);
    expect(clampScrubberIndex(5, 0)).toBe(0);
  });

  it("finds fallback and previous valid indices skipping null gaps", () => {
    expect(findFallbackIndex([], 0)).toBeNull();
    expect(findFallbackIndex([null, null], 2)).toBe(1);
    expect(findFallbackIndex([1, 2, null], 3)).toBe(1);

    expect(findPreviousValidIndex([1, null, 3], 2)).toBe(0);
    expect(findPreviousValidIndex([null, null, 3], 2)).toBeNull();
    expect(findPreviousValidIndex([1, null, 3], 1)).toBeNull();
  });

  it("finds nearest benchmark point by timestamp proximity or ratio fallback", () => {
    const t0 = Date.UTC(2026, 0, 1);
    const vis = [
      makePoint({ id: "v0", observed_at_ns: t0 * 1_000_000 }),
      makePoint({ id: "v1", observed_at_ns: (t0 + 2 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "v2", observed_at_ns: (t0 + 5 * DAY_MS) * 1_000_000 }),
    ];
    const visTimes = vis.map((p) => extractPointTimeMs(p));
    const bench = [
      makePoint({ id: "b0", observed_at_ns: (t0 + 0.1 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "b1", observed_at_ns: (t0 + 2.2 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "b2", observed_at_ns: (t0 + 4.8 * DAY_MS) * 1_000_000 }),
      makePoint({ id: "b3", observed_at_ns: (t0 + 6 * DAY_MS) * 1_000_000 }),
    ];

    expect(findNearestBenchmarkPoint(1, vis, visTimes, bench, true)?.id).toBe("b1");
    expect(findNearestBenchmarkPoint(1, vis, visTimes, bench, false)).toBeNull();
    expect(findNearestBenchmarkPoint(null, vis, visTimes, bench, true)).toBeNull();
  });

  it("computes delta labels, drift display, and scrubber announcement text honestly", () => {
    const pts: ReturnPoint[] = [
      makePoint({
        id: "p0",
        label: "Cut 1",
        return_pct: 1.25,
        pnl_minor: 12500,
        nav_minor: 1012500,
      }),
      makePoint({
        id: "p1",
        label: "Cut 2",
        return_pct: null,
        pnl_minor: null,
        nav_minor: null,
      }),
      makePoint({
        id: "p2",
        label: "Cut 3",
        return_pct: 2.75,
        pnl_minor: 27500,
        nav_minor: 1027500,
      }),
    ];

    expect(computeDeltaLabel(pts[2], 2.75, 0, pts, "pct", "USD")).toBe("+1.50 pp vs Cut 1");
    expect(computeDeltaLabel(pts[2], 275, 0, pts, "pnl", "USD")).toBe("+$150.00 vs Cut 1");
    expect(computeDeltaLabel(pts[1], null, null, pts, "pct", "USD")).toBe("No prior reading");

    const benchPt = makePoint({ id: "b2", return_pct: 2.25 });
    expect(computeDriftDisplay(pts[2], benchPt, 0.1)).toBeCloseTo(0.5, 6);
    expect(computeDriftDisplay(pts[1], benchPt, 0.1)).toBe(0.1);
    expect(computeDriftDisplay(pts[1], null, null)).toBeNull();

    expect(formatScrubberText(0, pts, "USD")).toBe("Cut 1: +1.25%, +$125.00, NAV $10,125.00");
    expect(formatScrubberText(1, pts, "USD")).toBe("Cut 2: No reading (missing interval)");
    expect(formatScrubberText(0, [], "USD")).toBe("No reading");
  });
});
