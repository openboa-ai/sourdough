import {
  useEffect,
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
import { Text } from "@openboa/yogurt";
import type { ReturnPoint } from "../../api/types";
import {
  buildSvgPath,
  buildValueSegments,
  clampScrubberIndex,
  computeChartGeometry,
  computeSlotMetrics,
  computeXTicks,
  computeYTicks,
  defaultMeasureTickWidth,
  formatScrubberText,
  snapPointerToIndex,
  xAtIndex,
  xForBenchmarkIndex,
  yAtValue,
  type ChartDomain,
  type ChartUnit,
} from "./math";

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
  return defaultMeasureTickWidth(text);
}

export interface ChartSvgProps {
  title: string;
  readoutId: string;
  currency: string;
  unit: ChartUnit;
  visiblePoints: ReturnPoint[];
  visibleTimes: Array<number | null>;
  values: Array<number | null>;
  visibleBenchmarkPoints: ReturnPoint[];
  benchmarkValues: Array<number | null>;
  domain: ChartDomain;
  inspectedIdx: number | null;
  onActiveIdxChange: (idx: number | null) => void;
}

export function ChartSvg({
  title,
  readoutId,
  currency,
  unit,
  visiblePoints,
  visibleTimes,
  values,
  visibleBenchmarkPoints,
  benchmarkValues,
  domain,
  inspectedIdx,
  onActiveIdxChange,
}: ChartSvgProps) {
  const [plotWidth, setPlotWidth] = useState(560);
  const [fontGen, setFontGen] = useState(0);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const plotRef = useRef<HTMLDivElement | null>(null);

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
  }, [visiblePoints.length]);

  const geom = useMemo(() => computeChartGeometry(plotWidth), [plotWidth]);

  const yLayout = useMemo(
    () => computeYTicks(domain, geom, unit, currency),
    [currency, domain, geom, unit]
  );

  const fontSpec = useMemo(() => {
    if (typeof window === "undefined" || !plotRef.current) {
      return '420 12px "Rethink Sans", sans-serif';
    }
    const style = window.getComputedStyle(plotRef.current);
    const size = style.getPropertyValue("--yg-type-help-size").trim() || "12px";
    const family = style.fontFamily || '"Rethink Sans", sans-serif';
    return `420 ${size} ${family}`;
  }, [fontGen, plotWidth]);

  const xTicks = useMemo(
    () =>
      computeXTicks(visiblePoints, geom, (text) => measureTickWidth(text, fontSpec)),
    [fontGen, fontSpec, geom, visiblePoints]
  );

  const xAt = (idx: number) => xAtIndex(idx, visiblePoints.length, geom);

  const segments = useMemo(
    () =>
      buildValueSegments(
        values,
        (idx) => xAtIndex(idx, visiblePoints.length, geom),
        (v) => yAtValue(v, domain, geom)
      ),
    [domain, geom, values, visiblePoints.length]
  );

  const benchmarkSegments = useMemo(
    () =>
      buildValueSegments(
        benchmarkValues,
        (idx) =>
          xForBenchmarkIndex(
            idx,
            visibleBenchmarkPoints,
            visiblePoints,
            visibleTimes,
            geom
          ),
        (v) => yAtValue(v, domain, geom)
      ),
    [benchmarkValues, domain, geom, visibleBenchmarkPoints, visiblePoints, visibleTimes]
  );

  const { slotWidth, gapWidth } = useMemo(
    () => computeSlotMetrics(visiblePoints.length, geom.innerW),
    [geom.innerW, visiblePoints.length]
  );

  const handlePointerHover = (event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== "mouse" || event.buttons !== 0 || visiblePoints.length === 0) {
      return;
    }
    const el = plotRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const nextIdx = snapPointerToIndex(event.clientX, rect.left, geom, visiblePoints.length);
    if (nextIdx !== null) {
      onActiveIdxChange(nextIdx);
    }
  };

  const handleImageKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target instanceof HTMLInputElement || visiblePoints.length === 0) return;
    const base = inspectedIdx ?? visiblePoints.length - 1;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      onActiveIdxChange(Math.max(0, base - 1));
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      onActiveIdxChange(Math.min(visiblePoints.length - 1, base + 1));
    } else if (event.key === "Home") {
      event.preventDefault();
      onActiveIdxChange(0);
    } else if (event.key === "End") {
      event.preventDefault();
      onActiveIdxChange(visiblePoints.length - 1);
    }
  };

  return (
    <div
      ref={wrapRef}
      className="sd-chart-wrap"
      onPointerLeave={() => {
        if (!wrapRef.current?.contains(document.activeElement)) {
          onActiveIdxChange(null);
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
          onActiveIdxChange(clampScrubberIndex(raw, visiblePoints.length));
        }}
        format={(idxValue) => formatScrubberText(idxValue, visiblePoints, currency)}
      >
        <div className="sd-chart-frame">
          <div className="sd-chart-yaxis" aria-hidden="true">
            <div className="sd-chart-yaxis-sizer">
              {yLayout.ticks.map((tick) => (
                <Text
                  key={`sizer-${tick.key}`}
                  variant="help"
                  className="sd-tabular sd-chart-yaxis-sizer-item"
                >
                  {tick.label}
                </Text>
              ))}
            </div>
            {yLayout.ticks.map((tick) => (
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
              aria-label={`${title} chart on zero baseline`}
              onPointerMove={handlePointerHover}
              onKeyDown={handleImageKeyDown}
            >
              <svg
                className="sd-chart-svg"
                viewBox={`0 0 ${geom.plotWidth} ${geom.plotHeight}`}
                aria-hidden="true"
              >
                <line
                  x1={geom.padX}
                  y1={geom.topY}
                  x2={geom.plotWidth - geom.padX}
                  y2={geom.topY}
                  className="sd-chart-gridline"
                />
                {yLayout.showMidPos ? (
                  <line
                    x1={geom.padX}
                    y1={yLayout.midPosY}
                    x2={geom.plotWidth - geom.padX}
                    y2={yLayout.midPosY}
                    className="sd-chart-gridline-minor"
                  />
                ) : null}
                <line
                  x1={geom.padX}
                  y1={yLayout.zeroY}
                  x2={geom.plotWidth - geom.padX}
                  y2={yLayout.zeroY}
                  className="sd-chart-zero"
                />
                {yLayout.showMidNeg ? (
                  <line
                    x1={geom.padX}
                    y1={yLayout.midNegY}
                    x2={geom.plotWidth - geom.padX}
                    y2={yLayout.midNegY}
                    className="sd-chart-gridline-minor"
                  />
                ) : null}
                <line
                  x1={geom.padX}
                  y1={geom.bottomY}
                  x2={geom.plotWidth - geom.padX}
                  y2={geom.bottomY}
                  className="sd-chart-gridline"
                />

                {inspectedIdx !== null ? (
                  <line
                    x1={xAt(inspectedIdx)}
                    y1={geom.topY}
                    x2={xAt(inspectedIdx)}
                    y2={geom.bottomY}
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
                        y={geom.topY + 4}
                        width={gapWidth}
                        height={geom.innerH - 8}
                        rx={6}
                        className="sd-chart-gap"
                      />
                      <text
                        x={cx}
                        y={geom.topY + geom.innerH / 2 + 4}
                        textAnchor="middle"
                        className="sd-chart-label"
                      >
                        —
                      </text>
                    </g>
                  );
                })}

                {benchmarkSegments.map((seg, sIdx) => {
                  if (seg.length === 1) {
                    return (
                      <circle
                        key={`bpt-${sIdx}`}
                        cx={seg[0].x}
                        cy={seg[0].y}
                        r={3.5}
                        className="sd-chart-point-benchmark"
                      />
                    );
                  }
                  const d = buildSvgPath(seg);
                  return (
                    <g key={`bseg-${sIdx}`}>
                      <path d={d} className="sd-chart-line-benchmark" />
                      {seg.map((p) => (
                        <circle
                          key={`bdot-${p.idx}`}
                          cx={p.x}
                          cy={p.y}
                          r={3}
                          className="sd-chart-point-benchmark"
                        />
                      ))}
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
                  const d = buildSvgPath(seg);
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
                    y={geom.topY}
                    width={slotWidth}
                    height={geom.innerH}
                    className="sd-chart-hit"
                    onClick={() => onActiveIdxChange(idx)}
                  />
                ))}
              </svg>
            </div>

            <div className="sd-chart-xaxis" aria-hidden="true">
              {xTicks.map((tick) => {
                const pctLeft = (xAt(tick.idx) / geom.plotWidth) * 100;
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
                  aria-label={`Inspect ${title.toLowerCase()} cut`}
                  aria-describedby={readoutId}
                />
              </SliderTrack>
            </div>
          </SliderControl>
        </div>
      </Slider>
    </div>
  );
}
