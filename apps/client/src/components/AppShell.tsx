import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Command as SDKCommand,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@ui-sdk/react/command";
import {
  Badge,
  Button,
  Dialog,
  Divider,
  Icon,
  IconButton,
  Inline,
  Link,
  Notice,
  ProjectMark,
  Result,
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
  type IconName,
} from "@openboa/yogurt";
import {
  resolveActiveDomainModules,
  resolveHomeMode,
} from "../domains/registry";
import { splitRoute, useApp } from "../state/store";
import { CommandOutboxPanel } from "./CommandOutboxPanel";

function hasOpenModalOrPopover(): boolean {
  const matches = document.querySelectorAll(
    'dialog[open], [role="dialog"], [role="alertdialog"], [data-slot="popover-content"][data-open]'
  );
  for (const el of matches) {
    if (el.closest('[inert], [hidden], [aria-hidden="true"], [data-closed]')) {
      continue;
    }
    if (el instanceof HTMLDialogElement && !el.open) {
      continue;
    }
    return true;
  }
  return false;
}

interface SavedFocusTarget {
  focusId: string | null;
  ariaLabel: string | null;
  placeholder: string | null;
  name: string | null;
  inputType: string | null;
  tagName: string | null;
  text: string | null;
  inMain: boolean;
}

function escapeSelectorAttr(value: string): string {
  return typeof CSS !== "undefined" && typeof CSS.escape === "function"
    ? CSS.escape(value)
    : value.replace(/"/g, '\\"');
}

function normalizeSeqText(text: string): string {
  return text.replace(/#\d+/g, "#").replace(/\s+/g, " ").trim();
}

function describeFocusTarget(el: Element | null, depth = 0): SavedFocusTarget | null {
  if (!el || el === document.body || el === document.documentElement || depth > 2) {
    return null;
  }
  if (el.closest(".sd-privacy-shield")) {
    return null;
  }
  const popover = el.closest('[data-slot="popover-content"], .yg-popover, .yg-anchored-panel');
  if (popover) {
    const popId = popover.getAttribute("id");
    let trigger: Element | null = null;
    if (popId) {
      trigger = document.querySelector(`[aria-controls="${escapeSelectorAttr(popId)}"]`);
    }
    if (!trigger) {
      trigger = document.querySelector(
        'header.sd-topbar button[aria-expanded="true"][aria-haspopup]'
      );
    }
    if (trigger && trigger !== el) {
      return describeFocusTarget(trigger, depth + 1);
    }
  }
  const tagName = el.tagName;
  const isField = tagName === "INPUT" || tagName === "TEXTAREA" || tagName === "SELECT";
  return {
    focusId:
      el.getAttribute("data-focus-id") ??
      el.closest("[data-focus-id]")?.getAttribute("data-focus-id") ??
      null,
    ariaLabel: el.getAttribute("aria-label"),
    placeholder: el.getAttribute("placeholder"),
    name: el.getAttribute("name"),
    inputType: el instanceof HTMLInputElement ? el.type : null,
    tagName,
    text: isField ? null : (el.textContent ?? "").trim().slice(0, 80) || null,
    inMain: Boolean(el.closest("main")),
  };
}

function getFocusableElements(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  const candidates = root.querySelectorAll<HTMLElement>(
    'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]):not([disabled])'
  );
  return Array.from(candidates).filter((el) => {
    if (el.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
    return el.getClientRects().length > 0;
  });
}

function isVisibleFocusable(el: HTMLElement | null): el is HTMLElement {
  if (!el) return false;
  if (el.hasAttribute("disabled") || el.closest('[hidden], [inert], [aria-hidden="true"]')) {
    return false;
  }
  return el.getClientRects().length > 0;
}

function findExactSavedFocusTarget(saved: SavedFocusTarget | null): HTMLElement | null {
  if (!saved) return null;
  const mainEl = document.querySelector<HTMLElement>("main");
  if (saved.inMain && !mainEl) return null;
  const scopeRoot: ParentNode = saved.inMain && mainEl ? mainEl : document;

  if (saved.focusId) {
    const byFocusId = document.querySelector<HTMLElement>(
      `[data-focus-id="${escapeSelectorAttr(saved.focusId)}"]`
    );
    if (isVisibleFocusable(byFocusId)) {
      return byFocusId;
    }
  }
  if (saved.ariaLabel) {
    const byAria = scopeRoot.querySelector<HTMLElement>(
      `[aria-label="${escapeSelectorAttr(saved.ariaLabel)}"]`
    );
    if (isVisibleFocusable(byAria)) {
      return byAria;
    }
  }

  const candidates = getFocusableElements(
    scopeRoot instanceof HTMLElement ? scopeRoot : document.body
  );

  if (saved.tagName === "INPUT" || saved.tagName === "TEXTAREA" || saved.tagName === "SELECT") {
    const fieldCandidates = candidates.filter((el) => {
      if (el.tagName !== saved.tagName) return false;
      if (saved.inputType && el instanceof HTMLInputElement && el.type !== saved.inputType) {
        return false;
      }
      return true;
    });
    if (saved.name) {
      const byName = fieldCandidates.find((el) => el.getAttribute("name") === saved.name);
      if (byName) return byName;
    }
    if (saved.placeholder) {
      const byPlaceholder = fieldCandidates.find(
        (el) => el.getAttribute("placeholder") === saved.placeholder
      );
      if (byPlaceholder) return byPlaceholder;
    }
    if (!saved.name && !saved.placeholder && fieldCandidates.length === 1) {
      return fieldCandidates[0];
    }
  }

  if (saved.tagName && saved.text) {
    const exactTextMatch = candidates.find(
      (el) => el.tagName === saved.tagName && (el.textContent ?? "").trim().slice(0, 80) === saved.text
    );
    if (exactTextMatch) return exactTextMatch;

    if (saved.text.includes("#")) {
      const normalizedSaved = normalizeSeqText(saved.text);
      const seqNormalizedMatch = candidates.find(
        (el) =>
          el.tagName === saved.tagName &&
          normalizeSeqText((el.textContent ?? "").trim().slice(0, 80)) === normalizedSaved
      );
      if (seqNormalizedMatch) return seqNormalizedMatch;
    }
  }

  return null;
}

function isMainSkeletonActive(): boolean {
  const mainEl = document.querySelector<HTMLElement>("main");
  if (!mainEl) return true;
  return Boolean(mainEl.querySelector(".yg-skeleton"));
}

function focusFallbackTarget(saved: SavedFocusTarget | null): void {
  const mainFocusable = getFocusableElements(document.querySelector<HTMLElement>("main"))[0];
  const topbarFocusable = getFocusableElements(
    document.querySelector<HTMLElement>("header.sd-topbar")
  )[0];
  if (saved && !saved.inMain && topbarFocusable) {
    topbarFocusable.focus();
    return;
  }
  if (mainFocusable) {
    mainFocusable.focus();
    return;
  }
  topbarFocusable?.focus();
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
    unreadCeoReply,
    forceRefresh,
    appearance,
    setAppearance,
    toasts,
    pushToast,
    dismissToast,
  } = useApp();

  const [commandOpen, setCommandOpen] = useState(false);
  const [commandQuery, setCommandQuery] = useState("");
  const [unlocking, setUnlocking] = useState(false);
  const [unlockError, setUnlockError] = useState<string | null>(null);
  const gArmedUntilRef = useRef(0);
  const commandInputRef = useRef<HTMLInputElement | null>(null);
  const shieldRef = useRef<HTMLDivElement | null>(null);
  const preCommandFocusRef = useRef<SavedFocusTarget | null>(null);
  const restoreFocusRef = useRef<SavedFocusTarget | null>(null);
  const wasShieldActiveRef = useRef(false);

  const { pathname: path } = splitRoute(route);
  const activeModules = resolveActiveDomainModules(meta?.active_domains);
  const homeMode = resolveHomeMode(activeModules);
  const tradingActive = activeModules.some((m) => m.id === "trading");
  const activeRestrictions = meta?.active_restrictions_count ?? 0;
  const isAnyPaused = activeRestrictions > 0;
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

  const isShieldActive = hostLocked && linkState !== "unauthorized";

  useEffect(() => {
    if (!isShieldActive) {
      if (wasShieldActiveRef.current) {
        wasShieldActiveRef.current = false;
        const saved = restoreFocusRef.current;
        restoreFocusRef.current = null;

        let cancelled = false;
        let observer: MutationObserver | null = null;
        let timeoutId: number | null = null;
        let rafId: number | null = null;

        const cleanup = () => {
          cancelled = true;
          observer?.disconnect();
          observer = null;
          if (timeoutId !== null) {
            window.clearTimeout(timeoutId);
            timeoutId = null;
          }
          if (rafId !== null) {
            window.cancelAnimationFrame(rafId);
            rafId = null;
          }
          window.removeEventListener("pointerdown", onUserInteract, true);
          window.removeEventListener("keydown", onUserKeyDown, true);
        };

        const onUserInteract = () => cleanup();
        const onUserKeyDown = (event: KeyboardEvent) => {
          if (
            event.key === "Tab" ||
            event.key === "Enter" ||
            event.key === " " ||
            event.key.startsWith("Arrow")
          ) {
            cleanup();
          }
        };

        const immediate = findExactSavedFocusTarget(saved);
        if (immediate) {
          immediate.focus();
          return;
        }
        if (saved && !saved.inMain) {
          focusFallbackTarget(saved);
          return;
        }
        if (!isMainSkeletonActive()) {
          const fallbackRaf = window.requestAnimationFrame(() => {
            const retried = findExactSavedFocusTarget(saved);
            if (retried) {
              retried.focus();
            } else {
              focusFallbackTarget(saved);
            }
          });
          return () => window.cancelAnimationFrame(fallbackRaf);
        }

        window.addEventListener("pointerdown", onUserInteract, true);
        window.addEventListener("keydown", onUserKeyDown, true);

        observer = new MutationObserver(() => {
          if (cancelled) return;
          const exact = findExactSavedFocusTarget(saved);
          if (exact) {
            exact.focus();
            cleanup();
            return;
          }
          if (!isMainSkeletonActive() && rafId === null) {
            rafId = window.requestAnimationFrame(() => {
              rafId = null;
              if (cancelled) return;
              const settled = findExactSavedFocusTarget(saved);
              if (settled) {
                settled.focus();
              } else {
                focusFallbackTarget(saved);
              }
              cleanup();
            });
          }
        });
        observer.observe(document.body, { childList: true, subtree: true });

        timeoutId = window.setTimeout(() => {
          if (cancelled) return;
          const exact = findExactSavedFocusTarget(saved);
          if (exact) {
            exact.focus();
          } else {
            focusFallbackTarget(saved);
          }
          cleanup();
        }, 4000);

        return cleanup;
      }
      return;
    }

    if (!wasShieldActiveRef.current) {
      wasShieldActiveRef.current = true;
      if (!restoreFocusRef.current) {
        restoreFocusRef.current = describeFocusTarget(document.activeElement);
      }
    }

    const focusIntoShield = () => {
      const shield = shieldRef.current;
      if (!shield) return;
      if (shield.contains(document.activeElement)) return;
      const focusables = getFocusableElements(shield);
      (focusables[0] ?? shield).focus();
    };
    focusIntoShield();
    const raf = window.requestAnimationFrame(focusIntoShield);

    const onShieldKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (event.key === "Tab") {
        const shield = shieldRef.current;
        if (!shield) return;
        const focusables = getFocusableElements(shield);
        event.preventDefault();
        if (focusables.length === 0) {
          shield.focus();
          return;
        }
        const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        const idx = active ? focusables.indexOf(active) : -1;
        if (idx === -1) {
          (event.shiftKey ? focusables[focusables.length - 1] : focusables[0]).focus();
        } else {
          const nextIdx = event.shiftKey
            ? (idx - 1 + focusables.length) % focusables.length
            : (idx + 1) % focusables.length;
          focusables[nextIdx].focus();
        }
      }
    };

    document.addEventListener("keydown", onShieldKeyDown, true);
    return () => {
      window.cancelAnimationFrame(raf);
      document.removeEventListener("keydown", onShieldKeyDown, true);
    };
  }, [isShieldActive]);

  useEffect(() => {
    if (!commandOpen) return;
    const raf = window.requestAnimationFrame(() => {
      const inputEl =
        commandInputRef.current ??
        document.querySelector<HTMLInputElement>(".yg-command-search input");
      inputEl?.focus();
    });
    return () => window.cancelAnimationFrame(raf);
  }, [commandOpen]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
        if (hostLocked) {
          event.preventDefault();
          return;
        }
        event.preventDefault();
        setCommandQuery("");
        setCommandOpen((open) => {
          if (!open) {
            preCommandFocusRef.current = describeFocusTarget(document.activeElement);
          }
          return !open;
        });
        return;
      }

      if (
        hostLocked ||
        commandOpen ||
        event.defaultPrevented ||
        event.isComposing ||
        event.repeat ||
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        event.shiftKey
      ) {
        gArmedUntilRef.current = 0;
        return;
      }

      const target = event.target;
      if (
        target instanceof Element &&
        (target.closest(
          'input, textarea, select, [contenteditable="true"], [contenteditable=""], [role="textbox"]'
        ) ||
          (target instanceof HTMLElement && target.isContentEditable))
      ) {
        gArmedUntilRef.current = 0;
        return;
      }

      if (hasOpenModalOrPopover()) {
        gArmedUntilRef.current = 0;
        return;
      }

      const key = event.key.toLowerCase();
      const now = Date.now();
      if (gArmedUntilRef.current > now) {
        gArmedUntilRef.current = 0;
        const chordTargets: Record<string, string> = {
          ...(tradingActive
            ? {
                l: homeMode === "multi_overview" ? "/trading/live" : "/",
                p: "/trading/paper",
                s: "/trading/strategies",
              }
            : {}),
          a: "/agents",
          m: "/messages",
          v: "/approvals",
          c: "/controls",
          ",": "/settings",
        };
        const dest = chordTargets[key];
        if (dest) {
          event.preventDefault();
          navigate(dest);
        }
        return;
      }

      if (key === "g") {
        gArmedUntilRef.current = now + 900;
      } else {
        gArmedUntilRef.current = 0;
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [commandOpen, hostLocked, navigate, tradingActive, homeMode]);

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
    const savedPreCommand = preCommandFocusRef.current;
    preCommandFocusRef.current = null;
    if (id.startsWith("nav:")) {
      navigate(id.slice("nav:".length));
    } else if (id === "action:theme") {
      const next = isDark ? "light" : "dark";
      setAppearance(next);
      pushToast(`Switched appearance to ${next}.`);
    } else if (id === "action:lock") {
      restoreFocusRef.current =
        savedPreCommand ?? describeFocusTarget(document.activeElement);
      setUnlockError(null);
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

  return (
    <>
      {isShieldActive ? (
        <div
          ref={shieldRef}
          tabIndex={-1}
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
                pending={unlocking}
                pendingLabel="Verifying..."
                onClick={async () => {
                  setUnlocking(true);
                  setUnlockError(null);
                  const ok = await forceRefresh();
                  setUnlocking(false);
                  if (ok) {
                    setHostLocked(false);
                  } else {
                    setUnlockError(
                      "Could not reach local daemon to re-verify session."
                    );
                  }
                }}
              >
                Unlock Board Client
              </Button>
            }
          >
            <Stack gap={12}>
              <Text variant="help">
                Company telemetry and controls are concealed while locked. Your last observed
                sequence (seq #{lastEventSeq}) is preserved safely.
              </Text>
              {unlockError ? (
                <Notice title="Unlock verification failed" tone="danger" urgent>
                  {unlockError}
                </Notice>
              ) : null}
            </Stack>
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
                onClick={() => {
                  preCommandFocusRef.current = describeFocusTarget(document.activeElement);
                  setCommandQuery("");
                  setCommandOpen(true);
                }}
              >
                ⌘K
              </Button>

              <Dialog
                open={commandOpen}
                onOpenChange={(open) => {
                  setCommandOpen(open);
                  if (!open) {
                    setCommandQuery("");
                    preCommandFocusRef.current = null;
                  }
                }}
                title="Board Client Command"
              >
                {commandOpen ? (
                  <SDKCommand
                    query={commandQuery}
                    onQueryChange={setCommandQuery}
                    filter="contains"
                    loop
                    onAction={(id) => {
                      setCommandOpen(false);
                      setCommandQuery("");
                      handleCommandSelect(id);
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

          <div className="sd-main-wrap">
            <main className="sd-main">
              {linkState === "stale" ? (
                <Notice
                  title="Live connection interrupted — showing last known snapshot"
                  tone="warning"
                  urgent
                  action={
                    <Button
                      emphasis="soft"
                      size="compact"
                      shape="pill"
                      onClick={() => {
                        void forceRefresh();
                      }}
                    >
                      Reconnect
                    </Button>
                  }
                >
                  {lastError ?? "Mutating controls are locked fail-closed until the local daemon reconnects."}
                </Notice>
              ) : null}

              {linkState === "live" && isAnyPaused ? (
                <Notice
                  title="Operational restrictions are currently active"
                  tone="warning"
                  action={
                    <Button
                      emphasis="quiet"
                      size="compact"
                      shape="pill"
                      onClick={() => navigate("/controls")}
                    >
                      Inspect Controls
                    </Button>
                  }
                >
                  Affected operations remain gated while unrestricted company work continues undisturbed.
                </Notice>
              ) : null}

              {linkState !== "unauthorized" ? <CommandOutboxPanel /> : null}

              {linkState === "unauthorized" ? (
                <div data-agent-hidden="">
                  <Result
                    title="Session access revoked or unauthorized"
                    action={
                      <Button
                        emphasis="soft"
                        shape="pill"
                        onClick={() => {
                          void forceRefresh();
                        }}
                      >
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
        <div className="sd-toast-corner" data-agent-hidden="">
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
