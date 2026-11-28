import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { Button, Notice, Result, Stack, Text } from "@openboa/yogurt";
import { useControlPlane, usePreferences } from "../../state/store";

export interface SavedFocusTarget {
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

export function describeFocusTarget(el: Element | null, depth = 0): SavedFocusTarget | null {
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

export interface PrivacyShieldProps {
  active: boolean;
  restoreFocusRef: MutableRefObject<SavedFocusTarget | null>;
}

export function PrivacyShield({ active, restoreFocusRef }: PrivacyShieldProps) {
  const { lastEventSeq, forceRefresh } = useControlPlane();
  const { setHostLocked } = usePreferences();

  const [unlocking, setUnlocking] = useState(false);
  const [unlockError, setUnlockError] = useState<string | null>(null);
  const shieldRef = useRef<HTMLDivElement | null>(null);
  const wasShieldActiveRef = useRef(false);

  useEffect(() => {
    if (!active) {
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
      setUnlockError(null);
      setUnlocking(false);
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
        const activeEl = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        const idx = activeEl ? focusables.indexOf(activeEl) : -1;
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
  }, [active, restoreFocusRef]);

  if (!active) {
    return null;
  }

  return (
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
  );
}
