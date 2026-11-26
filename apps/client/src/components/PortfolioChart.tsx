import { useMemo, useState } from "react";
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

function sliceTimeframe(points: ReturnPoint[], tf: Timeframe): ReturnPoint[] {
  if (tf === "all" || points.length <= 2) return points;
  if (tf === "30d") return points.slice(-30);
  if (tf === "7d") return points.slice(-7);
  return points.slice(-4);
}

export function PortfolioChart({ points, currency, asOfLabel }: PortfolioChartProps) {
  const [unit, setUnit] = useState<"pct" | "pnl">("pct");
  const [timeframe, setTimeframe] = useState<Timeframe>("all");
  const [activeIdx, setActiveIdx] = useState<number | null>(null);

  const visiblePoints = useMemo(
    () => sliceTimeframe(points, timeframe),
    [points, timeframe]
  );

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

  const validValues = values.filter(
    (v): v is number => v !== null && Number.isFinite(v)
  );
  const hasAny = validValues.length > 0;

  const minVal = hasAny ? Math.min(0, ...validValues) : -1;
  const maxVal = hasAny ? Math.max(0, ...validValues) : 1;
  const span = Math.max(maxVal - minVal, 0.5);
  const pad = span * 0.16;
  const yMin = minVal - pad;
  const yMax = maxVal + pad;

  const width = 700;
  const height = 248;
  const padL = 62;
  const padR = 20;
  const padT = 20;
  const padB = 34;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;

  const xAt = (idx: number) =>
    visiblePoints.length <= 1
      ? padL + innerW / 2
      : padL + (idx / (visiblePoints.length - 1)) * innerW;

  const yAt = (v: number) => padT + ((yMax - v) / (yMax - yMin)) * innerH;
  const zeroY = yAt(0);

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

  const columns: DataColumn[] = [
    { id: "label", label: "Valuation Cut", kind: "identity" },
    { id: "return_pct", label: "Cumulative Return", kind: "numeric", align: "end" },
    { id: "pnl", label: "Cumulative P&L", kind: "numeric", align: "end" },
    { id: "nav", label: "Sealed Paper NAV", kind: "numeric", align: "end" },
    { id: "source", label: "Source", kind: "text" },
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
      nav:
        pt.nav_minor === null
          ? "No reading"
          : formatMinorCurrency(pt.nav_minor, currency),
      source: pt.source,
    },
  }));

  const formatAxis = (v: number) =>
    unit === "pct"
      ? `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`
      : `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(0)}`;

  const slotWidth =
    visiblePoints.length <= 1 ? innerW : innerW / Math.max(1, visiblePoints.length - 1);

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
            value={timeframe}
            onValueChange={(next) => {
              setTimeframe(next as Timeframe);
              setActiveIdx(null);
            }}
            options={[
              { id: "1d", label: "1D" },
              { id: "7d", label: "7D" },
              { id: "30d", label: "30D" },
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
          <div className="sd-chart-readout" role="status" aria-live="polite">
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
            className="sd-chart-wrap"
            tabIndex={0}
            role="group"
            aria-label="Interactive paper cumulative performance chart. Use Left and Right arrows to inspect cuts."
            onKeyDown={(e) => {
              if (visiblePoints.length === 0) return;
              const base = inspectedIdx ?? visiblePoints.length - 1;
              if (e.key === "ArrowLeft") {
                e.preventDefault();
                setActiveIdx(Math.max(0, base - 1));
              } else if (e.key === "ArrowRight") {
                e.preventDefault();
                setActiveIdx(Math.min(visiblePoints.length - 1, base + 1));
              }
            }}
            onMouseLeave={() => setActiveIdx(null)}
          >
            <svg
              className="sd-chart-svg"
              viewBox={`0 0 ${width} ${height}`}
              role="img"
              aria-label="Paper cumulative performance chart on zero baseline"
            >
              <line x1={padL} y1={padT} x2={width - padR} y2={padT} className="sd-chart-gridline" />
              <line
                x1={padL}
                y1={padT + innerH}
                x2={width - padR}
                y2={padT + innerH}
                className="sd-chart-gridline"
              />
              <line x1={padL} y1={zeroY} x2={width - padR} y2={zeroY} className="sd-chart-zero" />

              {inspectedIdx !== null ? (
                <line
                  x1={xAt(inspectedIdx)}
                  y1={padT}
                  x2={xAt(inspectedIdx)}
                  y2={padT + innerH}
                  className="sd-chart-crosshair"
                />
              ) : null}

              <text x={padL - 10} y={padT + 4} textAnchor="end" className="sd-chart-label">
                {formatAxis(yMax)}
              </text>
              <text x={padL - 10} y={zeroY + 4} textAnchor="end" className="sd-chart-label">
                {unit === "pct" ? "0.0%" : "$0"}
              </text>
              <text x={padL - 10} y={padT + innerH + 4} textAnchor="end" className="sd-chart-label">
                {formatAxis(yMin)}
              </text>

              {values.map((v, idx) => {
                if (v !== null && Number.isFinite(v)) return null;
                const cx = xAt(idx);
                return (
                  <g key={`gap-${visiblePoints[idx].id}`}>
                    <rect
                      x={cx - 14}
                      y={padT + 4}
                      width={28}
                      height={innerH - 8}
                      rx={6}
                      className="sd-chart-gap"
                    />
                    <text
                      x={cx}
                      y={padT + innerH / 2 + 3}
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

              {visiblePoints.map((pt, idx) => {
                const cx = xAt(idx);
                return (
                  <g key={`hit-${pt.id}`}>
                    <text
                      x={cx}
                      y={height - 10}
                      textAnchor="middle"
                      className="sd-chart-label"
                    >
                      {pt.label}
                    </text>
                    <rect
                      x={cx - slotWidth / 2}
                      y={padT}
                      width={slotWidth}
                      height={innerH}
                      className="sd-chart-hit"
                      onMouseEnter={() => setActiveIdx(idx)}
                      onClick={() => setActiveIdx(idx)}
                    />
                  </g>
                );
              })}
            </svg>
          </div>
        )}

        <Inline justify="between" align="center" wrap>
          <Text variant="meta">
            Hover or use arrow keys to inspect cuts · Missing intervals stay unbridged.
          </Text>
          <Text variant="meta" className="sd-tabular">
            {visiblePoints.length} of {points.length} sealed cuts
          </Text>
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
