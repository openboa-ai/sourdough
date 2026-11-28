import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Button,
  Notice,
  Result,
  SidebarLayout,
  ToastStack,
} from "@openboa/yogurt";
import {
  resolveActiveDomainModules,
  resolveHomeMode,
} from "../../domains/registry";
import { hasOpenOverlay, isEditableElement } from "../../lib/domFocus";
import {
  useControlPlane,
  usePreferences,
  useRouter,
} from "../../state/store";
import { CommandOutboxPanel } from "../CommandOutboxPanel";
import { AppSidebar } from "./AppSidebar";
import { AppTopbar } from "./AppTopbar";
import {
  describeFocusTarget,
  PrivacyShield,
  type SavedFocusTarget,
} from "./PrivacyShield";

export function AppShell({ children }: { children: ReactNode }) {
  const { navigate } = useRouter();
  const { hostLocked, setHostLocked, toasts, dismissToast } = usePreferences();
  const { meta, linkState, lastError, forceRefresh } = useControlPlane();

  const [commandOpen, setCommandOpen] = useState(false);
  const gArmedUntilRef = useRef(0);
  const preCommandFocusRef = useRef<SavedFocusTarget | null>(null);
  const restoreFocusRef = useRef<SavedFocusTarget | null>(null);

  const activeModules = resolveActiveDomainModules(meta?.active_domains);
  const homeMode = resolveHomeMode(activeModules);
  const tradingActive = activeModules.some((m) => m.id === "trading");
  const activeRestrictions = meta?.active_restrictions_count ?? 0;
  const isAnyPaused = activeRestrictions > 0;
  const isShieldActive = hostLocked && linkState !== "unauthorized";

  const handleOpenCommand = useCallback(() => {
    preCommandFocusRef.current = describeFocusTarget(document.activeElement);
    setCommandOpen(true);
  }, []);

  const handleCommandOpenChange = useCallback((open: boolean) => {
    setCommandOpen(open);
    if (!open) {
      preCommandFocusRef.current = null;
    }
  }, []);

  const handleRequestLock = useCallback(() => {
    const savedPreCommand = preCommandFocusRef.current;
    preCommandFocusRef.current = null;
    restoreFocusRef.current =
      savedPreCommand ?? describeFocusTarget(document.activeElement);
    setHostLocked(true);
  }, [setHostLocked]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
        if (hostLocked) {
          event.preventDefault();
          return;
        }
        event.preventDefault();
        setCommandOpen((open) => {
          if (!open) {
            preCommandFocusRef.current = describeFocusTarget(document.activeElement);
          } else {
            preCommandFocusRef.current = null;
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

      if (isEditableElement(event.target)) {
        gArmedUntilRef.current = 0;
        return;
      }

      if (hasOpenOverlay()) {
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

  return (
    <>
      <PrivacyShield active={isShieldActive} restoreFocusRef={restoreFocusRef} />

      {!isShieldActive ? (
        <SidebarLayout
          label="Sourdough Board Client"
          navigation={<AppSidebar />}
          width="16.75rem"
          compactAt={860}
          variant="flat"
          collapse="hide"
        >
          <AppTopbar
            commandOpen={commandOpen}
            onOpenCommand={handleOpenCommand}
            onCommandOpenChange={handleCommandOpenChange}
            onRequestLock={handleRequestLock}
          />

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
      ) : null}

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
