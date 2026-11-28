import { Badge, Inline, SegmentedControl, Text } from "@openboa/yogurt";
import type { ReturnPoint } from "../../api/types";
import { formatMinorCurrency, formatSignedPct } from "../../lib/format";
import type { ChartUnit, Timeframe } from "./math";

export interface ChartHeaderControlsProps {
  effectiveTimeframe: Timeframe;
  availability: Record<Timeframe, boolean>;
  onTimeframeChange: (tf: Timeframe) => void;
  unit: ChartUnit;
  onUnitChange: (unit: ChartUnit) => void;
}

export function ChartHeaderControls({
  effectiveTimeframe,
  availability,
  onTimeframeChange,
  unit,
  onUnitChange,
}: ChartHeaderControlsProps) {
  return (
    <Inline gap={8} align="center" wrap>
      <SegmentedControl
        label="Performance timeframe"
        value={effectiveTimeframe}
        onValueChange={(next) => {
          const tf = next as Timeframe;
          if (!availability[tf]) return;
          onTimeframeChange(tf);
        }}
        options={[
          { id: "1d", label: "1D", disabled: !availability["1d"] },
          { id: "7d", label: "7D", disabled: !availability["7d"] },
          { id: "30d", label: "30D", disabled: !availability["30d"] },
          { id: "all", label: "ALL" },
        ]}
      />
      <SegmentedControl
        label="Performance unit"
        value={unit}
        onValueChange={(next) => onUnitChange(next as ChartUnit)}
        options={[
          { id: "pct", label: "Return %" },
          { id: "pnl", label: "P&L $" },
        ]}
      />
    </Inline>
  );
}

export interface ChartLegendProps {
  readoutId: string;
  inspectedPoint: ReturnPoint;
  inspectedValue: number | null;
  unit: ChartUnit;
  currency: string;
  deltaLabel: string;
  primarySeriesLabel?: string;
  hasBenchmark: boolean;
  benchmarkLabel?: string;
  inspectedBenchmarkPoint: ReturnPoint | null;
  driftDisplay: number | null;
}

export function ChartLegend({
  readoutId,
  inspectedPoint,
  inspectedValue,
  unit,
  currency,
  deltaLabel,
  primarySeriesLabel,
  hasBenchmark,
  benchmarkLabel,
  inspectedBenchmarkPoint,
  driftDisplay,
}: ChartLegendProps) {
  return (
    <div id={readoutId} className="sd-chart-readout" role="status" aria-live="polite">
      <Inline gap={8} align="center" wrap>
        <Badge
          tone={inspectedValue === null ? "warning" : "brand"}
          variant="soft"
          dot={false}
        >
          {inspectedPoint.label}
        </Badge>
        {primarySeriesLabel ? (
          <Inline gap={6} align="center">
            <span className="sd-chart-legend-solid" aria-hidden="true" />
            <Text variant="meta">{primarySeriesLabel}</Text>
          </Inline>
        ) : null}
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
        {hasBenchmark && benchmarkLabel && inspectedBenchmarkPoint ? (
          <Inline gap={6} align="center" wrap>
            <span className="sd-chart-legend-dashed" aria-hidden="true" />
            <Text variant="meta" className="sd-tabular">
              {benchmarkLabel}:{" "}
              {unit === "pct"
                ? formatSignedPct(inspectedBenchmarkPoint.return_pct)
                : formatMinorCurrency(inspectedBenchmarkPoint.pnl_minor, currency, {
                    signed: true,
                  })}
            </Text>
            {driftDisplay !== null ? (
              <Badge
                tone={Math.abs(driftDisplay) <= 0.25 ? "success" : "warning"}
                variant="soft"
                dot={false}
              >
                Drift {driftDisplay > 0 ? "+" : ""}
                {driftDisplay.toFixed(2)} pp
              </Badge>
            ) : null}
          </Inline>
        ) : null}
        <Text variant="meta" className="sd-tabular">
          NAV:{" "}
          {inspectedPoint.nav_minor === null
            ? "No reading"
            : formatMinorCurrency(inspectedPoint.nav_minor, currency)}
        </Text>
        <Text variant="meta">{inspectedPoint.source}</Text>
      </Inline>
    </div>
  );
}
