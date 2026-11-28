import { Fragment, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Card,
  DataTable,
  DisclosureRow,
  Divider,
  Inline,
  List,
  RecordFacts,
  RoutineRow,
  Stack,
  Surface,
  Text,
  WorkEvent,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type {
  AgentDetailProjection,
  AgentDreamingSummary,
  AgentEpisodeItem,
  AgentResponsibilityItem,
  AgentWorkDecisionItem,
} from "../../../api/types";
import { ControlledInlineConfirm } from "../../../components/ControlledInlineConfirm";
import {
  formatDeadlineNs,
  formatMinorCurrency,
  formatNsWithClock,
  newCommandId,
} from "../../../lib/format";
import { useControlPlane, useOutbox } from "../../../state/store";
import {
  workEventKindForRun,
  workEventStateForRun,
  workItemStateTone,
  workItemSummary,
} from "./helpers";

function formatCooldownSeconds(ns: number | undefined): string {
  if (!ns || ns <= 0) return "No cooldown";
  const sec = Math.round(ns / 1_000_000_000);
  if (sec < 60) return `${sec}s cooldown`;
  const min = Math.round(sec / 60);
  return `${min}m cooldown`;
}

export interface AgentWorkTabProps {
  agentId: string;
  workItems: AgentDetailProjection["tabs"]["work_items"];
  workActivity: AgentDetailProjection["tabs"]["work_activity"];
  conditions: AgentDetailProjection["tabs"]["conditions"];
  workDecisions?: AgentWorkDecisionItem[];
  agentEpisodes?: AgentEpisodeItem[];
  responsibilities?: AgentResponsibilityItem[];
  dreamingSummary?: AgentDreamingSummary | null;
}

export function AgentWorkTab({
  agentId,
  workItems,
  workActivity,
  conditions,
  workDecisions = [],
  agentEpisodes = [],
  responsibilities = [],
  dreamingSummary = null,
}: AgentWorkTabProps) {
  const { meta } = useControlPlane();
  const { canWrite, outbox, dispatchCommand } = useOutbox();
  const [cancelWorkTargetId, setCancelWorkTargetId] = useState<string | null>(null);

  useEffect(() => {
    setCancelWorkTargetId(null);
  }, [agentId]);

  const canCancelWork =
    canWrite &&
    Boolean(meta?.supported_commands?.cancel_work?.includes("work-command-v1"));

  return (
    <Stack gap={12}>
      <Card title="Work Items" variant="raised">
        {workItems.length === 0 ? (
          <Text variant="help">No active or queued work items for this agent.</Text>
        ) : (
          <Stack gap={12}>
            {workItems.map((item, idx) => {
              const summary = workItemSummary(item.payload);
              const isCancellable = item.state === "due" || item.state === "waiting";
              const isConfirmOpen = cancelWorkTargetId === item.work_item_id;
              const isPending =
                (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
                outbox.intent?.command_type === "cancel_work" &&
                outbox.intent?.preconditions?.work_item_id === item.work_item_id;

              return (
                <Fragment key={item.work_item_id}>
                  {idx > 0 ? <Divider /> : null}
                  <Stack gap={8}>
                    <Inline justify="between" align="center" gap={8} wrap>
                      <Inline gap={8} align="center" wrap>
                        <Text variant="body">{item.duty_kind}</Text>
                        <Badge
                          tone={workItemStateTone(item.state)}
                          variant="soft"
                          dot={false}
                        >
                          {item.state}
                        </Badge>
                        <Text className="sd-mono">{item.work_item_id}</Text>
                      </Inline>
                      <Inline gap={8} align="center" wrap>
                        <Text variant="help" className="sd-tabular">
                          Updated {formatNsWithClock(item.updated_at_ns, "Unavailable")}
                        </Text>
                        {isCancellable ? (
                          <Button
                            emphasis="soft"
                            tone="danger"
                            size="compact"
                            shape="pill"
                            disabled={!canCancelWork}
                            onClick={() =>
                              setCancelWorkTargetId((cur) =>
                                cur === item.work_item_id ? null : item.work_item_id
                              )
                            }
                          >
                            Cancel Work Item
                          </Button>
                        ) : null}
                      </Inline>
                    </Inline>
                    <Text variant="help">
                      Source: {item.source_kind} ({item.source_id})
                      {item.attempt_count && item.attempt_count > 0
                        ? ` · Attempts: ${item.attempt_count}`
                        : ""}
                      {item.next_retry_at_ns
                        ? ` · Next retry: ${formatDeadlineNs(item.next_retry_at_ns)}`
                        : ""}
                      {summary ? ` · ${summary}` : ""}
                    </Text>
                    {meta && (isCancellable || isConfirmOpen) ? (
                      <ControlledInlineConfirm
                        open={isConfirmOpen}
                        surface="inline"
                        pending={isPending}
                        onCancel={() => setCancelWorkTargetId(null)}
                        decision={{
                          id: `cancel-work-${item.work_item_id}`,
                          revision: `${item.work_item_id}:${item.state}:${item.updated_at_ns}`,
                          title: `Cancel Work Item ${item.work_item_id}`,
                          consequence:
                            "Cancels this work item and any queued, running, or waiting runs attached to it. Preserves completed work, published reports, and standing grants.",
                          confirmLabel: "Confirm Cancel Work Item",
                          destructive: true,
                          available: canCancelWork && isCancellable,
                          requireReason: true,
                          defaultReason: `Board cancelled ${item.duty_kind} work item`,
                          whatChanges: [
                            {
                              label: "Work item",
                              value: `${item.work_item_id} transitions to cancelled`,
                            },
                            {
                              label: "Attached runs",
                              value: "Queued/running/waiting runs cancelled",
                            },
                          ],
                          whatStaysUntouched: [
                            {
                              label: "Standing authority",
                              value: "Grant and role remain intact",
                            },
                            {
                              label: "Private Desk & reports",
                              value: "Existing drafts and publications preserved",
                            },
                          ],
                          technicalDetails: [
                            { label: "command_type", value: "cancel_work" },
                            { label: "work_item_id", value: item.work_item_id },
                            { label: "expected_state", value: item.state },
                          ],
                        }}
                        onConfirm={async ({ reason }) => {
                          const lifecycle = await dispatchCommand({
                            company_id: meta.company_id,
                            command_id: newCommandId("cancel-work"),
                            command_type: "cancel_work",
                            contract_version: "work-command-v1",
                            preconditions: {
                              work_item_id: item.work_item_id,
                              expected_state: item.state,
                            },
                            payload: { reason },
                            label: `Cancel work item ${item.work_item_id}`,
                            sent_ns: Date.now() * 1_000_000,
                          });
                          if (
                            lifecycle === "applied" ||
                            lifecycle === "accepted" ||
                            lifecycle === "unknown"
                          ) {
                            setCancelWorkTargetId(null);
                          }
                        }}
                      />
                    ) : null}
                  </Stack>
                </Fragment>
              );
            })}
          </Stack>
        )}
      </Card>

      <Card title="Recent Work Runs" variant="raised">
        {workActivity.length === 0 ? (
          <Text variant="help">No work runs recorded for this agent yet.</Text>
        ) : (
          <Stack gap={12}>
            {workActivity.map((run, idx) => (
              <Fragment key={run.run_id}>
                {idx > 0 ? <Divider /> : null}
                <Stack gap={6}>
                  <WorkEvent
                    event={{
                      id: run.run_id,
                      kind: workEventKindForRun(run.status),
                      title: run.duty_kind,
                      detail: `Run ${run.run_id} · Item ${run.work_item_id} · Fence #${run.fencing_token} · Cap ${
                        run.budget_ceiling_minor > 0
                          ? formatMinorCurrency(run.budget_ceiling_minor)
                          : "No run cap"
                      } · Created ${formatNsWithClock(run.created_at_ns, "Unavailable")}`,
                      time: formatNsWithClock(run.updated_at_ns, "Unavailable"),
                      state: workEventStateForRun(run.status),
                    }}
                  />
                  {run.outcome !== null ? (
                    <DisclosureRow label="Run outcome" initiallyOpen={false}>
                      <Surface variant="surface" density="compact">
                        <Text as="p" className="sd-mono sd-agent-pre">
                          {JSON.stringify(run.outcome, null, 2)}
                        </Text>
                      </Surface>
                    </DisclosureRow>
                  ) : null}
                </Stack>
              </Fragment>
            ))}
          </Stack>
        )}
      </Card>

      <Card title="Armed Wake Conditions" variant="raised">
        {conditions.length === 0 ? (
          <Text variant="help">No wake conditions armed.</Text>
        ) : (
          <List label="Agent wake conditions" divided>
            {conditions.map((c) => {
              const fireSummary =
                c.max_fires !== null && c.max_fires !== undefined
                  ? `Fires ${c.fire_count ?? 0}/${c.max_fires}`
                  : c.fire_count !== undefined
                    ? `Fires ${c.fire_count}`
                    : null;
              const cooldownSummary =
                c.cooldown_ns && c.cooldown_ns > 0
                  ? formatCooldownSeconds(c.cooldown_ns)
                  : null;
              const cadenceParts = [
                c.condition_id,
                c.source_identity,
                cooldownSummary,
                fireSummary,
              ].filter(Boolean);
              return (
                <li key={c.condition_id} className="yg-list-item">
                  <RoutineRow
                    routine={{
                      id: c.condition_id,
                      title: c.trigger_kind,
                      cadence: cadenceParts.join(" · "),
                      state: c.status === "armed" ? "scheduled" : "paused",
                      nextRun:
                        c.due_at_ns === null
                          ? "Not scheduled"
                          : formatNsWithClock(c.due_at_ns, "Not scheduled"),
                    }}
                  />
                </li>
              );
            })}
          </List>
        )}
      </Card>

      <WorkDecisionsAndEpisodesSection
        workDecisions={workDecisions}
        agentEpisodes={agentEpisodes}
        responsibilities={responsibilities}
        dreamingSummary={dreamingSummary}
      />
    </Stack>
  );
}

function WorkDecisionsAndEpisodesSection({
  workDecisions,
  agentEpisodes,
  responsibilities,
  dreamingSummary,
}: {
  workDecisions: AgentWorkDecisionItem[];
  agentEpisodes: AgentEpisodeItem[];
  responsibilities: AgentResponsibilityItem[];
  dreamingSummary: AgentDreamingSummary | null;
}) {
  const decisionCols = useMemo<DataColumn[]>(
    () => [
      { id: "action", label: "Action", kind: "role" },
      { id: "evidence", label: "Evidence & Snapshot", kind: "text" },
      { id: "rationale", label: "Rationale (Redacted)", kind: "text" },
      { id: "time", label: "Recorded", kind: "numeric", align: "end" },
    ],
    []
  );
  const decisionRows = useMemo<DataRow[]>(
    () =>
      workDecisions.map((d) => ({
        id: d.decision_id,
        cells: {
          action: (
            <Stack gap={2}>
              <Badge tone="neutral" variant="soft" dot={false}>
                {d.action_kind_redacted ? "redacted" : d.action_kind}
              </Badge>
              <Text variant="help" className="sd-mono">
                {d.action_id}
              </Text>
            </Stack>
          ),
          evidence: (
            <Stack gap={2}>
              <Text variant="help" className="sd-mono">
                {d.evidence_refs.length} refs · {d.evidence_digest.slice(0, 12)}
              </Text>
              {d.active_snapshot_id ? (
                <Text variant="help" className="sd-mono">
                  snap {d.active_snapshot_id.slice(0, 12)}
                </Text>
              ) : null}
            </Stack>
          ),
          rationale: (
            <Stack gap={2}>
              <Text variant="help">Redacted to digest</Text>
              <Text variant="help" className="sd-mono">
                {d.rationale_digest.slice(0, 16)}
              </Text>
            </Stack>
          ),
          time: (
            <Text className="sd-tabular">
              {formatNsWithClock(d.decision_at_ns, "Unavailable")}
            </Text>
          ),
        },
      })),
    [workDecisions]
  );

  const episodeCols = useMemo<DataColumn[]>(
    () => [
      { id: "duty", label: "Duty & Outcome", kind: "identity" },
      { id: "summary", label: "Summary (Redacted)", kind: "text" },
      { id: "usage", label: "Tools & Tokens", kind: "numeric", align: "end" },
      { id: "cost", label: "Cost", kind: "numeric", align: "end" },
      { id: "time", label: "Recorded", kind: "numeric", align: "end" },
    ],
    []
  );
  const episodeRows = useMemo<DataRow[]>(
    () =>
      agentEpisodes.map((ep) => ({
        id: ep.episode_id,
        cells: {
          duty: (
            <Stack gap={2}>
              <Text variant="body">{ep.duty_kind}</Text>
              <Badge
                tone={
                  ep.outcome_status === "completed"
                    ? "success"
                    : ep.outcome_status === "failed"
                      ? "danger"
                      : "warning"
                }
                variant="soft"
                dot={false}
              >
                {ep.outcome_status}
              </Badge>
            </Stack>
          ),
          summary: (
            <Stack gap={2}>
              <Text variant="help">
                {ep.summary_redacted === false
                  ? ep.summary_text
                  : `Redacted (${ep.outcome_status}:${ep.duty_kind})`}
              </Text>
              <Text variant="help" className="sd-mono">
                digest {ep.summary_digest.slice(0, 16)}
              </Text>
            </Stack>
          ),
          usage: (
            <Text className="sd-tabular">
              {ep.tool_calls_count} calls · {ep.input_tokens + ep.output_tokens} tok
            </Text>
          ),
          cost: (
            <Text className="sd-tabular">
              {formatMinorCurrency(ep.cost_minor_units)}
            </Text>
          ),
          time: (
            <Text className="sd-tabular">
              {formatNsWithClock(ep.recorded_at_ns, "Unavailable")}
            </Text>
          ),
        },
      })),
    [agentEpisodes]
  );

  return (
    <>
      <Card
        title={`Work Decisions (${workDecisions.length})`}
        description="Evidence-backed shift decisions (private rationales remain redacted to SHA-256 digests)"
        variant="raised"
        className="sd-ledger-card"
      >
        {decisionRows.length === 0 ? (
          <Text variant="help">No work decisions recorded yet.</Text>
        ) : (
          <DataTable caption="Agent work decisions" columns={decisionCols} rows={decisionRows} />
        )}
      </Card>

      <Card
        title={`Episodic Memory (${agentEpisodes.length})`}
        description="Shift episode telemetry (private narrative summaries remain redacted)"
        variant="raised"
        className="sd-ledger-card"
      >
        {episodeRows.length === 0 ? (
          <Text variant="help">No agent episodes recorded yet.</Text>
        ) : (
          <DataTable caption="Agent episodes" columns={episodeCols} rows={episodeRows} />
        )}
      </Card>

      <Card
        title="Dreaming & Proactive Continuity"
        description="Redacted work.dreaming consolidation cycles and standing discovery hysteresis"
        variant="raised"
      >
        <Stack gap={12}>
          {dreamingSummary ? (
            <div className="sd-facts-wrap">
              <RecordFacts
                source="work_runs (work.dreaming)"
                asOf={
                  dreamingSummary.last_run_at_ns
                    ? formatNsWithClock(dreamingSummary.last_run_at_ns)
                    : "No cycles"
                }
                facts={[
                  {
                    id: "dream-total",
                    label: "Dreaming Cycles",
                    value: `${dreamingSummary.total_runs} total (${dreamingSummary.completed_runs} completed · ${dreamingSummary.failed_runs} failed)`,
                  },
                  {
                    id: "dream-status",
                    label: "Last Cycle Status",
                    value: dreamingSummary.last_status ?? "Never run",
                  },
                ]}
              />
            </div>
          ) : null}
          {dreamingSummary?.last_outcome ? (
            <DisclosureRow label="Last redacted dreaming outcome" initiallyOpen={false}>
              <Surface variant="surface" density="compact">
                <Text as="p" className="sd-mono sd-agent-pre">
                  {JSON.stringify(dreamingSummary.last_outcome, null, 2)}
                </Text>
              </Surface>
            </DisclosureRow>
          ) : null}
          {responsibilities.length > 0 ? (
            <List label="Standing responsibilities and discovery state" divided>
              {responsibilities.map((r) => (
                <li key={r.responsibility_id} className="yg-list-item">
                  <RoutineRow
                    routine={{
                      id: r.responsibility_id,
                      title: `${r.duty_kind} (${r.status})`,
                      cadence: `${r.remit} · No-ops: ${r.consecutive_noops} · Window wakes: ${r.window_wakes}`,
                      state: r.status === "active" ? "scheduled" : "paused",
                      nextRun:
                        r.cooldown_until_ns > 0
                          ? `Cooldown ${formatDeadlineNs(r.cooldown_until_ns)}`
                          : "Ready",
                    }}
                  />
                </li>
              ))}
            </List>
          ) : (
            <Text variant="help">No standing responsibilities registered for this agent.</Text>
          )}
        </Stack>
      </Card>
    </>
  );
}
