import {
  Badge,
  Button,
  Card,
  Inline,
  List,
  PageHeader,
  RecordFacts,
  SegmentedControl,
  Stack,
  Surface,
  Text,
  type Appearance,
  type Material,
} from "@openboa/yogurt";
import {
  REGISTERED_DOMAIN_MODULES,
  resolveActiveDomainModules,
} from "../domains/registry";
import { useApp } from "../state/store";

export function SettingsView() {
  const {
    meta,
    lastEventSeq,
    appearance,
    setAppearance,
    material,
    setMaterial,
    setHostLocked,
    pushToast,
    navigate,
  } = useApp();

  const activeModules = resolveActiveDomainModules(meta?.active_domains);
  const activeIds = new Set(activeModules.map((m) => m.id));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Settings"
        detail="Board Client appearance, installed business modules, RBAC session identity, and privacy lock"
      />

      <div className="sd-split-equal">
        <Card
          title="Appearance & Surface"
          description="Local client preferences persisted in this browser"
          variant="raised"
        >
          <Stack gap={16}>
            <Stack gap={6}>
              <Text variant="meta">Appearance</Text>
              <SegmentedControl
                label="Board Client appearance"
                value={appearance}
                onValueChange={(next) => {
                  setAppearance(next as Appearance);
                  pushToast(`Switched appearance to ${next}.`);
                }}
                options={[
                  { id: "light", label: "Light" },
                  { id: "dark", label: "Dark" },
                  { id: "auto", label: "Auto" },
                ]}
              />
            </Stack>

            <Stack gap={6}>
              <Text variant="meta">Surface Material</Text>
              <SegmentedControl
                label="Surface material"
                value={material}
                onValueChange={(next) => {
                  setMaterial(next as Material);
                  pushToast(`Surface material set to ${next}.`);
                }}
                options={[
                  { id: "neutral", label: "Neutral" },
                  { id: "paper", label: "Paper" },
                  { id: "mist", label: "Mist" },
                ]}
              />
            </Stack>
          </Stack>
        </Card>

        <Card
          title="Host Session & Privacy"
          description="Local single-writer UDS daemon binding and privacy shield"
          variant="raised"
          headerAction={
            <Button
              emphasis="soft"
              size="compact"
              shape="pill"
              onClick={() => setHostLocked(true)}
            >
              Lock Screen
            </Button>
          }
        >
          {meta ? (
            <div className="sd-facts-wrap">
              <RecordFacts
                source="GET /v1/meta"
                asOf={`seq #${lastEventSeq}`}
                facts={[
                  { id: "co", label: "Company ID", value: meta.company_id },
                  { id: "charter", label: "Charter Revision", value: meta.charter_revision },
                  {
                    id: "principal",
                    label: "Board Principal",
                    value: meta.board_access.principal_id ?? "Unbound",
                  },
                  {
                    id: "role",
                    label: "Board Role",
                    value: meta.board_access.role ?? "unbound",
                  },
                  { id: "pid", label: "Daemon PID", value: String(meta.daemon_pid) },
                  {
                    id: "domains",
                    label: "Active Domains",
                    value: meta.active_domains.join(", ") || "core",
                  },
                ]}
              />
            </div>
          ) : (
            <Text variant="help">Connecting to local control plane...</Text>
          )}
        </Card>
      </div>

      <Card
        title="Installed Business Modules"
        description="Industry-specific workload modules mounted dynamically above the core Company OS"
        variant="raised"
      >
        <List label="Registered business modules" divided>
          {REGISTERED_DOMAIN_MODULES.map((mod) => {
            const installed = activeIds.has(mod.id);
            return (
              <li key={mod.id} className="yg-list-item sd-list-row-padded">
                <Surface variant="surface" density="compact">
                  <Inline justify="between" align="center" gap={12} wrap>
                    <Stack gap={4}>
                      <Inline gap={8} align="center">
                        <Text variant="body">{mod.label}</Text>
                        <Badge tone={installed ? "success" : "neutral"} dot={false}>
                          {installed ? "Active" : "Not Installed"}
                        </Badge>
                        <Text variant="meta" className="sd-mono">
                          domain:{mod.domainKey}
                        </Text>
                      </Inline>
                      <Text variant="help">{mod.description}</Text>
                    </Stack>
                    {installed ? (
                      <Button
                        emphasis="quiet"
                        size="compact"
                        shape="pill"
                        onClick={() => navigate(mod.defaultPath)}
                      >
                        Open Module
                      </Button>
                    ) : null}
                  </Inline>
                </Surface>
              </li>
            );
          })}
        </List>
      </Card>
    </Stack>
  );
}
