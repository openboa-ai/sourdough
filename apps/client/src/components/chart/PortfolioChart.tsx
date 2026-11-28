import { useId, useMemo, useState, type ReactNode } from "react";
import {
  Badge,
  Card,
  DataTable,
  DisclosureRow,
  Inline,
  Stack,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type { ReturnPoint } from "../../api/types";
import { formatMinorCurrency, formatSignedPct } from "../../lib/format";
import { ChartHeaderControls, ChartLegend } from "./ChartLegend";
import { ChartSvg } from "./ChartSvg";
import {
  computeDeltaLabel,
  computeDriftDisplay,
  computeTimeframeSlices,
  computeYDomain,
  extractPointTimeMs,
  extractSeriesValues,
  filterVisibleBenchmarkPoints,
  findFallbackIndex,
  findNearestBenchmarkPoint,
  findPreviousValidIndex,
  type ChartUnit,
  type Timeframe,
  type TimestampedReturnPoint,
} from "./math";

export interface PortfolioChartProps {
  points: ReturnPoint[];
  currency: string;
  asOfLabel: string;
  title?: string;
  subtitlePct?: string;
  subtitlePnl?: string;
  emptyMessage?: string;
  emptyAction?: ReactNode;
  navColumnLabel?: string;
  primarySeriesLabel?: string;
  benchmarkLabel?: string;
  benchmarkPoints?: ReturnPoint[];
  benchmarkDriftPp?: number | null;
}

export function PortfolioChart({
  points,
  currency,
  asOfLabel,
  title = "Cumulative Performance",
  subtitlePct,
  subtitlePnl,
  emptyMessage,
  emptyAction,
  navColumnLabel = "Sealed NAV",
  primarySeriesLabel,
  benchmarkLabel,
  benchmarkPoints,
  benchmarkDriftPp,
}: PortfolioChartProps) {
  const [unit, setUnit] = useState<ChartUnit>("pct");
  const [timeframe, setTimeframe] = useState<Timeframe>("all");
  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  const readoutId = useId();

  const timeframeInfo = useMemo(() => computeTimeframeSlices(points), [points]);
  const effectiveTimeframe: Timeframe = timeframeInfo.availability[timeframe]
    ? timeframe
    : "all";
  const visiblePoints = timeframeInfo.slices[effectiveTimeframe];

  const values = useMemo(
    () => extractSeriesValues(visiblePoints, unit),
    [visiblePoints, unit]
  );

  const visibleBenchmarkPoints = useMemo(
    () => filterVisibleBenchmarkPoints(benchmarkPoints, visiblePoints),
    [benchmarkPoints, visiblePoints]
  );

  const benchmarkValues = useMemo(
    () => extractSeriesValues(visibleBenchmarkPoints, unit),
    [visibleBenchmarkPoints, unit]
  );

  const { hasAny, hasBenchmark, domain } = useMemo(
    () => computeYDomain(values, benchmarkValues),
    [benchmarkValues, values]
  );

  const visibleTimes = useMemo(
    () => visiblePoints.map((pt) => extractPointTimeMs(pt as TimestampedReturnPoint)),
    [visiblePoints]
  );

  const fallbackIdx = useMemo(
    () => findFallbackIndex(values, visiblePoints.length),
    [values, visiblePoints.length]
  );

  const inspectedIdx =
    activeIdx !== null && activeIdx < visiblePoints.length ? activeIdx : fallbackIdx;
  const inspectedPoint = inspectedIdx !== null ? visiblePoints[inspectedIdx] : null;
  const inspectedValue = inspectedIdx !== null ? values[inspectedIdx] : null;

  const inspectedBenchmarkPoint = useMemo(
    () =>
      findNearestBenchmarkPoint(
        inspectedIdx,
        visiblePoints,
        visibleTimes,
        visibleBenchmarkPoints,
        hasBenchmark
      ),
    [hasBenchmark, inspectedIdx, visibleBenchmarkPoints, visiblePoints, visibleTimes]
  );

  const prevValidIdx = useMemo(
    () => findPreviousValidIndex(values, inspectedIdx),
    [inspectedIdx, values]
  );

  const deltaLabel = useMemo(
    () =>
      computeDeltaLabel(
        inspectedPoint,
        inspectedValue,
        prevValidIdx,
        visiblePoints,
        unit,
        currency
      ),
    [currency, inspectedPoint, inspectedValue, prevValidIdx, unit, visiblePoints]
  );

  const driftDisplay = useMemo(
    () => computeDriftDisplay(inspectedPoint, inspectedBenchmarkPoint, benchmarkDriftPp),
    [benchmarkDriftPp, inspectedBenchmarkPoint, inspectedPoint]
  );

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
      label: navColumnLabel,
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

  const resolvedDescription =
    unit === "pct"
      ? subtitlePct ?? `0% baseline · Service-projected returns · ${asOfLabel}`
      : subtitlePnl ?? `$0 baseline · Service-projected cumulative P&L · ${asOfLabel}`;

  return (
    <Card
      title={title}
      description={resolvedDescription}
      headerAction={
        <ChartHeaderControls
          effectiveTimeframe={effectiveTimeframe}
          availability={timeframeInfo.availability}
          onTimeframeChange={(tf) => {
            setTimeframe(tf);
            setActiveIdx(null);
          }}
          unit={unit}
          onUnitChange={setUnit}
        />
      }
      variant="raised"
    >
      <Stack gap={12}>
        {inspectedPoint ? (
          <ChartLegend
            readoutId={readoutId}
            inspectedPoint={inspectedPoint}
            inspectedValue={inspectedValue}
            unit={unit}
            currency={currency}
            deltaLabel={deltaLabel}
            primarySeriesLabel={primarySeriesLabel}
            hasBenchmark={hasBenchmark}
            benchmarkLabel={benchmarkLabel}
            inspectedBenchmarkPoint={inspectedBenchmarkPoint}
            driftDisplay={driftDisplay}
          />
        ) : null}

        {!hasAny ? (
          <div className="sd-chart-standby">
            <Badge tone="neutral" variant="outline" dot={false}>
              0.0% Baseline · Standby
            </Badge>
            <Text variant="help">
              {emptyMessage ??
                "No readings yet. Sealed valuations will plot here on a 0% baseline without inventing missing intervals."}
            </Text>
            {emptyAction ?? null}
          </div>
        ) : (
          <ChartSvg
            title={title}
            readoutId={readoutId}
            currency={currency}
            unit={unit}
            visiblePoints={visiblePoints}
            visibleTimes={visibleTimes}
            values={values}
            visibleBenchmarkPoints={visibleBenchmarkPoints}
            benchmarkValues={benchmarkValues}
            domain={domain}
            inspectedIdx={inspectedIdx}
            onActiveIdxChange={setActiveIdx}
          />
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
            caption={`${title} readings`}
            columns={columns}
            rows={rows}
          />
        </DisclosureRow>
      </Stack>
    </Card>
  );
}
