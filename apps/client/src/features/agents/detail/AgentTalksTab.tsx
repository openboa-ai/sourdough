import {
  Badge,
  Button,
  Card,
  Inline,
  Message,
  Notice,
  Stack,
  Text,
  Transcript,
} from "@openboa/yogurt";
import type { AgentDetailProjection } from "../../../api/types";
import { useRouter } from "../../../state/store";

export interface AgentTalksTabProps {
  agentId: string;
  eventSeq: number;
  talks: AgentDetailProjection["tabs"]["interactions_and_talks"];
  displayNameFor: (id: string) => string;
}

export function AgentTalksTab({
  agentId,
  eventSeq,
  talks,
  displayNameFor,
}: AgentTalksTabProps) {
  const { navigate } = useRouter();

  return (
    <Stack gap={12}>
      <Notice title="Discussion only · Read-only Board observation" tone="neutral">
        Internal agent conversations do not assign or accept work on their own. To give direction,
        send an advisory message to Founder · CEO in Company Messages.
      </Notice>
      <Card variant="raised">
        {talks.length === 0 ? (
          <Text variant="help">No project messages involving this agent yet.</Text>
        ) : (
          <div className="sd-agent-talks">
            <Transcript label="Agent internal discussions" revision={eventSeq}>
              {talks.map((msg) => (
                <Message
                  key={msg.message_id}
                  speaker={`${msg.sender_name} (${msg.sender_role})`}
                  variant={msg.sender_id === agentId ? "sent" : "received"}
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
                          navigate(`/messages?channel=${encodeURIComponent(msg.project_id)}`)
                        }
                      >
                        #{msg.project_id}
                      </Button>
                      {msg.audience.length > 0 ? (
                        <Inline gap={6} align="center" wrap>
                          <Text variant="meta">Audience:</Text>
                          {msg.audience.map((aud) => (
                            <Badge key={aud} tone="neutral" variant="outline" dot={false}>
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
                          onClick={() => navigate("/reports")}
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
  );
}
