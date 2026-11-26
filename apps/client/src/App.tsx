import { Button, Result } from "@openboa/yogurt";
import { Yogurt } from "@openboa/yogurt/yogurt";
import { AppShell } from "./components/AppShell";
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
import { ExecutionTiersView } from "./views/ExecutionTiersView";
import { LiveTradingView } from "./views/LiveTradingView";
import { MessagesView } from "./views/MessagesView";
import { OrderTraceView } from "./views/OrderTraceView";
import {
  BootstrapView,
  SystemAuditView,
  SystemEvalsView,
  SystemSafetyView,
} from "./views/SystemViews";

function RouteSwitch() {
  const { route, meta, navigate } = useApp();
  const { pathname: path } = splitRoute(route);

  if (meta?.board_access.bootstrap_locked || (!meta && path === "/bootstrap")) {
    return <BootstrapView />;
  }
  if (path === "/bootstrap") {
    return <LiveTradingView />;
  }
  if (path === "/" || path === "/performance") {
    return <LiveTradingView />;
  }
  const orderId = decodeSingleSegment(path, "/orders/");
  if (orderId) {
    return <OrderTraceView orderId={orderId} />;
  }
  if (path === "/office") {
    return <AgentsActivityView />;
  }
  const agentId = decodeSingleSegment(path, "/office/agents/");
  if (agentId) {
    return <AgentDetailView agentId={agentId} />;
  }
  if (path === "/messages" || path === "/ceo") {
    return <MessagesView />;
  }
  if (
    path === "/company/approvals" ||
    decodeSingleSegment(path, "/company/approvals/") !== null
  ) {
    return <ApprovalsView />;
  }
  if (path === "/company/strategy-trials") {
    return <StrategiesView />;
  }
  if (path === "/company/execution-tiers") {
    return <ExecutionTiersView />;
  }
  if (path === "/company/finance") {
    return <FinanceView />;
  }
  if (path === "/company/org") {
    return <OrgReportsView />;
  }
  if (path === "/restrictions") {
    return <RestrictionsView />;
  }
  if (path === "/system/checkpoints") {
    return <SystemSafetyView />;
  }
  if (path === "/system/evaluations") {
    return <SystemEvalsView />;
  }
  if (path === "/system/audit" || path === "/system/daemon") {
    return <SystemAuditView />;
  }
  return (
    <Result
      title="Page not found"
      action={
        <Button emphasis="soft" shape="pill" onClick={() => navigate("/")}>
          Back to Live Trading
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
