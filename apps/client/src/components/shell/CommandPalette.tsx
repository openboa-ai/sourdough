import { useEffect, useRef, useState } from "react";
import {
  Command as SDKCommand,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@ui-sdk/react/command";
import { Dialog, Icon, Text, type IconName } from "@openboa/yogurt";
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

export interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRequestLock: () => void;
}

export function CommandPalette({
  open,
  onOpenChange,
  onRequestLock,
}: CommandPaletteProps) {
  const { navigate } = useRouter();
  const { appearance, setAppearance, pushToast } = usePreferences();
  const { meta } = useControlPlane();
  const { projection: approvalsProjection, pendingCount } = useApprovalsCache();

  const [query, setQuery] = useState("");
  const commandInputRef = useRef<HTMLInputElement | null>(null);

  const activeModules = resolveActiveDomainModules(meta?.active_domains);
  const homeMode = resolveHomeMode(activeModules);
  const pendingApprovals = approvalsProjection
    ? pendingCount
    : (meta?.pending_approvals_count ?? 0);
  const isDark = appearance === "dark";

  useEffect(() => {
    if (!open) {
      setQuery("");
      return;
    }
    const raf = window.requestAnimationFrame(() => {
      const inputEl =
        commandInputRef.current ??
        document.querySelector<HTMLInputElement>(".yg-command-search input");
      inputEl?.focus();
    });
    return () => window.cancelAnimationFrame(raf);
  }, [open]);

  const commandItems: Array<{
    id: string;
    label: string;
    hint: string;
    icon: IconName;
    shortcut?: string;
    keywords?: string[];
  }> = [
    ...(homeMode === "multi_overview"
      ? [
          {
            id: "nav:/",
            label: "Overview",
            hint: "Home",
            icon: "home" as const,
            keywords: ["Overview", "Home", "Modules", "Summary"],
          },
        ]
      : []),
    {
      id: "nav:/approvals",
      label: pendingApprovals > 0 ? `Approvals (${pendingApprovals} pending)` : "Approvals",
      hint: "Inbox",
      icon: "check",
      shortcut: "G V",
      keywords: ["Approvals", "Decisions", "Notifications", "Pending", "Governance"],
    },
    ...activeModules.flatMap((mod) =>
      mod.navItems.map((item) => ({
        id: `nav:${item.path}`,
        label: item.label,
        hint: mod.label,
        icon: item.icon,
        shortcut: item.shortcut,
        keywords: item.keywords,
      }))
    ),
    {
      id: "nav:/agents",
      label: "Agents",
      hint: "Company",
      icon: "workflow",
      shortcut: "G A",
      keywords: ["Agents", "Activity", "Office", "Graph", "Workers", "Roster", "Sandbox"],
    },
    {
      id: "nav:/messages",
      label: "Messages",
      hint: "Company",
      icon: "chat",
      shortcut: "G M",
      keywords: ["Messages", "CEO", "Founder", "Chat", "Direct", "Threads"],
    },
    {
      id: "nav:/finance",
      label: "Finance",
      hint: "Company",
      icon: "bookmark",
      keywords: ["Finance", "Costs", "Accounting", "Journals", "Statements", "Budget"],
    },
    {
      id: "nav:/reports",
      label: "Reports",
      hint: "Company",
      icon: "person",
      keywords: ["Reports", "Org", "Organization", "Publications", "Custody", "Team"],
    },
    {
      id: "nav:/controls",
      label: "Controls",
      hint: "System",
      icon: "info",
      shortcut: "G C",
      keywords: ["Controls", "Restrictions", "Risk", "Stops", "Pause", "Resume", "Gates"],
    },
    {
      id: "nav:/backups",
      label: "Backups",
      hint: "System",
      icon: "calendar",
      keywords: ["Backups", "Checkpoints", "Safety", "Restore", "Snapshot"],
    },
    {
      id: "nav:/audit",
      label: "Audit",
      hint: "System",
      icon: "folder",
      keywords: ["Audit", "Log", "Evaluations", "Lineage", "Daemon", "Diagnostics", "Events"],
    },
    {
      id: "nav:/settings",
      label: "Settings",
      hint: "System",
      icon: "sliders",
      shortcut: "G ,",
      keywords: ["Settings", "Appearance", "Material", "Modules", "RBAC", "Privacy"],
    },
    {
      id: "action:theme",
      label: isDark ? "Switch to Light Appearance" : "Switch to Dark Appearance",
      hint: "Appearance",
      icon: isDark ? "sun" : "moon",
      keywords: ["Theme", "Appearance", "Light", "Dark"],
    },
    {
      id: "action:lock",
      label: "Lock Board Client Screen",
      hint: "Privacy",
      icon: "close",
      keywords: ["Lock", "Privacy", "Shield", "Screen"],
    },
  ];

  const handleCommandSelect = (id: string) => {
    if (id.startsWith("nav:")) {
      navigate(id.slice("nav:".length));
    } else if (id === "action:theme") {
      const next = isDark ? "light" : "dark";
      setAppearance(next);
      pushToast(`Switched appearance to ${next}.`);
    } else if (id === "action:lock") {
      onRequestLock();
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) {
          setQuery("");
        }
        onOpenChange(nextOpen);
      }}
      title="Board Client Command"
    >
      {open ? (
        <SDKCommand
          query={query}
          onQueryChange={setQuery}
          filter="contains"
          loop
          onAction={(id) => {
            setQuery("");
            handleCommandSelect(id);
            onOpenChange(false);
          }}
        >
          <div className="yg-command-search">
            <Icon name="search" />
            <CommandInput
              ref={commandInputRef}
              autoFocus
              aria-label="Find a command"
              placeholder="Find a command..."
            />
          </div>
          <CommandEmpty className="yg-help">No matching commands</CommandEmpty>
          <CommandList className="yg-command-list" aria-label="Commands">
            {commandItems.map((item) => (
              <CommandItem
                key={item.id}
                value={item.id}
                label={item.label}
                keywords={[item.hint, ...(item.keywords ?? [])]}
              >
                <Icon name={item.icon} />
                <span className="yg-command-copy">
                  <span>{item.label}</span>
                  <Text variant="meta"> · {item.hint}</Text>
                </span>
                {item.shortcut ? (
                  <kbd className="sd-command-kbd" aria-hidden="true">
                    {item.shortcut}
                  </kbd>
                ) : null}
              </CommandItem>
            ))}
          </CommandList>
        </SDKCommand>
      ) : null}
    </Dialog>
  );
}
