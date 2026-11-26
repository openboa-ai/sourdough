import { useEffect, useState } from "react";
import {
  AgentAvatar,
  Badge,
  Breadcrumb,
  Button,
  Card,
  DataTable,
  Inline,
  Message,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Skeleton,
  Stack,
  Status,
  Tabs,
  Text,
  Transcript,
  type DataColumn,
  type DataRow,
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

/**
 * Full-screen Agent Detail view (/office/agents/:agentId — ZERO right slide-over drawers).
 * Shows Profile Header, Desk & Workspace (opaque private draft count only),
 * Authority / Permissions / Budget, and 4 individual activity tabs.
 */
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
            Back to Agents & Activity
          </Button>
        }
      >
        No agent record with ID {agentId} exists in this company.
      </Result>
    );
  }

  const profile = data.profile;
  const activeRun = profile.active_run;

  const runCols: DataColumn[] = [
    { id: "run", label: "Work Run", kind: "identity" },
    { id: "duty", label: "Duty Kind", kind: "text" },
    { id: "status", label: "Status", kind: "role" },
    { id: "fence", label: "Fence Token", kind: "numeric", align: "end" },
    { id: "updated", label: "Updated", kind: "numeric", align: "end" },
  ];

  const runRows: DataRow[] = data.tabs.work_activity.map((run) => ({
    id: run.run_id,
    cells: {
      run: <Text className="sd-mono">{run.run_id.slice(0, 14)}</Text>,
      duty: run.duty_kind,
      status: (
        <Badge
          tone={
            run.status === "running"
              ? "brand"
              : run.status === "completed"
                ? "success"
                : run.status === "failed" || run.status === "cancelled"
                  ? "danger"
                  : "neutral"
          }
          dot={false}
        >
          {run.status}
        </Badge>
      ),
      fence: <Text className="sd-tabular">#{run.fencing_token}</Text>,
      updated: <Text className="sd-tabular">{formatRelativeNs(run.updated_at_ns)}</Text>,
    },
  }));

  const condCols: DataColumn[] = [
    { id: "id", label: "Condition", kind: "identity" },
    { id: "kind", label: "Trigger", kind: "text" },
    { id: "due", label: "Scheduled For", kind: "numeric", align: "end" },
    { id: "status", label: "Status", kind: "role" },
  ];

  const condRows: DataRow[] = data.tabs.conditions.map((c) => ({
    id: c.condition_id,
    cells: {
      id: <Text className="sd-mono">{c.condition_id}</Text>,
      kind: c.trigger_kind,
      due: c.due_at_ns ? formatNsClock(c.due_at_ns) : "Not scheduled",
      status: (
        <Badge tone={c.status === "armed" ? "brand" : "neutral"} dot={false}>
          {c.status}
        </Badge>
      ),
    },
  }));

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

      {/* Inline confirmation for interrupting an active run (zero popups!) */}
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

      {/* Side-by-side Desk & Workspace + Authority, Permissions & Budget */}
      <div className="sd-split-equal">
        <Card
          title="Desk & Workspace"
          description="Isolated per-agent workspace and selective publication boundary"
          variant="outlined"
        >
          <Stack gap={12}>
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
            <Text variant="meta">{data.desk_workspace.private_draft_notice}</Text>
          </Stack>
        </Card>

        <Card
          title="Authority, Permissions & Budget"
          description="Scoped standing grants and hard operating ceilings"
          variant="outlined"
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

      {/* 4 Individual Activity Tabs */}
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
                <Card title="Recent Work Runs" variant="outlined">
                  {runRows.length === 0 ? (
                    <Text variant="help">No work runs recorded for this agent yet.</Text>
                  ) : (
                    <DataTable caption="Agent work runs" columns={runCols} rows={runRows} />
                  )}
                </Card>
                <Card title="Armed Wake Conditions" variant="outlined">
                  {condRows.length === 0 ? (
                    <Text variant="help">No wake conditions armed.</Text>
                  ) : (
                    <DataTable
                      caption="Agent wake conditions"
                      columns={condCols}
                      rows={condRows}
                    />
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
                <Card variant="outlined">
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
                  <Card variant="outlined">
                    <Text variant="help">
                      No shared company reports published by this agent yet. Unpublished work stays
                      private on their Desk.
                    </Text>
                  </Card>
                ) : (
                  data.tabs.published_reports.map((pub) => (
                    <Card
                      key={pub.asset_id}
                      title={`Asset ${pub.asset_id}`}
                      description={`Project #${pub.project_id} · Audience: ${pub.audience.join(", ") || "company"}`}
                      variant="outlined"
                    >
                      <Text variant="body">{pub.content}</Text>
                    </Card>
                  ))
                )}
              </Stack>
            ),
          },
          {
            id: "peers",
            label: `Peer Requests & Handoffs (${data.tabs.peer_requests.length + data.tabs.handoffs.length})`,
            panel: (
              <Stack gap={12}>
                <Card title="Blocking Peer Requests" variant="outlined">
                  {peerRows.length === 0 ? (
                    <Text variant="help">No peer requests recorded.</Text>
                  ) : (
                    <DataTable caption="Peer requests" columns={peerCols} rows={peerRows} />
                  )}
                </Card>
                <Card title="Work Handoff Offers" variant="outlined">
                  {data.tabs.handoffs.length === 0 ? (
                    <Text variant="help">No work handoffs recorded.</Text>
                  ) : (
                    <Stack gap={8}>
                      {data.tabs.handoffs.map((h) => (
                        <Inline key={h.offer_id} justify="between" align="center" wrap>
                          <Text variant="body">
                            {h.sender_name} → {h.recipient_name} · {h.expected_result}
                          </Text>
                          <Badge tone={h.accepted ? "success" : "warning"} dot={false}>
                            {h.accepted ? "Accepted" : "Offered"}
                          </Badge>
                        </Inline>
                      ))}
                    </Stack>
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
