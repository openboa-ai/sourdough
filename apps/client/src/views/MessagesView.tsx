import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ThinkingIndicator } from "@openboa/yogurt/motion-thinking-indicator";
import {
  AgentAvatar,
  AgentRow,
  Badge,
  Button,
  Card,
  Composer,
  Divider,
  EmptyResult,
  FilterBar,
  Heading,
  Inline,
  Link,
  List,
  Message,
  Notice,
  PageHeader,
  ResourceRow,
  Result,
  Skeleton,
  Stack,
  Surface,
  Text,
  ThreadRow,
  Transcript,
  type Tone,
} from "@openboa/yogurt";
import { normalizeAvatarVariant } from "../api/avatar";
import {
  fetchProjection,
  formatMinorCurrency,
  formatNsClock,
  formatRelativeNs,
  newCommandId,
} from "../api/client";
import type {
  AgentsGraphProjection,
  ApprovalsProjection,
  ContextRef,
  FinanceProjection,
  MessagesProjection,
  OrderTraceProjection,
  OrgProjection,
  RestrictionsProjection,
  StrategiesProjection,
  SystemAuditProjection,
} from "../api/types";
import { useApp } from "../state/store";

interface ContextResolution {
  statusLabel: string;
  statusTone: Tone;
  detail: string;
  resolvedAgentId?: string | null;
}

function formatMessageCount(count: number): string {
  return `${count} ${count === 1 ? "message" : "messages"}`;
}

function formatMemberCount(count: number): string {
  return `${count} ${count === 1 ? "member" : "members"}`;
}

function formatContextKind(kind: string): string {
  return kind.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function formatDeliveryState(state: string): string {
  switch (state) {
    case "recorded":
      return "Recorded";
    case "sent":
      return "Sent";
    case "delivered":
      return "Delivered";
    case "read":
    case "read_by_ceo":
      return "Read by CEO";
    default:
      return state;
  }
}

function resolveContextHref(
  kind: string,
  id: string,
  resolvedAgentId?: string | null
): string | null {
  switch (kind) {
    case "trading_order":
      return `/orders/${encodeURIComponent(id)}`;
    case "decision_question":
      return `/approvals/${encodeURIComponent(id)}`;
    case "agent":
      return `/agents/${encodeURIComponent(id)}`;
    case "shared_publication":
    case "company":
      return "/reports";
    case "strategy_release":
    case "challenger_trial":
    case "strategy_trial":
      return "/trading/strategies";
    case "restriction":
    case "restriction_cause":
      return "/controls";
    case "work_run":
      return resolvedAgentId
        ? `/agents/${encodeURIComponent(resolvedAgentId)}`
        : "/audit";
    case "account_balance":
      return "/finance";
    case "audit_event":
      return "/audit";
    default:
      return null;
  }
}

function statusToneForOrder(status: string): Tone {
  if (status === "filled") return "success";
  if (status.startsWith("cancelled") || status === "rejected" || status === "denied") {
    return "warning";
  }
  return "brand";
}

function statusToneForApproval(state: string): Tone {
  if (state === "applied") return "success";
  if (state === "declined") return "warning";
  if (state === "expired") return "danger";
  return "brand";
}

async function resolveLiveContextState(
  ctx: ContextRef,
  signal?: AbortSignal
): Promise<ContextResolution> {
  const fallback: ContextResolution = {
    statusLabel: "Advisory ref",
    statusTone: "neutral",
    detail: "Advisory context only · No actions execute from chat",
  };

  if (ctx.kind === "trading_order") {
    const res = await fetchProjection<OrderTraceProjection>(
      `orders/${encodeURIComponent(ctx.id)}`,
      { signal }
    );
    const order = res.data?.order;
    if (!order) return fallback;
    return {
      statusLabel: order.status,
      statusTone: statusToneForOrder(order.status),
      detail: `${order.side.toUpperCase()} ${order.instrument_id} · ${order.filled_qty}/${order.ordered_qty} filled · ${order.agent_name}`,
    };
  }

  if (ctx.kind === "agent") {
    const res = await fetchProjection<AgentsGraphProjection>("agents-graph", { signal });
    const node = res.data?.nodes.find((n) => n.agent_id === ctx.id);
    if (!node) return fallback;
    const tone: Tone = node.archived
      ? "neutral"
      : node.live_status === "working" || node.live_status === "running"
        ? "brand"
        : "success";
    return {
      statusLabel: node.archived ? "archived" : node.live_status,
      statusTone: tone,
      detail: `${node.display_name} · ${node.role_label}${node.current_task ? ` · ${node.current_task}` : ""}`,
    };
  }

  if (ctx.kind === "decision_question") {
    const res = await fetchProjection<ApprovalsProjection>("approvals", { signal });
    const item =
      res.data?.pending.find((q) => q.question_id === ctx.id) ??
      res.data?.history.find((q) => q.question_id === ctx.id);
    if (!item) return fallback;
    return {
      statusLabel: item.state,
      statusTone: statusToneForApproval(item.state),
      detail: item.summary,
    };
  }

  if (ctx.kind === "shared_publication") {
    const res = await fetchProjection<OrgProjection>("org", { signal });
    const pub = res.data?.publications.find((p) => p.asset_id === ctx.id);
    if (!pub) return fallback;
    return {
      statusLabel: "Published",
      statusTone: "success",
      detail: `Author: ${pub.author_name} (${pub.author_role}) · Project #${pub.project_id}`,
    };
  }

  if (ctx.kind === "strategy_release") {
    const res = await fetchProjection<StrategiesProjection>("strategies", { signal });
    const rel = res.data?.releases.find((r) => r.release_id === ctx.id);
    if (!rel) return fallback;
    const isActive = res.data?.active_deployment?.release_id === ctx.id;
    return {
      statusLabel: isActive ? "Active" : "Registered",
      statusTone: isActive ? "brand" : "neutral",
      detail: `Owner: ${rel.owner_name} · Trials: ${rel.research_trial_count_n}`,
    };
  }

  if (ctx.kind === "challenger_trial" || ctx.kind === "strategy_trial") {
    const res = await fetchProjection<StrategiesProjection>("strategies", { signal });
    const trial = res.data?.trials.find((t) => t.comparison_id === ctx.id);
    if (!trial) return fallback;
    const label = trial.verdict ?? trial.status;
    const tone: Tone =
      trial.verdict === "PROMOTE"
        ? "success"
        : trial.invalidation_reason
          ? "warning"
          : "brand";
    return {
      statusLabel: label,
      statusTone: tone,
      detail: `${trial.challenger_release_id} vs ${trial.incumbent_release_id} · ${trial.status}`,
    };
  }

  if (ctx.kind === "restriction" || ctx.kind === "restriction_cause") {
    const res = await fetchProjection<RestrictionsProjection>("restrictions", { signal });
    const item =
      res.data?.active_causes.find((r) => r.restriction_id === ctx.id) ??
      res.data?.history.find((r) => r.restriction_id === ctx.id);
    if (!item) return fallback;
    return {
      statusLabel: item.active ? "Active" : "Cleared",
      statusTone: item.active ? "warning" : "success",
      detail: item.reason,
    };
  }

  if (ctx.kind === "work_run") {
    const res = await fetchProjection<SystemAuditProjection>("system-audit", { signal });
    const fence = res.data?.active_fences.find((f) => f.run_id === ctx.id);
    if (!fence) return fallback;
    return {
      statusLabel: fence.status,
      statusTone: fence.status === "running" ? "brand" : "neutral",
      detail: `Agent ${fence.agent_id} · ${fence.duty_kind} · Fence #${fence.fencing_token}`,
      resolvedAgentId: fence.agent_id,
    };
  }

  if (ctx.kind === "account_balance") {
    const res = await fetchProjection<FinanceProjection>("finance", { signal });
    const bal = res.data?.account_balances.find((a) => a.account === ctx.id);
    if (!bal) return fallback;
    return {
      statusLabel: "Tracked",
      statusTone: "neutral",
      detail: `Account ${bal.account} · Balance ${formatMinorCurrency(bal.balance_minor)}`,
    };
  }

  if (ctx.kind === "audit_event") {
    const res = await fetchProjection<SystemAuditProjection>("system-audit", { signal });
    const ev = res.data?.events.find(
      (e) => String(e.event_seq) === ctx.id || e.entity_id === ctx.id
    );
    if (!ev) return fallback;
    return {
      statusLabel: ev.event_type,
      statusTone: "neutral",
      detail: `${ev.domain} · ${ev.entity_type} ${ev.entity_id} (seq ${ev.event_seq})`,
    };
  }

  return fallback;
}

export function MessagesView() {
  const {
    routePathname,
    routeSearchParams,
    navigate,
    refreshSeq,
    meta,
    linkState,
    hostLocked,
    canWrite,
    outbox,
    pendingAskContext,
    setPendingAskContext,
    askCeo,
    unreadCeoReply,
    markMessagesVisited,
    dispatchCommand,
    reportApiError,
  } = useApp();

  const [data, setData] = useState<MessagesProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [selectedChannel, setSelectedChannel] = useState<string>(
    () => routeSearchParams.get("channel") || "ceo-direct"
  );
  const [channelQuery, setChannelQuery] = useState("");
  const [draft, setDraft] = useState("");
  const [unreadChannelIds, setUnreadChannelIds] = useState<Set<string>>(() => new Set());
  const [liveChannelActivityNs, setLiveChannelActivityNs] = useState<Record<string, number>>({});
  const [freshMessageIds, setFreshMessageIds] = useState<Set<string>>(() => new Set());
  const [ctxState, setCtxState] = useState<ContextResolution>({
    statusLabel: "Advisory ref",
    statusTone: "neutral",
    detail: "Advisory context only · No actions execute from chat",
  });

  const selectedChannelRef = useRef(selectedChannel);
  selectedChannelRef.current = selectedChannel;
  const prevChannelCountsRef = useRef<Map<string, number> | null>(null);
  const seenMessageIdsRef = useRef<Set<string> | null>(null);
  const freshTimerRef = useRef<number | null>(null);
  const prevRouteSearchRef = useRef(routeSearchParams.toString());

  useEffect(() => {
    const urlChannel = routeSearchParams.get("channel") || "ceo-direct";
    setSelectedChannel(urlChannel);
  }, [routeSearchParams]);

  useEffect(() => {
    const prevSearch = prevRouteSearchRef.current;
    const currSearch = routeSearchParams.toString();
    prevRouteSearchRef.current = currSearch;
    if (prevSearch === currSearch) return;
    const prevParams = new URLSearchParams(prevSearch);
    if (
      prevParams.get("context_kind") &&
      prevParams.get("context_id") &&
      currSearch === "" &&
      pendingAskContext !== null
    ) {
      setPendingAskContext(null);
    }
  }, [routeSearchParams, pendingAskContext, setPendingAskContext]);

  useEffect(() => {
    return () => {
      if (freshTimerRef.current !== null) {
        window.clearTimeout(freshTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<MessagesProjection>("messages", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) {
          const nextData = res.data;
          setData(nextData);

          const nextCounts = new Map<string, number>();
          const bumpedChannels: string[] = [];
          for (const ch of nextData.internal_channels) {
            nextCounts.set(ch.channel_id, ch.message_count);
            if (prevChannelCountsRef.current !== null) {
              const prevCount = prevChannelCountsRef.current.get(ch.channel_id) ?? 0;
              if (ch.message_count > prevCount) {
                bumpedChannels.push(ch.channel_id);
              }
            }
          }
          if (bumpedChannels.length > 0) {
            setLiveChannelActivityNs((prev) => {
              const updated = { ...prev };
              for (const id of bumpedChannels) {
                updated[id] = nextData.as_of_ns;
              }
              return updated;
            });
            setUnreadChannelIds((prev) => {
              const next = new Set(prev);
              for (const id of bumpedChannels) {
                if (id !== selectedChannelRef.current) {
                  next.add(id);
                }
              }
              return next;
            });
          }
          prevChannelCountsRef.current = nextCounts;

          const allMsgKeys = new Set<string>();
          for (const m of nextData.ceo_thread.messages) {
            allMsgKeys.add(`ceo:${m.message_id}`);
          }
          for (const ch of nextData.internal_channels) {
            for (const m of ch.messages) {
              allMsgKeys.add(`${ch.channel_id}:${m.message_id}`);
            }
          }
          if (seenMessageIdsRef.current !== null) {
            const newlyArrived = new Set<string>();
            for (const key of allMsgKeys) {
              if (!seenMessageIdsRef.current.has(key)) {
                newlyArrived.add(key);
              }
            }
            if (newlyArrived.size > 0) {
              setFreshMessageIds(newlyArrived);
              if (freshTimerRef.current !== null) {
                window.clearTimeout(freshTimerRef.current);
              }
              freshTimerRef.current = window.setTimeout(() => {
                setFreshMessageIds(new Set());
              }, 380);
            }
          }
          seenMessageIdsRef.current = allMsgKeys;
        }
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load message channels.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  const activeInternal = useMemo(
    () => data?.internal_channels.find((ch) => ch.channel_id === selectedChannel),
    [data, selectedChannel]
  );
  const isCeoThread = Boolean(data && !activeInternal);

  useEffect(() => {
    if (data && isCeoThread) {
      markMessagesVisited(data.event_seq);
    }
  }, [data, isCeoThread, markMessagesVisited]);

  useEffect(() => {
    if (!pendingAskContext || pendingAskContext.kind !== "company") return;
    if (!meta) {
      setCtxState({
        statusLabel: "Advisory ref",
        statusTone: "neutral",
        detail: "Advisory context only · No actions execute from chat",
        resolvedAgentId: null,
      });
      return;
    }
    setCtxState({
      statusLabel: meta.active_restrictions_count > 0 ? "Restricted" : "Operational",
      statusTone: meta.active_restrictions_count > 0 ? "warning" : "success",
      detail: `Company ${meta.company_id} · Charter ${meta.charter_revision} · ${meta.pending_approvals_count} pending approvals`,
      resolvedAgentId: null,
    });
  }, [pendingAskContext, meta]);

  useEffect(() => {
    if (!pendingAskContext || pendingAskContext.kind === "company") return;
    const controller = new AbortController();
    let active = true;
    setCtxState({
      statusLabel: "Checking",
      statusTone: "neutral",
      detail: "Advisory context only · No actions execute from chat",
      resolvedAgentId: null,
    });
    void (async () => {
      try {
        const resolved = await resolveLiveContextState(pendingAskContext, controller.signal);
        if (active && !controller.signal.aborted) {
          setCtxState(resolved);
        }
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setCtxState({
          statusLabel: "Advisory ref",
          statusTone: "neutral",
          detail: "Live status unavailable; advisory reference preserved.",
          resolvedAgentId: null,
        });
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [pendingAskContext, refreshSeq, reportApiError]);

  const selectChannel = useCallback(
    (channelId: string) => {
      setSelectedChannel(channelId);
      setUnreadChannelIds((prev) => {
        if (!prev.has(channelId)) return prev;
        const next = new Set(prev);
        next.delete(channelId);
        return next;
      });
      const params = new URLSearchParams(routeSearchParams);
      params.set("channel", channelId);
      const qs = params.toString();
      navigate(qs ? `${routePathname}?${qs}` : routePathname);
    },
    [navigate, routePathname, routeSearchParams]
  );

  const filteredChannels = useMemo(() => {
    if (!data) return [];
    const q = channelQuery.trim().toLowerCase();
    if (!q) return data.internal_channels;
    return data.internal_channels.filter((ch) => {
      if (ch.name.toLowerCase().includes(q) || ch.channel_id.toLowerCase().includes(q)) {
        return true;
      }
      if (
        ch.members.some(
          (m) =>
            m.display_name.toLowerCase().includes(q) ||
            m.role_label.toLowerCase().includes(q)
        )
      ) {
        return true;
      }
      const lastMsg = ch.messages[ch.messages.length - 1];
      return Boolean(
        lastMsg &&
          (lastMsg.body.toLowerCase().includes(q) ||
            lastMsg.sender_name.toLowerCase().includes(q))
      );
    });
  }, [data, channelQuery]);

  if (loading && !data) {
    return <Skeleton label="Loading company messages" lines={6} />;
  }

  if (!data) {
    return (
      <Result
        title="Company Messages unavailable"
        action={
          <Button
            emphasis="soft"
            shape="pill"
            onClick={() => {
              setLoading(true);
              setError(null);
              setRetrySeq((n) => n + 1);
            }}
          >
            Retry
          </Button>
        }
      >
        {error ?? "Could not load message channels."}
      </Result>
    );
  }

  const ceoMsgs = data.ceo_thread.messages;
  const lastCeoMsg = ceoMsgs[ceoMsgs.length - 1];
  const ceoDisplayName = data.ceo_thread.ceo_display_name || "Founder · CEO";
  const sendingCeo =
    outbox.intent?.command_type === "send_ceo_message" &&
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted");
  const hasBoardControl =
    data.ceo_thread.writable &&
    Boolean(data.ceo_thread.ceo_agent_id) &&
    meta?.board_access.role === "board_control" &&
    !meta?.board_access.bootstrap_locked;
  const canRenderComposer =
    hasBoardControl &&
    linkState === "live" &&
    !hostLocked &&
    (canWrite || sendingCeo);

  const handleSendCeo = async ({ text }: { text: string }) => {
    const body = text.trim();
    if (!body || !meta || !data.ceo_thread.ceo_agent_id) return;
    const savedDraft = draft;
    const ctx = pendingAskContext;
    setDraft("");
    const lifecycle = await dispatchCommand({
      company_id: meta.company_id,
      command_id: newCommandId("ceo-msg"),
      command_type: "send_ceo_message",
      contract_version: "ceo-chat-command-v1",
      preconditions: {
        ceo_agent_id: data.ceo_thread.ceo_agent_id,
        charter_revision: data.charter_revision,
      },
      payload: {
        target_agent_id: data.ceo_thread.ceo_agent_id,
        body,
        ...(ctx ? { context_ref: ctx } : {}),
      },
      label: `Advisory message to ${ceoDisplayName}`,
      sent_ns: Date.now() * 1_000_000,
    });
    if (
      lifecycle === "applied" ||
      lifecycle === "accepted" ||
      lifecycle === "unknown"
    ) {
      setPendingAskContext(null);
    } else {
      setDraft((prev) => (prev ? prev : savedDraft));
    }
  };

  const pendingSourceHref = pendingAskContext
    ? resolveContextHref(
        pendingAskContext.kind,
        pendingAskContext.id,
        ctxState.resolvedAgentId
      )
    : null;

  return (
    <Stack gap={16}>
      <PageHeader
        title="Company Messages"
        detail="Direct advisory channel with the CEO and observable internal company channels"
      />

      <div className="sd-messages-shell">
        <Card variant="raised" density="compact" className="sd-channel-rail">
          <Stack gap={12}>
            <Stack gap={6}>
              <Text variant="cap">Direct (Board ↔ CEO)</Text>
              <AgentRow
                current={isCeoThread}
                agent={{
                  id: "ceo-direct",
                  name: data.ceo_thread.ceo_display_name,
                  role: "Direct 1:1",
                  state: sendingCeo ? "working" : "ready",
                  art: normalizeAvatarVariant(data.ceo_thread.ceo_avatar_variant),
                  preview: `${formatMessageCount(ceoMsgs.length)} · ${
                    hasBoardControl ? "Writable" : "Read-only"
                  }`,
                  time: lastCeoMsg ? formatRelativeNs(lastCeoMsg.recorded_at_ns) : undefined,
                  dateTime: lastCeoMsg
                    ? new Date(lastCeoMsg.recorded_at_ns / 1_000_000).toISOString()
                    : undefined,
                  unread: unreadCeoReply && !isCeoThread,
                }}
                onSelect={() => selectChannel("ceo-direct")}
              />
            </Stack>

            <Divider />

            <Stack gap={8}>
              <Text variant="cap">Internal Channels (Read-Only)</Text>
              {data.internal_channels.length > 0 ? (
                <FilterBar
                  label="Filter internal channels"
                  hiddenLabel
                  placeholder="Filter channels or members..."
                  size="compact"
                  query={channelQuery}
                  onQueryChange={setChannelQuery}
                />
              ) : null}

              {data.internal_channels.length === 0 ? (
                <Text variant="meta">No shared internal channels created yet.</Text>
              ) : filteredChannels.length === 0 ? (
                <EmptyResult
                  query={channelQuery}
                  onClear={() => setChannelQuery("")}
                />
              ) : (
                <List label="Internal company channels">
                  {filteredChannels.map((ch) => {
                    const lastMsg = ch.messages[ch.messages.length - 1];
                    const observedNs = liveChannelActivityNs[ch.channel_id];
                    const isUnread = unreadChannelIds.has(ch.channel_id);
                    return (
                      <li key={ch.channel_id} className="yg-list-item sd-channel-item">
                        <ThreadRow
                          selected={selectedChannel === ch.channel_id}
                          thread={{
                            id: ch.channel_id,
                            title: ch.name,
                            preview: lastMsg
                              ? `${lastMsg.sender_name}: ${lastMsg.body}`
                              : formatMessageCount(ch.message_count),
                            ...(observedNs
                              ? {
                                  updatedAt: {
                                    dateTime: new Date(observedNs / 1_000_000).toISOString(),
                                    label: formatRelativeNs(observedNs),
                                  },
                                }
                              : {}),
                            unread: isUnread,
                          }}
                          onOpen={() => selectChannel(ch.channel_id)}
                        />
                        <div className="sd-channel-item-meta">
                          <Badge tone="neutral" variant="soft" dot={false}>
                            {formatMemberCount(ch.members.length)}
                          </Badge>
                          <Text variant="meta" className="sd-tabular">
                            {formatMessageCount(ch.message_count)}
                          </Text>
                        </div>
                      </li>
                    );
                  })}
                </List>
              )}
            </Stack>
          </Stack>
        </Card>

        <Card variant="raised" className="sd-thread-pane">
          {isCeoThread ? (
            <Stack gap={12} className="sd-thread-stack">
              <Inline justify="between" align="center" wrap>
                <Inline gap={12} align="center">
                  <AgentAvatar
                    agent={normalizeAvatarVariant(data.ceo_thread.ceo_avatar_variant)}
                    state={sendingCeo ? "working" : "ready"}
                    size={36}
                    label={data.ceo_thread.ceo_display_name}
                  />
                  <Stack gap={2}>
                    <Heading level={2}>{data.ceo_thread.ceo_display_name}</Heading>
                    <Text variant="meta">
                      Direct 1:1 Board ↔ CEO Line · Sole writable channel
                    </Text>
                  </Stack>
                </Inline>
                <Badge tone="brand" variant="soft" dot={false}>
                  Advisory Direct Line
                </Badge>
              </Inline>

              <Notice title="Advisory boundary" tone="neutral">
                {data.ceo_thread.advisory_notice}
              </Notice>

              <div className="sd-thread-scroll">
                {ceoMsgs.length === 0 ? (
                  <Text variant="help">
                    No messages yet. Ask {ceoDisplayName} about{" "}
                    {meta?.active_domains.includes("trading")
                      ? "trading posture, strategy trials, or pending approvals."
                      : "company posture, active work, or pending approvals."}
                  </Text>
                ) : (
                  <Transcript
                    label="Board and CEO direct conversation"
                    revision={ceoMsgs.length}
                    readingAnchor="when-away"
                  >
                    {ceoMsgs.map((msg) => {
                      const refHref = msg.context_ref
                        ? resolveContextHref(msg.context_ref.kind, msg.context_ref.id)
                        : null;
                      const isFresh = freshMessageIds.has(`ceo:${msg.message_id}`);
                      return (
                        <div
                          key={msg.message_id}
                          className={isFresh ? "sd-live-enter" : undefined}
                        >
                          <Message
                            speaker={msg.sender_name}
                            time={formatNsClock(msg.recorded_at_ns)}
                            dateTime={new Date(msg.recorded_at_ns / 1_000_000).toISOString()}
                            variant={msg.sender_kind === "board" ? "sent" : "received"}
                          >
                            <Stack gap={6}>
                              {msg.context_ref ? (
                                refHref ? (
                                  <Link
                                    href={refHref}
                                    emphasis="quiet"
                                    size="compact"
                                    className="sd-context-ref-link"
                                    onClick={(e) => {
                                      e.preventDefault();
                                      navigate(refHref);
                                    }}
                                  >
                                    Ref: {msg.context_ref.label}
                                  </Link>
                                ) : (
                                  <Badge tone="neutral" variant="outline" dot={false}>
                                    Ref: {msg.context_ref.label}
                                  </Badge>
                                )
                              ) : null}
                              <Text variant="body">{msg.body}</Text>
                              <Text variant="meta" className="sd-tabular">
                                {formatDeliveryState(msg.delivery_state)}
                              </Text>
                            </Stack>
                          </Message>
                        </div>
                      );
                    })}
                  </Transcript>
                )}
              </div>

              {sendingCeo ? (
                <ThinkingIndicator active label={`Delivering advisory message to ${ceoDisplayName}`} />
              ) : null}

              {pendingAskContext ? (
                <Surface
                  variant="surface"
                  density="compact"
                  className="sd-context-card"
                >
                  <Stack gap={8}>
                    <Inline justify="between" align="start" gap={8} wrap>
                      <div className="sd-context-card-copy">
                        <Heading level={4}>{pendingAskContext.label}</Heading>
                        <Text variant="help">
                          Context · {formatContextKind(pendingAskContext.kind)} ·{" "}
                          {pendingAskContext.id}
                        </Text>
                      </div>
                      <Badge tone={ctxState.statusTone} variant="soft" dot={false}>
                        {ctxState.statusLabel}
                      </Badge>
                    </Inline>
                    <Text variant="help">{ctxState.detail}</Text>
                    <Inline justify="between" align="center" gap={8} wrap>
                      {pendingSourceHref ? (
                        <Link
                          href={pendingSourceHref}
                          emphasis="soft"
                          size="compact"
                          onClick={(e) => {
                            e.preventDefault();
                            navigate(pendingSourceHref);
                          }}
                        >
                          View source item
                        </Link>
                      ) : (
                        <Text variant="meta">Read-only reference</Text>
                      )}
                      <Button
                        emphasis="quiet"
                        size="compact"
                        shape="pill"
                        onClick={() => setPendingAskContext(null)}
                      >
                        Dismiss
                      </Button>
                    </Inline>
                  </Stack>
                </Surface>
              ) : null}

              {canRenderComposer ? (
                <Composer
                  label={`Advisory message to ${ceoDisplayName}`}
                  value={draft}
                  onValueChange={setDraft}
                  onSend={(payload) => void handleSendCeo(payload)}
                  pending={sendingCeo}
                  placeholder={`Message ${ceoDisplayName} (advisory only)...`}
                  shortcutHint="Cmd+Enter to send"
                />
              ) : !hasBoardControl ? (
                <Notice title="Read-only mode" tone="neutral">
                  Board control authority is required to send advisory messages to {ceoDisplayName}.
                </Notice>
              ) : (
                <Notice title="Controls locked" tone="neutral">
                  Controls are locked until the pending command or connection resolves.
                </Notice>
              )}
            </Stack>
          ) : activeInternal ? (
            <Stack gap={12} className="sd-thread-stack">
              <Inline justify="between" align="center" wrap>
                <Stack gap={4}>
                  <Heading level={2}>{activeInternal.name}</Heading>
                  <Text variant="meta">
                    {formatMemberCount(activeInternal.members.length)} · Revision{" "}
                    {activeInternal.revision}
                  </Text>
                  <Inline gap={6} align="center" wrap>
                    {activeInternal.members.map((m) => (
                      <Button
                        key={m.agent_id}
                        emphasis="quiet"
                        size="compact"
                        shape="pill"
                        onClick={() =>
                          navigate(`/agents/${encodeURIComponent(m.agent_id)}`)
                        }
                      >
                        {m.display_name} ({m.role_label})
                      </Button>
                    ))}
                  </Inline>
                </Stack>
                <Badge tone="neutral" variant="outline" dot={false}>
                  Read-Only Observation
                </Badge>
              </Inline>

              <Notice title="Discussion only · Read-only for Board" tone="neutral">
                Board members can read internal team conversations in real time, but messages here
                are strictly read-only. To give guidance, switch to {ceoDisplayName} (Direct 1:1).
              </Notice>

              <div className="sd-thread-scroll">
                {activeInternal.messages.length === 0 ? (
                  <Text variant="help">No messages posted in {activeInternal.name} yet.</Text>
                ) : (
                  <Transcript
                    label={`${activeInternal.name} internal conversation`}
                    revision={activeInternal.messages.length}
                    readingAnchor="when-away"
                  >
                    {activeInternal.messages.map((msg) => {
                      const isFresh = freshMessageIds.has(
                        `${activeInternal.channel_id}:${msg.message_id}`
                      );
                      return (
                        <div
                          key={msg.message_id}
                          className={isFresh ? "sd-live-enter" : undefined}
                        >
                          <Message
                            speaker={`${msg.sender_name} · ${msg.sender_role}`}
                            avatar={
                              <AgentAvatar
                                agent={normalizeAvatarVariant(msg.avatar_variant)}
                                state="ready"
                                animated={false}
                                size={26}
                                label={msg.sender_name}
                              />
                            }
                            variant="received"
                          >
                            <Stack gap={6}>
                              <Text variant="body">{msg.body}</Text>
                              {msg.asset_id ? (
                                <Stack gap={6}>
                                  <ResourceRow
                                    resource={{
                                      id: msg.asset_id,
                                      kind: "output",
                                      title: `Publication ${msg.asset_id}`,
                                      detail: "Shared company report · Click to open in Reports",
                                    }}
                                    onOpen={() => navigate("/reports")}
                                  />
                                  <Inline gap={8} align="center" wrap>
                                    <Button
                                      emphasis="quiet"
                                      size="compact"
                                      shape="pill"
                                      onClick={() =>
                                        askCeo({
                                          kind: "shared_publication",
                                          id: msg.asset_id!,
                                          label: `Publication ${msg.asset_id} (from ${activeInternal.name})`,
                                        })
                                      }
                                    >
                                      Ask CEO about this publication
                                    </Button>
                                  </Inline>
                                </Stack>
                              ) : null}
                              <Text variant="meta" className="sd-tabular">
                                {msg.delivery_count} delivered · {msg.read_count} read
                                {msg.audience.length > 0
                                  ? ` · Audience: ${msg.audience.length}`
                                  : ""}
                              </Text>
                            </Stack>
                          </Message>
                        </div>
                      );
                    })}
                  </Transcript>
                )}
              </div>
            </Stack>
          ) : null}
        </Card>
      </div>
    </Stack>
  );
}
