import { useEffect, useState } from "react";
import { ThinkingIndicator } from "@openboa/yogurt/motion-thinking-indicator";
import {
  AgentAvatar,
  AgentRow,
  Badge,
  Button,
  Card,
  Composer,
  Divider,
  Heading,
  Inline,
  List,
  Message,
  Notice,
  PageHeader,
  Result,
  ScrollArea,
  Skeleton,
  Stack,
  Text,
  ThreadRow,
  Transcript,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatNsClock,
  newCommandId,
} from "../api/client";
import type { MessagesProjection } from "../api/types";
import { useApp } from "../state/store";

function initialChannelFromRoute(route: string): string {
  const qIdx = route.indexOf("?");
  if (qIdx === -1) return "ceo-direct";
  const params = new URLSearchParams(route.slice(qIdx + 1));
  return params.get("channel") || "ceo-direct";
}

export function MessagesView() {
  const {
    route,
    navigate,
    refreshSeq,
    meta,
    canWrite,
    outbox,
    pendingAskContext,
    setPendingAskContext,
    dispatchCommand,
    reportApiError,
  } = useApp();

  const [data, setData] = useState<MessagesProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedChannel, setSelectedChannel] = useState<string>(() =>
    initialChannelFromRoute(route)
  );
  const [draft, setDraft] = useState("");

  useEffect(() => {
    setSelectedChannel(initialChannelFromRoute(route));
  }, [route]);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<MessagesProjection>("messages", {
          signal: controller.signal,
        });
        if (res.data) setData(res.data);
      } catch (err) {
        if (!controller.signal.aborted) reportApiError(err);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [refreshSeq, reportApiError]);

  if (loading && !data) {
    return <Skeleton label="Loading company messages" lines={6} />;
  }

  if (!data) {
    return <Result title="Company Messages unavailable">Could not load message channels.</Result>;
  }

  const activeInternal = data.internal_channels.find(
    (ch) => ch.channel_id === selectedChannel
  );
  const isCeoThread = !activeInternal;
  const sendingCeo =
    outbox.intent?.command_type === "send_ceo_message" &&
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted");

  const handleSendCeo = ({ text }: { text: string }) => {
    const body = text.trim();
    if (!body || !meta || !data.ceo_thread.ceo_agent_id) return;
    const ctx = pendingAskContext;
    setDraft("");
    setPendingAskContext(null);
    void dispatchCommand({
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
      label: `Advisory message to ${data.ceo_thread.ceo_display_name}`,
      sent_ns: Date.now() * 1_000_000,
    });
  };

  return (
    <Stack gap={16}>
      <PageHeader
        title="Company Messages"
        detail="Direct advisory channel with Founder · CEO plus read-only observation of internal company channels"
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
                  art: data.ceo_thread.ceo_avatar_variant,
                  preview: `${data.ceo_thread.messages.length} messages · Writable`,
                }}
                onSelect={() => setSelectedChannel("ceo-direct")}
              />
            </Stack>

            <Divider />

            <Stack gap={6}>
              <Text variant="cap">Internal Channels (Read-Only)</Text>
              {data.internal_channels.length === 0 ? (
                <Text variant="meta">No shared internal channels created yet.</Text>
              ) : (
                <List label="Internal company channels">
                  {data.internal_channels.map((ch) => {
                    const lastMsg = ch.messages[ch.messages.length - 1];
                    return (
                      <ThreadRow
                        key={ch.channel_id}
                        selected={selectedChannel === ch.channel_id}
                        thread={{
                          id: ch.channel_id,
                          title: ch.name,
                          preview: lastMsg
                            ? `${lastMsg.sender_name}: ${lastMsg.body}`
                            : `${ch.message_count} messages`,
                        }}
                        onOpen={() => setSelectedChannel(ch.channel_id)}
                      />
                    );
                  })}
                </List>
              )}
            </Stack>
          </Stack>
        </Card>

        <Card variant="raised" className="sd-thread-pane">
          {isCeoThread ? (
            <Stack gap={12} className="sd-thread-pane">
              <Inline justify="between" align="center" wrap>
                <Inline gap={12} align="center">
                  <AgentAvatar
                    agent={data.ceo_thread.ceo_avatar_variant}
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

              <ScrollArea label="Board and CEO messages" className="sd-thread-scroll">
                {data.ceo_thread.messages.length === 0 ? (
                  <Text variant="help">
                    No messages yet. Ask Founder · CEO about trading posture, strategy trials, or
                    pending approvals.
                  </Text>
                ) : (
                  <Transcript
                    label="Board and CEO direct conversation"
                    revision={data.ceo_thread.messages.length}
                    readingAnchor="when-away"
                  >
                    {data.ceo_thread.messages.map((msg) => (
                      <Message
                        key={msg.message_id}
                        speaker={msg.sender_name}
                        time={formatNsClock(msg.recorded_at_ns)}
                        variant={msg.sender_kind === "board" ? "sent" : "received"}
                      >
                        <Stack gap={6}>
                          {msg.context_ref ? (
                            <Badge tone="neutral" variant="outline" dot={false}>
                              Ref: {msg.context_ref.label}
                            </Badge>
                          ) : null}
                          <Text variant="body">{msg.body}</Text>
                        </Stack>
                      </Message>
                    ))}
                  </Transcript>
                )}
              </ScrollArea>

              {sendingCeo ? (
                <ThinkingIndicator active label="Delivering advisory message to Founder · CEO" />
              ) : null}

              {canWrite && data.ceo_thread.writable ? (
                <Composer
                  label={`Advisory message to ${data.ceo_thread.ceo_display_name}`}
                  value={draft}
                  onValueChange={setDraft}
                  onSend={handleSendCeo}
                  pending={sendingCeo}
                  attachments={
                    pendingAskContext
                      ? [
                          {
                            id: pendingAskContext.id,
                            name: pendingAskContext.label,
                            detail: `${pendingAskContext.kind} (read-only ref)`,
                          },
                        ]
                      : undefined
                  }
                  onRemoveAttachment={
                    pendingAskContext ? () => setPendingAskContext(null) : undefined
                  }
                  placeholder={`Message ${data.ceo_thread.ceo_display_name} (advisory only)...`}
                  shortcutHint="Cmd+Enter to send"
                />
              ) : (
                <Notice title="Read-only mode" tone="neutral">
                  Board control authority is required to send advisory messages to Founder · CEO.
                </Notice>
              )}
            </Stack>
          ) : (
            <Stack gap={12} className="sd-thread-pane">
              <Inline justify="between" align="center" wrap>
                <Stack gap={4}>
                  <Heading level={2}>{activeInternal.name}</Heading>
                  <Inline gap={6} align="center" wrap>
                    {activeInternal.members.map((m) => (
                      <Button
                        key={m.agent_id}
                        emphasis="quiet"
                        size="compact"
                        shape="pill"
                        onClick={() =>
                          navigate(`/office/agents/${encodeURIComponent(m.agent_id)}`)
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

              <Notice title="Observing internal company discussion" tone="neutral">
                Board members can read internal team conversations in real time, but messages here
                are strictly read-only. To give guidance, switch to Founder · CEO (Direct 1:1).
              </Notice>

              <ScrollArea label={`${activeInternal.name} messages`} className="sd-thread-scroll">
                {activeInternal.messages.length === 0 ? (
                  <Text variant="help">No messages posted in {activeInternal.name} yet.</Text>
                ) : (
                  <Transcript
                    label={`${activeInternal.name} internal conversation`}
                    revision={activeInternal.messages.length}
                    readingAnchor="when-away"
                  >
                    {activeInternal.messages.map((msg) => (
                      <Message
                        key={msg.message_id}
                        speaker={`${msg.sender_name} · ${msg.sender_role}`}
                        avatar={
                          <AgentAvatar
                            agent={msg.avatar_variant}
                            state="ready"
                            animated={false}
                            size={26}
                            label={msg.sender_name}
                          />
                        }
                        variant="received"
                      >
                        <Stack gap={4}>
                          <Text variant="body">{msg.body}</Text>
                          {msg.asset_id ? (
                            <Text variant="meta">
                              Referenced publication: {msg.asset_id}
                            </Text>
                          ) : null}
                        </Stack>
                      </Message>
                    ))}
                  </Transcript>
                )}
              </ScrollArea>
            </Stack>
          )}
        </Card>
      </div>
    </Stack>
  );
}
