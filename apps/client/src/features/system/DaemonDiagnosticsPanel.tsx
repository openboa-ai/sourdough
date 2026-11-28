import {
  Badge,
  Card,
  Notice,
  RecordFacts,
  Stack,
  Text,
  type Fact,
} from "@openboa/yogurt";
import type { ResidentHealth } from "../../api/types";
import { formatNsWithClock } from "../../lib/format";

export interface DaemonDiagnosticsPanelProps {
  health: ResidentHealth | null | undefined;
  eventSeq: number;
}

export function DaemonDiagnosticsPanel({
  health,
  eventSeq,
}: DaemonDiagnosticsPanelProps) {
  if (!health) {
    return (
      <Card
        title="Always-On Resident Health"
        description="Resident autonomous work scheduler telemetry"
        variant="raised"
      >
        <Text variant="help">
          Resident scheduler health is unavailable (control plane running without an attached
          resident scheduler loop).
        </Text>
      </Card>
    );
  }

  const sleepTone =
    health.last_error
      ? "danger"
      : health.sleep_state === "awake"
        ? "success"
        : "neutral";
  const sleepLabel =
    health.sleep_state === "awake"
      ? "Awake"
      : health.sleep_state === "sleeping"
        ? "Sleeping"
        : health.sleep_state;

  const facts: Fact[] = [
    {
      id: "sleep",
      label: "Scheduler State",
      value: (
        <Badge tone={sleepTone} variant="soft" dot={false}>
          {sleepLabel}
        </Badge>
      ),
    },
    {
      id: "running",
      label: "Loop Running",
      value: health.running ? "Active" : "Stopped",
    },
    {
      id: "ticks",
      label: "Ticks Completed",
      value: String(health.ticks_completed),
    },
    {
      id: "runs",
      label: "Runs Executed",
      value: String(health.runs_executed),
    },
    {
      id: "triggers",
      label: "Triggers Enqueued",
      value: String(health.triggers_enqueued),
    },
    {
      id: "wakes",
      label: "Wake Count",
      value: String(health.wake_count),
    },
    {
      id: "wake-reason",
      label: "Last Wake Reason",
      value: health.last_wake_reason ?? "None recorded",
    },
    {
      id: "recovered",
      label: "Recovered Runs",
      value: String(health.recovered_runs_count),
    },
    {
      id: "last-tick",
      label: "Last Tick",
      value: formatNsWithClock(health.last_tick_at_ns, "No tick yet"),
    },
    {
      id: "last-good-tick",
      label: "Last Healthy Tick",
      value: formatNsWithClock(health.last_good_tick_at_ns, "No tick yet"),
    },
  ];

  return (
    <Card
      title="Always-On Resident Health"
      description="Autonomous event-driven scheduler sleep/wake diagnostics"
      variant="raised"
      headerAction={
        <Badge tone={sleepTone} variant="soft" dot={false}>
          {sleepLabel}
        </Badge>
      }
    >
      <Stack gap={12}>
        {health.last_error ? (
          <Notice title="Last Scheduler Error" tone="danger">
            {health.last_error}
          </Notice>
        ) : null}
        <div className="sd-facts-wrap">
          <RecordFacts
            source="GET /v1/meta (resident_health)"
            asOf={`seq #${eventSeq}`}
            facts={facts}
          />
        </div>
      </Stack>
    </Card>
  );
}
