import type { ReturnPoint } from "../../api/types";
import { formatMinorCurrency, formatSignedPct } from "../../lib/format";

export type ChartUnit = "pct" | "pnl";
export type Timeframe = "all" | "30d" | "7d" | "1d";

export type TimestampedReturnPoint = ReturnPoint & {
  observed_at?: number | string | null;
  observed_at_ns?: number | null;
  cut_at_ns?: number | null;
};

export interface ChartDomain {
  minVal: number;
  maxVal: number;
  yMin: number;
  yMax: number;
}

export interface ChartGeometry {
  plotWidth: number;
  plotHeight: number;
  padX: number;
  padY: number;
  innerW: number;
  innerH: number;
  topY: number;
  bottomY: number;
}

export interface YAxisTick {
  key: string;
  y: number;
  label: string;
}

export interface YAxisLayout {
  zeroY: number;
  midPosY: number;
  midNegY: number;
  showMidPos: boolean;
  showMidNeg: boolean;
  ticks: YAxisTick[];
}

export interface XAxisTick {
  idx: number;
  label: string;
  edge: "start" | "mid" | "end";
}

export interface PlottedPoint {
  x: number;
  y: number;
  value: number;
  idx: number;
}

export interface TimeframeSlices {
  hasTimestamps: boolean;
  availability: Record<Timeframe, boolean>;
  slices: Record<Timeframe, ReturnPoint[]>;
}

export const DAY_MS = 86_400_000;
export const DEFAULT_PLOT_HEIGHT = 200;
export const DEFAULT_PAD_X = 12;
export const DEFAULT_PAD_Y = 12;

const ISO_DATE_RE =
  /^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;
const ISO_DATETIME_RE =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$/;
const ISO_DATE_ONLY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

export function formatXAxisTick(label: string, compact = false): string {
  const trimmed = label.trim();
  const dtMatch = ISO_DATETIME_RE.exec(trimmed);
  if (dtMatch) {
    return compact
      ? `${dtMatch[2]}-${dtMatch[3]}`
      : `${dtMatch[2]}-${dtMatch[3]} ${dtMatch[4]}:${dtMatch[5]}`;
  }
  if (compact) {
    const dMatch = ISO_DATE_ONLY_RE.exec(trimmed);
    if (dMatch) return `${dMatch[2]}-${dMatch[3]}`;
  }
  return trimmed;
}

export function truncateTick(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  if (maxChars <= 1) return text.slice(0, 1);
  return `${text.slice(0, maxChars - 1)}…`;
}

export function defaultMeasureTickWidth(text: string): number {
  return Math.max(24, text.length * 10.5);
}

export function extractPointTimeMs(pt: TimestampedReturnPoint): number | null {
  if (
    typeof pt.observed_at_ns === "number" &&
    Number.isFinite(pt.observed_at_ns) &&
    pt.observed_at_ns > 0
  ) {
    return pt.observed_at_ns / 1_000_000;
  }
  if (
    typeof pt.cut_at_ns === "number" &&
    Number.isFinite(pt.cut_at_ns) &&
    pt.cut_at_ns > 0
  ) {
    return pt.cut_at_ns / 1_000_000;
  }
  if (
    typeof pt.observed_at === "number" &&
    Number.isFinite(pt.observed_at) &&
    pt.observed_at > 0
  ) {
    if (pt.observed_at > 1e15) return pt.observed_at / 1_000_000;
    if (pt.observed_at > 1e12) return pt.observed_at;
    return pt.observed_at * 1000;
  }
  if (typeof pt.observed_at === "string" && ISO_DATE_RE.test(pt.observed_at.trim())) {
    const parsed = Date.parse(pt.observed_at.trim());
    return Number.isFinite(parsed) ? parsed : null;
  }
  if (ISO_DATE_RE.test(pt.label.trim())) {
    const parsed = Date.parse(pt.label.trim());
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function computeTimeframeSlices(points: ReturnPoint[]): TimeframeSlices {
  const timestamps = points.map((pt) => extractPointTimeMs(pt as TimestampedReturnPoint));
  const hasTimestamps = points.length >= 2 && timestamps.every((t) => t !== null);
  if (!hasTimestamps) {
    return {
      hasTimestamps: false,
      availability: { "1d": false, "7d": false, "30d": false, all: true },
      slices: {
        "1d": points,
        "7d": points,
        "30d": points,
        all: points,
      },
    };
  }

  const numericTimes = timestamps as number[];
  const latestMs = Math.max(...numericTimes);
  const earliestMs = Math.min(...numericTimes);
  const totalSpanMs = latestMs - earliestMs;
  const slice1d = points.filter((_, i) => numericTimes[i] >= latestMs - DAY_MS);
  const slice7d = points.filter((_, i) => numericTimes[i] >= latestMs - 7 * DAY_MS);
  const slice30d = points.filter((_, i) => numericTimes[i] >= latestMs - 30 * DAY_MS);

  return {
    hasTimestamps: true,
    availability: {
      "1d": slice1d.length >= 2,
      "7d": totalSpanMs > DAY_MS && slice7d.length >= 2,
      "30d": totalSpanMs > 7 * DAY_MS && slice30d.length >= 2,
      all: true,
    },
    slices: {
      "1d": slice1d,
      "7d": slice7d,
      "30d": slice30d,
      all: points,
    },
  };
}

export function filterVisibleBenchmarkPoints(
  benchmarkPoints: ReturnPoint[] | undefined,
  visiblePoints: ReturnPoint[]
): ReturnPoint[] {
  if (!benchmarkPoints || benchmarkPoints.length === 0) return [];
  if (visiblePoints.length === 0) return benchmarkPoints;
  const visTimes = visiblePoints.map((pt) => extractPointTimeMs(pt as TimestampedReturnPoint));
  const benchTimes = benchmarkPoints.map((pt) => extractPointTimeMs(pt as TimestampedReturnPoint));
  if (visTimes.every((t) => t !== null) && benchTimes.every((t) => t !== null)) {
    const minVis = Math.min(...(visTimes as number[]));
    const maxVis = Math.max(...(visTimes as number[]));
    const filtered = benchmarkPoints.filter((_, i) => {
      const bt = (benchTimes as number[])[i];
      return bt >= minVis - 1_000 && bt <= maxVis + 1_000;
    });
    return filtered.length > 0 ? filtered : benchmarkPoints;
  }
  return benchmarkPoints;
}

export function extractSeriesValues(
  points: ReturnPoint[],
  unit: ChartUnit
): Array<number | null> {
  return points.map((pt) =>
    unit === "pct"
      ? pt.return_pct
      : pt.pnl_minor === null
        ? null
        : pt.pnl_minor / 100
  );
}

export function computeYDomain(
  primaryValues: Array<number | null>,
  benchmarkValues: Array<number | null> = []
): { hasAny: boolean; hasBenchmark: boolean; domain: ChartDomain } {
  const validValues = primaryValues.filter(
    (v): v is number => v !== null && Number.isFinite(v)
  );
  const validBenchValues = benchmarkValues.filter(
    (v): v is number => v !== null && Number.isFinite(v)
  );
  const hasAny = validValues.length > 0;
  const hasBenchmark = validBenchValues.length > 0;

  const allValidForScale = hasBenchmark
    ? [...validValues, ...validBenchValues]
    : validValues;
  const minVal = hasAny ? Math.min(0, ...allValidForScale) : -1;
  const maxVal = hasAny ? Math.max(0, ...allValidForScale) : 1;
  const span = Math.max(maxVal - minVal, 0.5);
  const pad = span * 0.16;
  const yMin = minVal - pad;
  const yMax = maxVal + pad;

  return {
    hasAny,
    hasBenchmark,
    domain: { minVal, maxVal, yMin, yMax },
  };
}

export function computeChartGeometry(
  plotWidth: number,
  plotHeight = DEFAULT_PLOT_HEIGHT,
  padX = DEFAULT_PAD_X,
  padY = DEFAULT_PAD_Y
): ChartGeometry {
  const innerW = Math.max(40, plotWidth - padX * 2);
  const innerH = plotHeight - padY * 2;
  return {
    plotWidth,
    plotHeight,
    padX,
    padY,
    innerW,
    innerH,
    topY: padY,
    bottomY: padY + innerH,
  };
}

export function xAtIndex(idx: number, count: number, geom: ChartGeometry): number {
  return count <= 1
    ? geom.padX + geom.innerW / 2
    : geom.padX + (idx / (count - 1)) * geom.innerW;
}

export function yAtValue(
  value: number,
  domain: ChartDomain,
  geom: ChartGeometry
): number {
  return geom.padY + ((domain.yMax - value) / (domain.yMax - domain.yMin)) * geom.innerH;
}

export function xForBenchmarkIndex(
  bIdx: number,
  benchmarkPoints: ReturnPoint[],
  visiblePoints: ReturnPoint[],
  visibleTimes: Array<number | null>,
  geom: ChartGeometry
): number {
  const m = benchmarkPoints.length;
  if (m <= 1) return geom.padX + geom.innerW / 2;
  if (m === visiblePoints.length) return xAtIndex(bIdx, visiblePoints.length, geom);
  const bTime = extractPointTimeMs(benchmarkPoints[bIdx] as TimestampedReturnPoint);
  if (bTime !== null && visiblePoints.length >= 2 && visibleTimes.every((t) => t !== null)) {
    const vt = visibleTimes as number[];
    if (bTime <= vt[0]) return xAtIndex(0, visiblePoints.length, geom);
    if (bTime >= vt[vt.length - 1]) {
      return xAtIndex(vt.length - 1, visiblePoints.length, geom);
    }
    for (let i = 0; i < vt.length - 1; i += 1) {
      if (bTime >= vt[i] && bTime <= vt[i + 1]) {
        const spanT = vt[i + 1] - vt[i];
        const frac = spanT > 0 ? (bTime - vt[i]) / spanT : 0;
        const x0 = xAtIndex(i, visiblePoints.length, geom);
        const x1 = xAtIndex(i + 1, visiblePoints.length, geom);
        return x0 + frac * (x1 - x0);
      }
    }
  }
  return geom.padX + (bIdx / Math.max(1, m - 1)) * geom.innerW;
}

export function computeSlotMetrics(
  pointCount: number,
  innerW: number
): { slotWidth: number; gapWidth: number } {
  const slotWidth = pointCount <= 1 ? innerW : innerW / Math.max(1, pointCount - 1);
  const gapWidth = Math.max(8, Math.min(28, slotWidth * 0.65));
  return { slotWidth, gapWidth };
}

export function buildValueSegments(
  values: Array<number | null>,
  xForIndex: (idx: number) => number,
  yForValue: (v: number) => number
): PlottedPoint[][] {
  const segments: PlottedPoint[][] = [];
  let current: PlottedPoint[] = [];
  values.forEach((v, idx) => {
    if (v === null || !Number.isFinite(v)) {
      if (current.length > 0) {
        segments.push(current);
        current = [];
      }
    } else {
      current.push({ x: xForIndex(idx), y: yForValue(v), value: v, idx });
    }
  });
  if (current.length > 0) segments.push(current);
  return segments;
}

export function buildSvgPath(segment: PlottedPoint[]): string {
  return segment
    .map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`)
    .join(" ");
}

export function formatAxisTick(
  value: number,
  unit: ChartUnit,
  currency: string
): string {
  if (unit === "pct") {
    return `${value > 0 ? "+" : value < 0 ? "-" : ""}${Math.abs(value).toFixed(1)}%`;
  }
  const safeCurrency = /^[A-Z]{3}$/.test(currency) ? currency : "USD";
  const absFormatted = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: safeCurrency,
    maximumFractionDigits: Math.abs(value) >= 100 ? 0 : 2,
  }).format(Math.abs(value));
  if (value > 0) return `+${absFormatted}`;
  if (value < 0) return `-${absFormatted}`;
  return absFormatted;
}

export function computeYTicks(
  domain: ChartDomain,
  geom: ChartGeometry,
  unit: ChartUnit,
  currency: string
): YAxisLayout {
  const zeroY = yAtValue(0, domain, geom);
  const midPosVal = domain.yMax / 2;
  const midNegVal = domain.yMin / 2;
  const midPosY = yAtValue(midPosVal, domain, geom);
  const midNegY = yAtValue(midNegVal, domain, geom);
  const showMidPos =
    domain.yMax > 0 &&
    Math.abs(midPosY - geom.topY) >= 24 &&
    Math.abs(zeroY - midPosY) >= 24;
  const showMidNeg =
    domain.yMin < 0 &&
    Math.abs(geom.bottomY - midNegY) >= 24 &&
    Math.abs(midNegY - zeroY) >= 24;

  const ticks: YAxisTick[] = [
    {
      key: "zero",
      y: zeroY,
      label: unit === "pct" ? "0.0%" : formatAxisTick(0, "pnl", currency),
    },
  ];
  if (Math.abs(geom.topY - zeroY) >= 18) {
    ticks.push({
      key: "max",
      y: geom.topY,
      label: formatAxisTick(domain.yMax, unit, currency),
    });
  }
  if (Math.abs(geom.bottomY - zeroY) >= 18) {
    ticks.push({
      key: "min",
      y: geom.bottomY,
      label: formatAxisTick(domain.yMin, unit, currency),
    });
  }
  if (showMidPos) {
    ticks.push({
      key: "midPos",
      y: midPosY,
      label: formatAxisTick(midPosVal, unit, currency),
    });
  }
  if (showMidNeg) {
    ticks.push({
      key: "midNeg",
      y: midNegY,
      label: formatAxisTick(midNegVal, unit, currency),
    });
  }

  return { zeroY, midPosY, midNegY, showMidPos, showMidNeg, ticks };
}

export function computeXTicks(
  points: ReturnPoint[],
  geom: ChartGeometry,
  measureWidth: (text: string) => number = defaultMeasureTickWidth
): XAxisTick[] {
  const n = points.length;
  if (n === 0) return [];

  const MIN_GAP = 14;
  const fitTickToWidth = (label: string, maxPx: number): string => {
    if (measureWidth(label) <= maxPx) return label;
    let lo = 1;
    let hi = label.length;
    let best = truncateTick(label, 1);
    while (lo <= hi) {
      const mid = Math.floor((lo + hi) / 2);
      const candidate = truncateTick(label, mid);
      if (measureWidth(candidate) <= maxPx) {
        best = candidate;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return best;
  };

  if (n === 1) {
    const single = fitTickToWidth(formatXAxisTick(points[0].label), geom.innerW);
    return [{ idx: 0, label: single, edge: "mid" }];
  }

  const boundsFor = (idx: number, text: string): [number, number] => {
    const xPx = geom.padX + (idx / (n - 1)) * geom.innerW;
    const w = measureWidth(text);
    if (idx === 0) return [xPx, xPx + w];
    if (idx === n - 1) return [xPx - w, xPx];
    return [xPx - w / 2, xPx + w / 2];
  };

  let firstLabel = formatXAxisTick(points[0].label);
  let lastLabel = formatXAxisTick(points[n - 1].label);
  let firstBounds = boundsFor(0, firstLabel);
  let lastBounds = boundsFor(n - 1, lastLabel);

  if (firstBounds[1] + MIN_GAP > lastBounds[0]) {
    firstLabel = formatXAxisTick(points[0].label, true);
    lastLabel = formatXAxisTick(points[n - 1].label, true);
    firstBounds = boundsFor(0, firstLabel);
    lastBounds = boundsFor(n - 1, lastLabel);
  }

  if (firstBounds[1] + MIN_GAP > lastBounds[0]) {
    const spanPx = Math.max(0, lastBounds[1] - firstBounds[0] - MIN_GAP);
    const maxEndPx = Math.floor(spanPx / 2);
    if (maxEndPx < 32) {
      return [{ idx: 0, label: fitTickToWidth(firstLabel, geom.innerW), edge: "start" }];
    }
    firstLabel = fitTickToWidth(firstLabel, maxEndPx);
    lastLabel = fitTickToWidth(lastLabel, maxEndPx);
    firstBounds = [
      firstBounds[0],
      firstBounds[0] + Math.min(maxEndPx, measureWidth(firstLabel)),
    ];
    lastBounds = [
      lastBounds[1] - Math.min(maxEndPx, measureWidth(lastLabel)),
      lastBounds[1],
    ];
  }

  const kept: Array<{
    idx: number;
    label: string;
    edge: "start" | "mid" | "end";
    bounds: [number, number];
  }> = [{ idx: 0, label: firstLabel, edge: "start", bounds: firstBounds }];

  for (let idx = 1; idx < n - 1; idx += 1) {
    const label = formatXAxisTick(points[idx].label);
    const bounds = boundsFor(idx, label);
    const prev = kept[kept.length - 1];
    if (bounds[0] >= prev.bounds[1] + MIN_GAP && bounds[1] + MIN_GAP <= lastBounds[0]) {
      kept.push({ idx, label, edge: "mid", bounds });
    }
  }

  kept.push({ idx: n - 1, label: lastLabel, edge: "end", bounds: lastBounds });
  return kept.map(({ idx, label, edge }) => ({ idx, label, edge }));
}

export function findFallbackIndex(
  values: Array<number | null>,
  pointCount: number
): number | null {
  for (let i = pointCount - 1; i >= 0; i -= 1) {
    if (values[i] !== null) return i;
  }
  return pointCount > 0 ? pointCount - 1 : null;
}

export function findPreviousValidIndex(
  values: Array<number | null>,
  inspectedIdx: number | null
): number | null {
  if (inspectedIdx === null || values[inspectedIdx] === null) return null;
  for (let i = inspectedIdx - 1; i >= 0; i -= 1) {
    const val = values[i];
    if (val !== null && Number.isFinite(val)) return i;
  }
  return null;
}

export function clampScrubberIndex(rawIndex: number, pointCount: number): number {
  if (pointCount <= 0) return 0;
  return Math.max(0, Math.min(pointCount - 1, Math.round(rawIndex)));
}

export function snapPointerToIndex(
  clientX: number,
  rectLeft: number,
  geom: ChartGeometry,
  pointCount: number
): number | null {
  if (pointCount <= 0) return null;
  if (pointCount === 1) return 0;
  const relX = clientX - rectLeft - geom.padX;
  const ratio = geom.innerW > 0 ? Math.max(0, Math.min(1, relX / geom.innerW)) : 0;
  return Math.round(ratio * (pointCount - 1));
}

export function findNearestBenchmarkPoint(
  inspectedIdx: number | null,
  visiblePoints: ReturnPoint[],
  visibleTimes: Array<number | null>,
  visibleBenchmarkPoints: ReturnPoint[],
  hasBenchmark: boolean
): ReturnPoint | null {
  if (!hasBenchmark || inspectedIdx === null || visibleBenchmarkPoints.length === 0) {
    return null;
  }
  if (visibleBenchmarkPoints.length === visiblePoints.length) {
    return visibleBenchmarkPoints[inspectedIdx] ?? null;
  }
  const targetTime = visibleTimes[inspectedIdx];
  if (targetTime !== null) {
    let best: ReturnPoint | null = null;
    let bestDiff = Number.POSITIVE_INFINITY;
    for (const bp of visibleBenchmarkPoints) {
      const bt = extractPointTimeMs(bp as TimestampedReturnPoint);
      if (bt === null) continue;
      const diff = Math.abs(bt - targetTime);
      if (diff < bestDiff) {
        bestDiff = diff;
        best = bp;
      }
    }
    if (best) return best;
  }
  const ratio =
    visiblePoints.length <= 1 ? 1 : inspectedIdx / Math.max(1, visiblePoints.length - 1);
  const mappedIdx = Math.round(ratio * (visibleBenchmarkPoints.length - 1));
  return visibleBenchmarkPoints[mappedIdx] ?? null;
}

export function computeDeltaLabel(
  inspectedPoint: ReturnPoint | null,
  inspectedValue: number | null,
  prevValidIdx: number | null,
  visiblePoints: ReturnPoint[],
  unit: ChartUnit,
  currency: string
): string {
  if (inspectedPoint === null || inspectedValue === null || prevValidIdx === null) {
    return "No prior reading";
  }
  const priorPt = visiblePoints[prevValidIdx];
  if (!priorPt) {
    return "No prior reading";
  }
  if (unit === "pct") {
    if (inspectedPoint.return_pct === null || priorPt.return_pct === null) {
      return "No prior reading";
    }
    const deltaPp = inspectedPoint.return_pct - priorPt.return_pct;
    const sign = deltaPp > 0 ? "+" : deltaPp < 0 ? "-" : "";
    return `${sign}${Math.abs(deltaPp).toFixed(2)} pp vs ${priorPt.label}`;
  }
  if (inspectedPoint.pnl_minor === null || priorPt.pnl_minor === null) {
    return "No prior reading";
  }
  const deltaMinor = inspectedPoint.pnl_minor - priorPt.pnl_minor;
  return `${formatMinorCurrency(deltaMinor, currency, { signed: true })} vs ${priorPt.label}`;
}

export function computeDriftDisplay(
  inspectedPoint: ReturnPoint | null,
  inspectedBenchmarkPoint: ReturnPoint | null,
  fallbackDriftPp: number | null | undefined
): number | null {
  if (
    inspectedPoint &&
    inspectedBenchmarkPoint &&
    inspectedPoint.return_pct !== null &&
    inspectedBenchmarkPoint.return_pct !== null
  ) {
    return inspectedPoint.return_pct - inspectedBenchmarkPoint.return_pct;
  }
  return fallbackDriftPp ?? null;
}

export function formatScrubberText(
  idxValue: number,
  visiblePoints: ReturnPoint[],
  currency: string
): string {
  const idx = clampScrubberIndex(idxValue, visiblePoints.length);
  const pt = visiblePoints[idx];
  if (!pt) return "No reading";
  if (pt.return_pct === null && pt.pnl_minor === null) {
    return `${pt.label}: No reading (missing interval)`;
  }
  return `${pt.label}: ${formatSignedPct(pt.return_pct)}, ${formatMinorCurrency(
    pt.pnl_minor,
    currency,
    { signed: true }
  )}, NAV ${pt.nav_minor === null ? "No reading" : formatMinorCurrency(pt.nav_minor, currency)}`;
}
