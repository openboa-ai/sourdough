import { Fragment, useEffect, useRef, useState } from "react";
import { ThinkingIndicator } from "@openboa/yogurt/motion-thinking-indicator";
import {
  AgentAvatar,
  Badge,
  Button,
  Card,
  DataTable,
  DisclosureRow,
  Divider,
  Inline,
  List,
  Message,
  Navbar,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  RoutineRow,
  RuntimePanel,
  Skeleton,
  Stack,
  Status,
  Surface,
  Tabs,
  Text,
  Transcript,
  WorkEvent,
  type DataColumn,
  type DataRow,
  type Fact,
  type RuntimeState,
} from "@openboa/yogurt";
import {
  ApiError,
  fetchProjection,
  formatMinorCurrency,
  formatNsClock,
  formatRelativeNs,
  newCommandId,
} from "../api/client";
import type { AgentDetailProjection } from "../api/types";
import {
  ContextualApprovalList,
  usePendingApprovals,
} from "../components/ContextualApprovalCard";
import { ControlledInlineConfirm } from "../components/ControlledInlineConfirm";
import { SpaBreadcrumb } from "../components/SpaBreadcrumb";
import { useApp, useEscapeBack } from "../state/store";

type WorkActivityItem = AgentDetailProjection["tabs"]["work_activity"][number];
type WorkItemEntry = AgentDetailProjection["tabs"]["work_items"][number];
type PeerRequestEntry = AgentDetailProjection["tabs"]["peer_requests"][number];

function normalizeToNs(epoch: number | null | undefined): number | null {
  if (epoch === null || epoch === undefined || !Number.isFinite(epoch) || epoch <= 0) {
    return null;
  }
  if (epoch < 1e12) return Math.round(epoch * 1_000_000_000);
  if (epoch < 1e15) return Math.round(epoch * 1_000_000);
  return epoch;
}

function formatTimestamp(epoch: number | null | undefined, emptyLabel: string): string {
  const ns = normalizeToNs(epoch);
  if (ns === null) return emptyLabel;
  return `${formatNsClock(ns)} (${formatRelativeNs(ns)})`;
}

function detailStatusTone(
  archived: boolean,
  activeRunStatus: string | undefined,
  onboardingState: string | null
): "neutral" | "brand" | "warning" | "danger" | "success" {
  if (archived) return "neutral";
  if (activeRunStatus === "running") return "brand";
  if (activeRunStatus === "waiting") return "warning";
  if (onboardingState === "blocked") return "danger";
  return "success";
}

function workEventKindForRun(status: string): "task" | "routine" | "review" {
  if (status === "completed") return "task";
  if (status === "running" || status === "queued" || status === "waiting") return "routine";
  return "review";
}

function workEventStateForRun(
  status: string
): "running" | "ready" | "waiting" | "failed" | "done" {
  if (status === "running") return "running";
  if (status === "completed") return "ready";
  if (status === "waiting" || status === "queued") return "waiting";
  if (status === "failed" || status === "cancelled" || status === "fenced") return "failed";
  return "done";
}

function workItemStateTone(
  state: string
): "brand" | "warning" | "success" | "danger" | "neutral" {
  if (state === "running") return "brand";
  if (state === "due" || state === "scheduled" || state === "waiting") return "warning";
  if (state === "settled") return "success";
  if (state === "blocked" || state === "cancelled") return "danger";
  return "neutral";
}

function workItemSummary(payload: Record<string, unknown>): string | null {
  for (const key of ["summary", "title", "reason", "task"]) {
    const val = payload[key];
    if (typeof val === "string" && val.trim().length > 0) {
      return val.trim();
    }
  }
  return null;
}

export function AgentDetailView({ agentId }: { agentId: string }) {
  const {
    navigate,
    refreshSeq,
    meta,
    canWrite,
    outbox,
    dispatchCommand,
    askCeo,
    reportApiError,
  } = useApp();
  const [data, setData] = useState<AgentDetailProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [activeTab, setActiveTab] = useState("work");
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false);
  const [cancelWorkTargetId, setCancelWorkTargetId] = useState<string | null>(null);
  const prevAgentIdRef = useRef(agentId);

  const activeData = data && data.profile.agent_id === agentId ? data : null;
  const isSwitchingAgent = prevAgentIdRef.current !== agentId;
  useEscapeBack("/agents", activeData ? activeData.profile.agent_id : agentId);

  const agentApprovals = usePendingApprovals((q) => {
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
  });

  useEffect(() => {
    if (prevAgentIdRef.current !== agentId) {
      prevAgentIdRef.current = agentId;
      setLoading(true);
      setData(null);
      setError(null);
      setErrorStatus(null);
      setStopConfirmOpen(false);
      setCancelWorkTargetId(null);
    }
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<AgentDetailProjection>(
          `agents/${encodeURIComponent(agentId)}`,
          { signal: controller.signal }
        );
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
        setErrorStatus(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setErrorStatus(err instanceof ApiError ? err.status : null);
        setError(err instanceof Error ? err.message : "Failed to load agent details");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [agentId, refreshSeq, retrySeq, reportApiError]);

  if ((loading || isSwitchingAgent) && !activeData) {
    return <Skeleton label="Loading agent desk and permissions" lines={6} />;
  }

  if (!activeData) {
    const isNotFound = errorStatus === 404 || error === null;
    return (
      <Result
        title={isNotFound ? "Agent not found" : "Agent details unavailable"}
        action={
          <Inline gap={8} wrap>
            {!isNotFound ? (
              <Button
                emphasis="soft"
                shape="pill"
                onClick={() => {
                  setLoading(true);
                  setError(null);
                  setErrorStatus(null);
                  setRetrySeq((n) => n + 1);
                }}
              >
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

  const agentNameById = new Map<string, string>();
  for (const talk of activeData.tabs.interactions_and_talks) {
    if (talk.sender_id && talk.sender_name) {
      agentNameById.set(talk.sender_id, talk.sender_name);
    }
  }
  for (const pr of activeData.tabs.peer_requests) {
    if (pr.requester_agent_id && pr.requester_name) {
      agentNameById.set(pr.requester_agent_id, pr.requester_name);
    }
    if (pr.recipient_agent_id && pr.recipient_name) {
      agentNameById.set(pr.recipient_agent_id, pr.recipient_name);
    }
  }
  for (const h of activeData.tabs.handoffs) {
    if (h.sender_id && h.sender_name) {
      agentNameById.set(h.sender_id, h.sender_name);
    }
    if (h.recipient_id && h.recipient_name) {
      agentNameById.set(h.recipient_id, h.recipient_name);
    }
  }
  if (meta?.founder_id) {
    agentNameById.set(meta.founder_id, "Founder · CEO");
  }
  agentNameById.set(profile.agent_id, profile.display_name);
  const displayNameFor = (id: string): string => agentNameById.get(id) ?? id;

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
  const canCancelWork =
    canWrite &&
    Boolean(meta?.supported_commands?.cancel_work?.includes("work-command-v1"));

  const deskFacts: Fact[] = [
    {
      id: "exec",
      label: "Execution Sandbox",
      value: activeData.desk_workspace.execution_profile,
    },
    {
      id: "manifest",
      label: "Desk Manifest Revision",
      value: (
        <Text className="sd-mono">
          {activeData.desk_workspace.desk_manifest_revision ?? "Unavailable"}
        </Text>
      ),
    },
    {
      id: "drafts",
      label: "Private Drafts",
      value: (
        <Badge tone="neutral" variant="soft" dot={false}>
          {activeData.desk_workspace.private_draft_count}{" "}
          {activeData.desk_workspace.private_draft_count === 1
            ? "private draft"
            : "private drafts"}
        </Badge>
      ),
    },
    {
      id: "pubs",
      label: "Published Reports",
      value: String(activeData.desk_workspace.published_count),
    },
    {
      id: "runs",
      label: "Recorded Work Runs",
      value: String(activeData.desk_workspace.work_run_count),
    },
    {
      id: "onboarding",
      label: "Onboarding",
      value: profile.onboarding_state
        ? `${profile.onboarding_state} (${profile.onboarding_attempts} ${profile.onboarding_attempts === 1 ? "attempt" : "attempts"})`
        : "Complete / Direct",
    },
  ];

  if (profile.onboarding_purpose) {
    deskFacts.push({
      id: "onboarding-purpose",
      label: "Onboarding Purpose",
      value: profile.onboarding_purpose,
    });
  }

  if (profile.archived) {
    deskFacts.push({
      id: "archive-reason",
      label: "Archive Reason",
      value: profile.archive_reason ?? "Archived (no reason recorded)",
    });
  }

  const deskFactsView = (
    <div className="sd-facts-wrap">
      <RecordFacts
        source="private_desk + shared_publication"
        asOf={`seq #${activeData.event_seq}`}
        facts={deskFacts}
      />
    </div>
  );

  const authorityFacts: Fact[] = [
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
      value: activeData.authority_budget.grant_id
        ? `${activeData.authority_budget.grant_scope || "None"} (${activeData.authority_budget.grant_id})`
        : activeData.authority_budget.grant_scope || "None",
    },
    {
      id: "budget",
      label: "Monthly Operating Budget",
      value:
        activeData.authority_budget.monthly_budget_minor > 0
          ? formatMinorCurrency(activeData.authority_budget.monthly_budget_minor)
          : "Company mandate governed",
    },
    {
      id: "turns",
      label: "Allocated Turns",
      value:
        activeData.authority_budget.allocated_turns === null
          ? profile.is_ceo
            ? "Founder / Unbounded"
            : "Unavailable"
          : `${activeData.authority_budget.allocated_turns} turns`,
    },
    {
      id: "hire",
      label: "Staffing Authority",
      value: activeData.authority_budget.can_hire
        ? `Can hire up to ${activeData.authority_budget.hiring_slots ?? 0} slots (${activeData.authority_budget.hiring_turns ?? 0} turns)`
        : "Individual contributor (no hiring authority)",
    },
  ];

  const peerById = new Map<string, PeerRequestEntry>(
    activeData.tabs.peer_requests.map((pr) => [pr.request_id, pr])
  );

  const peerCols: DataColumn[] = [
    {
      id: "dir",
      label: "Direction",
      kind: "role",
      sortValue: (row) => peerById.get(row.id)?.direction ?? "",
    },
    {
      id: "peer",
      label: "Counterparty",
      kind: "identity",
      sortValue: (row) => {
        const pr = peerById.get(row.id);
        if (!pr) return "";
        return pr.direction === "outgoing" ? pr.recipient_name : pr.requester_name;
      },
    },
    {
      id: "project",
      label: "Channel",
      kind: "text",
      sortValue: (row) => peerById.get(row.id)?.project_id ?? "",
    },
    {
      id: "question",
      label: "Question & Expected Decision",
      kind: "text",
      sortValue: (row) => peerById.get(row.id)?.question ?? "",
    },
    {
      id: "state",
      label: "State",
      kind: "role",
      sortValue: (row) => peerById.get(row.id)?.state ?? "",
    },
    {
      id: "created",
      label: "Created",
      kind: "numeric",
      align: "end",
      sortValue: (row) => peerById.get(row.id)?.created_at_ns ?? 0,
    },
  ];

  const peerRows: DataRow[] = activeData.tabs.peer_requests.map((pr) => ({
    id: pr.request_id,
    cells: {
      dir: (
        <Badge tone="neutral" variant="outline" dot={false}>
          {pr.direction}
        </Badge>
      ),
      peer: pr.direction === "outgoing" ? pr.recipient_name : pr.requester_name,
      project: pr.project_id ? (
        <Button
          emphasis="quiet"
          size="compact"
          shape="pill"
          onClick={() =>
            navigate(`/messages?channel=${encodeURIComponent(pr.project_id!)}`)
          }
        >
          #{pr.project_id}
        </Button>
      ) : (
        <Text variant="help">Direct</Text>
      ),
      question: (
        <Stack gap={4}>
          <Text variant="body">{pr.question}</Text>
          <Text variant="help">Expected: {pr.expected_decision}</Text>
          {pr.response ? (
            <DisclosureRow label="Response payload" initiallyOpen={false}>
              <Surface variant="surface" density="compact">
                <Text as="p" className="sd-mono sd-agent-pre">
                  {JSON.stringify(pr.response, null, 2)}
                </Text>
              </Surface>
            </DisclosureRow>
          ) : (
            <Text variant="help">Response: Pending</Text>
          )}
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
      created: (
        <Text variant="help" className="sd-tabular">
          {formatTimestamp(pr.created_at_ns, "Unavailable")}
        </Text>
      ),
    },
  }));

  const taskBannerLabel = activeRun
    ? `${profile.display_name} is ${activeRun.status === "running" ? "executing" : "waiting on"} ${activeRun.duty_kind} · Fence #${activeRun.fencing_token}${
        activeRun.budget_ceiling_minor > 0
          ? ` · Cap ${formatMinorCurrency(activeRun.budget_ceiling_minor)}`
          : ""
      }`
    : null;

  const renderWorkItemRow = (item: WorkItemEntry, idx: number) => {
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
              <Badge tone={workItemStateTone(item.state)} variant="soft" dot={false}>
                {item.state}
              </Badge>
              <Text className="sd-mono">{item.work_item_id}</Text>
            </Inline>
            <Inline gap={8} align="center" wrap>
              <Text variant="help" className="sd-tabular">
                Updated {formatTimestamp(item.updated_at_ns, "Unavailable")}
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
                  { label: "Work item", value: `${item.work_item_id} transitions to cancelled` },
                  { label: "Attached runs", value: "Queued/running/waiting runs cancelled" },
                ],
                whatStaysUntouched: [
                  { label: "Standing authority", value: "Grant and role remain intact" },
                  { label: "Private Desk & reports", value: "Existing drafts and publications preserved" },
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
  };

  const renderWorkRunRow = (run: WorkActivityItem, idx: number) => (
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
            } · Created ${formatTimestamp(run.created_at_ns, "Unavailable")}`,
            time: formatTimestamp(run.updated_at_ns, "Unavailable"),
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
  );

  const sandbox = activeData.agent_sandbox ?? null;
  const hasSandboxActivity = Boolean(
    sandbox &&
      (sandbox.fill_count > 0 ||
        sandbox.orders.length > 0 ||
        sandbox.positions.some((p) => Math.abs(p.quantity) > 0) ||
        activeRun?.duty_kind === "quant.agent_sandbox")
  );
  const showSandboxTab = Boolean(activeData.sandbox_authorized && hasSandboxActivity);
  const effectiveTab = activeTab === "sandbox" && !showSandboxTab ? "work" : activeTab;
  const sandboxCurrency = sandbox?.binding.currency || "USD";
  const sandboxFacts: Fact[] = sandbox
    ? [
        {
          id: "sbx-acct",
          label: "Sandbox Account",
          value: <Text className="sd-mono">{sandbox.binding.account_id}</Text>,
        },
        {
          id: "sbx-cash",
          label: "Virtual Cash",
          value: (
            <Text className="sd-tabular">
              {formatMinorCurrency(sandbox.cash_minor_units, sandboxCurrency)}
            </Text>
          ),
        },
        {
          id: "sbx-exp",
          label: "Gross Exposure / Cap",
          value: (
            <Text className="sd-tabular">
              {formatMinorCurrency(sandbox.gross_exposure_minor_units, sandboxCurrency)} /{" "}
              {formatMinorCurrency(sandbox.exposure_limit_minor_units, sandboxCurrency)}
            </Text>
          ),
        },
        {
          id: "sbx-pnl",
          label: "Realized P&L",
          value: (
            <Text
              className={`sd-tabular ${
                sandbox.realized_pnl_minor_units > 0
                  ? "sd-tone-pos"
                  : sandbox.realized_pnl_minor_units < 0
                    ? "sd-tone-neg"
                    : ""
              }`}
            >
              {formatMinorCurrency(sandbox.realized_pnl_minor_units, sandboxCurrency, {
                signed: true,
              })}
            </Text>
          ),
        },
        {
          id: "sbx-fees",
          label: "Simulated Fees & Slippage",
          value: (
            <Text className="sd-tabular">
              {formatMinorCurrency(sandbox.total_fees_minor_units, sandboxCurrency)} (
              {sandbox.binding.fee_bps} bps fee · {sandbox.binding.slippage_bps} bps slip)
            </Text>
          ),
        },
      ]
    : [];

  const sandboxPosCols: DataColumn[] = [
    { id: "instrument", label: "Instrument", kind: "identity" },
    { id: "qty", label: "Quantity", kind: "numeric", align: "end" },
    { id: "entry", label: "Avg Entry", kind: "numeric", align: "end" },
    { id: "pnl", label: "Realized P&L", kind: "numeric", align: "end" },
    { id: "fees", label: "Fees", kind: "numeric", align: "end" },
  ];

  const sandboxPosRows: DataRow[] = (sandbox?.positions ?? []).map((p) => ({
    id: p.instrument_id,
    cells: {
      instrument: <Text variant="body">{p.instrument_id}</Text>,
      qty: <Text className="sd-tabular">{p.quantity}</Text>,
      entry: (
        <Text className="sd-tabular">
          {formatMinorCurrency(p.avg_entry_minor_units, sandboxCurrency)}
        </Text>
      ),
      pnl: (
        <Text className="sd-tabular">
          {formatMinorCurrency(p.realized_pnl_minor_units, sandboxCurrency, { signed: true })}
        </Text>
      ),
      fees: (
        <Text className="sd-tabular">
          {formatMinorCurrency(p.total_fees_minor_units, sandboxCurrency)}
        </Text>
      ),
    },
  }));

  const sandboxOrderCols: DataColumn[] = [
    { id: "order", label: "Order & Instrument", kind: "identity" },
    { id: "side", label: "Side / Qty", kind: "numeric", align: "end" },
    { id: "price", label: "Fill / Limit", kind: "numeric", align: "end" },
    { id: "fee", label: "Fee", kind: "numeric", align: "end" },
    { id: "status", label: "Status", kind: "role" },
  ];

  const sandboxOrderRows: DataRow[] = (sandbox?.orders ?? []).map((o) => ({
    id: o.order_id,
    cells: {
      order: (
        <Stack gap={2}>
          <Text variant="body">{o.instrument_id}</Text>
          <Text variant="meta" className="sd-mono">
            {o.order_id}
          </Text>
        </Stack>
      ),
      side: (
        <Text className="sd-tabular">
          {o.side.toUpperCase()} {o.filled_quantity}/{o.quantity}
        </Text>
      ),
      price: (
        <Text className="sd-tabular">
          {formatMinorCurrency(o.fill_price_minor_units ?? o.limit_minor_units, sandboxCurrency)}
        </Text>
      ),
      fee: (
        <Text className="sd-tabular">
          {formatMinorCurrency(o.fee_minor_units, sandboxCurrency)}
        </Text>
      ),
      status: (
        <Badge
          tone={o.status === "filled" ? "success" : o.status === "open" ? "brand" : "neutral"}
          dot={false}
        >
          {o.status}
        </Badge>
      ),
    },
  }));

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

      <div className="sd-split-equal">
        {profile.archived ? (
          <Stack gap={12}>
            <RuntimePanel
              state="offline"
              title="Desk & Isolated Harness"
              detail={activeData.desk_workspace.private_draft_notice}
            />
            <Card
              variant="raised"
              title="Archived Desk & Workspace Summary"
              description={profile.archive_reason ?? "Archived (no reason recorded)"}
            >
              {deskFactsView}
            </Card>
          </Stack>
        ) : (
          <RuntimePanel
            state={runtimeState}
            title="Desk & Isolated Harness"
            detail={activeData.desk_workspace.private_draft_notice}
            preview={deskFactsView}
          />
        )}

        <Card
          title="Authority, Permissions & Budget"
          description="Scoped standing grants and hard operating ceilings"
          variant="raised"
        >
          <Stack gap={12}>
            <div className="sd-facts-wrap">
              <RecordFacts
                source="standing_grant + staffing_authority"
                asOf={`seq #${activeData.event_seq}`}
                facts={authorityFacts}
              />
            </div>
            <Stack gap={4}>
              <Text variant="meta">Hard Prohibitions</Text>
              {activeData.authority_budget.prohibitions.length === 0 ? (
                <Text variant="help">No additional prohibitions recorded.</Text>
              ) : (
                activeData.authority_budget.prohibitions.map((rule, idx) => (
                  <Text key={idx} variant="help">
                    · {rule}
                  </Text>
                ))
              )}
            </Stack>
          </Stack>
        </Card>
      </div>

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
              <Stack gap={12}>
                <Card title="Work Items" variant="raised">
                  {activeData.tabs.work_items.length === 0 ? (
                    <Text variant="help">No active or queued work items for this agent.</Text>
                  ) : (
                    <Stack gap={12}>
                      {activeData.tabs.work_items.map((item, idx) =>
                        renderWorkItemRow(item, idx)
                      )}
                    </Stack>
                  )}
                </Card>

                <Card title="Recent Work Runs" variant="raised">
                  {activeData.tabs.work_activity.length === 0 ? (
                    <Text variant="help">No work runs recorded for this agent yet.</Text>
                  ) : (
                    <Stack gap={12}>
                      {activeData.tabs.work_activity.map((run, idx) =>
                        renderWorkRunRow(run, idx)
                      )}
                    </Stack>
                  )}
                </Card>

                <Card title="Armed Wake Conditions" variant="raised">
                  {activeData.tabs.conditions.length === 0 ? (
                    <Text variant="help">No wake conditions armed.</Text>
                  ) : (
                    <List label="Agent wake conditions" divided>
                      {activeData.tabs.conditions.map((c) => (
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
                                  : formatTimestamp(c.due_at_ns, "Not scheduled"),
                            }}
                          />
                        </li>
                      ))}
                    </List>
                  )}
                </Card>
              </Stack>
            ),
          },
          {
            id: "talks",
            label: `Interactions & Talks (${activeData.tabs.interactions_and_talks.length})`,
            panel: (
              <Stack gap={12}>
                <Notice title="Discussion only · Read-only Board observation" tone="neutral">
                  Internal agent conversations do not assign or accept work on their own. To give
                  direction, send an advisory message to Founder · CEO in Company Messages.
                </Notice>
                <Card variant="raised">
                  {activeData.tabs.interactions_and_talks.length === 0 ? (
                    <Text variant="help">No project messages involving this agent yet.</Text>
                  ) : (
                    <div className="sd-agent-talks">
                      <Transcript
                        label="Agent internal discussions"
                        revision={activeData.event_seq}
                      >
                        {activeData.tabs.interactions_and_talks.map((msg) => (
                          <Message
                            key={msg.message_id}
                            speaker={`${msg.sender_name} (${msg.sender_role})`}
                            variant={msg.sender_id === profile.agent_id ? "sent" : "received"}
                          >
                            <Stack gap={6}>
                              <Text as="p" variant="body">
                                {msg.body}
                              </Text>
                              <Inline gap={8} align="center" wrap>
                                <Button
                                  emphasis="quiet"
                                  size="compact"
                                  shape="pill"
                                  onClick={() =>
                                    navigate(
                                      `/messages?channel=${encodeURIComponent(msg.project_id)}`
                                    )
                                  }
                                >
                                  #{msg.project_id}
                                </Button>
                                {msg.audience.length > 0 ? (
                                  <Inline gap={6} align="center" wrap>
                                    <Text variant="meta">Audience:</Text>
                                    {msg.audience.map((aud) => (
                                      <Badge
                                        key={aud}
                                        tone="neutral"
                                        variant="outline"
                                        dot={false}
                                      >
                                        {displayNameFor(aud)}
                                      </Badge>
                                    ))}
                                  </Inline>
                                ) : null}
                                {msg.asset_id ? (
                                  <Button
                                    emphasis="quiet"
                                    size="compact"
                                    shape="pill"
                                    onClick={() => navigate("/company/org")}
                                  >
                                    Asset {msg.asset_id}
                                  </Button>
                                ) : null}
                              </Inline>
                            </Stack>
                          </Message>
                        ))}
                      </Transcript>
                    </div>
                  )}
                </Card>
              </Stack>
            ),
          },
          {
            id: "reports",
            label: `Published Reports (${activeData.tabs.published_reports.length})`,
            panel: (
              <Stack gap={12}>
                {activeData.tabs.published_reports.length === 0 ? (
                  <Card variant="raised">
                    <Text variant="help">
                      No shared company reports published by this agent yet. Unpublished work stays
                      private on their Desk.
                    </Text>
                  </Card>
                ) : (
                  <Card title="Selective Company Publications" variant="raised">
                    <Stack gap={12}>
                      {activeData.tabs.published_reports.map((pub, idx) => {
                        const excerpt =
                          pub.content.length > 200
                            ? `${pub.content.slice(0, 198)}…`
                            : pub.content;
                        return (
                          <Fragment key={pub.asset_id}>
                            {idx > 0 ? <Divider /> : null}
                            <Stack gap={8}>
                              <Inline justify="between" align="center" gap={8} wrap>
                                <Inline gap={8} align="center" wrap>
                                  <Text variant="body" className="sd-mono">
                                    {pub.asset_id}
                                  </Text>
                                  <Button
                                    emphasis="quiet"
                                    size="compact"
                                    shape="pill"
                                    onClick={() =>
                                      navigate(
                                        `/messages?channel=${encodeURIComponent(pub.project_id)}`
                                      )
                                    }
                                  >
                                    #{pub.project_id}
                                  </Button>
                                  <Badge tone="neutral" variant="soft" dot={false}>
                                    Work item {pub.work_item_id}
                                  </Badge>
                                </Inline>
                                <Inline gap={6} align="center" wrap>
                                  {pub.audience.map((aud) => (
                                    <Badge
                                      key={aud}
                                      tone="neutral"
                                      variant="outline"
                                      dot={false}
                                    >
                                      {displayNameFor(aud)}
                                    </Badge>
                                  ))}
                                </Inline>
                              </Inline>

                              <Text as="p" variant="help">
                                {excerpt}
                              </Text>

                              {pub.content.length > 200 ? (
                                <DisclosureRow label="Read full report" initiallyOpen={false}>
                                  <Surface variant="surface" density="compact">
                                    <Text as="p" variant="body" className="sd-m-0">
                                      {pub.content}
                                    </Text>
                                  </Surface>
                                </DisclosureRow>
                              ) : null}

                              <Inline gap={8} wrap>
                                <Button
                                  emphasis="soft"
                                  size="compact"
                                  shape="pill"
                                  onClick={() =>
                                    askCeo({
                                      kind: "shared_publication",
                                      id: pub.asset_id,
                                      label: `Asset ${pub.asset_id} by ${profile.display_name}`,
                                    })
                                  }
                                >
                                  Ask CEO about this report
                                </Button>
                                <Button
                                  emphasis="quiet"
                                  size="compact"
                                  shape="pill"
                                  onClick={() => navigate("/company/org")}
                                >
                                  View in Org
                                </Button>
                              </Inline>
                            </Stack>
                          </Fragment>
                        );
                      })}
                    </Stack>
                  </Card>
                )}
              </Stack>
            ),
          },
          {
            id: "peers",
            label: `Peer Requests & Handoffs (${activeData.tabs.peer_requests.length + activeData.tabs.handoffs.length})`,
            panel: (
              <Stack gap={12}>
                <Card title="Blocking Peer Requests" variant="raised" className="sd-ledger-card">
                  {peerRows.length === 0 ? (
                    <Text variant="help">No peer requests recorded.</Text>
                  ) : (
                    <DataTable caption="Peer requests" columns={peerCols} rows={peerRows} />
                  )}
                </Card>
                <Card title="Work Handoff Offers" variant="raised">
                  {activeData.tabs.handoffs.length === 0 ? (
                    <Text variant="help">No work handoffs recorded.</Text>
                  ) : (
                    <List label="Work handoff offers" divided>
                      {activeData.tabs.handoffs.map((h) => (
                        <li key={h.offer_id} className="yg-list-item">
                          <WorkEvent
                            event={{
                              id: h.offer_id,
                              kind: "handoff",
                              title: `${h.sender_name} → ${h.recipient_name}`,
                              detail: h.expected_result,
                              state: h.accepted ? "ready" : "waiting",
                            }}
                            inlineMeta={
                              <Inline gap={6} align="center" wrap>
                                <Badge tone="neutral" variant="outline" dot={false}>
                                  {h.direction}
                                </Badge>
                                <Button
                                  emphasis="quiet"
                                  size="compact"
                                  shape="pill"
                                  onClick={() => navigate("/company/org")}
                                >
                                  Asset {h.asset_id}
                                </Button>
                              </Inline>
                            }
                            actions={
                              <Button
                                emphasis="soft"
                                size="compact"
                                shape="pill"
                                onClick={() =>
                                  askCeo({
                                    kind: "shared_publication",
                                    id: h.asset_id,
                                    label: `Handoff ${h.offer_id} (${h.asset_id})`,
                                  })
                                }
                              >
                                Ask CEO
                              </Button>
                            }
                          />
                        </li>
                      ))}
                    </List>
                  )}
                </Card>
              </Stack>
            ),
          },
          ...(showSandboxTab && sandbox
            ? [
                {
                  id: "sandbox",
                  label:
                    sandbox.fill_count > 0
                      ? `Paper Sandbox (${sandbox.fill_count} ${sandbox.fill_count === 1 ? "fill" : "fills"})`
                      : "Paper Sandbox",
                  panel: (
                    <Stack gap={12}>
                      <Notice title="Isolated personal paper sandbox" tone="neutral">
                        This virtual account belongs exclusively to {profile.display_name} for
                        strategy testing. Orders here never touch shared company paper books or
                        live venues.
                      </Notice>
                      <Stack gap={12}>
                        <Card
                          title="Sandbox Portfolio Summary"
                          description={`Binding ${sandbox.binding.binding_id} · Quote source ${sandbox.binding.quote_source}`}
                          variant="raised"
                          headerAction={
                            <Button
                              emphasis="quiet"
                              size="compact"
                              shape="pill"
                              onClick={() => navigate("/trading/paper")}
                            >
                              Paper Trading
                            </Button>
                          }
                        >
                          <div className="sd-facts-wrap">
                            <RecordFacts
                              source="agent_sandbox"
                              asOf={`seq #${activeData.event_seq}`}
                              facts={sandboxFacts}
                            />
                          </div>
                        </Card>
                        <div className="sd-split-equal">
                          <Card title="Sandbox Positions" variant="raised">
                            {sandboxPosRows.length === 0 ? (
                              <Text variant="help">No sandbox positions recorded yet.</Text>
                            ) : (
                              <DataTable
                                caption="Agent personal paper sandbox positions"
                                columns={sandboxPosCols}
                                rows={sandboxPosRows}
                              />
                            )}
                          </Card>
                          <Card title="Recent Sandbox Orders" variant="raised">
                            {sandboxOrderRows.length === 0 ? (
                              <Text variant="help">No sandbox orders recorded yet.</Text>
                            ) : (
                              <DataTable
                                caption="Agent personal paper sandbox orders"
                                columns={sandboxOrderCols}
                                rows={sandboxOrderRows}
                              />
                            )}
                          </Card>
                        </div>
                      </Stack>
                    </Stack>
                  ),
                },
              ]
            : []),
        ]}
      />
    </Stack>
  );
}
