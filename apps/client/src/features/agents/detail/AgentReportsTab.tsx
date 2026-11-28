import { Fragment } from "react";
import {
  Badge,
  Button,
  Card,
  DisclosureRow,
  Divider,
  Inline,
  Stack,
  Surface,
  Text,
} from "@openboa/yogurt";
import type { AgentDetailProjection } from "../../../api/types";
import { useRouter } from "../../../state/store";

export interface AgentReportsTabProps {
  agentDisplayName: string;
  publishedReports: AgentDetailProjection["tabs"]["published_reports"];
  displayNameFor: (id: string) => string;
}

export function AgentReportsTab({
  agentDisplayName,
  publishedReports,
  displayNameFor,
}: AgentReportsTabProps) {
  const { navigate, askCeo } = useRouter();

  return (
    <Stack gap={12}>
      {publishedReports.length === 0 ? (
        <Card variant="raised">
          <Text variant="help">
            No shared company reports published by this agent yet. Unpublished work stays private
            on their Desk.
          </Text>
        </Card>
      ) : (
        <Card title="Selective Company Publications" variant="raised">
          <Stack gap={12}>
            {publishedReports.map((pub, idx) => {
              const excerpt =
                pub.content.length > 200 ? `${pub.content.slice(0, 198)}…` : pub.content;
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
                            navigate(`/messages?channel=${encodeURIComponent(pub.project_id)}`)
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
                          <Badge key={aud} tone="neutral" variant="outline" dot={false}>
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
                            label: `Asset ${pub.asset_id} by ${agentDisplayName}`,
                          })
                        }
                      >
                        Ask CEO about this report
                      </Button>
                      <Button
                        emphasis="quiet"
                        size="compact"
                        shape="pill"
                        onClick={() => navigate("/reports")}
                      >
                        View in Reports
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
  );
}
