import { useEffect, useState, type ReactNode } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Button,
  Command,
  Divider,
  Icon,
  IconButton,
  Inline,
  Link,
  Notice,
  Popover,
  PopoverDescription,
  PopoverTitle,
  ProjectMark,
  RecordFacts,
  Result,
  SegmentedControl,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarHeader,
  SidebarItem,
  SidebarLayout,
  SidebarTrigger,
  Stack,
  Status,
  Text,
  ToastStack,
  type Material,
} from "@openboa/yogurt";
import { newCommandId } from "../api/client";
import { useApp } from "../state/store";
import { CommandOutboxPanel } from "./CommandOutboxPanel";
import { ControlledInlineConfirm } from "./ControlledInlineConfirm";

function pathnameOf(route: string): string {
  const idx = route.indexOf("?");
  return idx === -1 ? route : route.slice(0, idx);
}

export function AppShell({ children }: { children: ReactNode }) {
  const {
    route,
    navigate,
    meta,
    linkState,
    lastError,
    lastEventSeq,
    hostLocked,
    setHostLocked,
    canWrite,
    dispatchCommand,
    forceRefresh,
    appearance,
    setAppearance,
    material,
    setMaterial,
    toasts,
    pushToast,
    dismissToast,
  } = useApp();

  const [safetyStripOpen, setSafetyStripOpen] = useState(false);

  const path = pathnameOf(route);
  const paperActive = meta?.active_domains.includes("paper") ?? false;
  const paperRiskSuspended = Boolean(meta?.paper_risk_suspended);
  const activeRestrictions = meta?.active_restrictions_count ?? 0;
  const isAnyPaused = paperRiskSuspended || activeRestrictions > 0;
  const pendingApprovals = meta?.pending_approvals_count ?? 0;
  const isDark = appearance === "dark";

  const statusTone =
    linkState === "live"
      ? isAnyPaused
        ? "warning"
        : "success"
      : linkState === "connecting"
        ? "neutral"
        : "danger";

  const statusLabel =
    linkState === "live"
      ? isAnyPaused
        ? `Paused · seq #${lastEventSeq}`
        : `Live · seq #${lastEventSeq}`
      : linkState === "stale"
        ? "Reconnecting"
        : linkState === "unauthorized"
          ? "Access denied"
          : "Connecting";

  const canResumePaper = Boolean(
    meta?.supported_commands?.resume_new_risk?.includes("paper-risk-command-v1")
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "k") return;
      const trigger = document.querySelector<HTMLButtonElement>(".sd-command-slot button");
      if (trigger) {
        event.preventDefault();
        trigger.click();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const commandItems = [
    { id: "nav:/", label: "Live Trading & Portfolio", hint: "Core", icon: "chart" as const, shortcut: "G L" },
    { id: "nav:/office", label: "Agents & Activity Graph", hint: "Core", icon: "workflow" as const, shortcut: "G A" },
    { id: "nav:/messages", label: "Company Messages (Founder · CEO)", hint: "Core", icon: "chat" as const, shortcut: "G M" },
    {
      id: "nav:/company/approvals",
      label: pendingApprovals > 0 ? `Board Approvals (${pendingApprovals} pending)` : "Board Approvals",
      hint: "Company",
      icon: "check" as const,
    },
    { id: "nav:/company/strategy-trials", label: "Strategies & Challenger Trials", hint: "Company", icon: "sliders" as const },
    { id: "nav:/company/finance", label: "Finance & Operating Costs", hint: "Company", icon: "bookmark" as const },
    { id: "nav:/company/org", label: "Organization & Published Reports", hint: "Company", icon: "person" as const },
    { id: "nav:/system/checkpoints", label: "Safety, Checkpoints & Restore", hint: "System", icon: "clock" as const },
    { id: "nav:/system/evaluations", label: "Evaluations & Tool Lineage", hint: "System", icon: "puzzle" as const },
    { id: "nav:/system/audit", label: "Audit Log & Diagnostics", hint: "System", icon: "folder" as const },
    {
      id: "action:theme",
      label: isDark ? "Switch to Light Appearance" : "Switch to Dark Appearance",
      hint: "Yogurt",
      icon: (isDark ? "sun" : "moon") as "sun" | "moon",
    },
    { id: "action:lock", label: "Lock Board Client Screen", hint: "Privacy", icon: "close" as const },
  ];

  const handleCommandSelect = (id: string) => {
    if (id.startsWith("nav:")) {
      navigate(id.slice("nav:".length));
    } else if (id === "action:theme") {
      const next = isDark ? "light" : "dark";
      setAppearance(next);
      pushToast(`Switched appearance to ${next}.`);
    } else if (id === "action:lock") {
      setHostLocked(true);
    }
  };

  const navigation = (
    <>
      <SidebarHeader>
        <Inline gap={8} align="center" wrap={false}>
          <ProjectMark project="sourdough" size={26} label="Sourdough" />
          <Stack gap={2}>
            <Text className="sd-brand-title">Sourdough</Text>
            <Text variant="meta">Board Client</Text>
          </Stack>
        </Inline>
      </SidebarHeader>

      <SidebarContent label="Board navigation">
        <SidebarGroup label="Core">
          <SidebarItem
            label="Live Trading"
            icon={<Icon name="chart" />}
            active={path === "/" || path.startsWith("/orders/") || path === "/performance"}
            onSelect={() => navigate("/")}
          />
          <SidebarItem
            label="Agents & Activity"
            icon={<Icon name="workflow" />}
            active={path === "/office" || path.startsWith("/office/")}
            onSelect={() => navigate("/office")}
          />
          <SidebarItem
            label="Messages"
            icon={<Icon name="chat" />}
            active={path === "/messages" || path === "/ceo"}
            onSelect={() => navigate("/messages")}
          />
        </SidebarGroup>

        <SidebarGroup label="Company">
          <SidebarItem
            label="Approvals"
            icon={<Icon name="check" />}
            active={path === "/company/approvals"}
            badge={
              pendingApprovals > 0 ? (
                <Badge tone="brand" dot={false}>
                  {pendingApprovals}
                </Badge>
              ) : undefined
            }
            onSelect={() => navigate("/company/approvals")}
          />
          <SidebarItem
            label="Strategies"
            icon={<Icon name="sliders" />}
            active={path === "/company/strategy-trials"}
            onSelect={() => navigate("/company/strategy-trials")}
          />
          <SidebarItem
            label="Finance & Costs"
            icon={<Icon name="bookmark" />}
            active={path === "/company/finance"}
            onSelect={() => navigate("/company/finance")}
          />
          <SidebarItem
            label="Org & Reports"
            icon={<Icon name="person" />}
            active={path === "/company/org"}
            onSelect={() => navigate("/company/org")}
          />
        </SidebarGroup>

        <Divider />

        <SidebarGroup label="System">
          <SidebarItem
            label="Safety & Restore"
            icon={<Icon name="clock" />}
            active={path === "/system/checkpoints" || path === "/restrictions"}
            badge={
              (meta?.active_restrictions_count ?? 0) > 0 ? (
                <Badge tone="warning" dot={false}>
                  {meta?.active_restrictions_count}
                </Badge>
              ) : undefined
            }
            onSelect={() => navigate("/system/checkpoints")}
          />
          <SidebarItem
            label="Evaluations"
            icon={<Icon name="puzzle" />}
            active={path === "/system/evaluations"}
            onSelect={() => navigate("/system/evaluations")}
          />
          <SidebarItem
            label="Audit Log"
            icon={<Icon name="folder" />}
            active={path === "/system/audit"}
            onSelect={() => navigate("/system/audit")}
          />
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <Inline justify="between" align="center" className="sd-w-full">
          <Text variant="meta">
            {meta?.board_access.role === "board_control"
              ? "Board Control"
              : meta?.board_access.role === "board_viewer"
                ? "Read-Only Viewer"
                : meta?.board_access.bootstrap_locked
                  ? "Bootstrap Locked"
                  : "Unbound"}
          </Text>
          {paperActive ? (
            <Badge tone="neutral" variant="outline" dot={false}>
              Paper
            </Badge>
          ) : null}
        </Inline>
      </SidebarFooter>
    </>
  );

  return (
    <>
      {hostLocked ? (
        <div
          className="sd-privacy-shield"
          role="dialog"
          aria-modal="true"
          aria-label="Session locked"
          data-agent-hidden=""
        >
          <Result
            title="Board Client locked"
            action={
              <Button
                emphasis="filled"
                tone="brand"
                shape="pill"
                onClick={() => {
                  setHostLocked(false);
                  forceRefresh();
                }}
              >
                Unlock Board Client
              </Button>
            }
          >
            Company telemetry and controls are concealed while locked. Your last observed
            sequence (seq #{lastEventSeq}) is preserved safely.
          </Result>
        </div>
      ) : (
        <SidebarLayout
          label="Sourdough Board Client"
          navigation={navigation}
          width="16.75rem"
          compactAt={860}
          variant="flat"
          collapse="hide"
        >
          <header className="sd-topbar">
            <Inline gap={8} align="center" wrap={false}>
              <SidebarTrigger label="Toggle sidebar" size="compact" />
              <Link
                href="/"
                onClick={(e) => {
                  e.preventDefault();
                  navigate("/");
                }}
              >
                <Text variant="body">
                  {meta ? `Company ${meta.company_id.slice(0, 12)}` : "Sourdough"}
                </Text>
              </Link>
              {paperActive ? (
                <Badge tone="brand" variant="soft" dot={false}>
                  Paper
                </Badge>
              ) : null}
            </Inline>

            <Inline gap={8} align="center" wrap={false}>
              <span className="sd-command-slot">
                <Command
                  label="⌘K"
                  title="Board Client Command"
                  triggerText="⌘K"
                  items={commandItems}
                  onSelect={handleCommandSelect}
                />
              </span>

              <IconButton
                size="compact"
                emphasis="quiet"
                icon={isDark ? "sun" : "moon"}
                label={isDark ? "Switch to light appearance" : "Switch to dark appearance"}
                onClick={() => {
                  const next = isDark ? "light" : "dark";
                  setAppearance(next);
                  pushToast(`Switched appearance to ${next}.`);
                }}
              />

              <Popover
                label="Control plane diagnostics"
                align="end"
                trigger={
                  <Button emphasis="quiet" size="compact" shape="pill">
                    <Status tone={statusTone}>{statusLabel}</Status>
                  </Button>
                }
              >
                <Stack gap={12}>
                  <PopoverTitle>Control Plane & Surface</PopoverTitle>
                  <PopoverDescription>
                    Single-writer local UDS daemon telemetry and Yogurt surface controls.
                  </PopoverDescription>
                  {meta ? (
                    <Stack gap={8}>
                      <RecordFacts
                        source="GET /v1/meta"
                        asOf={`seq #${lastEventSeq}`}
                        facts={[
                          { id: "co", label: "Company", value: meta.company_id },
                          { id: "ch", label: "Charter", value: meta.charter_revision },
                          { id: "pid", label: "Daemon PID", value: String(meta.daemon_pid) },
                          { id: "key", label: "Store key", value: meta.store_key },
                          {
                            id: "role",
                            label: "Board role",
                            value: meta.board_access.role ?? "unbound",
                          },
                        ]}
                      />
                      <Inline justify="between" align="center">
                        <Text variant="meta">Copy company ID</Text>
                        <CopyButton value={meta.company_id} presentation="quiet" />
                      </Inline>
                    </Stack>
                  ) : (
                    <Text variant="help">Connecting to local daemon...</Text>
                  )}

                  <Divider />

                  <Stack gap={6}>
                    <Text variant="meta">Yogurt Surface Material</Text>
                    <SegmentedControl
                      label="Yogurt material"
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

                  <Inline justify="between" align="center" wrap>
                    <Button
                      emphasis="soft"
                      size="compact"
                      shape="pill"
                      onClick={() => setHostLocked(true)}
                    >
                      Lock Screen
                    </Button>
                    <Button
                      emphasis="quiet"
                      size="compact"
                      shape="pill"
                      onClick={() => navigate("/system/audit")}
                    >
                      System Diagnostics
                    </Button>
                  </Inline>
                </Stack>
              </Popover>

              {paperActive ? (
                paperRiskSuspended ? (
                  <Button
                    emphasis="filled"
                    tone="brand"
                    size="compact"
                    shape="pill"
                    onClick={() => {
                      if (!canResumePaper) {
                        navigate("/restrictions");
                        return;
                      }
                      setSafetyStripOpen((v) => !v);
                    }}
                  >
                    Resume Trading
                  </Button>
                ) : activeRestrictions > 0 ? (
                  <Button
                    emphasis="soft"
                    tone="danger"
                    size="compact"
                    shape="pill"
                    onClick={() => navigate("/restrictions")}
                  >
                    Active Stops ({activeRestrictions})
                  </Button>
                ) : (
                  <Button
                    emphasis="soft"
                    tone="danger"
                    size="compact"
                    shape="pill"
                    disabled={!canWrite}
                    onClick={() => setSafetyStripOpen((v) => !v)}
                  >
                    Pause New Orders
                  </Button>
                )
              ) : (
                <Button
                  emphasis="soft"
                  size="compact"
                  shape="pill"
                  onClick={() => navigate("/messages")}
                >
                  Message CEO
                </Button>
              )}
            </Inline>
          </header>

          <div className="sd-main-wrap">
            <main className="sd-main">
              {paperActive && meta && linkState !== "unauthorized" ? (
                <ControlledInlineConfirm
                  open={safetyStripOpen}
                  onCancel={() => setSafetyStripOpen(false)}
                  decision={{
                    id: paperRiskSuspended ? "resume-paper-orders" : "pause-paper-orders",
                    revision: `${meta.company_id}:${meta.charter_revision}:${meta.paper_risk_effective_revision}`,
                    title: paperRiskSuspended
                      ? "Resume New Paper Orders"
                      : "Pause New Paper Orders",
                    consequence: paperRiskSuspended
                      ? "Allow authorized agents to submit new paper orders again. Server verifies live paper quote freshness (<= 15s) upon confirmation."
                      : "Immediately block all new paper order submissions while keeping open positions and reconciliation active.",
                    confirmLabel: paperRiskSuspended
                      ? "Resume Paper Trading"
                      : "Pause New Paper Orders",
                    destructive: !paperRiskSuspended,
                    available: canWrite && (!paperRiskSuspended || canResumePaper),
                    requireReason: true,
                    defaultReason: paperRiskSuspended
                      ? "Board confirmed paper risk review complete"
                      : "Board precautionary pause on new paper orders",
                    whatChanges: paperRiskSuspended
                      ? [
                          { label: "Paper risk gate", value: "New paper orders re-enabled" },
                          { label: "Freshness check", value: "Verified server-side (<= 15s)" },
                        ]
                      : [
                          { label: "Paper risk gate", value: "New paper orders blocked immediately" },
                          { label: "Audit trail", value: "Recorded in command_receipt & audit_event" },
                        ],
                    whatStaysUntouched: [
                      { label: "Open paper positions", value: "Preserved unchanged (never auto-liquidated)" },
                      { label: "In-flight fills & accounting", value: "Reconciliation continues normally" },
                      { label: "Research & messaging", value: "Agents continue analysis & reporting" },
                    ],
                    technicalDetails: [
                      { label: "command_type", value: paperRiskSuspended ? "resume_new_risk" : "suspend_new_risk" },
                      { label: "contract_version", value: "paper-risk-command-v1" },
                      { label: "charter_revision", value: meta.charter_revision },
                      { label: "expected_revision", value: String(meta.paper_risk_effective_revision) },
                      { label: "company_id", value: meta.company_id },
                    ],
                  }}
                  onConfirm={({ reason }) => {
                    const cmdType = paperRiskSuspended ? "resume_new_risk" : "suspend_new_risk";
                    void dispatchCommand({
                      company_id: meta.company_id,
                      command_id: newCommandId(paperRiskSuspended ? "resume" : "pause"),
                      command_type: cmdType,
                      contract_version: "paper-risk-command-v1",
                      preconditions: {
                        charter_revision: meta.charter_revision,
                        expected_revision: meta.paper_risk_effective_revision,
                      },
                      payload: { reason },
                      label: paperRiskSuspended ? "Resume new paper orders" : "Pause new paper orders",
                      sent_ns: Date.now() * 1_000_000,
                    });
                  }}
                />
              ) : null}

              {linkState === "stale" ? (
                <Notice
                  title="Live connection interrupted — showing last known snapshot"
                  tone="warning"
                  urgent
                  action={
                    <Button emphasis="soft" size="compact" shape="pill" onClick={forceRefresh}>
                      Reconnect
                    </Button>
                  }
                >
                  {lastError ?? "Mutating controls are locked fail-closed until the local daemon reconnects."}
                </Notice>
              ) : null}

              {linkState === "live" && isAnyPaused ? (
                <Notice
                  title="New paper orders or company operations are currently paused"
                  tone="warning"
                  action={
                    <Button
                      emphasis="quiet"
                      size="compact"
                      shape="pill"
                      onClick={() => navigate("/restrictions")}
                    >
                      Inspect Restrictions
                    </Button>
                  }
                >
                  Open positions, fill reconciliation, and internal research continue undisturbed.
                </Notice>
              ) : null}

              {linkState !== "unauthorized" ? <CommandOutboxPanel /> : null}

              {linkState === "unauthorized" ? (
                <div data-agent-hidden="">
                  <Result
                    title="Session access revoked or unauthorized"
                    action={
                      <Button emphasis="soft" shape="pill" onClick={forceRefresh}>
                        Retry Session
                      </Button>
                    }
                  >
                    {lastError ?? "Reload from the authenticated local client-web bridge URL."}
                  </Result>
                </div>
              ) : (
                children
              )}
            </main>
          </div>
        </SidebarLayout>
      )}

      {toasts.length > 0 ? (
        <div className="sd-toast-corner">
          <ToastStack
            label="Board client notifications"
            items={toasts}
            onDismiss={dismissToast}
            presentation="stacked"
          />
        </div>
      ) : null}
    </>
  );
}
