import { useEffect, useState } from "react";
import { ThinkingIndicator } from "@openboa/yogurt/motion-thinking-indicator";
import {
  AgentAvatar,
  Badge,
  Breadcrumb,
  Button,
  Card,
  DataTable,
  Inline,
  List,
  Message,
  Navbar,
  Notice,
  PageHeader,
  RecordFacts,
  ResourceRow,
  Result,
  RoutineRow,
  RuntimePanel,
  Skeleton,
  Stack,
  Status,
  Tabs,
  Text,
  Transcript,
  WorkEvent,
  type DataColumn,
  type DataRow,
  type RuntimeState,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatMinorCurrency,
  formatNsClock,
  formatRelativeNs,
  newCommandId,
} from "../api/client";
import type { AgentDetailProjection } from "../api/types";
import { ControlledInlineConfirm } from "../components/ControlledInlineConfirm";
import { useApp } from "../state/store";

export function AgentDetailView({ agentId }: { agentId: string }) {
  const { navigate, refreshSeq, meta, canWrite, dispatchCommand, reportApiError } = useApp();
  const [data, setData] = useState<AgentDetailProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("work");
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<AgentDetailProjection>(
          `agents/${encodeURIComponent(agentId)}`,
          { signal: controller.signal }
        );
        if (res.data) setData(res.data);
      } catch (err) {
        if (!controller.signal.aborted) reportApiError(err);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [agentId, refreshSeq, reportApiError]);

  if (loading && !data) {
    return <Skeleton label="Loading agent desk and permissions" lines={6} />;
  }

  if (!data) {
    return (
      <Result
        title="Agent not found"
        action={
          <Button emphasis="soft" shape="pill" onClick={() => navigate("/office")}>
            Back to Agents &amp; Activity
          </Button>
        }
      >
        No agent record with ID {agentId} exists in this company.
      </Result>
    );
  }

  const profile = data.profile;
  const activeRun = profile.active_run;

  const runtimeState: RuntimeState = profile.archived
    ? "offline"
    : activeRun?.status === "running"
      ? "working"
      : activeRun?.status === "waiting"
        ? "waiting"
        : "available";

  const peerCols: DataColumn[] = [
    { id: "dir", label: "Direction", kind: "role" },
    { id: "peer", label: "Counterparty", kind: "identity" },
    { id: "question", label: "Question & Expected Decision", kind: "text" },
    { id: "state", label: "State", kind: "role" },
  ];

  const peerRows: DataRow[] = data.tabs.peer_requests.map((pr) => ({
    id: pr.request_id,
    cells: {
      dir: (
        <Badge tone="neutral" variant="outline" dot={false}>
          {pr.direction}
        </Badge>
      ),
      peer: pr.direction === "outgoing" ? pr.recipient_name : pr.requester_name,
      question: (
        <Stack gap={2}>
          <Text variant="body">{pr.question}</Text>
          <Text variant="meta">Expected: {pr.expected_decision}</Text>
        </Stack>
      ),
      state: (
        <Badge
          tone={
            pr.state === "open"
              ? "warning"
              : pr.state === "answered"
                ? "success"
                : "neutral"
          }
          dot={false}
        >
          {pr.state}
        </Badge>
      ),
    },
  }));

  return (
    <Stack gap={16}>
      <Navbar
        variant="back"
        label="Back to Agents & Activity"
        onBack={() => navigate("/office", { focusReturnId: profile.agent_id })}
      />

      <Breadcrumb
        items={[
          { id: "office", label: "Agents & Activity", href: "/office" },
          {
            id: profile.agent_id,
            label: profile.display_name,
            href: `/office/agents/${profile.agent_id}`,
          },
        ]}
      />

      <PageHeader
        leading={
          <AgentAvatar
            agent={profile.avatar_variant}
            state={profile.avatar_state}
            size={44}
            label={profile.display_name}
          />
        }
        title={profile.display_name}
        detail={`${profile.role_label} · ${profile.execution_profile}`}
        action={
          <Inline gap={8} align="center" wrap>
            <Status tone={activeRun?.status === "running" ? "brand" : "success"}>
              {profile.archived
                ? "Archived"
                : activeRun
                  ? activeRun.status
                  : "Standing watch"}
            </Status>
            <Button
              emphasis="soft"
              size="compact"
              shape="pill"
              onClick={() =>
                navigate("/messages", {
                  contextRef: {
                    kind: "agent",
                    id: profile.agent_id,
                    label: `${profile.display_name} (${profile.role_label})`,
                  },
                })
              }
            >
              Ask CEO about {profile.display_name}
            </Button>
            {activeRun &&
            (activeRun.status === "running" || activeRun.status === "waiting") ? (
              <Button
                emphasis="soft"
                tone="danger"
                size="compact"
                shape="pill"
                disabled={!canWrite}
                onClick={() => setStopConfirmOpen((v) => !v)}
              >
                Stop Current Task
              </Button>
            ) : null}
          </Inline>
        }
      />

      {activeRun?.status === "running" ? (
        <ThinkingIndicator
          active
          label={`${profile.display_name} is executing ${activeRun.duty_kind} (fence #${activeRun.fencing_token})`}
        />
      ) : null}

      {activeRun && meta ? (
        <ControlledInlineConfirm
          open={stopConfirmOpen}
          onCancel={() => setStopConfirmOpen(false)}
          decision={{
            id: `interrupt-${activeRun.run_id}`,
            revision: `${activeRun.run_id}:${activeRun.fencing_token}`,
            title: `Interrupt ${profile.display_name}'s Current Run`,
            consequence:
              "Cancels the active fenced work run immediately and marks its work item cancelled. Does not revoke the agent's standing grant or delete published reports.",
            confirmLabel: "Stop Current Task",
            destructive: true,
            available: canWrite,
            requireReason: true,
            defaultReason: `Board interrupted ${activeRun.duty_kind} run`,
            whatChanges: [
              { label: "Active run", value: `${activeRun.duty_kind} transitions to cancelled` },
              { label: "Fencing token", value: `Token #${activeRun.fencing_token} invalidated` },
            ],
            whatStaysUntouched: [
              { label: "Standing authority", value: "Grant and role remain intact" },
              { label: "Private Desk & reports", value: "Existing drafts and publications preserved" },
            ],
            technicalDetails: [
              { label: "command_type", value: "interrupt_run" },
              { label: "run_id", value: activeRun.run_id },
              { label: "expected_fencing_token", value: String(activeRun.fencing_token) },
            ],
          }}
          onConfirm={({ reason }) => {
            void dispatchCommand({
              company_id: meta.company_id,
              command_id: newCommandId("interrupt"),
              command_type: "interrupt_run",
              contract_version: "work-command-v1",
              preconditions: {
                run_id: activeRun.run_id,
                expected_fencing_token: activeRun.fencing_token,
              },
              payload: { reason },
              label: `Stop ${profile.display_name}'s active run`,
              sent_ns: Date.now() * 1_000_000,
            });
          }}
        />
      ) : null}

      <div className="sd-split-equal">
        <RuntimePanel
          state={runtimeState}
          title="Desk & Isolated Harness"
          detail={data.desk_workspace.private_draft_notice}
          preview={
            <RecordFacts
              source="private_desk + shared_publication"
              asOf={`seq #${data.event_seq}`}
              facts={[
                {
                  id: "exec",
                  label: "Execution Sandbox",
                  value: data.desk_workspace.execution_profile,
                },
                {
                  id: "drafts",
                  label: "Private Drafts",
                  value: (
                    <Badge tone="neutral" variant="soft" dot={false}>
                      {data.desk_workspace.private_draft_count} private drafts
                    </Badge>
                  ),
                },
                {
                  id: "pubs",
                  label: "Published Reports",
                  value: String(data.desk_workspace.published_count),
                },
                {
                  id: "runs",
                  label: "Recorded Work Runs",
                  value: String(data.desk_workspace.work_run_count),
                },
              ]}
            />
          }
        />

        <Card
          title="Authority, Permissions & Budget"
          description="Scoped standing grants and hard operating ceilings"
          variant="raised"
        >
          <Stack gap={12}>
            <RecordFacts
              source="standing_grant + staffing_authority"
              asOf={`seq #${data.event_seq}`}
              facts={[
                {
                  id: "role",
                  label: "Assigned Role",
                  value: (
                    <Badge tone={profile.is_ceo ? "brand" : "neutral"} dot={false}>
                      {profile.role_label}
                    </Badge>
                  ),
                },
                {
                  id: "scope",
                  label: "Standing Grant Scope",
                  value: data.authority_budget.grant_scope || "None",
                },
                {
                  id: "budget",
                  label: "Monthly Operating Budget",
                  value:
                    data.authority_budget.monthly_budget_minor > 0
                      ? formatMinorCurrency(data.authority_budget.monthly_budget_minor)
                      : "Company mandate governed",
                },
                {
                  id: "hire",
                  label: "Staffing Authority",
                  value: data.authority_budget.can_hire
                    ? `Can hire up to ${data.authority_budget.hiring_slots} slots (${data.authority_budget.hiring_turns} turns)`
                    : "Individual contributor (no hiring authority)",
                },
              ]}
            />
            <Stack gap={4}>
              <Text variant="meta">Hard Prohibitions</Text>
              {data.authority_budget.prohibitions.map((rule, idx) => (
                <Text key={idx} variant="help">
                  · {rule}
                </Text>
              ))}
            </Stack>
          </Stack>
        </Card>
      </div>

      <Tabs
        label="Agent individual activity"
        presentation="pill"
        value={activeTab}
        onValueChange={setActiveTab}
        items={[
          {
            id: "work",
            label: `Work Activity (${data.tabs.work_activity.length})`,
            panel: (
              <Stack gap={12}>
                <Card title="Recent Work Runs" variant="raised">
                  {data.tabs.work_activity.length === 0 ? (
                    <Text variant="help">No work runs recorded for this agent yet.</Text>
                  ) : (
                    <List label="Agent work runs" divided>
                      {data.tabs.work_activity.map((run) => (
                        <WorkEvent
                          key={run.run_id}
                          event={{
                            id: run.run_id,
                            kind: "task",
                            title: run.duty_kind,
                            detail: `Run ${run.run_id.slice(0, 14)} · Fence #${run.fencing_token}`,
                            time: formatRelativeNs(run.updated_at_ns),
                            state:
                              run.status === "running"
                                ? "running"
                                : run.status === "completed"
                                  ? "ready"
                                  : run.status === "waiting"
                                    ? "waiting"
                                    : run.status === "failed" || run.status === "cancelled"
                                      ? "failed"
                                      : "done",
                          }}
                        />
                      ))}
                    </List>
                  )}
                </Card>

                <Card title="Armed Wake Conditions" variant="raised">
                  {data.tabs.conditions.length === 0 ? (
                    <Text variant="help">No wake conditions armed.</Text>
                  ) : (
                    <List label="Agent wake conditions" divided>
                      {data.tabs.conditions.map((c) => (
                        <RoutineRow
                          key={c.condition_id}
                          routine={{
                            id: c.condition_id,
                            title: c.trigger_kind,
                            cadence: c.condition_id,
                            state: c.status === "armed" ? "scheduled" : "paused",
                            nextRun: c.due_at_ns ? formatNsClock(c.due_at_ns) : undefined,
                          }}
                        />
                      ))}
                    </List>
                  )}
                </Card>
              </Stack>
            ),
          },
          {
            id: "talks",
            label: `Interactions & Talks (${data.tabs.interactions_and_talks.length})`,
            panel: (
              <Stack gap={12}>
                <Notice title="Discussion only · Read-only Board observation" tone="neutral">
                  Internal agent conversations do not assign or accept work on their own. To give
                  direction, send an advisory message to Founder · CEO in Company Messages.
                </Notice>
                <Card variant="raised">
                  {data.tabs.interactions_and_talks.length === 0 ? (
                    <Text variant="help">No project messages involving this agent yet.</Text>
                  ) : (
                    <Transcript
                      label="Agent internal discussions"
                      revision={data.event_seq}
                    >
                      {data.tabs.interactions_and_talks.map((msg) => (
                        <Message
                          key={msg.message_id}
                          speaker={`${msg.sender_name} (${msg.sender_role}) · #${msg.project_id}`}
                          variant={msg.sender_id === profile.agent_id ? "sent" : "received"}
                        >
                          {msg.body}
                        </Message>
                      ))}
                    </Transcript>
                  )}
                </Card>
              </Stack>
            ),
          },
          {
            id: "reports",
            label: `Published Reports (${data.tabs.published_reports.length})`,
            panel: (
              <Stack gap={12}>
                {data.tabs.published_reports.length === 0 ? (
                  <Card variant="raised">
                    <Text variant="help">
                      No shared company reports published by this agent yet. Unpublished work stays
                      private on their Desk.
                    </Text>
                  </Card>
                ) : (
                  <Card title="Selective Company Publications" variant="raised">
                    <List label="Published reports" divided>
                      {data.tabs.published_reports.map((pub) => (
                        <ResourceRow
                          key={pub.asset_id}
                          resource={{
                            id: pub.asset_id,
                            kind: "output",
                            title: `Asset ${pub.asset_id}`,
                            detail: `#${pub.project_id} · ${pub.content}`,
                          }}
                          onOpen={() =>
                            navigate("/messages", {
                              contextRef: {
                                kind: "shared_publication",
                                id: pub.asset_id,
                                label: `Asset ${pub.asset_id} by ${profile.display_name}`,
                              },
                            })
                          }
                        />
                      ))}
                    </List>
                  </Card>
                )}
              </Stack>
            ),
          },
          {
            id: "peers",
            label: `Peer Requests & Handoffs (${data.tabs.peer_requests.length + data.tabs.handoffs.length})`,
            panel: (
              <Stack gap={12}>
                <Card title="Blocking Peer Requests" variant="raised">
                  {peerRows.length === 0 ? (
                    <Text variant="help">No peer requests recorded.</Text>
                  ) : (
                    <DataTable caption="Peer requests" columns={peerCols} rows={peerRows} />
                  )}
                </Card>
                <Card title="Work Handoff Offers" variant="raised">
                  {data.tabs.handoffs.length === 0 ? (
                    <Text variant="help">No work handoffs recorded.</Text>
                  ) : (
                    <List label="Work handoff offers" divided>
                      {data.tabs.handoffs.map((h) => (
                        <WorkEvent
                          key={h.offer_id}
                          event={{
                            id: h.offer_id,
                            kind: "handoff",
                            title: `${h.sender_name} → ${h.recipient_name}`,
                            detail: h.expected_result,
                            state: h.accepted ? "ready" : "waiting",
                          }}
                        />
                      ))}
                    </List>
                  )}
                </Card>
              </Stack>
            ),
          },
        ]}
      />
    </Stack>
  );
}
