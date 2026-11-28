import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ThinkingIndicator } from "@openboa/yogurt/motion-thinking-indicator";
import {
  AgentAvatar,
  Button,
  Inline,
  Navbar,
  PageHeader,
  Result,
  Skeleton,
  Stack,
  Status,
  Tabs,
  Text,
  type RuntimeState,
} from "@openboa/yogurt";
import { normalizeAvatarVariant } from "../../../api/avatar";
import type { AgentDetailProjection, ApprovalQuestionItem } from "../../../api/types";
import { useProjection } from "../../../api/useProjection";
import {
  ContextualApprovalList,
  usePendingApprovals,
} from "../../../components/ContextualApprovalCard";
import { ControlledInlineConfirm } from "../../../components/ControlledInlineConfirm";
import { SpaBreadcrumb } from "../../../components/SpaBreadcrumb";
import { formatMinorCurrency, newCommandId } from "../../../lib/format";
import {
  useControlPlane,
  useEscapeBack,
  useOutbox,
  useRouter,
} from "../../../state/store";
import { AgentDeskAuthoritySection } from "./AgentDeskAuthoritySection";
import { AgentHandoffsTab } from "./AgentHandoffsTab";
import { AgentReportsTab } from "./AgentReportsTab";
import { AgentSandboxTab } from "./AgentSandboxTab";
import { AgentTalksTab } from "./AgentTalksTab";
import { AgentWorkTab } from "./AgentWorkTab";
import { detailStatusTone } from "./helpers";

export function AgentDetailView({ agentId }: { agentId: string }) {
  const { navigate, askCeo } = useRouter();
  const { meta } = useControlPlane();
  const { canWrite, outbox, dispatchCommand } = useOutbox();

  const projectionPath = `agents/${encodeURIComponent(agentId)}`;
  const { data, loading, error, refetch } =
    useProjection<AgentDetailProjection>(projectionPath);

  const [activeTab, setActiveTab] = useState("work");
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false);
  const prevAgentIdRef = useRef(agentId);

  const activeData = data && data.profile.agent_id === agentId ? data : null;
  const isSwitchingAgent = prevAgentIdRef.current !== agentId;

  useEffect(() => {
    if (prevAgentIdRef.current !== agentId) {
      prevAgentIdRef.current = agentId;
      setStopConfirmOpen(false);
    }
  }, [agentId]);

  useEscapeBack("/agents", activeData ? activeData.profile.agent_id : agentId);

  const approvalPredicate = useCallback(
    (q: ApprovalQuestionItem) => {
      const imp = q.impact as Record<string, unknown>;
      const explicitTarget =
        typeof imp.agent_id === "string" || typeof imp.target_agent_id === "string";
      if (explicitTarget) {
        return imp.agent_id === agentId || imp.target_agent_id === agentId;
      }
      return (
        q.created_by_agent_id === agentId &&
        q.kind !== "live_promotion" &&
        q.kind !== "paper_to_live_migration"
      );
    },
    [agentId]
  );
  const agentApprovals = usePendingApprovals(approvalPredicate);

  const agentNameById = useMemo(() => {
    const map = new Map<string, string>();
    if (!activeData) return map;
    for (const talk of activeData.tabs.interactions_and_talks) {
      if (talk.sender_id && talk.sender_name) {
        map.set(talk.sender_id, talk.sender_name);
      }
    }
    for (const pr of activeData.tabs.peer_requests) {
      if (pr.requester_agent_id && pr.requester_name) {
        map.set(pr.requester_agent_id, pr.requester_name);
      }
      if (pr.recipient_agent_id && pr.recipient_name) {
        map.set(pr.recipient_agent_id, pr.recipient_name);
      }
    }
    for (const h of activeData.tabs.handoffs) {
      if (h.sender_id && h.sender_name) {
        map.set(h.sender_id, h.sender_name);
      }
      if (h.recipient_id && h.recipient_name) {
        map.set(h.recipient_id, h.recipient_name);
      }
    }
    if (meta?.founder_id) {
      map.set(meta.founder_id, "Founder · CEO");
    }
    map.set(activeData.profile.agent_id, activeData.profile.display_name);
    return map;
  }, [activeData, meta?.founder_id]);

  const displayNameFor = useCallback(
    (id: string): string => agentNameById.get(id) ?? id,
    [agentNameById]
  );

  if ((loading || isSwitchingAgent) && !activeData) {
    return <Skeleton label="Loading agent desk and permissions" lines={6} />;
  }

  if (!activeData) {
    const isNotFound = error === null || /\b404\b|not found/i.test(error);
    return (
      <Result
        title={isNotFound ? "Agent not found" : "Agent details unavailable"}
        action={
          <Inline gap={8} wrap>
            {!isNotFound ? (
              <Button emphasis="soft" shape="pill" onClick={refetch}>
                Retry
              </Button>
            ) : null}
            <Button emphasis="soft" shape="pill" onClick={() => navigate("/agents")}>
              Back to Agents
            </Button>
          </Inline>
        }
      >
        {isNotFound
          ? `No agent record with ID ${agentId} exists in this company.`
          : error ?? "Control plane temporarily unavailable while loading agent details."}
      </Result>
    );
  }

  const profile = activeData.profile;
  const activeRun = profile.active_run;

  const runtimeState: RuntimeState = profile.archived
    ? "offline"
    : activeRun?.status === "running"
      ? "working"
      : activeRun?.status === "waiting"
        ? "waiting"
        : "available";

  const canInterruptRun =
    canWrite &&
    Boolean(meta?.supported_commands?.interrupt_run?.includes("work-command-v1"));

  const taskBannerLabel = activeRun
    ? `${profile.display_name} is ${activeRun.status === "running" ? "executing" : "waiting on"} ${activeRun.duty_kind} · Fence #${activeRun.fencing_token}${
        activeRun.budget_ceiling_minor > 0
          ? ` · Cap ${formatMinorCurrency(activeRun.budget_ceiling_minor)}`
          : ""
      }`
    : null;

  const sandbox = activeData.agent_sandbox ?? null;
  const hasSandboxActivity = Boolean(
    sandbox &&
      (sandbox.fill_count > 0 ||
        sandbox.orders.length > 0 ||
        sandbox.positions.some((p) => Math.abs(p.quantity) > 0) ||
        activeRun?.duty_kind === "trading.agent_sandbox" ||
        activeRun?.duty_kind === "quant.agent_sandbox")
  );
  const showSandboxTab = Boolean(activeData.sandbox_authorized && hasSandboxActivity);
  const effectiveTab = activeTab === "sandbox" && !showSandboxTab ? "work" : activeTab;

  return (
    <Stack gap={16}>
      <Navbar
        variant="back"
        label="Back to Agents"
        onBack={() => navigate("/agents", { focusReturnId: profile.agent_id })}
      />

      <SpaBreadcrumb
        focusReturnId={profile.agent_id}
        items={[
          { id: "agents", label: "Agents", href: "/agents" },
          {
            id: profile.agent_id,
            label: profile.display_name,
            href: `/agents/${encodeURIComponent(profile.agent_id)}`,
          },
        ]}
      />

      <PageHeader
        leading={
          <AgentAvatar
            agent={normalizeAvatarVariant(profile.avatar_variant)}
            state={profile.avatar_state}
            size={44}
            label={profile.display_name}
          />
        }
        title={profile.display_name}
        detail={`${profile.role_label} · ${profile.execution_profile}`}
        action={
          <Inline gap={8} align="center" wrap>
            <Status
              tone={detailStatusTone(
                profile.archived,
                activeRun?.status,
                profile.onboarding_state
              )}
            >
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
                askCeo({
                  kind: "agent",
                  id: profile.agent_id,
                  label: `${profile.display_name} (${profile.role_label})`,
                })
              }
            >
              Ask CEO about {profile.display_name}
            </Button>
            {!profile.archived &&
            activeRun &&
            (activeRun.status === "running" || activeRun.status === "waiting") ? (
              <Button
                emphasis="soft"
                tone="danger"
                size="compact"
                shape="pill"
                disabled={!canInterruptRun}
                onClick={() => setStopConfirmOpen((v) => !v)}
              >
                Stop Current Task
              </Button>
            ) : null}
          </Inline>
        }
      />

      {!profile.archived &&
      activeRun &&
      (activeRun.status === "running" || activeRun.status === "waiting") &&
      taskBannerLabel ? (
        <Inline gap={8} align="center" wrap>
          <ThinkingIndicator
            active={activeRun.status === "running"}
            label={taskBannerLabel}
          />
          <Text variant="help">{taskBannerLabel}</Text>
        </Inline>
      ) : null}

      {!profile.archived && activeRun && meta ? (
        <ControlledInlineConfirm
          open={stopConfirmOpen}
          pending={
            (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
            outbox.intent?.command_type === "interrupt_run" &&
            outbox.intent?.preconditions?.run_id === activeRun.run_id
          }
          onCancel={() => setStopConfirmOpen(false)}
          decision={{
            id: `interrupt-${activeRun.run_id}`,
            revision: `${activeRun.run_id}:${activeRun.fencing_token}`,
            title: `Interrupt ${profile.display_name}'s Current Run`,
            consequence:
              "Cancels the active fenced work run immediately and marks its work item cancelled. Does not revoke the agent's standing grant or delete published reports.",
            confirmLabel: "Stop Current Task",
            destructive: true,
            available: canInterruptRun,
            requireReason: true,
            defaultReason: `Board interrupted ${activeRun.duty_kind} run`,
            whatChanges: [
              { label: "Active run", value: `${activeRun.duty_kind} transitions to cancelled` },
              {
                label: "Fencing token",
                value: `Token #${activeRun.fencing_token} invalidated`,
              },
            ],
            whatStaysUntouched: [
              { label: "Standing authority", value: "Grant and role remain intact" },
              {
                label: "Private Desk & reports",
                value: "Existing drafts and publications preserved",
              },
            ],
            technicalDetails: [
              { label: "command_type", value: "interrupt_run" },
              { label: "run_id", value: activeRun.run_id },
              { label: "expected_fencing_token", value: String(activeRun.fencing_token) },
            ],
          }}
          onConfirm={async ({ reason }) => {
            const lifecycle = await dispatchCommand({
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
            if (
              lifecycle === "applied" ||
              lifecycle === "accepted" ||
              lifecycle === "unknown"
            ) {
              setStopConfirmOpen(false);
            }
          }}
        />
      ) : null}

      <ContextualApprovalList questions={agentApprovals} />

      <AgentDeskAuthoritySection data={activeData} runtimeState={runtimeState} />

      <Tabs
        label="Agent individual activity"
        presentation="pill"
        value={effectiveTab}
        onValueChange={setActiveTab}
        items={[
          {
            id: "work",
            label: `Work Activity (${activeData.tabs.work_items.length + activeData.tabs.work_activity.length})`,
            panel: (
              <AgentWorkTab
                agentId={profile.agent_id}
                workItems={activeData.tabs.work_items}
                workActivity={activeData.tabs.work_activity}
                conditions={activeData.tabs.conditions}
              />
            ),
          },
          {
            id: "talks",
            label: `Interactions & Talks (${activeData.tabs.interactions_and_talks.length})`,
            panel: (
              <AgentTalksTab
                agentId={profile.agent_id}
                eventSeq={activeData.event_seq}
                talks={activeData.tabs.interactions_and_talks}
                displayNameFor={displayNameFor}
              />
            ),
          },
          {
            id: "reports",
            label: `Published Reports (${activeData.tabs.published_reports.length})`,
            panel: (
              <AgentReportsTab
                agentDisplayName={profile.display_name}
                publishedReports={activeData.tabs.published_reports}
                displayNameFor={displayNameFor}
              />
            ),
          },
          {
            id: "peers",
            label: `Peer Requests & Handoffs (${activeData.tabs.peer_requests.length + activeData.tabs.handoffs.length})`,
            panel: (
              <AgentHandoffsTab
                peerRequests={activeData.tabs.peer_requests}
                handoffs={activeData.tabs.handoffs}
              />
            ),
          },
          ...(showSandboxTab && sandbox
            ? [
                {
                  id: "sandbox",
                  label:
                    sandbox.fill_count > 0
                      ? `Trading Sandbox (${sandbox.fill_count} ${sandbox.fill_count === 1 ? "fill" : "fills"})`
                      : "Trading Sandbox",
                  panel: (
                    <AgentSandboxTab
                      agentDisplayName={profile.display_name}
                      eventSeq={activeData.event_seq}
                      sandbox={sandbox}
                    />
                  ),
                },
              ]
            : []),
        ]}
      />
    </Stack>
  );
}
