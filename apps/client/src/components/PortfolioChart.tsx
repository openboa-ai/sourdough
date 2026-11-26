import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import {
  Slider,
  SliderControl,
  SliderIndicator,
  SliderThumb,
  SliderTrack,
} from "@ui-sdk/react/slider";
import {
  Badge,
  Card,
  DataTable,
  DisclosureRow,
  Inline,
  SegmentedControl,
  Stack,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import { formatMinorCurrency, formatSignedPct } from "../api/client";
import type { ReturnPoint } from "../api/types";

interface PortfolioChartProps {
  points: ReturnPoint[];
  currency: string;
  asOfLabel: string;
}

type Timeframe = "all" | "30d" | "7d" | "1d";

type TimestampedReturnPoint = ReturnPoint & {
  observed_at?: number | string | null;
  observed_at_ns?: number | null;
  cut_at_ns?: number | null;
};

const DAY_MS = 86_400_000;
const ISO_DATE_RE =
  /^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;
const ISO_DATETIME_RE =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$/;
const ISO_DATE_ONLY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

function formatXAxisTick(label: string, compact = false): string {
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

function truncateTick(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  if (maxChars <= 1) return text.slice(0, 1);
  return `${text.slice(0, maxChars - 1)}…`;
}

let tickMeasureCanvasCtx: CanvasRenderingContext2D | null | undefined;

function getTickMeasureContext(): CanvasRenderingContext2D | null {
  if (tickMeasureCanvasCtx !== undefined) return tickMeasureCanvasCtx;
  if (typeof document === "undefined") {
    tickMeasureCanvasCtx = null;
    return null;
  }
  try {
    const canvas = document.createElement("canvas");
    tickMeasureCanvasCtx = canvas.getContext("2d");
  } catch {
    tickMeasureCanvasCtx = null;
  }
  return tickMeasureCanvasCtx;
}

function measureTickWidth(text: string, fontSpec: string): number {
  const ctx = getTickMeasureContext();
  if (ctx) {
    ctx.font = fontSpec;
    return Math.max(24, Math.ceil(ctx.measureText(text).width) + 2);
  }
  return Math.max(24, text.length * 10.5);
}

function extractPointTimeMs(pt: TimestampedReturnPoint): number | null {
  if (typeof pt.observed_at_ns === "number" && Number.isFinite(pt.observed_at_ns) && pt.observed_at_ns > 0) {
    return pt.observed_at_ns / 1_000_000;
  }
  if (typeof pt.cut_at_ns === "number" && Number.isFinite(pt.cut_at_ns) && pt.cut_at_ns > 0) {
    return pt.cut_at_ns / 1_000_000;
  }
  if (typeof pt.observed_at === "number" && Number.isFinite(pt.observed_at) && pt.observed_at > 0) {
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

function formatAxisTick(value: number, unit: "pct" | "pnl", currency: string): string {
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

export function PortfolioChart({ points, currency, asOfLabel }: PortfolioChartProps) {
  const [unit, setUnit] = useState<"pct" | "pnl">("pct");
  const [timeframe, setTimeframe] = useState<Timeframe>("all");
  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  const [plotWidth, setPlotWidth] = useState(560);
  const [fontGen, setFontGen] = useState(0);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const plotRef = useRef<HTMLDivElement | null>(null);
  const readoutId = useId();

  useEffect(() => {
    if (typeof document === "undefined" || !("fonts" in document) || !document.fonts?.ready) {
      return;
    }
    let active = true;
    void document.fonts.ready.then(() => {
      if (active) setFontGen((n) => n + 1);
    });
    return () => {
      active = false;
    };
  }, []);

  useLayoutEffect(() => {
    const el = plotRef.current;
    if (!el) return;
    const measure = () => {
      const next = Math.max(180, Math.round(el.getBoundingClientRect().width));
      setPlotWidth((prev) => (prev === next ? prev : next));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(el);
    return () => ro.disconnect();
  }, [points.length]);

  const timeframeInfo = useMemo(() => {
    const timestamps = points.map((pt) => extractPointTimeMs(pt as TimestampedReturnPoint));
    const hasTimestamps = points.length >= 2 && timestamps.every((t) => t !== null);
    if (!hasTimestamps) {
      return {
        hasTimestamps: false,
        availability: { "1d": false, "7d": false, "30d": false, all: true } as Record<
          Timeframe,
          boolean
        >,
        slices: {
          "1d": points,
          "7d": points,
          "30d": points,
          all: points,
        } as Record<Timeframe, ReturnPoint[]>,
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
      } as Record<Timeframe, boolean>,
      slices: {
        "1d": slice1d,
        "7d": slice7d,
        "30d": slice30d,
        all: points,
      } as Record<Timeframe, ReturnPoint[]>,
    };
  }, [points]);

  const effectiveTimeframe: Timeframe = timeframeInfo.availability[timeframe] ? timeframe : "all";
  const visiblePoints = timeframeInfo.slices[effectiveTimeframe];

  const values = useMemo(
    () =>
      visiblePoints.map((pt) =>
        unit === "pct"
          ? pt.return_pct
          : pt.pnl_minor === null
            ? null
            : pt.pnl_minor / 100
      ),
    [visiblePoints, unit]
  );

  const validValues = values.filter((v): v is number => v !== null && Number.isFinite(v));
  const hasAny = validValues.length > 0;

  const minVal = hasAny ? Math.min(0, ...validValues) : -1;
  const maxVal = hasAny ? Math.max(0, ...validValues) : 1;
  const span = Math.max(maxVal - minVal, 0.5);
  const pad = span * 0.16;
  const yMin = minVal - pad;
  const yMax = maxVal + pad;

  const plotHeight = 200;
  const padX = 12;
  const padY = 12;
  const innerW = Math.max(40, plotWidth - padX * 2);
  const innerH = plotHeight - padY * 2;

  const xAt = (idx: number) =>
    visiblePoints.length <= 1
      ? padX + innerW / 2
      : padX + (idx / (visiblePoints.length - 1)) * innerW;

  const yAt = (v: number) => padY + ((yMax - v) / (yMax - yMin)) * innerH;
  const zeroY = yAt(0);
  const topY = padY;
  const bottomY = padY + innerH;
  const midPosVal = yMax / 2;
  const midNegVal = yMin / 2;
  const midPosY = yAt(midPosVal);
  const midNegY = yAt(midNegVal);
  const showMidPos =
    yMax > 0 && Math.abs(midPosY - topY) >= 24 && Math.abs(zeroY - midPosY) >= 24;
  const showMidNeg =
    yMin < 0 && Math.abs(bottomY - midNegY) >= 24 && Math.abs(midNegY - zeroY) >= 24;

  const yTicks = useMemo(() => {
    const ticks: Array<{ key: string; y: number; label: string }> = [
      { key: "zero", y: zeroY, label: unit === "pct" ? "0.0%" : formatAxisTick(0, "pnl", currency) },
    ];
    if (Math.abs(topY - zeroY) >= 18) {
      ticks.push({ key: "max", y: topY, label: formatAxisTick(yMax, unit, currency) });
    }
    if (Math.abs(bottomY - zeroY) >= 18) {
      ticks.push({ key: "min", y: bottomY, label: formatAxisTick(yMin, unit, currency) });
    }
    if (showMidPos) {
      ticks.push({ key: "midPos", y: midPosY, label: formatAxisTick(midPosVal, unit, currency) });
    }
    if (showMidNeg) {
      ticks.push({ key: "midNeg", y: midNegY, label: formatAxisTick(midNegVal, unit, currency) });
    }
    return ticks;
  }, [
    bottomY,
    currency,
    midNegVal,
    midNegY,
    midPosVal,
    midPosY,
    showMidNeg,
    showMidPos,
    topY,
    unit,
    yMax,
    yMin,
    zeroY,
  ]);

  const fontSpec = useMemo(() => {
    if (typeof window === "undefined" || !plotRef.current) {
      return '420 12px "Rethink Sans", sans-serif';
    }
    const style = window.getComputedStyle(plotRef.current);
    const size = style.getPropertyValue("--yg-type-help-size").trim() || "12px";
    const family = style.fontFamily || '"Rethink Sans", sans-serif';
    return `420 ${size} ${family}`;
  }, [fontGen, plotWidth]);

  const xTicks = useMemo(() => {
    const n = visiblePoints.length;
    if (n === 0) return [] as Array<{ idx: number; label: string; edge: "start" | "mid" | "end" }>;

    const MIN_GAP = 14;
    const estimateWidth = (text: string) => measureTickWidth(text, fontSpec);
    const fitTickToWidth = (label: string, maxPx: number): string => {
      if (estimateWidth(label) <= maxPx) return label;
      let lo = 1;
      let hi = label.length;
      let best = truncateTick(label, 1);
      while (lo <= hi) {
        const mid = Math.floor((lo + hi) / 2);
        const candidate = truncateTick(label, mid);
        if (estimateWidth(candidate) <= maxPx) {
          best = candidate;
          lo = mid + 1;
        } else {
          hi = mid - 1;
        }
      }
      return best;
    };

    if (n === 1) {
      const single = fitTickToWidth(formatXAxisTick(visiblePoints[0].label), innerW);
      return [{ idx: 0, label: single, edge: "mid" as const }];
    }

    const boundsFor = (idx: number, text: string): [number, number] => {
      const xPx = padX + (idx / (n - 1)) * innerW;
      const w = estimateWidth(text);
      if (idx === 0) return [xPx, xPx + w];
      if (idx === n - 1) return [xPx - w, xPx];
      return [xPx - w / 2, xPx + w / 2];
    };

    let firstLabel = formatXAxisTick(visiblePoints[0].label);
    let lastLabel = formatXAxisTick(visiblePoints[n - 1].label);
    let firstBounds = boundsFor(0, firstLabel);
    let lastBounds = boundsFor(n - 1, lastLabel);

    if (firstBounds[1] + MIN_GAP > lastBounds[0]) {
      firstLabel = formatXAxisTick(visiblePoints[0].label, true);
      lastLabel = formatXAxisTick(visiblePoints[n - 1].label, true);
      firstBounds = boundsFor(0, firstLabel);
      lastBounds = boundsFor(n - 1, lastLabel);
    }

    if (firstBounds[1] + MIN_GAP > lastBounds[0]) {
      const spanPx = Math.max(0, lastBounds[1] - firstBounds[0] - MIN_GAP);
      const maxEndPx = Math.floor(spanPx / 2);
      if (maxEndPx < 32) {
        return [{ idx: 0, label: fitTickToWidth(firstLabel, innerW), edge: "start" as const }];
      }
      firstLabel = fitTickToWidth(firstLabel, maxEndPx);
      lastLabel = fitTickToWidth(lastLabel, maxEndPx);
      firstBounds = [
        firstBounds[0],
        firstBounds[0] + Math.min(maxEndPx, estimateWidth(firstLabel)),
      ];
      lastBounds = [
        lastBounds[1] - Math.min(maxEndPx, estimateWidth(lastLabel)),
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
      const label = formatXAxisTick(visiblePoints[idx].label);
      const bounds = boundsFor(idx, label);
      const prev = kept[kept.length - 1];
      if (bounds[0] >= prev.bounds[1] + MIN_GAP && bounds[1] + MIN_GAP <= lastBounds[0]) {
        kept.push({ idx, label, edge: "mid", bounds });
      }
    }

    kept.push({ idx: n - 1, label: lastLabel, edge: "end", bounds: lastBounds });
    return kept.map(({ idx, label, edge }) => ({ idx, label, edge }));
  }, [fontGen, fontSpec, innerW, padX, visiblePoints]);

  const segments: Array<Array<{ x: number; y: number; value: number; idx: number }>> = [];
  let current: Array<{ x: number; y: number; value: number; idx: number }> = [];
  values.forEach((v, idx) => {
    if (v === null || !Number.isFinite(v)) {
      if (current.length > 0) {
        segments.push(current);
        current = [];
      }
    } else {
      current.push({ x: xAt(idx), y: yAt(v), value: v, idx });
    }
  });
  if (current.length > 0) segments.push(current);

  const fallbackIdx = useMemo(() => {
    for (let i = visiblePoints.length - 1; i >= 0; i -= 1) {
      if (values[i] !== null) return i;
    }
    return visiblePoints.length > 0 ? visiblePoints.length - 1 : null;
  }, [visiblePoints.length, values]);

  const inspectedIdx =
    activeIdx !== null && activeIdx < visiblePoints.length ? activeIdx : fallbackIdx;
  const inspectedPoint = inspectedIdx !== null ? visiblePoints[inspectedIdx] : null;
  const inspectedValue = inspectedIdx !== null ? values[inspectedIdx] : null;

  const prevValidIdx = useMemo(() => {
    if (inspectedIdx === null || inspectedValue === null) return null;
    for (let i = inspectedIdx - 1; i >= 0; i -= 1) {
      const val = values[i];
      if (val !== null && Number.isFinite(val)) return i;
    }
    return null;
  }, [inspectedIdx, inspectedValue, values]);

  const deltaLabel = useMemo(() => {
    if (inspectedPoint === null || inspectedValue === null || prevValidIdx === null) {
      return "No prior reading";
    }
    const priorPt = visiblePoints[prevValidIdx];
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
  }, [currency, inspectedPoint, inspectedValue, prevValidIdx, unit, visiblePoints]);

  const pointById = useMemo(
    () => new Map(visiblePoints.map((pt, idx) => [pt.id, { idx, pt }])),
    [visiblePoints]
  );

  const columns: DataColumn[] = [
    {
      id: "label",
      label: "Valuation Cut",
      kind: "identity",
      sortValue: (row) => pointById.get(row.id)?.idx ?? row.id,
    },
    {
      id: "return_pct",
      label: "Cumulative Return",
      kind: "numeric",
      align: "end",
      sortValue: (row) => pointById.get(row.id)?.pt.return_pct ?? null,
    },
    {
      id: "pnl",
      label: "Cumulative P&L",
      kind: "numeric",
      align: "end",
      sortValue: (row) => pointById.get(row.id)?.pt.pnl_minor ?? null,
    },
    {
      id: "nav",
      label: "Sealed Paper NAV",
      kind: "numeric",
      align: "end",
      sortValue: (row) => pointById.get(row.id)?.pt.nav_minor ?? null,
    },
    {
      id: "source",
      label: "Source",
      kind: "text",
      sortValue: (row) => pointById.get(row.id)?.pt.source ?? "",
    },
  ];

  const rows: DataRow[] = visiblePoints.map((pt) => ({
    id: pt.id,
    cells: {
      label: pt.label,
      return_pct: pt.return_pct === null ? "No reading" : formatSignedPct(pt.return_pct),
      pnl:
        pt.pnl_minor === null
          ? "No reading"
          : formatMinorCurrency(pt.pnl_minor, currency, { signed: true }),
      nav: pt.nav_minor === null ? "No reading" : formatMinorCurrency(pt.nav_minor, currency),
      source: pt.source,
    },
  }));

  const slotWidth =
    visiblePoints.length <= 1 ? innerW : innerW / Math.max(1, visiblePoints.length - 1);
  const gapWidth = Math.max(8, Math.min(28, slotWidth * 0.65));

  const formatScrubberText = (idxValue: number) => {
    const idx = Math.max(0, Math.min(visiblePoints.length - 1, Math.round(idxValue)));
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
  };

  const handlePointerHover = (event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== "mouse" || event.buttons !== 0 || visiblePoints.length === 0) return;
    const el = plotRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const relX = event.clientX - rect.left - padX;
    const ratio = innerW > 0 ? Math.max(0, Math.min(1, relX / innerW)) : 0;
    const nextIdx =
      visiblePoints.length <= 1 ? 0 : Math.round(ratio * (visiblePoints.length - 1));
    setActiveIdx(nextIdx);
  };

  const handleImageKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target instanceof HTMLInputElement || visiblePoints.length === 0) return;
    const base = inspectedIdx ?? visiblePoints.length - 1;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setActiveIdx(Math.max(0, base - 1));
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setActiveIdx(Math.min(visiblePoints.length - 1, base + 1));
    } else if (event.key === "Home") {
      event.preventDefault();
      setActiveIdx(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setActiveIdx(visiblePoints.length - 1);
    }
  };

  return (
    <Card
      title="Paper Cumulative Performance"
      description={
        unit === "pct"
          ? `0% baseline · Service-projected paper returns · ${asOfLabel}`
          : `$0 baseline · Service-projected cumulative P&L · ${asOfLabel}`
      }
      headerAction={
        <Inline gap={8} align="center" wrap>
          <SegmentedControl
            label="Performance timeframe"
            value={effectiveTimeframe}
            onValueChange={(next) => {
              const tf = next as Timeframe;
              if (!timeframeInfo.availability[tf]) return;
              setTimeframe(tf);
              setActiveIdx(null);
            }}
            options={[
              { id: "1d", label: "1D", disabled: !timeframeInfo.availability["1d"] },
              { id: "7d", label: "7D", disabled: !timeframeInfo.availability["7d"] },
              { id: "30d", label: "30D", disabled: !timeframeInfo.availability["30d"] },
              { id: "all", label: "ALL" },
            ]}
          />
          <SegmentedControl
            label="Performance unit"
            value={unit}
            onValueChange={(next) => setUnit(next as "pct" | "pnl")}
            options={[
              { id: "pct", label: "Return %" },
              { id: "pnl", label: "P&L $" },
            ]}
          />
        </Inline>
      }
      variant="raised"
    >
      <Stack gap={12}>
        {inspectedPoint ? (
          <div id={readoutId} className="sd-chart-readout" role="status" aria-live="polite">
            <Inline gap={8} align="center" wrap>
              <Badge
                tone={inspectedValue === null ? "warning" : "brand"}
                variant="soft"
                dot={false}
              >
                {inspectedPoint.label}
              </Badge>
              <Text
                variant="body"
                className={`sd-tabular ${
                  inspectedValue === null
                    ? "sd-tone-muted"
                    : inspectedValue >= 0
                      ? "sd-tone-pos"
                      : "sd-tone-neg"
                }`}
              >
                {inspectedValue === null
                  ? "No reading (missing interval)"
                  : unit === "pct"
                    ? `${formatSignedPct(inspectedPoint.return_pct)} · ${formatMinorCurrency(
                        inspectedPoint.pnl_minor,
                        currency,
                        { signed: true }
                      )}`
                    : `${formatMinorCurrency(inspectedPoint.pnl_minor, currency, {
                        signed: true,
                      })} · ${formatSignedPct(inspectedPoint.return_pct)}`}
              </Text>
              <Text variant="help" className="sd-tabular sd-tone-muted">
                Delta: {deltaLabel}
              </Text>
            </Inline>
            <Inline gap={12} align="center" wrap>
              <Text variant="meta" className="sd-tabular">
                NAV:{" "}
                {inspectedPoint.nav_minor === null
                  ? "No reading"
                  : formatMinorCurrency(inspectedPoint.nav_minor, currency)}
              </Text>
              <Text variant="meta">{inspectedPoint.source}</Text>
            </Inline>
          </div>
        ) : null}

        {!hasAny ? (
          <Text variant="help">
            No readings yet. Sealed paper valuations will plot here on a 0% baseline without inventing missing intervals.
          </Text>
        ) : (
          <div
            ref={wrapRef}
            className="sd-chart-wrap"
            onPointerLeave={() => {
              if (!wrapRef.current?.contains(document.activeElement)) {
                setActiveIdx(null);
              }
            }}
          >
            <Slider
              min={0}
              max={Math.max(1, visiblePoints.length - 1)}
              step={1}
              disabled={visiblePoints.length <= 1}
              value={visiblePoints.length <= 1 ? 0 : (inspectedIdx ?? 0)}
              onValueChange={(next) => {
                const raw = Array.isArray(next) ? next[0] : next;
                const clamped = Math.max(0, Math.min(visiblePoints.length - 1, Math.round(raw)));
                setActiveIdx(clamped);
              }}
              format={formatScrubberText}
            >
              <div className="sd-chart-frame">
                <div className="sd-chart-yaxis" aria-hidden="true">
                  <div className="sd-chart-yaxis-sizer">
                    {yTicks.map((tick) => (
                      <Text
                        key={`sizer-${tick.key}`}
                        variant="help"
                        className="sd-tabular sd-chart-yaxis-sizer-item"
                      >
                        {tick.label}
                      </Text>
                    ))}
                  </div>
                  {yTicks.map((tick) => (
                    <Text
                      key={tick.key}
                      variant="help"
                      className="sd-tabular sd-chart-axis-text"
                      style={{ top: `${tick.y}px` }}
                    >
                      {tick.label}
                    </Text>
                  ))}
                </div>

                <SliderControl className="sd-chart-plot">
                  <div
                    ref={plotRef}
                    role="img"
                    tabIndex={-1}
                    aria-label="Paper cumulative performance chart on zero baseline"
                    onPointerMove={handlePointerHover}
                    onKeyDown={handleImageKeyDown}
                  >
                    <svg
                      className="sd-chart-svg"
                      viewBox={`0 0 ${plotWidth} ${plotHeight}`}
                      aria-hidden="true"
                    >
                      <line
                        x1={padX}
                        y1={topY}
                        x2={plotWidth - padX}
                        y2={topY}
                        className="sd-chart-gridline"
                      />
                      {showMidPos ? (
                        <line
                          x1={padX}
                          y1={midPosY}
                          x2={plotWidth - padX}
                          y2={midPosY}
                          className="sd-chart-gridline-minor"
                        />
                      ) : null}
                      <line
                        x1={padX}
                        y1={zeroY}
                        x2={plotWidth - padX}
                        y2={zeroY}
                        className="sd-chart-zero"
                      />
                      {showMidNeg ? (
                        <line
                          x1={padX}
                          y1={midNegY}
                          x2={plotWidth - padX}
                          y2={midNegY}
                          className="sd-chart-gridline-minor"
                        />
                      ) : null}
                      <line
                        x1={padX}
                        y1={bottomY}
                        x2={plotWidth - padX}
                        y2={bottomY}
                        className="sd-chart-gridline"
                      />

                      {inspectedIdx !== null ? (
                        <line
                          x1={xAt(inspectedIdx)}
                          y1={topY}
                          x2={xAt(inspectedIdx)}
                          y2={bottomY}
                          className="sd-chart-crosshair"
                        />
                      ) : null}

                      {values.map((v, idx) => {
                        if (v !== null && Number.isFinite(v)) return null;
                        const cx = xAt(idx);
                        return (
                          <g key={`gap-${visiblePoints[idx].id}`}>
                            <rect
                              x={cx - gapWidth / 2}
                              y={topY + 4}
                              width={gapWidth}
                              height={innerH - 8}
                              rx={6}
                              className="sd-chart-gap"
                            />
                            <text
                              x={cx}
                              y={topY + innerH / 2 + 4}
                              textAnchor="middle"
                              className="sd-chart-label"
                            >
                              —
                            </text>
                          </g>
                        );
                      })}

                      {segments.map((seg, sIdx) => {
                        if (seg.length === 1) {
                          const isSelected = inspectedIdx === seg[0].idx;
                          return (
                            <circle
                              key={`pt-${sIdx}`}
                              cx={seg[0].x}
                              cy={seg[0].y}
                              r={isSelected ? 6 : 4.5}
                              className={isSelected ? "sd-chart-point-active" : "sd-chart-point"}
                            />
                          );
                        }
                        const d = seg
                          .map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`)
                          .join(" ");
                        return (
                          <g key={`seg-${sIdx}`}>
                            <path d={d} className="sd-chart-line" />
                            {seg.map((p) => {
                              const isSelected = inspectedIdx === p.idx;
                              return (
                                <circle
                                  key={`dot-${p.idx}`}
                                  cx={p.x}
                                  cy={p.y}
                                  r={isSelected ? 6 : 4}
                                  className={isSelected ? "sd-chart-point-active" : "sd-chart-point"}
                                />
                              );
                            })}
                          </g>
                        );
                      })}

                      {visiblePoints.map((pt, idx) => (
                        <rect
                          key={`hit-${pt.id}`}
                          x={xAt(idx) - slotWidth / 2}
                          y={topY}
                          width={slotWidth}
                          height={innerH}
                          className="sd-chart-hit"
                          onClick={() => setActiveIdx(idx)}
                        />
                      ))}
                    </svg>
                  </div>

                  <div className="sd-chart-xaxis" aria-hidden="true">
                    {xTicks.map((tick) => {
                      const pctLeft = (xAt(tick.idx) / plotWidth) * 100;
                      return (
                        <Text
                          key={`xtick-${visiblePoints[tick.idx].id}`}
                          variant="help"
                          className="sd-tabular sd-chart-axis-tick"
                          data-edge={tick.edge}
                          style={{ left: `${pctLeft}%` }}
                        >
                          {tick.label}
                        </Text>
                      );
                    })}
                  </div>

                  <div className="sd-chart-scrubber-rail">
                    <SliderTrack className="sd-chart-scrubber-track">
                      <SliderIndicator className="sd-chart-scrubber-indicator" />
                      <SliderThumb
                        className="sd-chart-scrubber-thumb"
                        aria-label="Inspect paper cumulative performance cut"
                        aria-describedby={readoutId}
                      />
                    </SliderTrack>
                  </div>
                </SliderControl>
              </div>
            </Slider>
          </div>
        )}

        <Inline justify="between" align="center" wrap>
          <Text variant="meta">
            Hover, drag, or use arrow/Home/End keys to inspect cuts · Missing intervals stay unbridged.
          </Text>
          <Inline gap={8} align="center">
            {!timeframeInfo.hasTimestamps && points.length > 0 ? (
              <Badge tone="neutral" variant="outline" dot={false}>
                Unwindowed cuts · ALL
              </Badge>
            ) : null}
            <Text variant="meta" className="sd-tabular">
              {visiblePoints.length} of {points.length} sealed cuts
            </Text>
          </Inline>
        </Inline>

        <DisclosureRow label="View readings" initiallyOpen={false}>
          <DataTable
            caption="Paper cumulative return readings"
            columns={columns}
            rows={rows}
          />
        </DisclosureRow>
      </Stack>
    </Card>
  );
}
