import { useCallback, useMemo, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Button,
  Card,
  Inline,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Skeleton,
  Stack,
  Tabs,
  Text,
} from "@openboa/yogurt";
import type { ApprovalQuestionItem, StrategiesProjection } from "../../../api/types";
import { useProjection } from "../../../api/useProjection";
import {
  ContextualApprovalList,
  usePendingApprovals,
} from "../../../components/ContextualApprovalCard";
import { formatNsWithClock } from "../../../lib/format";
import { useControlPlane } from "../../../state/store";
import { ChallengerTrialsPanel, ExecutionAdapterPanel } from "./ChallengerTrialsPanel";
import {
  StrategyActivationConfirm,
  StrategyDeploymentHistoryPanel,
  StrategyReleaseList,
  type StrategyConfirmTarget,
} from "./StrategyReleaseList";

const isStrategyApproval = (q: ApprovalQuestionItem): boolean =>
  q.kind === "strategy_promotion" ||
  q.kind === "strategy_release" ||
  q.kind === "challenger_promotion";

export function StrategiesView() {
  const { meta } = useControlPlane();
  const { data, loading, error, refetch } =
    useProjection<StrategiesProjection>("strategies");
  const strategyApprovals = usePendingApprovals(isStrategyApproval);

  const [activeTab, setActiveTab] = useState("active");
  const [confirmTarget, setConfirmTarget] = useState<StrategyConfirmTarget | null>(null);

  const principalId = meta?.board_access?.principal_id ?? null;

  const releaseById = useMemo(
    () => new Map((data?.releases ?? []).map((r) => [r.release_id, r])),
    [data?.releases]
  );

  const consumedComparisonIds = useMemo(() => {
    const set = new Set<string>();
    for (const d of data?.deployments ?? []) {
      if (d.comparison_id) set.add(d.comparison_id);
    }
    return set;
  }, [data?.deployments]);

  const isSelfOwned = useCallback(
    (releaseId: string) => {
      if (!principalId) return false;
      return releaseById.get(releaseId)?.owner_agent_id === principalId;
    },
    [principalId, releaseById]
  );

  if (loading && !data) return <Skeleton label="Loading strategy trials" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Strategies unavailable"
        action={
          <Button emphasis="soft" shape="pill" onClick={refetch}>
            Retry
          </Button>
        }
      >
        {error ?? "Could not load strategies."}
      </Result>
    );
  }

  if (!data.trading_enabled || (meta !== null && !meta.active_domains.includes("trading"))) {
    return (
      <Stack gap={16}>
        <PageHeader title="Strategies & Trials" detail="Trading domain not installed" />
        <Result title="Trading strategies inactive">
          Activate the Trading domain module to register releases and run paired challenger trials.
        </Result>
      </Stack>
    );
  }

  const selfOwnedReleases = data.releases.filter((r) =>
    Boolean(principalId && r.owner_agent_id === principalId)
  );

  return (
    <Stack gap={16}>
      <PageHeader
        title="Strategies"
        detail="Release versions, paired challenger trials, forward rollbacks, and historical performance lineage"
      />

      <ContextualApprovalList questions={strategyApprovals} />

      {selfOwnedReleases.length > 0 ? (
        <Notice title="Self-activation guard active" tone="neutral">
          Self-activation guard: release author ({principalId}) cannot activate or roll back their
          own release ({selfOwnedReleases.map((r) => r.release_id).join(", ")}). A separate Board
          Control principal must promote or roll back these releases.
        </Notice>
      ) : null}

      {confirmTarget ? (
        <StrategyActivationConfirm
          data={data}
          confirmTarget={confirmTarget}
          isSelfOwned={isSelfOwned}
          onClose={() => setConfirmTarget(null)}
        />
      ) : null}

      <Tabs
        label="Strategy sections"
        presentation="pill"
        value={activeTab}
        onValueChange={setActiveTab}
        items={[
          {
            id: "active",
            label: "Active Deployment",
            panel:
              activeTab === "active" ? (
                <Card variant="raised" title="Active Strategy Deployment">
                  {data.active_deployment ? (
                    <div className="sd-facts-wrap">
                      <RecordFacts
                        source="trading_strategy_deployment"
                        asOf={`seq #${data.event_seq}`}
                        facts={[
                          {
                            id: "dep",
                            label: "Deployment ID",
                            value: (
                              <Inline gap={6} align="center">
                                <Text className="sd-mono">
                                  {data.active_deployment.deployment_id}
                                </Text>
                                <CopyButton
                                  value={data.active_deployment.deployment_id}
                                  presentation="quiet"
                                />
                              </Inline>
                            ),
                          },
                          {
                            id: "rel",
                            label: "Active Release",
                            value: (
                              <Text className="sd-mono">
                                {data.active_deployment.release_id}
                              </Text>
                            ),
                          },
                          {
                            id: "prog",
                            label: "Program Identity",
                            value: (
                              <Text className="sd-mono">
                                {data.active_deployment.program_identity}
                              </Text>
                            ),
                          },
                          {
                            id: "bind",
                            label: "Binding Revision",
                            value: (
                              <Inline gap={6} align="center">
                                <Text className="sd-mono">
                                  {data.active_deployment.binding_revision}
                                </Text>
                                <CopyButton
                                  value={data.active_deployment.binding_revision}
                                  presentation="quiet"
                                />
                              </Inline>
                            ),
                          },
                          {
                            id: "cmp",
                            label: "Qualifying Trial",
                            value: data.active_deployment.comparison_id ?? "Initial baseline",
                          },
                          {
                            id: "status",
                            label: "Status",
                            value: (
                              <Badge tone="success" dot={false}>
                                {data.active_deployment.status}
                              </Badge>
                            ),
                          },
                          {
                            id: "act",
                            label: "Activated At",
                            value: formatNsWithClock(data.active_deployment.activated_at_ns),
                          },
                          {
                            id: "cmd",
                            label: "Command ID",
                            value: (
                              <Inline gap={6} align="center">
                                <Text className="sd-mono">
                                  {data.active_deployment.command_id}
                                </Text>
                                <CopyButton
                                  value={data.active_deployment.command_id}
                                  presentation="quiet"
                                />
                              </Inline>
                            ),
                          },
                        ]}
                      />
                    </div>
                  ) : (
                    <Text variant="help">No active strategy deployment recorded yet.</Text>
                  )}
                </Card>
              ) : null,
          },
          {
            id: "releases",
            label: `Releases (${data.releases.length})`,
            panel:
              activeTab === "releases" ? (
                <StrategyReleaseList
                  data={data}
                  consumedComparisonIds={consumedComparisonIds}
                  isSelfOwned={isSelfOwned}
                  onSelectConfirm={setConfirmTarget}
                />
              ) : null,
          },
          {
            id: "deployments",
            label: `Deployments (${data.deployments.length})`,
            panel:
              activeTab === "deployments" ? (
                <StrategyDeploymentHistoryPanel
                  data={data}
                  isSelfOwned={isSelfOwned}
                  onSelectConfirm={setConfirmTarget}
                />
              ) : null,
          },
          {
            id: "trials",
            label: `Challenger Trials (${data.trials.length})`,
            panel:
              activeTab === "trials" ? (
                <ChallengerTrialsPanel
                  data={data}
                  consumedComparisonIds={consumedComparisonIds}
                  isSelfOwned={isSelfOwned}
                  onSelectConfirm={setConfirmTarget}
                />
              ) : null,
          },
          {
            id: "adapter",
            label: "Execution Adapter",
            panel: activeTab === "adapter" ? <ExecutionAdapterPanel data={data} /> : null,
          },
        ]}
      />
    </Stack>
  );
}
