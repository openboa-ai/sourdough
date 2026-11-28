import { useEffect, useMemo, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyResult,
  FilterBar,
  Inline,
  List,
  Navbar,
  PageHeader,
  RecordFacts,
  Result,
  Skeleton,
  Stack,
  Text,
  WorkEvent,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type { SystemAuditProjection } from "../../api/types";
import { useProjection } from "../../api/useProjection";
import { SpaBreadcrumb } from "../../components/SpaBreadcrumb";
import { formatDeadlineNs, formatNsClock, formatNsWithClock } from "../../lib/format";
import { useControlPlane, useRouter } from "../../state/store";

const AUDIT_DOMAINS = [
  "all",
  "trading",
  "accounting",
  "assets",
  "org",
  "work",
  "ceo",
  "ops",
  "governance",
  "messages",
] as const;

export function SystemAuditView({
  embedded = false,
  mode,
}: {
  embedded?: boolean;
  mode?: "audit" | "daemon";
} = {}) {
  const { navigate, registerEscapeBack } = useRouter();
  const { meta } = useControlPlane();
  const effectiveDaemon = mode === "daemon";
  const { data, loading, error, refetch } =
    useProjection<SystemAuditProjection>("system-audit");
  const [auditQuery, setAuditQuery] = useState("");
  const [selectedDomain, setSelectedDomain] = useState<string>("all");

  const tradingActive = meta?.active_domains.includes("trading") ?? false;
  const visibleAuditDomains = useMemo(
    () => AUDIT_DOMAINS.filter((d) => d !== "trading" || tradingActive),
    [tradingActive]
  );

  useEffect(() => {
    if (!tradingActive && selectedDomain === "trading") {
      setSelectedDomain("all");
    }
  }, [tradingActive, selectedDomain]);

  useEffect(() => {
    if (!effectiveDaemon || embedded) return;
    return registerEscapeBack("/audit", "system-daemon-entry");
  }, [effectiveDaemon, embedded, registerEscapeBack]);

  const filteredEvents = useMemo(() => {
    const events = data?.events ?? [];
    const q = auditQuery.trim().toLowerCase();
    return events.filter((e) => {
      if (selectedDomain !== "all" && e.domain !== selectedDomain) return false;
      if (!q) return true;
      return `${e.event_seq} ${e.domain} ${e.event_type} ${e.entity_type} ${e.entity_id}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.events, selectedDomain, auditQuery]);

  if (loading && !data) return <Skeleton label="Loading audit log and leases" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Audit Log unavailable"
        action={
          <Button emphasis="soft" shape="pill" onClick={refetch}>
            Retry
          </Button>
        }
      >
        {error ?? "Could not load audit trail."}
      </Result>
    );
  }

  const evById = new Map(filteredEvents.map((e) => [String(e.event_seq), e]));
  const evCols: DataColumn[] = [
    {
      id: "seq",
      label: "Seq",
      kind: "numeric",
      align: "end",
      sortValue: (row) => evById.get(row.id)?.event_seq,
    },
    {
      id: "domain",
      label: "Domain",
      kind: "role",
      sortValue: (row) => evById.get(row.id)?.domain,
    },
    {
      id: "event",
      label: "Event Type",
      kind: "identity",
      sortValue: (row) => evById.get(row.id)?.event_type,
    },
    {
      id: "entity",
      label: "Entity",
      kind: "text",
      sortValue: (row) => {
        const ev = evById.get(row.id);
        return ev ? `${ev.entity_type}:${ev.entity_id}` : "";
      },
    },
    {
      id: "time",
      label: "Recorded",
      kind: "numeric",
      align: "end",
      sortValue: (row) => evById.get(row.id)?.recorded_at_ns,
    },
  ];
  const evRows: DataRow[] = filteredEvents.map((e) => ({
    id: String(e.event_seq),
    cells: {
      seq: <Text className="sd-tabular">#{e.event_seq}</Text>,
      domain: (
        <Badge tone="neutral" variant="outline" dot={false}>
          {e.domain}
        </Badge>
      ),
      event: e.event_type,
      entity: (
        <Text className="sd-mono">
          {e.entity_type}:{e.entity_id}
        </Text>
      ),
      time: <Text className="sd-tabular">{formatNsClock(e.recorded_at_ns)}</Text>,
    },
  }));

  const bindById = new Map(data.board_bindings.map((b) => [b.principal_id, b]));
  const bindCols: DataColumn[] = [
    {
      id: "principal",
      label: "Principal",
      kind: "identity",
      sortValue: (row) => bindById.get(row.id)?.principal_id,
    },
    {
      id: "role",
      label: "Role",
      kind: "role",
      sortValue: (row) => bindById.get(row.id)?.role_name,
    },
    {
      id: "scope",
      label: "Scope",
      kind: "text",
      sortValue: (row) => bindById.get(row.id)?.scope_ref,
    },
    {
      id: "rev",
      label: "Revision",
      kind: "numeric",
      align: "end",
      sortValue: (row) => bindById.get(row.id)?.binding_revision,
    },
    {
      id: "active",
      label: "Active",
      kind: "role",
      sortValue: (row) => (bindById.get(row.id)?.active ? 1 : 0),
    },
  ];
  const bindRows: DataRow[] = data.board_bindings.map((b) => ({
    id: b.principal_id,
    cells: {
      principal: <Text className="sd-mono">{b.principal_id}</Text>,
      role: (
        <Badge tone={b.role_name === "board_control" ? "brand" : "neutral"} dot={false}>
          {b.role_name}
        </Badge>
      ),
      scope: b.scope_ref,
      rev: <Text className="sd-tabular">{b.binding_revision}</Text>,
      active: (
        <Badge tone={b.active ? "success" : "neutral"} dot={false}>
          {b.active ? "Active" : "Revoked"}
        </Badge>
      ),
    },
  }));

  const condById = new Map(data.conditions.map((c) => [c.condition_id, c]));
  const condCols: DataColumn[] = [
    {
      id: "condition",
      label: "Condition",
      kind: "identity",
      sortValue: (row) => condById.get(row.id)?.condition_id,
    },
    {
      id: "owner",
      label: "Owner",
      kind: "text",
      sortValue: (row) => condById.get(row.id)?.owner_agent_id,
    },
    {
      id: "trigger",
      label: "Trigger",
      kind: "role",
      sortValue: (row) => condById.get(row.id)?.trigger_kind,
    },
    {
      id: "source",
      label: "Source Identity",
      kind: "text",
      sortValue: (row) => condById.get(row.id)?.source_identity ?? "",
    },
    {
      id: "due",
      label: "Due",
      kind: "numeric",
      align: "end",
      sortValue: (row) => condById.get(row.id)?.due_at_ns ?? null,
    },
    {
      id: "status",
      label: "Status",
      kind: "role",
      sortValue: (row) => condById.get(row.id)?.status,
    },
  ];
  const condRows: DataRow[] = data.conditions.map((c) => ({
    id: c.condition_id,
    cells: {
      condition: <Text className="sd-mono">{c.condition_id}</Text>,
      owner: <Text className="sd-mono">{c.owner_agent_id}</Text>,
      trigger: (
        <Badge tone="neutral" variant="outline" dot={false}>
          {c.trigger_kind}
        </Badge>
      ),
      source: <Text className="sd-mono">{c.source_identity ?? "None"}</Text>,
      due: (
        <Text className="sd-tabular">
          {c.due_at_ns === null ? "Not scheduled" : formatNsWithClock(c.due_at_ns)}
        </Text>
      ),
      status: (
        <Badge tone={c.status === "armed" ? "brand" : "neutral"} dot={false}>
          {c.status}
        </Badge>
      ),
    },
  }));

  const runtimeCard = meta ? (
    <Card
      title="Control Plane Runtime Identity"
      variant="raised"
      headerAction={<CopyButton value={meta.company_id} presentation="quiet" />}
    >
      <div className="sd-facts-wrap">
        <RecordFacts
          source="GET /v1/meta"
          asOf={`seq #${data.event_seq}`}
          facts={[
            { id: "co", label: "Company ID", value: meta.company_id },
            { id: "ch", label: "Charter Revision", value: meta.charter_revision },
            { id: "pid", label: "Daemon PID", value: String(meta.daemon_pid) },
            { id: "inc", label: "Service Incarnation", value: meta.service_incarnation_id },
            { id: "key", label: "Store Key", value: meta.store_key },
            { id: "ver", label: "Control Plane Version", value: meta.control_plane_version },
            { id: "dom", label: "Installed Domains", value: meta.active_domains.join(", ") },
          ]}
        />
      </div>
    </Card>
  ) : null;

  const bindingsCard = (
    <Card
      title="Board Principal Bindings"
      variant="raised"
      className="sd-ledger-card"
    >
      {bindRows.length === 0 ? (
        <Text variant="help">No principals bound (company is bootstrap-locked).</Text>
      ) : (
        <DataTable caption="Board bindings" columns={bindCols} rows={bindRows} />
      )}
    </Card>
  );

  const fencesCard = (
    <Card title={`Active Work Leases & Fences (${data.active_fences.length})`} variant="raised">
      {data.active_fences.length === 0 ? (
        <Text variant="help">No running or waiting work runs holding active leases.</Text>
      ) : (
        <List label="Active work leases" divided>
          {data.active_fences.map((f) => (
            <li key={f.run_id} className="yg-list-item">
              <WorkEvent
                event={{
                  id: f.run_id,
                  kind: f.status === "done" || f.status === "completed" ? "task" : "routine",
                  title: `${f.duty_kind} · ${f.agent_id}`,
                  detail: `Fence #${f.fencing_token} · Lease: ${f.lease_owner ?? "Unassigned"} · Expires: ${f.lease_expires_at_ns === null ? "No expiry" : `${formatNsClock(f.lease_expires_at_ns)} (${formatDeadlineNs(f.lease_expires_at_ns)})`}`,
                  time: f.status,
                  state: f.status === "running" ? "running" : "waiting",
                }}
              />
            </li>
          ))}
        </List>
      )}
    </Card>
  );

  const conditionsCard = (
    <Card
      title={`Armed Work Conditions (${data.conditions.length})`}
      variant="raised"
      className="sd-ledger-card"
    >
      {condRows.length === 0 ? (
        <Text variant="help">No armed work conditions registered.</Text>
      ) : (
        <DataTable caption="Armed work conditions" columns={condCols} rows={condRows} />
      )}
    </Card>
  );

  return (
    <Stack gap={16}>
      {embedded ? (
        effectiveDaemon ? (
          <>
            <div className="sd-split-equal">
              {runtimeCard}
              {fencesCard}
            </div>
            {bindingsCard}
            {conditionsCard}
          </>
        ) : null
      ) : effectiveDaemon ? (
        <>
          <Navbar
            variant="back"
            label="Back to Audit"
            onBack={() => navigate("/audit", { focusReturnId: "system-daemon-entry" })}
          />
          <SpaBreadcrumb
            focusReturnId="system-daemon-entry"
            items={[
              { id: "audit", label: "Audit", href: "/audit" },
              { id: "daemon", label: "Daemon Diagnostics", href: "/audit?tab=daemon" },
            ]}
          />
          <PageHeader
            title="Daemon Diagnostics"
            detail="Single-writer UDS runtime identity, installed domains, Board authority bindings, and active fencing leases"
          />
          <div className="sd-split-equal">
            {runtimeCard}
            {fencesCard}
          </div>
          {bindingsCard}
          {conditionsCard}
        </>
      ) : (
        <>
          <PageHeader
            title="Audit"
            detail="Append-only redacted audit stream, armed work conditions, active fencing leases, and Board authority bindings"
            action={
              <Inline gap={8} align="center">
                <Button
                  emphasis="soft"
                  size="compact"
                  shape="pill"
                  data-focus-id="system-daemon-entry"
                  onClick={() =>
                    navigate("/audit?tab=daemon", { focusReturnId: "system-daemon-entry" })
                  }
                >
                  Daemon Diagnostics
                </Button>
              </Inline>
            }
          />
          <div className="sd-split-equal">
            {runtimeCard}
            {fencesCard}
          </div>
          {bindingsCard}
          {conditionsCard}
        </>
      )}

      {!embedded || !effectiveDaemon ? (
        <Card
          title="Append-Only Redacted Audit Events"
          variant="raised"
          className="sd-ledger-card"
        >
          <Stack gap={12}>
            <Inline gap={6} wrap>
              {visibleAuditDomains.map((domain) => (
                <Button
                  key={domain}
                  size="compact"
                  shape="pill"
                  emphasis={selectedDomain === domain ? "filled" : "soft"}
                  onClick={() => setSelectedDomain(domain)}
                >
                  {domain === "all" ? "All" : domain}
                </Button>
              ))}
            </Inline>
            <FilterBar
              label="Filter audit log"
              hiddenLabel
              placeholder="Filter by domain, event type, or entity..."
              size="compact"
              query={auditQuery}
              onQueryChange={setAuditQuery}
            />
            {data.events.length === 0 ? (
              <Text variant="help">No audit events recorded yet.</Text>
            ) : evRows.length === 0 ? (
              <EmptyResult
                query={auditQuery || selectedDomain}
                onClear={() => {
                  setAuditQuery("");
                  setSelectedDomain("all");
                }}
              />
            ) : (
              <DataTable caption="Audit events" columns={evCols} rows={evRows} />
            )}
          </Stack>
        </Card>
      ) : null}
    </Stack>
  );
}
