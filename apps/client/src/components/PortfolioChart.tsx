import { useMemo, useState } from "react";
import {
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

/**
 * Custom SVG chart strictly framed on 0% Cumulative Return or $0 Cumulative P&L.
 * Never interpolates across null gaps (restarts SVG path cleanly on each valid segment),
 * never coerces missing values to 0, and uses zero CSS gradients.
 */
export function PortfolioChart({ points, currency, asOfLabel }: PortfolioChartProps) {
  const [unit, setUnit] = useState<"pct" | "pnl">("pct");

  const values = useMemo(
    () =>
      points.map((pt) =>
        unit === "pct"
          ? pt.return_pct
          : pt.pnl_minor === null
            ? null
            : pt.pnl_minor / 100
      ),
    [points, unit]
  );

  const validValues = values.filter(
    (v): v is number => v !== null && Number.isFinite(v)
  );
  const hasAny = validValues.length > 0;

  const minVal = hasAny ? Math.min(0, ...validValues) : -1;
  const maxVal = hasAny ? Math.max(0, ...validValues) : 1;
  const span = Math.max(maxVal - minVal, 0.5);
  const pad = span * 0.14;
  const yMin = minVal - pad;
  const yMax = maxVal + pad;

  const width = 680;
  const height = 220;
  const padL = 58;
  const padR = 18;
  const padT = 18;
  const padB = 30;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;

  const xAt = (idx: number) =>
    points.length <= 1
      ? padL + innerW / 2
      : padL + (idx / (points.length - 1)) * innerW;

  const yAt = (v: number) => padT + ((yMax - v) / (yMax - yMin)) * innerH;
  const zeroY = yAt(0);

  // Split valid points into continuous segments, breaking cleanly on null readings.
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

  const columns: DataColumn[] = [
    { id: "label", label: "Valuation Cut", kind: "identity" },
    { id: "return_pct", label: "Cumulative Return", kind: "numeric", align: "end" },
    { id: "pnl", label: "Cumulative P&L", kind: "numeric", align: "end" },
    { id: "nav", label: "Sealed Paper NAV", kind: "numeric", align: "end" },
    { id: "source", label: "Source", kind: "text" },
  ];

  const rows: DataRow[] = points.map((pt) => ({
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

  return (
    <Card
      title="Paper Cumulative Performance"
      description={
        unit === "pct"
          ? `0% baseline · Service-projected paper returns · ${asOfLabel}`
          : `$0 baseline · Service-projected cumulative P&L · ${asOfLabel}`
      }
      headerAction={
        <SegmentedControl
          label="Performance unit"
          value={unit}
          onValueChange={(next) => setUnit(next as "pct" | "pnl")}
          options={[
            { id: "pct", label: "Return %" },
            { id: "pnl", label: "P&L $" },
          ]}
        />
      }
      variant="outlined"
    >
      <Stack gap={12}>
        {!hasAny ? (
          <Text variant="help">
            No readings yet. Sealed paper valuations will plot here on a 0% baseline without inventing missing intervals.
          </Text>
        ) : (
          <div className="sd-chart-frame">
            <svg
              className="sd-chart-svg"
              viewBox={`0 0 ${width} ${height}`}
              role="img"
              aria-label="Paper cumulative performance chart on zero baseline"
            >
              {/* Top & bottom subtle gridlines */}
              <line
                x1={padL}
                y1={padT}
                x2={width - padR}
                y2={padT}
                className="sd-chart-gridline"
              />
              <line
                x1={padL}
                y1={padT + innerH}
                x2={width - padR}
                y2={padT + innerH}
                className="sd-chart-gridline"
              />
              {/* Dashed 0% / $0 baseline */}
              <line
                x1={padL}
                y1={zeroY}
                x2={width - padR}
                y2={zeroY}
                className="sd-chart-zero"
              />

              {/* Axis labels */}
              <text x={padL - 8} y={padT + 4} textAnchor="end" className="sd-chart-label">
                {formatAxis(yMax)}
              </text>
              <text x={padL - 8} y={zeroY + 4} textAnchor="end" className="sd-chart-label">
                {unit === "pct" ? "0.0%" : "$0"}
              </text>
              <text
                x={padL - 8}
                y={padT + innerH + 4}
                textAnchor="end"
                className="sd-chart-label"
              >
                {formatAxis(yMin)}
              </text>

              {/* Null gap markers (explicitly uninterpolated) */}
              {values.map((v, idx) => {
                if (v !== null && Number.isFinite(v)) return null;
                const cx = xAt(idx);
                return (
                  <g key={`gap-${points[idx].id}`}>
                    <rect
                      x={cx - 12}
                      y={padT + 4}
                      width={24}
                      height={innerH - 8}
                      rx={4}
                      className="sd-chart-gap"
                    />
                    <text
                      x={cx}
                      y={padT + innerH / 2}
                      textAnchor="middle"
                      className="sd-chart-label"
                    >
                      —
                    </text>
                  </g>
                );
              })}

              {/* Disjoint valid path segments */}
              {segments.map((seg, sIdx) => {
                if (seg.length === 1) {
                  return (
                    <circle
                      key={`pt-${sIdx}`}
                      cx={seg[0].x}
                      cy={seg[0].y}
                      r={4}
                      fill="var(--yg-brand-ink, #175174)"
                    />
                  );
                }
                const d = seg
                  .map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`)
                  .join(" ");
                return (
                  <g key={`seg-${sIdx}`}>
                    <path d={d} className="sd-chart-line" />
                    {seg.map((p) => (
                      <circle
                        key={`dot-${p.idx}`}
                        cx={p.x}
                        cy={p.y}
                        r={3.5}
                        fill="var(--yg-brand-ink, #175174)"
                      />
                    ))}
                  </g>
                );
              })}

              {/* X-axis tick labels */}
              {points.map((pt, idx) => (
                <text
                  key={`x-${pt.id}`}
                  x={xAt(idx)}
                  y={height - 8}
                  textAnchor="middle"
                  className="sd-chart-label"
                >
                  {pt.label}
                </text>
              ))}
            </svg>
          </div>
        )}

        <Inline justify="between" align="center" wrap>
          <Text variant="meta">
            Missing intervals render as explicit gaps — never bridged or coerced to zero.
          </Text>
          <Text variant="meta" className="sd-tabular">
            {points.length} sealed cuts
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
