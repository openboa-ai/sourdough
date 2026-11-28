import { Fragment, useEffect, useState } from "react";
import {
  Badge,
  Button,
  Card,
  DisclosureRow,
  Divider,
  Inline,
  List,
  RoutineRow,
  Stack,
  Surface,
  Text,
  WorkEvent,
} from "@openboa/yogurt";
import type { AgentDetailProjection } from "../../../api/types";
import { ControlledInlineConfirm } from "../../../components/ControlledInlineConfirm";
import {
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

export interface AgentWorkTabProps {
  agentId: string;
  workItems: AgentDetailProjection["tabs"]["work_items"];
  workActivity: AgentDetailProjection["tabs"]["work_activity"];
  conditions: AgentDetailProjection["tabs"]["conditions"];
}

export function AgentWorkTab({
  agentId,
  workItems,
  workActivity,
  conditions,
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
            {conditions.map((c) => (
              <li key={c.condition_id} className="yg-list-item">
                <RoutineRow
                  routine={{
                    id: c.condition_id,
                    title: c.trigger_kind,
                    cadence: c.source_identity
                      ? `${c.condition_id} · ${c.source_identity}`
                      : c.condition_id,
                    state: c.status === "armed" ? "scheduled" : "paused",
                    nextRun:
                      c.due_at_ns === null
                        ? "Not scheduled"
                        : formatNsWithClock(c.due_at_ns, "Not scheduled"),
                  }}
                />
              </li>
            ))}
          </List>
        )}
      </Card>
    </Stack>
  );
}
