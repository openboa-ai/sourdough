import {
  Button,
  IconButton,
  Inline,
  Link,
  SidebarTrigger,
  Text,
} from "@openboa/yogurt";
import {
  resolveActiveDomainModules,
  resolveHomeMode,
} from "../../domains/registry";
import {
  useApprovalsCache,
  useControlPlane,
  usePreferences,
  useRouter,
} from "../../state/store";
import { CommandPalette } from "./CommandPalette";

export interface AppTopbarProps {
  commandOpen: boolean;
  onOpenCommand: () => void;
  onCommandOpenChange: (open: boolean) => void;
  onRequestLock: () => void;
}

export function AppTopbar({
  commandOpen,
  onOpenCommand,
  onCommandOpenChange,
  onRequestLock,
}: AppTopbarProps) {
  const { navigate } = useRouter();
  const { appearance, setAppearance, pushToast } = usePreferences();
  const { meta } = useControlPlane();
  const { projection: approvalsProjection, pendingCount } = useApprovalsCache();

  const activeModules = resolveActiveDomainModules(meta?.active_domains);
  const homeMode = resolveHomeMode(activeModules);
  const pendingApprovals = approvalsProjection
    ? pendingCount
    : (meta?.pending_approvals_count ?? 0);
  const isDark = appearance === "dark";

  return (
    <header className="sd-topbar">
      <Inline gap={8} align="center" wrap={false}>
        <SidebarTrigger label="Toggle sidebar" size="compact" />
        <Link
          href={homeMode === "core_only" ? "/agents" : "/"}
          className="sd-brand-link"
          onClick={(e) => {
            e.preventDefault();
            navigate(homeMode === "core_only" ? "/agents" : "/");
          }}
        >
          <Text variant="body">
            {meta ? `Company ${meta.company_id.slice(0, 12)}` : "Sourdough"}
          </Text>
        </Link>
      </Inline>

      <Inline gap={8} align="center" wrap>
        {pendingApprovals > 0 ? (
          <Button
            emphasis="soft"
            tone="brand"
            size="compact"
            shape="pill"
            onClick={() => navigate("/approvals")}
          >
            Approvals ({pendingApprovals})
          </Button>
        ) : null}
        <Button
          emphasis="soft"
          size="compact"
          shape="pill"
          aria-label="Open command palette"
          aria-keyshortcuts="Meta+K Control+K"
          onClick={onOpenCommand}
        >
          ⌘K
        </Button>

        <CommandPalette
          open={commandOpen}
          onOpenChange={onCommandOpenChange}
          onRequestLock={onRequestLock}
        />

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
      </Inline>
    </header>
  );
}
