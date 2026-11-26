import { useEffect, useMemo, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import { TaskList } from "@openboa/yogurt/motion-task-list";
import {
  Badge,
  Button,
  Card,
  DataTable,
  DisclosureRow,
  EmptyResult,
  FilterBar,
  Inline,
  List,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Skeleton,
  Stack,
  Text,
  TextField,
  WorkEvent,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatNsClock,
  formatRelativeNs,
  newCommandId,
} from "../api/client";
import type {
  SystemAuditProjection,
  SystemEvalsProjection,
  SystemSafetyProjection,
} from "../api/types";
import { ControlledInlineConfirm } from "../components/ControlledInlineConfirm";
import { useApp } from "../state/store";

export function SystemSafetyView() {
  const { navigate, refreshSeq, meta, canWrite, dispatchCommand, reportApiError } = useApp();
  const [data, setData] = useState<SystemSafetyProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [cutConfirmOpen, setCutConfirmOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<SystemSafetyProjection>("system-safety", {
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

  if (loading && !data) return <Skeleton label="Loading safety and checkpoints" lines={5} />;
  if (!data) return <Result title="Safety & Restore unavailable">Could not load safety state.</Result>;

  const tasks = data.safe_custody_hold.map((step) => ({
    id: step.id,
    label: step.label,
    state: (step.status === "done"
      ? "done"
      : step.status === "active"
        ? "running"
        : "queued") as "done" | "running" | "queued",
  }));

  const cpCols: DataColumn[] = [
    { id: "name", label: "Archive", kind: "identity" },
    { id: "size", label: "Size", kind: "numeric", align: "end" },
    { id: "modified", label: "Recorded", kind: "numeric", align: "end" },
  ];
  const cpRows: DataRow[] = data.checkpoints.map((cp) => ({
    id: cp.archive_name,
    cells: {
      name: <Text className="sd-mono">{cp.archive_name}</Text>,
      size: <Text className="sd-tabular">{(cp.size_bytes / 1024).toFixed(1)} KB</Text>,
      modified: <Text className="sd-tabular">{formatRelativeNs(cp.modified_ns)}</Text>,
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Safety, Checkpoints & Restore"
        detail="Verified SQLite checkpoint archives, 5-step Safe Custody Hold, and active restrictions"
        action={
          <Inline gap={8} align="center">
            <Button
              emphasis="soft"
              size="compact"
              shape="pill"
              onClick={() => navigate("/restrictions")}
            >
              Active Restrictions ({data.active_restrictions.length})
            </Button>
            <Button
              emphasis="filled"
              tone="brand"
              size="compact"
              shape="pill"
              disabled={!canWrite}
              onClick={() => setCutConfirmOpen((v) => !v)}
            >
              Cut Verified Checkpoint
            </Button>
          </Inline>
        }
      />

      {meta ? (
        <ControlledInlineConfirm
          open={cutConfirmOpen}
          onCancel={() => setCutConfirmOpen(false)}
          decision={{
            id: `cut-checkpoint-${data.company_id}`,
            revision: data.charter_revision,
            title: "Cut Verified Company Checkpoint",
            consequence:
              "Creates a checksummed sourdough-checkpoint-v2 archive under the company's private checkpoints directory without interrupting read projections.",
            confirmLabel: "Cut Verified Checkpoint",
            destructive: false,
            available: canWrite,
            requireReason: true,
            defaultReason: "Scheduled Board backup checkpoint",
            whatChanges: [
              { label: "Archive directory", value: "New verified .zip checkpoint created" },
              { label: "Audit log", value: "Records ops.checkpoint_cut with SHA-256" },
            ],
            whatStaysUntouched: [
              { label: "Live company store", value: "Zero mutations to positions or agents" },
              { label: "Active restrictions", value: "Unchanged" },
            ],
            technicalDetails: [
              { label: "command_type", value: "cut_checkpoint" },
              { label: "contract_version", value: "checkpoint-command-v1" },
              { label: "charter_revision", value: data.charter_revision },
            ],
          }}
          onConfirm={({ reason }) => {
            void dispatchCommand({
              company_id: meta.company_id,
              command_id: newCommandId("ckpt"),
              command_type: "cut_checkpoint",
              contract_version: "checkpoint-command-v1",
              preconditions: { charter_revision: data.charter_revision },
              payload: { reason },
              label: "Cut verified company checkpoint",
              sent_ns: Date.now() * 1_000_000,
            });
          }}
        />
      ) : null}

      <div className="sd-split-equal">
        <Card title="Safe Custody Hold (5-Step Readiness)" variant="raised">
          <TaskList label="Safe Custody Hold checklist" tasks={tasks} />
        </Card>

        <Card title="Verified Checkpoint Archives" variant="raised">
          {cpRows.length === 0 ? (
            <Text variant="help">No checkpoints cut yet. Cut one before major charter changes.</Text>
          ) : (
            <DataTable caption="Company checkpoints" columns={cpCols} rows={cpRows} />
          )}
        </Card>
      </div>
    </Stack>
  );
}

export function SystemEvalsView() {
  const { refreshSeq, reportApiError } = useApp();
  const [data, setData] = useState<SystemEvalsProjection | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<SystemEvalsProjection>("system-evals", {
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

  if (loading && !data) return <Skeleton label="Loading evaluations and tool lineage" lines={5} />;
  if (!data) return <Result title="Evaluations unavailable">Could not load evaluation state.</Result>;

  const qualCols: DataColumn[] = [
    { id: "receipt", label: "Qualification", kind: "identity" },
    { id: "candidate", label: "Candidate", kind: "text" },
    { id: "trials", label: "Pass^k Trials", kind: "numeric", align: "end" },
    { id: "verdict", label: "Result", kind: "role" },
  ];
  const qualRows: DataRow[] = data.qualifications.map((q) => ({
    id: q.receipt_id,
    cells: {
      receipt: <Text className="sd-mono">{q.receipt_id}</Text>,
      candidate: q.candidate_asset_id,
      trials: <Text className="sd-tabular">{q.trial_count}</Text>,
      verdict: (
        <Badge tone={q.passed ? "success" : "danger"} dot={false}>
          {q.passed ? "Passed" : "Rejected"}
        </Badge>
      ),
    },
  }));

  const suiteCols: DataColumn[] = [
    { id: "rev", label: "Suite Revision", kind: "identity" },
    { id: "obs", label: "Observations", kind: "numeric", align: "end" },
    { id: "grades", label: "Blind Grades", kind: "numeric", align: "end" },
    { id: "pairs", label: "Pairwise Swaps", kind: "numeric", align: "end" },
  ];
  const suiteRows: DataRow[] = data.offline_evaluator.suites.map((s) => ({
    id: s.revision,
    cells: {
      rev: <Text className="sd-mono">{s.revision}</Text>,
      obs: <Text className="sd-tabular">{s.observation_count}</Text>,
      grades: <Text className="sd-tabular">{s.grade_count}</Text>,
      pairs: <Text className="sd-tabular">{s.pairwise_grade_count}</Text>,
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Evaluations & Tool Lineage"
        detail="Non-self-evaluated pass^3 tool qualifications, DAG blast-radius lineage, and isolated offline suites"
      />

      <Notice title="Isolated Offline Evaluator Boundary" tone="neutral">
        {data.offline_evaluator.isolated_notice}
      </Notice>

      <div className="sd-split-equal">
        <Card title="Review Tool Qualifications (pass^k)" variant="raised">
          {qualRows.length === 0 ? (
            <Text variant="help">No review tool qualifications recorded yet.</Text>
          ) : (
            <DataTable caption="Review qualifications" columns={qualCols} rows={qualRows} />
          )}
        </Card>

        <Card title="Tool Activation DAG & Quarantine Blast Radius" variant="raised">
          {data.activations.length === 0 ? (
            <Text variant="help">No review tool activations recorded yet.</Text>
          ) : (
            <Stack gap={12}>
              {data.activations.map((act) => (
                <Card
                  key={act.request_id}
                  variant="outlined"
                  density="compact"
                  title={`${act.candidate_id} (rev ${act.revision})`}
                  description={`Operation: ${act.operation} · Parent: ${act.parent_asset_id ?? "root"}`}
                  headerAction={
                    <Badge tone={act.quarantined ? "danger" : "success"} dot={false}>
                      {act.quarantined ? "Quarantined" : "Active"}
                    </Badge>
                  }
                >
                  <Stack gap={4}>
                    <Text variant="meta">
                      Transitive descendants:{" "}
                      {act.blast_radius_descendants.length > 0
                        ? act.blast_radius_descendants.join(", ")
                        : "None"}
                    </Text>
                    <Text variant="meta">
                      Fallback target: {act.fallback_candidate_id ?? "No change to active tool"}
                    </Text>
                  </Stack>
                </Card>
              ))}
            </Stack>
          )}
        </Card>
      </div>

      <Card
        title="Offline Blind Trial Suites (Separate SQLite DB)"
        description={
          data.offline_evaluator.connected
            ? "Connected read-only to external offline evaluator store"
            : "No offline evaluator store attached to this daemon"
        }
        variant="raised"
      >
        {suiteRows.length === 0 ? (
          <Text variant="help">
            Pass --evaluator-store &lt;offline.sqlite&gt; to serve-company-daemon to inspect
            isolated blind evaluation suites here.
          </Text>
        ) : (
          <DataTable caption="Offline trial suites" columns={suiteCols} rows={suiteRows} />
        )}
      </Card>
    </Stack>
  );
}

export function SystemAuditView() {
  const { refreshSeq, meta, reportApiError } = useApp();
  const [data, setData] = useState<SystemAuditProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [auditQuery, setAuditQuery] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<SystemAuditProjection>("system-audit", {
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

  const filteredEvents = useMemo(() => {
    const events = data?.events ?? [];
    const q = auditQuery.trim().toLowerCase();
    if (!q) return events;
    return events.filter((e) =>
      `${e.event_seq} ${e.domain} ${e.event_type} ${e.entity_type} ${e.entity_id}`
        .toLowerCase()
        .includes(q)
    );
  }, [data?.events, auditQuery]);

  if (loading && !data) return <Skeleton label="Loading audit log and leases" lines={5} />;
  if (!data) return <Result title="Audit Log unavailable">Could not load audit trail.</Result>;

  const evCols: DataColumn[] = [
    { id: "seq", label: "Seq", kind: "numeric", align: "end" },
    { id: "domain", label: "Domain", kind: "role" },
    { id: "event", label: "Event Type", kind: "identity" },
    { id: "entity", label: "Entity", kind: "text" },
    { id: "time", label: "Recorded", kind: "numeric", align: "end" },
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

  const bindCols: DataColumn[] = [
    { id: "principal", label: "Principal", kind: "identity" },
    { id: "role", label: "Role", kind: "role" },
    { id: "scope", label: "Scope", kind: "text" },
    { id: "rev", label: "Revision", kind: "numeric", align: "end" },
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
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Audit Log & System Diagnostics"
        detail="Append-only redacted audit stream, active fencing leases, and Board authority bindings"
      />

      {meta ? (
        <Card
          title="Control Plane Runtime Identity"
          variant="raised"
          headerAction={<CopyButton value={meta.company_id} presentation="quiet" />}
        >
          <RecordFacts
            source="GET /v1/meta"
            asOf={`seq #${data.event_seq}`}
            facts={[
              { id: "co", label: "Company ID", value: meta.company_id },
              { id: "ch", label: "Charter Revision", value: meta.charter_revision },
              { id: "pid", label: "Daemon PID", value: String(meta.daemon_pid) },
              { id: "inc", label: "Service Incarnation", value: meta.service_incarnation_id },
              { id: "dom", label: "Installed Domains", value: meta.active_domains.join(", ") },
            ]}
          />
        </Card>
      ) : null}

      <div className="sd-split-equal">
        <Card title="Board Principal Bindings" variant="raised">
          {bindRows.length === 0 ? (
            <Text variant="help">No principals bound (company is bootstrap-locked).</Text>
          ) : (
            <DataTable caption="Board bindings" columns={bindCols} rows={bindRows} />
          )}
        </Card>

        <Card title={`Active Work Leases & Fences (${data.active_fences.length})`} variant="raised">
          {data.active_fences.length === 0 ? (
            <Text variant="help">No running or waiting work runs holding active leases.</Text>
          ) : (
            <List label="Active work leases" divided>
              {data.active_fences.map((f) => (
                <WorkEvent
                  key={f.run_id}
                  event={{
                    id: f.run_id,
                    kind: "task",
                    title: `${f.duty_kind} · ${f.agent_id}`,
                    detail: `Fence #${f.fencing_token}`,
                    state: f.status === "running" ? "running" : "waiting",
                  }}
                />
              ))}
            </List>
          )}
        </Card>
      </div>

      <Card title="Append-Only Redacted Audit Events" variant="raised">
        <Stack gap={12}>
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
            <EmptyResult query={auditQuery} onClear={() => setAuditQuery("")} />
          ) : (
            <DataTable caption="Audit events" columns={evCols} rows={evRows} />
          )}
        </Stack>
      </Card>
    </Stack>
  );
}

export function BootstrapView() {
  const { meta, controlsLocked, dispatchCommand } = useApp();
  const [principalId, setPrincipalId] = useState("board-owner-1");
  const [proof, setProof] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);

  if (!meta) return <Skeleton label="Loading bootstrap status" lines={4} />;

  const challenge = meta.board_access.bootstrap_challenge ?? "";

  return (
    <Stack gap={16}>
      <PageHeader
        title="Register Initial Board Owner"
        detail="Bootstrap lock is active until an authenticated local Board owner binds authority"
      />

      <Notice title="Bootstrap Locked" tone="warning" urgent>
        No Board principal is bound to this company yet. Verify the server challenge below and
        compute SHA-256(&quot;&lt;challenge&gt;:&lt;company_id&gt;&quot;) to claim board_control.
      </Notice>

      <Card
        title="Owner Bootstrap Challenge"
        variant="raised"
        headerAction={
          challenge ? <CopyButton value={challenge} presentation="quiet" /> : undefined
        }
      >
        <Stack gap={12}>
          <RecordFacts
            source="GET /v1/meta (bootstrap_locked)"
            asOf="One-time setup"
            facts={[
              { id: "co", label: "Company ID", value: meta.company_id },
              { id: "ch", label: "OwnerChallenge", value: challenge || "Unavailable" },
            ]}
          />

          <TextField
            label="Board Principal ID"
            value={principalId}
            onChange={(e) => setPrincipalId(e.target.value)}
            placeholder="board-owner-1"
          />

          <div data-agent-secret="">
            <TextField
              label="OwnerProof (hex SHA-256)"
              value={proof}
              onChange={(e) => setProof(e.target.value)}
              placeholder="Paste computed 64-char hex proof"
              help="Redacted from agent snapshots via data-agent-secret."
            />
          </div>

          <Inline justify="end">
            <Button
              emphasis="filled"
              tone="brand"
              shape="pill"
              disabled={!principalId.trim() || !proof.trim()}
              onClick={() => setConfirmOpen(true)}
            >
              Review Owner Binding
            </Button>
          </Inline>

          <ControlledInlineConfirm
            open={confirmOpen}
            onCancel={() => setConfirmOpen(false)}
            decision={{
              id: `register-owner-${meta.company_id}`,
              revision: `bootstrap:${challenge}`,
              title: `Bind ${principalId} as Initial Board Owner`,
              consequence:
                "Unlocks Board governance and control actions for this principal and permanently closes one-time bootstrap registration.",
              confirmLabel: "Register Board Owner",
              destructive: false,
              available: !controlsLocked && Boolean(principalId.trim() && proof.trim() && challenge),
              whatChanges: [
                { label: "Board role", value: `${principalId} bound as board_control` },
                { label: "Bootstrap gate", value: "Permanently closed" },
              ],
              whatStaysUntouched: [
                { label: "Company charter", value: meta.charter_revision },
                { label: "Paper boundary", value: "Strictly Paper environment" },
              ],
              technicalDetails: [
                { label: "command_type", value: "register_owner" },
                { label: "contract_version", value: "rbac-command-v1" },
                { label: "company_id", value: meta.company_id },
              ],
            }}
            onConfirm={() => {
              void dispatchCommand({
                company_id: meta.company_id,
                command_id: newCommandId("owner"),
                command_type: "register_owner",
                contract_version: "rbac-command-v1",
                preconditions: { bootstrap_state: "locked" },
                payload: {
                  principal_id: principalId.trim(),
                  challenge,
                  proof: proof.trim(),
                },
                label: `Register Board owner (${principalId.trim()})`,
                sent_ns: Date.now() * 1_000_000,
              });
            }}
          />

          <DisclosureRow label="Technical details" initiallyOpen={false}>
            <RecordFacts
              source="Store identity"
              asOf="Local daemon"
              facts={[
                { id: "key", label: "Store key", value: meta.store_key },
                { id: "ch_rev", label: "Charter revision", value: meta.charter_revision },
              ]}
            />
          </DisclosureRow>
        </Stack>
      </Card>
    </Stack>
  );
}
