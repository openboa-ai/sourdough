import { Badge, Button, Card, Inline, PageHeader, Result, Stack, Text } from "@openboa/yogurt";
import { Yogurt } from "@openboa/yogurt/yogurt";
import { AppShell } from "./components/AppShell";
import {
  resolveActiveDomainModules,
  resolveHomeMode,
  type DomainModuleDefinition,
} from "./domains/registry";
import { AppProvider, decodeSingleSegment, splitRoute, useApp } from "./state/store";
import { AgentDetailView } from "./views/AgentDetailView";
import { AgentsActivityView } from "./views/AgentsActivityView";
import { ApprovalsView } from "./views/ApprovalsView";
import {
  FinanceView,
  OrgReportsView,
  RestrictionsView,
  StrategiesView,
} from "./views/CompanyViews";
import { LiveTradingHomeView } from "./views/LiveTradingHomeView";
import { LiveTradingView } from "./views/LiveTradingView";
import { MessagesView } from "./views/MessagesView";
import { OrderTraceView } from "./views/OrderTraceView";
import { SettingsView } from "./views/SettingsView";
import {
  AuditHubView,
  BootstrapView,
  SystemSafetyView,
} from "./views/SystemViews";

function CompanyOverviewView({ modules }: { modules: DomainModuleDefinition[] }) {
  const { navigate, meta } = useApp();
  return (
    <Stack gap={16}>
      <PageHeader
        title="Overview"
        detail={`${modules.length} active business modules mounted on Company OS`}
      />
      <div className="sd-tier-grid">
        {modules.map((mod) => (
          <Card
            key={mod.id}
            variant="raised"
            density="compact"
            title={mod.label}
            description={mod.description}
            headerAction={
              <Badge tone="success" variant="soft" dot={false}>
                Active
              </Badge>
            }
          >
            <Inline gap={8} wrap>
              {mod.navItems.map((item) => (
                <Button
                  key={item.id}
                  emphasis="soft"
                  size="compact"
                  shape="pill"
                  onClick={() =>
                    navigate(item.path === "/" ? "/trading/live" : item.path)
                  }
                >
                  {item.label}
                </Button>
              ))}
            </Inline>
          </Card>
        ))}
      </div>
      {meta ? (
        <Text variant="meta">
          Company {meta.company_id} · Charter {meta.charter_revision}
        </Text>
      ) : null}
    </Stack>
  );
}

function RouteSwitch() {
  const { route, meta, navigate } = useApp();
  const { pathname: path } = splitRoute(route);
  const activeModules = resolveActiveDomainModules(meta?.active_domains);
  const homeMode = resolveHomeMode(activeModules);

  if (meta?.board_access.bootstrap_locked || (!meta && path === "/bootstrap")) {
    return <BootstrapView />;
  }
  if (path === "/bootstrap" || path === "/") {
    if (homeMode === "multi_overview") {
      return <CompanyOverviewView modules={activeModules} />;
    }
    if (homeMode === "core_only" && meta) {
      return <AgentsActivityView />;
    }
    return <LiveTradingHomeView />;
  }
  if (path === "/trading/live" || path === "/company/execution-tiers") {
    return <LiveTradingHomeView />;
  }
  if (path === "/trading/paper" || path === "/paper" || path === "/performance") {
    return <LiveTradingView />;
  }
  const orderId = decodeSingleSegment(path, "/orders/");
  if (orderId) {
    return <OrderTraceView orderId={orderId} />;
  }
  if (path === "/trading/strategies" || path === "/company/strategy-trials") {
    return <StrategiesView />;
  }
  if (
    path === "/approvals" ||
    decodeSingleSegment(path, "/approvals/") !== null ||
    path === "/company/approvals" ||
    decodeSingleSegment(path, "/company/approvals/") !== null
  ) {
    return <ApprovalsView />;
  }
  if (path === "/agents" || path === "/office") {
    return <AgentsActivityView />;
  }
  const agentId =
    decodeSingleSegment(path, "/agents/") ??
    decodeSingleSegment(path, "/office/agents/");
  if (agentId) {
    return <AgentDetailView agentId={agentId} />;
  }
  if (path === "/messages" || path === "/ceo") {
    return <MessagesView />;
  }
  if (path === "/finance" || path === "/company/finance") {
    return <FinanceView />;
  }
  if (path === "/reports" || path === "/company/org") {
    return <OrgReportsView />;
  }
  if (path === "/controls" || path === "/restrictions") {
    return <RestrictionsView />;
  }
  if (path === "/backups" || path === "/system/checkpoints") {
    return <SystemSafetyView />;
  }
  if (path === "/audit" || path === "/system/audit") {
    return <AuditHubView initialTab="log" />;
  }
  if (path === "/system/evaluations") {
    return <AuditHubView initialTab="evals" />;
  }
  if (path === "/system/daemon") {
    return <AuditHubView initialTab="daemon" />;
  }
  if (path === "/settings") {
    return <SettingsView />;
  }
  return (
    <Result
      title="Page not found"
      action={
        <Button emphasis="soft" shape="pill" onClick={() => navigate("/")}>
          Back to Home
        </Button>
      }
    >
      No Board Client screen matches {path}.
    </Result>
  );
}

function YogurtStage() {
  const { appearance, material } = useApp();
  return (
    <Yogurt appearance={appearance} material={material} className="sd-stage">
      <AppShell>
        <RouteSwitch />
      </AppShell>
    </Yogurt>
  );
}

export function App() {
  return (
    <AppProvider>
      <YogurtStage />
    </AppProvider>
  );
}
