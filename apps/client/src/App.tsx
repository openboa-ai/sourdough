import { useEffect } from "react";
import { Badge, Button, Card, Inline, PageHeader, Result, Skeleton, Stack, Text } from "@openboa/yogurt";
import { Yogurt } from "@openboa/yogurt/yogurt";
import { AppShell } from "./components/AppShell";
import {
  resolveActiveDomainModules,
  resolveHomeMode,
  type DomainModuleDefinition,
} from "./domains/registry";
import {
  AppProvider,
  decodeSingleSegment,
  splitRoute,
  useControlPlane,
  usePreferences,
  useRouter,
} from "./state/store";
import { AgentDetailView } from "./views/AgentDetailView";
import { AgentsActivityView } from "./views/AgentsActivityView";
import { ApprovalsView } from "./views/ApprovalsView";
import {
  FinanceView,
  OrgReportsView,
  RestrictionsView,
} from "./views/CompanyViews";
import { MessagesView } from "./views/MessagesView";
import { SettingsView } from "./views/SettingsView";
import {
  AuditHubView,
  BootstrapView,
  SystemSafetyView,
} from "./views/SystemViews";
import { OrderTraceView } from "./views/trading/OrderTraceView";
import { StrategiesView } from "./views/trading/StrategiesView";
import { TradingDeskView } from "./views/trading/TradingDeskView";

function CompanyOverviewView({ modules }: { modules: DomainModuleDefinition[] }) {
  const { navigate } = useRouter();
  const { meta } = useControlPlane();
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
  const { route, routeSearchParams, navigate } = useRouter();
  const { meta } = useControlPlane();
  const { pathname: path } = splitRoute(route);
  const activeModules = resolveActiveDomainModules(meta?.active_domains);
  const homeMode = resolveHomeMode(activeModules);
  const tradingActive = activeModules.some((m) => m.id === "trading");

  useEffect(() => {
    if (
      meta &&
      !meta.board_access.bootstrap_locked &&
      homeMode === "core_only" &&
      (path === "/" || path === "/bootstrap")
    ) {
      navigate("/agents", { replace: true });
    }
  }, [meta, homeMode, path, navigate]);

  if (meta?.board_access.bootstrap_locked || (!meta && path === "/bootstrap")) {
    return <BootstrapView />;
  }
  if (!meta) {
    return <Skeleton label="Connecting to local control plane" lines={5} />;
  }
  if (path === "/bootstrap" || path === "/") {
    if (homeMode === "multi_overview") {
      return <CompanyOverviewView modules={activeModules} />;
    }
    if (homeMode === "core_only") {
      return <AgentsActivityView />;
    }
    return <TradingDeskView mode="live" />;
  }
  if (tradingActive && path === "/trading/live") {
    return <TradingDeskView mode="live" />;
  }
  if (tradingActive && path === "/trading/paper") {
    return <TradingDeskView mode="paper" />;
  }
  const orderId = tradingActive ? decodeSingleSegment(path, "/orders/") : null;
  if (orderId) {
    return <OrderTraceView orderId={orderId} />;
  }
  if (tradingActive && path === "/trading/strategies") {
    return <StrategiesView />;
  }
  if (path === "/approvals" || decodeSingleSegment(path, "/approvals/") !== null) {
    return <ApprovalsView />;
  }
  if (path === "/agents") {
    return <AgentsActivityView />;
  }
  const agentId = decodeSingleSegment(path, "/agents/");
  if (agentId) {
    return <AgentDetailView agentId={agentId} />;
  }
  if (path === "/messages") {
    return <MessagesView />;
  }
  if (path === "/finance") {
    return <FinanceView />;
  }
  if (path === "/reports") {
    return <OrgReportsView />;
  }
  if (path === "/controls") {
    return <RestrictionsView />;
  }
  if (path === "/backups") {
    return <SystemSafetyView />;
  }
  if (path === "/audit") {
    const rawTab = routeSearchParams.get("tab");
    const initialTab =
      rawTab === "evals" || rawTab === "daemon" || rawTab === "log" ? rawTab : "log";
    return <AuditHubView initialTab={initialTab} />;
  }
  if (path === "/settings") {
    return <SettingsView />;
  }
  return (
    <Result
      title="Page not found"
      action={
        <Button
          emphasis="soft"
          shape="pill"
          onClick={() => navigate(homeMode === "core_only" ? "/agents" : "/")}
        >
          Back to Home
        </Button>
      }
    >
      No Board Client screen matches {path}.
    </Result>
  );
}

function YogurtStage() {
  const { appearance, material } = usePreferences();
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
