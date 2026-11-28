import { useCallback, useMemo, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  AgentRow,
  Badge,
  Button,
  Card,
  DataTable,
  EmptyResult,
  FilterBar,
  Heading,
  Inline,
  List,
  Notice,
  PageHeader,
  Result,
  Skeleton,
  Stack,
  Surface,
  Switch,
  Tabs,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import { normalizeAvatarVariant } from "../../api/avatar";
import type { AgentsGraphProjection, OrgProjection } from "../../api/types";
import { useProjection } from "../../api/useProjection";
import { formatMinorCurrency } from "../../lib/format";
import { useControlPlane, useRouter } from "../../state/store";

export function OrgReportsView() {
  const { navigate, askCeo, routeSearchParams } = useRouter();
  const { meta } = useControlPlane();
  const { data, loading, error, refetch } = useProjection<OrgProjection>("org");
  const { data: graph, refetch: refetchGraph } =
    useProjection<AgentsGraphProjection>("agents-graph");

  const [tab, setTab] = useState(() =>
    routeSearchParams.get("tab") === "roster" ? "roster" : "reports"
  );
  const [rosterQuery, setRosterQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [reportsQuery, setReportsQuery] = useState("");
  const [expandedPubs, setExpandedPubs] = useState<Set<string>>(() => new Set());

  const graphById = useMemo(
    () => new Map((graph?.nodes ?? []).map((n) => [n.agent_id, n])),
    [graph?.nodes]
  );

  const agentNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of data?.roster ?? []) {
      if (a.agent_id && a.display_name) map.set(a.agent_id, a.display_name);
    }
    for (const n of graph?.nodes ?? []) {
      if (n.agent_id && n.display_name) map.set(n.agent_id, n.display_name);
    }
    for (const p of data?.publications ?? []) {
      if (p.author_id && p.author_name && !map.has(p.author_id)) {
        map.set(p.author_id, p.author_name);
      }
    }
    if (meta?.founder_id) {
      map.set(meta.founder_id, "Founder · CEO");
    }
    return map;
  }, [data?.roster, data?.publications, graph?.nodes, meta?.founder_id]);

  const displayNameFor = useCallback(
    (id: string): string => agentNameById.get(id) ?? id,
    [agentNameById]
  );

  const filteredRoster = useMemo(() => {
    const q = rosterQuery.trim().toLowerCase();
    return (data?.roster ?? []).filter((agent) => {
      if (!showArchived && agent.archived) return false;
      if (!q) return true;
      const g = graphById.get(agent.agent_id);
      const liveStatus = g?.live_status ?? agent.live_status ?? agent.onboarding_state ?? "active";
      return `${agent.display_name} ${agent.agent_id} ${agent.role_label} ${agent.grant_scope} ${agent.onboarding_state ?? ""} ${liveStatus}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.roster, showArchived, rosterQuery, graphById]);

  const filteredPublications = useMemo(() => {
    const q = reportsQuery.trim().toLowerCase();
    return (data?.publications ?? []).filter((p) => {
      if (!q) return true;
      const audienceNames = p.audience.map(displayNameFor).join(" ");
      return `${p.asset_id} ${p.author_name} ${p.author_role} ${p.project_id} ${p.work_item_id} ${p.audience.join(" ")} ${audienceNames} ${p.content}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.publications, reportsQuery, displayNameFor]);

  if (loading && !data) return <Skeleton label="Loading organization and reports" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Org & Reports unavailable"
        action={
          <Button
            emphasis="soft"
            shape="pill"
            onClick={() => {
              refetch();
              refetchGraph();
            }}
          >
            Retry
          </Button>
        }
      >
        {error ?? "Could not load organization data."}
      </Result>
    );
  }

  const custodyById = new Map(
    data.delegated_hr_custody.archived_employees.map((a) => [`${a.cutoff}-${a.agent_id}`, a])
  );
  const custodyCols: DataColumn[] = [
    {
      id: "cutoff",
      label: "Cutoff",
      kind: "numeric",
      align: "end",
      sortValue: (row) => custodyById.get(row.id)?.cutoff,
    },
    {
      id: "agent",
      label: "Archived Agent",
      kind: "identity",
      sortValue: (row) => custodyById.get(row.id)?.agent_name,
    },
    {
      id: "manager",
      label: "Custody Manager",
      kind: "text",
      sortValue: (row) => custodyById.get(row.id)?.manager_name,
    },
    {
      id: "employment_revision",
      label: "Employment Rev",
      kind: "text",
      sortValue: (row) => custodyById.get(row.id)?.employment_revision,
    },
    {
      id: "scope",
      label: "Prior Scope",
      kind: "role",
      sortValue: (row) => custodyById.get(row.id)?.previous_scope,
    },
    {
      id: "reason",
      label: "Reason",
      kind: "text",
      sortValue: (row) => custodyById.get(row.id)?.reason,
    },
  ];
  const custodyRows: DataRow[] = data.delegated_hr_custody.archived_employees.map((a) => ({
    id: `${a.cutoff}-${a.agent_id}`,
    cells: {
      cutoff: <Text className="sd-tabular">#{a.cutoff}</Text>,
      agent: (
        <Stack gap={2}>
          <Text variant="body">{a.agent_name}</Text>
          <Text variant="meta" className="sd-mono">
            {a.agent_id}
          </Text>
        </Stack>
      ),
      manager: `${a.manager_name} (${a.manager_id})`,
      employment_revision: (
        <Inline gap={6} align="center">
          <Text className="sd-mono">{a.employment_revision}</Text>
          <CopyButton value={a.employment_revision} presentation="quiet" />
        </Inline>
      ),
      scope: a.previous_scope,
      reason: a.reason,
    },
  }));

  const togglePub = (assetId: string) => {
    setExpandedPubs((prev) => {
      const next = new Set(prev);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
  };

  return (
    <Stack gap={16}>
      <PageHeader
        title="Reports"
        detail="Published company assets, employee roster, onboarding status, and delegated HR custody audit"
      />

      <Tabs
        label="Organization sections"
        presentation="pill"
        value={tab}
        onValueChange={setTab}
        items={[
          {
            id: "roster",
            label: `Roster (${filteredRoster.length})`,
            panel: tab === "roster" ? (
              <Card title="Company Roster & Onboarding" variant="raised">
                <Stack gap={12}>
                  <Stack gap={8}>
                    <FilterBar
                      label="Filter roster"
                      hiddenLabel
                      placeholder="Filter by agent, role, grant scope, or onboarding state..."
                      size="compact"
                      query={rosterQuery}
                      onQueryChange={setRosterQuery}
                    />
                    <Inline justify="end">
                      <Switch
                        label="Show archived"
                        checked={showArchived}
                        onCheckedChange={setShowArchived}
                      />
                    </Inline>
                  </Stack>
                  {filteredRoster.length === 0 ? (
                    <EmptyResult
                      query={rosterQuery}
                      onClear={() => setRosterQuery("")}
                    />
                  ) : (
                    <List label="Company agents" divided>
                      {filteredRoster.map((agent) => {
                        const graphNode = graphById.get(agent.agent_id);
                        const derivedAvatarState =
                          graphNode?.avatar_state ??
                          agent.avatar_state ??
                          (agent.archived
                            ? "done"
                            : agent.onboarding_state === "blocked"
                              ? "blocked"
                              : "ready");
                        const derivedLiveStatus =
                          graphNode?.live_status ??
                          agent.live_status ??
                          (agent.archived
                            ? "archived"
                            : agent.onboarding_state === "blocked"
                              ? "blocked"
                              : agent.onboarding_state ?? "active");
                        const onboardingLabel = agent.onboarding_state ?? "none";

                        return (
                          <li key={agent.agent_id} className="yg-list-item sd-list-row-padded">
                            <div data-focus-id={`org-agent-${agent.agent_id}`}>
                              <Stack gap={6}>
                                <AgentRow
                                  agent={{
                                    id: agent.agent_id,
                                    name: agent.display_name,
                                    role: `${agent.role_label} · ${derivedLiveStatus}`,
                                    state: derivedAvatarState,
                                    art: normalizeAvatarVariant(agent.avatar_variant),
                                    preview: `${agent.grant_scope || "No grant"} · ${agent.private_draft_count} private drafts`,
                                  }}
                                  onSelect={() =>
                                    navigate(`/agents/${encodeURIComponent(agent.agent_id)}`, {
                                      focusReturnId: `org-agent-${agent.agent_id}`,
                                    })
                                  }
                                />
                                <Inline gap={8} align="center" wrap>
                                  <Badge
                                    tone={
                                      agent.archived
                                        ? "neutral"
                                        : derivedLiveStatus === "blocked"
                                          ? "danger"
                                          : "brand"
                                    }
                                    dot={false}
                                  >
                                    {agent.archived ? "Archived" : derivedLiveStatus}
                                  </Badge>
                                  <Text variant="meta">
                                    Onboarding: {onboardingLabel} ({agent.onboarding_attempts}/3) · Budget:{" "}
                                    {formatMinorCurrency(agent.monthly_budget_minor)}
                                  </Text>
                                </Inline>
                              </Stack>
                            </div>
                          </li>
                        );
                      })}
                    </List>
                  )}
                </Stack>
              </Card>
            ) : null,
          },
          {
            id: "reports",
            label: `Published Reports (${data.publications.length})`,
            panel: tab === "reports" ? (
              <Card title="Selective Company Publications" variant="raised">
                <Stack gap={12}>
                  <FilterBar
                    label="Filter published reports"
                    hiddenLabel
                    placeholder="Filter by asset ID, author, project, work item, or content..."
                    size="compact"
                    query={reportsQuery}
                    onQueryChange={setReportsQuery}
                  />
                  {data.publications.length === 0 ? (
                    <Text variant="help">No published company reports yet.</Text>
                  ) : filteredPublications.length === 0 ? (
                    <EmptyResult query={reportsQuery} onClear={() => setReportsQuery("")} />
                  ) : (
                    <List label="Published company reports" divided>
                      {filteredPublications.map((p) => {
                        const isLong = p.content.length > 220;
                        const isExpanded = expandedPubs.has(p.asset_id);
                        const bodyText =
                          isLong && !isExpanded ? `${p.content.slice(0, 220)}…` : p.content;
                        const audienceList =
                          p.audience.length > 0 ? p.audience : ["internal"];

                        return (
                          <li key={p.asset_id} className="yg-list-item sd-list-row-padded">
                            <Surface variant="surface" density="compact">
                              <Stack gap={8}>
                                <Inline justify="between" align="center" gap={8} wrap>
                                  <Inline gap={6} align="center" wrap>
                                    <Heading level={4}>Report {p.asset_id}</Heading>
                                    <CopyButton value={p.asset_id} presentation="quiet" />
                                    <Text variant="meta">
                                      {p.author_name} ({p.author_role}) · #{p.project_id} · Work item:{" "}
                                      {p.work_item_id}
                                    </Text>
                                  </Inline>
                                  <Inline gap={6} align="center" wrap>
                                    {audienceList.map((aud) => (
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
                                <Text variant="body">{bodyText}</Text>
                                <Inline gap={8} align="center" wrap>
                                  <Button
                                    emphasis="soft"
                                    size="compact"
                                    shape="pill"
                                    onClick={() =>
                                      askCeo({
                                        kind: "shared_publication",
                                        id: p.asset_id,
                                        label: `Report ${p.asset_id} by ${p.author_name}`,
                                      })
                                    }
                                  >
                                    Ask CEO about this report
                                  </Button>
                                  <Button
                                    emphasis="quiet"
                                    size="compact"
                                    shape="pill"
                                    onClick={() =>
                                      navigate(`/agents/${encodeURIComponent(p.author_id)}`)
                                    }
                                  >
                                    View author
                                  </Button>
                                  {isLong ? (
                                    <Button
                                      emphasis="quiet"
                                      size="compact"
                                      shape="pill"
                                      aria-expanded={isExpanded}
                                      onClick={() => togglePub(p.asset_id)}
                                    >
                                      {isExpanded ? "Collapse" : "Show full report"}
                                    </Button>
                                  ) : null}
                                </Inline>
                              </Stack>
                            </Surface>
                          </li>
                        );
                      })}
                    </List>
                  )}
                </Stack>
              </Card>
            ) : null,
          },
          {
            id: "custody",
            label: `Delegated HR Custody (${custodyRows.length})`,
            panel: tab === "custody" ? (
              <Stack gap={12}>
                <Notice title={data.delegated_hr_custody.policy_notice} tone="neutral">
                  Routine onboarding and offboarding custody transfers are delegated to Founder ·
                  CEO under standing authority. Board members inspect offboarding custody transfers
                  here read-only.
                </Notice>
                <Card
                  title="Archived Employees & Custody Ledger"
                  variant="raised"
                  className="sd-ledger-card"
                >
                  {custodyRows.length === 0 ? (
                    <Text variant="help">No archived employees recorded.</Text>
                  ) : (
                    <DataTable
                      caption="Delegated HR custody ledger"
                      columns={custodyCols}
                      rows={custodyRows}
                    />
                  )}
                </Card>
              </Stack>
            ) : null,
          },
        ]}
      />
    </Stack>
  );
}
