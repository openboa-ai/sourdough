import {
  Badge,
  Button,
  Divider,
  Icon,
  Inline,
  ProjectMark,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarHeader,
  SidebarItem,
  Stack,
  Status,
  Text,
} from "@openboa/yogurt";
import {
  resolveActiveDomainModules,
  resolveHomeMode,
} from "../../domains/registry";
import {
  useApprovalsCache,
  useControlPlane,
  useRouter,
} from "../../state/store";

export function AppSidebar() {
  const { routePathname: path, navigate } = useRouter();
  const { meta, linkState, lastEventSeq, unreadCeoReply } = useControlPlane();
  const { projection: approvalsProjection, pendingCount } = useApprovalsCache();

  const activeModules = resolveActiveDomainModules(meta?.active_domains);
  const homeMode = resolveHomeMode(activeModules);
  const activeRestrictions = meta?.active_restrictions_count ?? 0;
  const isAnyPaused = activeRestrictions > 0;
  const pendingApprovals = approvalsProjection
    ? pendingCount
    : (meta?.pending_approvals_count ?? 0);

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

  return (
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
        <SidebarGroup>
          {homeMode === "multi_overview" ? (
            <SidebarItem
              label="Overview"
              icon={<Icon name="home" />}
              active={path === "/"}
              onSelect={() => navigate("/")}
            />
          ) : null}
          <SidebarItem
            label="Approvals"
            icon={<Icon name="check" />}
            active={path === "/approvals" || path.startsWith("/approvals/")}
            badge={
              pendingApprovals > 0 ? (
                <Badge tone="brand" dot={false}>
                  {pendingApprovals}
                </Badge>
              ) : undefined
            }
            onSelect={() => navigate("/approvals")}
          />
        </SidebarGroup>

        {activeModules.map((mod) => (
          <SidebarGroup key={mod.id} label={mod.label}>
            {mod.navItems.map((item) => (
              <SidebarItem
                key={item.id}
                label={item.label}
                icon={<Icon name={item.icon} />}
                active={
                  homeMode === "multi_overview" && item.path === "/"
                    ? path === "/trading/live"
                    : item.matches(path)
                }
                onSelect={() =>
                  navigate(
                    homeMode === "multi_overview" && item.path === "/"
                      ? "/trading/live"
                      : item.path
                  )
                }
              />
            ))}
          </SidebarGroup>
        ))}

        <SidebarGroup label="Company">
          <SidebarItem
            label="Agents"
            icon={<Icon name="workflow" />}
            active={
              path === "/agents" ||
              path.startsWith("/agents/") ||
              (homeMode === "core_only" && path === "/")
            }
            onSelect={() => navigate("/agents")}
          />
          <SidebarItem
            label="Messages"
            icon={<Icon name="chat" />}
            active={path === "/messages"}
            badge={
              unreadCeoReply ? (
                <span className="sd-unread-badge" title="New CEO reply">
                  <span className="yg-signal-dot" aria-hidden="true" />
                  <span className="sd-sr-only">New CEO reply</span>
                </span>
              ) : undefined
            }
            onSelect={() => navigate("/messages")}
          />
          <SidebarItem
            label="Finance"
            icon={<Icon name="bookmark" />}
            active={path === "/finance"}
            onSelect={() => navigate("/finance")}
          />
          <SidebarItem
            label="Reports"
            icon={<Icon name="person" />}
            active={path === "/reports"}
            onSelect={() => navigate("/reports")}
          />
        </SidebarGroup>

        <Divider />

        <SidebarGroup label="System">
          <SidebarItem
            label="Controls"
            icon={<Icon name="info" />}
            active={path === "/controls"}
            badge={
              activeRestrictions > 0 ? (
                <Badge tone="warning" dot={false}>
                  {activeRestrictions}
                </Badge>
              ) : undefined
            }
            onSelect={() => navigate("/controls")}
          />
          <SidebarItem
            label="Backups"
            icon={<Icon name="calendar" />}
            active={path === "/backups"}
            onSelect={() => navigate("/backups")}
          />
          <SidebarItem
            label="Audit"
            icon={<Icon name="folder" />}
            active={path === "/audit"}
            onSelect={() => navigate("/audit")}
          />
          <SidebarItem
            label="Settings"
            icon={<Icon name="sliders" />}
            active={path === "/settings"}
            onSelect={() => navigate("/settings")}
          />
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <Stack gap={6} className="sd-w-full">
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
            {activeModules.length > 0 ? (
              <Badge tone="neutral" variant="outline" dot={false}>
                {activeModules.map((m) => m.label).join(" · ")}
              </Badge>
            ) : (
              <Badge tone="neutral" variant="outline" dot={false}>
                Core OS
              </Badge>
            )}
          </Inline>
          <Button
            emphasis="quiet"
            size="compact"
            shape="pill"
            data-focus-id="sidebar-diagnostics-trigger"
            aria-label="Open daemon diagnostics"
            onClick={() => navigate("/audit?tab=daemon")}
          >
            <Status tone={statusTone}>{statusLabel}</Status>
          </Button>
        </Stack>
      </SidebarFooter>
    </>
  );
}
