import { AppShell } from "./components/AppShell";
import { AppProvider, useApp } from "./state/store";
import { AgentDetailView } from "./views/AgentDetailView";
import { AgentsActivityView } from "./views/AgentsActivityView";
import { ApprovalsView } from "./views/ApprovalsView";
import {
  FinanceView,
  OrgReportsView,
  RestrictionsView,
  StrategiesView,
} from "./views/CompanyViews";
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
  const { route, meta } = useApp();
  const path = route.includes("?") ? route.slice(0, route.indexOf("?")) : route;

  if (meta?.board_access.bootstrap_locked) {
    return <BootstrapView />;
  }

  if (path === "/bootstrap") {
    return <BootstrapView />;
  }
  if (path === "/" || path === "/performance") {
    return <LiveTradingView />;
  }
  if (path.startsWith("/orders/")) {
    const orderId = decodeURIComponent(path.slice("/orders/".length));
    return <OrderTraceView orderId={orderId} />;
  }
  if (path === "/office") {
    return <AgentsActivityView />;
  }
  if (path.startsWith("/office/agents/")) {
    const agentId = decodeURIComponent(path.slice("/office/agents/".length));
    return <AgentDetailView agentId={agentId} />;
  }
  if (path === "/messages" || path === "/ceo") {
    return <MessagesView />;
  }
  if (path === "/company/approvals") {
    return <ApprovalsView />;
  }
  if (path === "/company/strategy-trials") {
    return <StrategiesView />;
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
  return <LiveTradingView />;
}

export function App() {
  return (
    <AppProvider>
      <AppShell>
        <RouteSwitch />
      </AppShell>
    </AppProvider>
  );
}
