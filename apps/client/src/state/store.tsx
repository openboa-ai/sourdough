import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createYogurtAgent } from "@openboa/yogurt/agent";
import type { Appearance, Material } from "@openboa/yogurt";
import {
  ApiError,
  deletePendingOutboxApi,
  fetchEventsNdjson,
  fetchMeta,
  lookupCommandReceiptApi,
  readPendingOutboxApi,
  submitCommandApi,
  writePendingOutboxApi,
} from "../api/client";
import type {
  CommandLifecycle,
  CommandReceiptResponse,
  ContextRef,
  ControlPlaneMeta,
  LocalCommandIntent,
} from "../api/types";

export type LinkState = "connecting" | "live" | "stale" | "unauthorized" | "error";

export interface CommandOutboxState {
  lifecycle: CommandLifecycle;
  intent: LocalCommandIntent | null;
  receipt: CommandReceiptResponse | null;
  errorMessage: string | null;
  quarantineNotice: string | null;
}

export interface ToastItem {
  id: string;
  message: string;
}

interface AppContextValue {
  route: string;
  routePathname: string;
  routeSearchParams: URLSearchParams;
  performanceMode: boolean;
  daemonMode: boolean;
  activeApprovalQuestionId: string | null;
  navigate: (
    path: string,
    opts?: { replace?: boolean; contextRef?: ContextRef | null; focusReturnId?: string }
  ) => void;
  focusReturnId: string | null;
  clearFocusReturnId: () => void;
  pendingAskContext: ContextRef | null;
  setPendingAskContext: (ctx: ContextRef | null) => void;
  askCeo: (context: ContextRef | null) => void;
  registerEscapeBack: (targetPath: string, focusId?: string) => () => void;
  getLastSeenSeq: (companyId: string, principalId?: string | null) => number;
  markSeenSeq: (
    companyId: string,
    principalId: string | null | undefined,
    seq: number
  ) => void;
  unreadCeoReply: boolean;
  markMessagesVisited: (seq: number) => void;
  appearance: Appearance;
  setAppearance: (next: Appearance) => void;
  material: Material;
  setMaterial: (next: Material) => void;
  toasts: ToastItem[];
  pushToast: (message: string) => void;
  dismissToast: (id: string) => void;
  meta: ControlPlaneMeta | null;
  linkState: LinkState;
  lastError: string | null;
  refreshSeq: number;
  lastEventSeq: number;
  recentEventTypes: string[];
  hostLocked: boolean;
  setHostLocked: (locked: boolean) => void;
  controlsLocked: boolean;
  canWrite: boolean;
  outbox: CommandOutboxState;
  dispatchCommand: (intent: LocalCommandIntent) => Promise<CommandLifecycle>;
  lookupUnknownCommand: () => Promise<void>;
  retryNotFoundCommand: () => Promise<void>;
  discardPendingCommand: () => Promise<void>;
  dismissFinalOutbox: () => void;
  reportApiError: (err: unknown) => void;
  forceRefresh: () => Promise<boolean>;
}

const AppContext = createContext<AppContextValue | null>(null);

const ALLOWED_ASK_CONTEXT_KINDS = new Set([
  "paper_order",
  "agent",
  "shared_publication",
  "decision_question",
  "strategy_release",
  "challenger_trial",
  "strategy_trial",
  "restriction",
  "restriction_cause",
  "work_run",
  "company",
  "account_balance",
  "audit_event",
]);

export function splitRoute(raw: string): { pathname: string; search: string } {
  const hashIdx = raw.indexOf("#");
  const noHash = hashIdx === -1 ? raw : raw.slice(0, hashIdx);
  const qIdx = noHash.indexOf("?");
  const rawPath = qIdx === -1 ? noHash : noHash.slice(0, qIdx);
  const rawSearch = qIdx === -1 ? "" : noHash.slice(qIdx);
  let pathname = rawPath || "/";
  if (!pathname.startsWith("/")) {
    pathname = `/${pathname}`;
  }
  while (pathname.length > 1 && pathname.endsWith("/")) {
    pathname = pathname.slice(0, -1);
  }
  const search = rawSearch === "?" ? "" : rawSearch;
  return { pathname, search };
}

function normalizeRoute(raw: string): string {
  const { pathname, search } = splitRoute(raw);
  return `${pathname}${search}`;
}

export function decodeSingleSegment(pathname: string, prefix: string): string | null {
  if (!pathname.startsWith(prefix)) return null;
  const rawSegment = pathname.slice(prefix.length);
  if (!rawSegment || rawSegment.includes("/")) return null;
  try {
    const decoded = decodeURIComponent(rawSegment);
    return decoded && !decoded.includes("/") ? decoded : null;
  } catch {
    return null;
  }
}

function sanitizeAskContext(
  raw: { kind?: unknown; id?: unknown; label?: unknown } | null | undefined
): ContextRef | null {
  if (!raw || typeof raw.kind !== "string" || typeof raw.id !== "string") return null;
  const kind = raw.kind.trim();
  const id = raw.id.trim();
  if (
    !kind ||
    kind.length > 64 ||
    !ALLOWED_ASK_CONTEXT_KINDS.has(kind) ||
    !id ||
    id.length > 128 ||
    /[\u0000-\u001f\u007f]/.test(id)
  ) {
    return null;
  }
  const rawLabel = typeof raw.label === "string" ? raw.label.trim() : "";
  const label = (rawLabel || `${kind.replace(/_/g, " ")} · ${id}`).slice(0, 240);
  return { kind, id, label };
}

function parseAskContextFromSearch(search: string): ContextRef | null {
  if (!search) return null;
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const kind = params.get("context_kind");
  const id = params.get("context_id");
  if (!kind || !id) return null;
  return sanitizeAskContext({ kind, id, label: params.get("context_label") });
}

function isMessagesPathname(pathname: string): boolean {
  return pathname === "/messages" || pathname === "/ceo";
}

function isTier1Path(pathname: string): boolean {
  return (
    pathname.startsWith("/orders/") ||
    pathname.startsWith("/agents/") ||
    pathname.startsWith("/office/agents/") ||
    pathname.startsWith("/approvals/") ||
    pathname.startsWith("/company/approvals/") ||
    pathname === "/performance" ||
    pathname === "/system/daemon"
  );
}

function defaultEscapeBackTarget(pathname: string): string | null {
  if (pathname.startsWith("/orders/") || pathname === "/performance") return "/trading/paper";
  if (pathname.startsWith("/agents/") || pathname.startsWith("/office/agents/")) {
    return "/agents";
  }
  if (pathname.startsWith("/approvals/") || pathname.startsWith("/company/approvals/")) {
    return "/approvals";
  }
  if (pathname === "/system/daemon") return "/audit";
  return null;
}

function lastSeenStorageKey(companyId: string, principalId?: string | null): string {
  return `sd.lastSeenSeq.${companyId}.${principalId || "unbound"}`;
}

function lastMessagesStorageKey(companyId: string, principalId?: string | null): string {
  return `sd.lastMessagesSeq.${companyId}.${principalId || "unbound"}`;
}

function readStoredSeq(key: string): number {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return 0;
    const parsed = Number.parseInt(raw, 10);
    return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
  } catch {
    return 0;
  }
}

function writeStoredSeq(key: string, seq: number): void {
  if (!Number.isFinite(seq) || seq < 0) return;
  try {
    window.localStorage.setItem(key, String(Math.floor(seq)));
  } catch {
    // Ignore storage failures in restricted contexts.
  }
}

function isEditableElement(el: EventTarget | null): boolean {
  if (!(el instanceof Element)) return false;
  if (el instanceof HTMLElement && el.isContentEditable) return true;
  return Boolean(
    el.closest('input, textarea, select, [contenteditable="true"], [contenteditable=""], [role="textbox"]')
  );
}

const OPEN_OVERLAY_SELECTOR = [
  '[role="dialog"]',
  '[role="alertdialog"]',
  '[role="listbox"]',
  '[role="menu"]',
  '[data-slot="popover-content"][data-open]',
  '[data-slot="dialog-content"][data-open]',
  '[data-slot="combobox-content"][data-open]',
  ".yg-anchored-panel[data-open]",
  ".yg-popup-menu[data-open]",
  ".yg-popover[data-open]",
  '.yg-inline-command[data-open="true"]',
  '.yg-activity-island[data-open="true"]',
  ".yg-decision-card",
  ".sd-decision-inline",
  '[data-decision-confirm="open"]',
].join(", ");

function hasOpenOverlay(): boolean {
  const matches = document.querySelectorAll(OPEN_OVERLAY_SELECTOR);
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

export function AppProvider({ children }: { children: ReactNode }) {
  const [route, setRoute] = useState<string>(() =>
    normalizeRoute(window.location.pathname + window.location.search)
  );
  const [focusReturnId, setFocusReturnId] = useState<string | null>(null);
  const [pendingAskContext, setPendingAskContextState] = useState<ContextRef | null>(() => {
    const initial = splitRoute(window.location.pathname + window.location.search);
    return isMessagesPathname(initial.pathname)
      ? parseAskContextFromSearch(initial.search)
      : null;
  });
  const [appearance, setAppearanceState] = useState<Appearance>(() => {
    try {
      const saved = window.localStorage.getItem("sd-yogurt-appearance");
      if (saved === "light" || saved === "dark" || saved === "auto") return saved;
    } catch {
      // Ignore storage failures in restricted contexts.
    }
    return "light";
  });
  const [material, setMaterialState] = useState<Material>(() => {
    try {
      const saved = window.localStorage.getItem("sd-yogurt-material");
      if (saved === "neutral" || saved === "paper" || saved === "mist") return saved;
    } catch {
      // Ignore storage failures in restricted contexts.
    }
    return "neutral";
  });
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const setAppearance = useCallback((next: Appearance) => {
    setAppearanceState(next);
    try {
      window.localStorage.setItem("sd-yogurt-appearance", next);
    } catch {
      // Ignore storage failures.
    }
  }, []);

  const setMaterial = useCallback((next: Material) => {
    setMaterialState(next);
    try {
      window.localStorage.setItem("sd-yogurt-material", next);
    } catch {
      // Ignore storage failures.
    }
  }, []);

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const pushToast = useCallback((message: string) => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    setToasts((prev) => [...prev.slice(-3), { id, message }]);
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((item) => item.id !== id));
    }, 4500);
  }, []);

  const [meta, setMeta] = useState<ControlPlaneMeta | null>(null);
  const [linkState, setLinkState] = useState<LinkState>("connecting");
  const [lastError, setLastError] = useState<string | null>(null);
  const [refreshSeq, setRefreshSeq] = useState(0);
  const [lastEventSeq, setLastEventSeq] = useState(0);
  const [recentEventTypes, setRecentEventTypes] = useState<string[]>([]);
  const [hostLocked, setHostLocked] = useState(false);
  const [unreadCeoReply, setUnreadCeoReply] = useState(false);

  const [outbox, setOutbox] = useState<CommandOutboxState>({
    lifecycle: "idle",
    intent: null,
    receipt: null,
    errorMessage: null,
    quarantineNotice: null,
  });

  const incarnationRef = useRef<string | null>(null);
  const companyIdRef = useRef<string | null>(null);
  const storeKeyRef = useRef<string | null>(null);
  const principalIdRef = useRef<string | null | undefined>(undefined);
  const lastSeqRef = useRef<number>(0);
  const submittingRef = useRef<boolean>(false);
  const outboxRef = useRef<CommandOutboxState>(outbox);
  outboxRef.current = outbox;
  const routeRef = useRef<string>(route);
  routeRef.current = route;
  const hostLockedRef = useRef<boolean>(hostLocked);
  hostLockedRef.current = hostLocked;
  const linkStateRef = useRef<LinkState>(linkState);
  linkStateRef.current = linkState;
  const pendingReturnRef = useRef<{ path: string; focusId: string } | null>(null);
  const focusTargetPathRef = useRef<string | null>(null);
  const escapeRegRef = useRef<{ token: symbol; targetPath: string; focusId?: string } | null>(
    null
  );
  const startupMsgCheckedRef = useRef<string | null>(null);
  const prevBootstrapLockedRef = useRef<boolean | undefined>(undefined);
  const [metaReady, setMetaReady] = useState(false);
  const metaReadyRef = useRef(false);
  metaReadyRef.current = metaReady;

  const { pathname: routePathname, search: routeSearch } = useMemo(
    () => splitRoute(route),
    [route]
  );
  const routeSearchParams = useMemo(
    () => new URLSearchParams(routeSearch.startsWith("?") ? routeSearch.slice(1) : routeSearch),
    [routeSearch]
  );
  const performanceMode = routePathname === "/performance";
  const daemonMode = routePathname === "/system/daemon";
  const activeApprovalQuestionId = useMemo(
    () =>
      decodeSingleSegment(routePathname, "/approvals/") ??
      decodeSingleSegment(routePathname, "/company/approvals/"),
    [routePathname]
  );

  // Mount Yogurt agent bridge disabled by default (allow: () => false).
  useEffect(() => {
    try {
      createYogurtAgent({ allow: () => false });
    } catch {
      // Safe no-op in non-browser test hosts.
    }
  }, []);

  const getLastSeenSeq = useCallback(
    (companyId: string, principalId?: string | null): number => {
      if (!companyId) return 0;
      return readStoredSeq(lastSeenStorageKey(companyId, principalId));
    },
    []
  );

  const markSeenSeq = useCallback(
    (companyId: string, principalId: string | null | undefined, seq: number): void => {
      if (!companyId) return;
      writeStoredSeq(lastSeenStorageKey(companyId, principalId), seq);
    },
    []
  );

  const markMessagesVisited = useCallback((seq: number): void => {
    const companyId = companyIdRef.current;
    if (companyId && Number.isFinite(seq) && seq >= 0) {
      writeStoredSeq(
        lastMessagesStorageKey(companyId, principalIdRef.current),
        seq
      );
    }
    setUnreadCeoReply(false);
  }, []);

  useEffect(() => {
    if (!isMessagesPathname(routePathname)) return;
    const channel = routeSearchParams.get("channel");
    if (channel && channel !== "ceo-direct") return;
    markMessagesVisited(lastSeqRef.current || meta?.event_seq || 0);
  }, [
    routePathname,
    routeSearchParams,
    unreadCeoReply,
    meta?.event_seq,
    markMessagesVisited,
  ]);

  const setPendingAskContext = useCallback((ctx: ContextRef | null) => {
    const sanitized = ctx ? sanitizeAskContext(ctx) : null;
    setPendingAskContextState(sanitized);
    if (sanitized === null) {
      const sp = splitRoute(routeRef.current);
      if (isMessagesPathname(sp.pathname) && sp.search) {
        const params = new URLSearchParams(
          sp.search.startsWith("?") ? sp.search.slice(1) : sp.search
        );
        if (
          params.has("context_kind") ||
          params.has("context_id") ||
          params.has("context_label")
        ) {
          params.delete("context_kind");
          params.delete("context_id");
          params.delete("context_label");
          const s = params.toString();
          const nextRoute = s ? `${sp.pathname}?${s}` : sp.pathname;
          if (window.location.pathname + window.location.search !== nextRoute) {
            window.history.replaceState({}, "", nextRoute);
          }
          setRoute(nextRoute);
        }
      }
    }
  }, []);

  // Browser history synchronization.
  useEffect(() => {
    const onPop = () => {
      const prevPath = splitRoute(routeRef.current).pathname;
      const next = normalizeRoute(window.location.pathname + window.location.search);
      const { pathname: nextPath, search: nextSearch } = splitRoute(next);
      setRoute(next);
      if (isMessagesPathname(nextPath)) {
        setPendingAskContextState(parseAskContextFromSearch(nextSearch));
      } else {
        setPendingAskContextState(null);
      }
      if (pendingReturnRef.current && pendingReturnRef.current.path === nextPath) {
        focusTargetPathRef.current = nextPath;
        setFocusReturnId(pendingReturnRef.current.focusId);
        pendingReturnRef.current = null;
      }
      if (nextPath !== prevPath) {
        setOutbox((prev) =>
          prev.lifecycle === "applied"
            ? {
                lifecycle: "idle",
                intent: null,
                receipt: null,
                errorMessage: null,
                quarantineNotice: null,
              }
            : prev
        );
      }
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback(
    (
      path: string,
      opts?: { replace?: boolean; contextRef?: ContextRef | null; focusReturnId?: string }
    ) => {
      const prevSplit = splitRoute(routeRef.current);
      let { pathname: nextPath, search: nextSearch } = splitRoute(path);
      const isMsgRoute = isMessagesPathname(nextPath);

      if (opts && "contextRef" in opts) {
        const sanitized = opts.contextRef ? sanitizeAskContext(opts.contextRef) : null;
        setPendingAskContextState(sanitized);
        if (isMsgRoute) {
          const params = new URLSearchParams(
            nextSearch.startsWith("?") ? nextSearch.slice(1) : nextSearch
          );
          if (sanitized) {
            if (!params.has("context_kind")) params.set("context_kind", sanitized.kind);
            if (!params.has("context_id")) params.set("context_id", sanitized.id);
          } else {
            params.delete("context_kind");
            params.delete("context_id");
            params.delete("context_label");
          }
          const s = params.toString();
          nextSearch = s ? `?${s}` : "";
        }
      } else if (isMsgRoute) {
        const parsed = parseAskContextFromSearch(nextSearch);
        if (parsed) {
          setPendingAskContextState(parsed);
        } else if (nextPath !== prevSplit.pathname && !nextSearch) {
          setPendingAskContextState(null);
        }
      } else {
        setPendingAskContextState(null);
      }

      const normalized = `${nextPath}${nextSearch}`;

      if (opts?.focusReturnId) {
        if (isTier1Path(nextPath) && !isTier1Path(prevSplit.pathname)) {
          pendingReturnRef.current = {
            path: prevSplit.pathname,
            focusId: opts.focusReturnId,
          };
          focusTargetPathRef.current = null;
          setFocusReturnId(null);
        } else {
          pendingReturnRef.current = null;
          focusTargetPathRef.current = nextPath;
          setFocusReturnId(opts.focusReturnId);
        }
      } else if (
        !isTier1Path(nextPath) &&
        isTier1Path(prevSplit.pathname) &&
        pendingReturnRef.current?.path === nextPath
      ) {
        focusTargetPathRef.current = nextPath;
        setFocusReturnId(pendingReturnRef.current.focusId);
        pendingReturnRef.current = null;
      } else if (nextPath !== prevSplit.pathname) {
        if (!isTier1Path(nextPath)) {
          pendingReturnRef.current = null;
        }
        focusTargetPathRef.current = null;
        setFocusReturnId(null);
      }

      if (window.location.pathname + window.location.search !== normalized) {
        if (opts?.replace) {
          window.history.replaceState({}, "", normalized);
        } else {
          window.history.pushState({}, "", normalized);
        }
      }
      setRoute(normalized);

      if (nextPath !== prevSplit.pathname) {
        setOutbox((prev) =>
          prev.lifecycle === "applied"
            ? {
                lifecycle: "idle",
                intent: null,
                receipt: null,
                errorMessage: null,
                quarantineNotice: null,
              }
            : prev
        );
        window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
      }
    },
    []
  );

  const askCeo = useCallback(
    (context: ContextRef | null) => {
      if (!context) {
        navigate("/messages", { contextRef: null });
        return;
      }
      const sanitized = sanitizeAskContext(context);
      if (!sanitized) {
        navigate("/messages", { contextRef: null });
        return;
      }
      navigate(
        `/messages?context_kind=${encodeURIComponent(sanitized.kind)}&context_id=${encodeURIComponent(sanitized.id)}`,
        { contextRef: sanitized }
      );
    },
    [navigate]
  );

  const clearFocusReturnId = useCallback(() => {
    focusTargetPathRef.current = null;
    setFocusReturnId(null);
  }, []);

  const registerEscapeBack = useCallback((targetPath: string, focusId?: string) => {
    const token = Symbol("escapeBack");
    escapeRegRef.current = { token, targetPath, focusId };
    return () => {
      if (escapeRegRef.current?.token === token) {
        escapeRegRef.current = null;
      }
    };
  }, []);

  // Deterministic [data-focus-id="..."] focus restoration on return to Tier 0.
  useEffect(() => {
    if (!focusReturnId) return;
    if (focusTargetPathRef.current !== null && focusTargetPathRef.current !== routePathname) {
      return;
    }
    const escaped =
      typeof CSS !== "undefined" && typeof CSS.escape === "function"
        ? CSS.escape(focusReturnId)
        : focusReturnId.replace(/"/g, '\\"');
    const selector = `[data-focus-id="${escaped}"]`;

    const tryFocus = () => {
      const target = document.querySelector<HTMLElement>(selector);
      if (!target) return false;
      target.focus();
      focusTargetPathRef.current = null;
      setFocusReturnId(null);
      return true;
    };

    if (tryFocus()) return;

    const root = document.querySelector(".sd-main") ?? document.body;
    const observer = new MutationObserver(() => {
      if (tryFocus()) {
        observer.disconnect();
        window.clearTimeout(timer);
      }
    });
    observer.observe(root, { childList: true, subtree: true });
    const timer = window.setTimeout(() => {
      observer.disconnect();
      focusTargetPathRef.current = null;
      setFocusReturnId(null);
    }, 1500);

    return () => {
      observer.disconnect();
      window.clearTimeout(timer);
    };
  }, [focusReturnId, routePathname]);

  // Global guarded Escape handler for Tier 1 -> Tier 0 navigation.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.key !== "Escape" ||
        event.defaultPrevented ||
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        event.shiftKey ||
        hostLockedRef.current
      ) {
        return;
      }
      if (isEditableElement(event.target) || isEditableElement(document.activeElement)) {
        return;
      }
      if (hasOpenOverlay()) {
        return;
      }
      const currentPath = splitRoute(routeRef.current).pathname;
      const reg = escapeRegRef.current;
      const targetPath = reg?.targetPath ?? defaultEscapeBackTarget(currentPath);
      if (!targetPath || targetPath === currentPath) return;

      event.preventDefault();
      const effFocusId =
        reg?.focusId ??
        (pendingReturnRef.current?.path === targetPath
          ? pendingReturnRef.current.focusId
          : undefined);
      navigate(targetPath, effFocusId ? { focusReturnId: effFocusId } : undefined);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navigate]);

  const clearData = useCallback((reason: string, state: LinkState = "unauthorized") => {
    setMeta(null);
    setMetaReady(false);
    setLinkState(state);
    setLastError(reason);
    setRecentEventTypes([]);
    setUnreadCeoReply(false);
    startupMsgCheckedRef.current = null;
    setPendingAskContextState(null);
    setOutbox({
      lifecycle: "idle",
      intent: null,
      receipt: null,
      errorMessage: null,
      quarantineNotice: null,
    });
  }, []);

  const reportApiError = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        clearData(err.message, "unauthorized");
      }
    },
    [clearData]
  );

  const loadMeta = useCallback(
    async (signal?: AbortSignal, opts?: { retryTransientNetwork?: boolean }) => {
      try {
        const nextMeta = await fetchMeta(signal, opts);
        const nextPrincipal = nextMeta.board_access.principal_id;
        if (
          (companyIdRef.current !== null &&
            companyIdRef.current !== nextMeta.company_id) ||
          (storeKeyRef.current !== null &&
            storeKeyRef.current !== nextMeta.store_key) ||
          (principalIdRef.current !== undefined &&
            principalIdRef.current !== null &&
            principalIdRef.current !== nextPrincipal)
        ) {
          clearData(
            "Company, store, or Board principal scope changed; cleared session data.",
            "unauthorized"
          );
          return null;
        }
        companyIdRef.current = nextMeta.company_id;
        storeKeyRef.current = nextMeta.store_key;
        principalIdRef.current = nextPrincipal;
        incarnationRef.current = nextMeta.service_incarnation_id;
        lastSeqRef.current = Math.max(lastSeqRef.current, nextMeta.event_seq);
        setLastEventSeq(lastSeqRef.current);
        setMeta(nextMeta);
        setLinkState("live");
        setLastError(null);
        setMetaReady(true);

        const scopeKey = `${nextMeta.company_id}:${nextPrincipal ?? "unbound"}`;
        if (startupMsgCheckedRef.current !== scopeKey) {
          startupMsgCheckedRef.current = scopeKey;
          const savedMsgSeq = readStoredSeq(
            lastMessagesStorageKey(nextMeta.company_id, nextPrincipal)
          );
          if (savedMsgSeq > 0 && savedMsgSeq < nextMeta.event_seq) {
            void fetchEventsNdjson(savedMsgSeq, 0, signal)
              .then((events) => {
                if (signal?.aborted) return;
                if (events.some((e) => e.event_type === "ceo.message_replied")) {
                  const sp = splitRoute(routeRef.current);
                  const ch = new URLSearchParams(
                    sp.search.startsWith("?") ? sp.search.slice(1) : sp.search
                  ).get("channel");
                  if (isMessagesPathname(sp.pathname) && (!ch || ch === "ceo-direct")) {
                    markMessagesVisited(nextMeta.event_seq);
                  } else {
                    setUnreadCeoReply(true);
                  }
                }
              })
              .catch(() => undefined);
          }
        }

        return nextMeta;
      } catch (err) {
        if (signal?.aborted) return null;
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          clearData(err.message, "unauthorized");
          return null;
        }
        // Stale disconnect preserves last known read data while locking mutations.
        setLinkState((prev) => (prev === "unauthorized" ? "unauthorized" : "stale"));
        setLastError(err instanceof Error ? err.message : "Control plane unreachable");
        return null;
      }
    },
    [clearData, markMessagesVisited]
  );

  // Redirect away from /bootstrap once bootstrap_locked clears.
  useEffect(() => {
    const prevLocked = prevBootstrapLockedRef.current;
    const isLocked = meta?.board_access.bootstrap_locked;
    prevBootstrapLockedRef.current = isLocked;
    if (
      isLocked === false &&
      (prevLocked === true || prevLocked === undefined) &&
      splitRoute(routeRef.current).pathname === "/bootstrap"
    ) {
      navigate("/", { replace: true });
    }
  }, [meta?.board_access.bootstrap_locked, navigate]);

  // Startup bootstrap + pending outbox recovery.
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const retryTimers: number[] = [];

    const waitRetry = (ms: number) =>
      new Promise<void>((resolve) => {
        const id = window.setTimeout(resolve, ms);
        retryTimers.push(id);
      });

    const shouldStopRetry = () =>
      !active ||
      controller.signal.aborted ||
      linkStateRef.current === "unauthorized" ||
      metaReadyRef.current;

    void (async () => {
      let loaded = await loadMeta(controller.signal, { retryTransientNetwork: true });
      const retryDelays = [300, 800, 1600];
      for (let i = 0; !loaded && i < retryDelays.length; i += 1) {
        if (shouldStopRetry()) break;
        await waitRetry(retryDelays[i]);
        if (shouldStopRetry()) break;
        loaded = await loadMeta(controller.signal, { retryTransientNetwork: true });
      }
      if (!loaded || !active) return;
      try {
        const pendingInfo = await readPendingOutboxApi();
        if (!active) return;
        if (pendingInfo.quarantined_stale) {
          setOutbox({
            lifecycle: "idle",
            intent: null,
            receipt: null,
            errorMessage: null,
            quarantineNotice:
              pendingInfo.notice ??
              "A stale pending command file from another session was quarantined.",
          });
          return;
        }
        if (pendingInfo.pending) {
          const rec = pendingInfo.receipt;
          if (!rec) {
            setOutbox({
              lifecycle: "unknown",
              intent: pendingInfo.pending,
              receipt: null,
              errorMessage: "Reconnected with an unconfirmed request. Check its status before retrying.",
              quarantineNotice: null,
            });
          } else if (rec.status === "not_found") {
            setOutbox({
              lifecycle: "not_found_retry_choice",
              intent: pendingInfo.pending,
              receipt: rec,
              errorMessage: "No matching command receipt was found on the daemon.",
              quarantineNotice: null,
            });
          } else if (rec.state === "applied") {
            try {
              await deletePendingOutboxApi();
              if (!active) return;
              setOutbox({
                lifecycle: "applied",
                intent: pendingInfo.pending,
                receipt: rec,
                errorMessage: null,
                quarantineNotice: null,
              });
            } catch {
              if (!active) return;
              setOutbox({
                lifecycle: "unknown",
                intent: pendingInfo.pending,
                receipt: rec,
                errorMessage: "Applied on daemon, but failed to clear local pending file.",
                quarantineNotice: null,
              });
            }
          } else if (rec.state === "denied") {
            await deletePendingOutboxApi().catch(() => undefined);
            if (!active) return;
            setOutbox({
              lifecycle: "denied",
              intent: pendingInfo.pending,
              receipt: rec,
              errorMessage: rec.denial_code ?? "Denied",
              quarantineNotice: null,
            });
          } else {
            setOutbox({
              lifecycle: "accepted",
              intent: pendingInfo.pending,
              receipt: rec,
              errorMessage: null,
              quarantineNotice: null,
            });
          }
        }
      } catch {
        // Ignore outbox recovery network hiccup; meta poll will retry.
      }
    })();
    return () => {
      active = false;
      controller.abort();
      for (const id of retryTimers) {
        window.clearTimeout(id);
      }
    };
  }, [loadMeta]);

  // Coalesced NDJSON /api/v1/events long-poll stream (waits for initial meta load).
  useEffect(() => {
    if (!metaReady) return;
    let active = true;
    const controller = new AbortController();
    let coalesceTimer: number | null = null;
    let settleTimer: number | null = null;
    let pendingWaitResolve: (() => void) | null = null;

    const waitMsDelay = (ms: number) =>
      new Promise<void>((resolve) => {
        if (!active) {
          resolve();
          return;
        }
        pendingWaitResolve = resolve;
        settleTimer = window.setTimeout(() => {
          settleTimer = null;
          pendingWaitResolve = null;
          resolve();
        }, ms);
      });

    const cancelPendingWait = () => {
      if (settleTimer !== null) {
        window.clearTimeout(settleTimer);
        settleTimer = null;
      }
      if (pendingWaitResolve) {
        const resolve = pendingWaitResolve;
        pendingWaitResolve = null;
        resolve();
      }
    };

    const shutdown = () => {
      active = false;
      controller.abort();
      cancelPendingWait();
      if (coalesceTimer !== null) {
        window.clearTimeout(coalesceTimer);
        coalesceTimer = null;
      }
    };

    window.addEventListener("pagehide", shutdown);

    async function loop() {
      let waitMs = 0;
      while (active) {
        try {
          const events = await fetchEventsNdjson(
            lastSeqRef.current,
            waitMs,
            controller.signal
          );
          if (!active) break;
          if (events.length > 0) {
            const maxSeq = events[events.length - 1].event_seq;
            lastSeqRef.current = Math.max(lastSeqRef.current, maxSeq);
            setLastEventSeq(lastSeqRef.current);
            setRecentEventTypes(events.map((e) => e.event_type));
            const ceoReplies = events.filter(
              (e) => e.event_type === "ceo.message_replied"
            );
            if (ceoReplies.length > 0) {
              const sp = splitRoute(routeRef.current);
              const ch = new URLSearchParams(
                sp.search.startsWith("?") ? sp.search.slice(1) : sp.search
              ).get("channel");
              if (isMessagesPathname(sp.pathname) && (!ch || ch === "ceo-direct")) {
                markMessagesVisited(ceoReplies[ceoReplies.length - 1].event_seq);
              } else {
                setUnreadCeoReply(true);
              }
            }
            if (coalesceTimer !== null) {
              window.clearTimeout(coalesceTimer);
            }
            coalesceTimer = window.setTimeout(() => {
              if (!active) return;
              void loadMeta();
              setRefreshSeq((n) => n + 1);
            }, 150);
          } else if (linkStateRef.current === "stale") {
            void loadMeta();
          }
          if (waitMs === 0) {
            waitMs = 3000;
            await waitMsDelay(400);
            if (!active) break;
          }
        } catch (err) {
          if (!active || controller.signal.aborted) break;
          if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
            clearData(err.message, "unauthorized");
            break;
          }
          setLinkState("stale");
          setLastError(err instanceof Error ? err.message : "Live event stream disconnected");
          await waitMsDelay(1500);
          if (active) {
            await loadMeta();
          }
        }
      }
    }

    void loop();
    return () => {
      window.removeEventListener("pagehide", shutdown);
      shutdown();
    };
  }, [clearData, loadMeta, markMessagesVisited, metaReady]);

  const dispatchCommand = useCallback(
    async (intent: LocalCommandIntent): Promise<CommandLifecycle> => {
      if (submittingRef.current) return outboxRef.current.lifecycle;
      submittingRef.current = true;
      try {
        setOutbox({
          lifecycle: "submitting",
          intent,
          receipt: null,
          errorMessage: null,
          quarantineNotice: null,
        });

        // Persist pending intent BEFORE wire transmission so mid-flight drops recover cleanly.
        try {
          await writePendingOutboxApi(intent);
        } catch (err) {
          setOutbox({
            lifecycle: "denied",
            intent,
            receipt: null,
            errorMessage:
              err instanceof Error
                ? `Failed to record local pending guard: ${err.message}`
                : "Failed to record local pending guard",
            quarantineNotice: null,
          });
          return "denied";
        }

        try {
          const receipt = await submitCommandApi(intent);
          if (receipt.state === "applied") {
            try {
              await deletePendingOutboxApi();
            } catch {
              setOutbox({
                lifecycle: "unknown",
                intent,
                receipt,
                errorMessage: "Command applied, but clearing local pending file failed.",
                quarantineNotice: null,
              });
              return "unknown";
            }
            setOutbox({
              lifecycle: "applied",
              intent,
              receipt,
              errorMessage: null,
              quarantineNotice: null,
            });
            await loadMeta();
            setRefreshSeq((n) => n + 1);
            return "applied";
          }
          if (receipt.state === "denied") {
            await deletePendingOutboxApi().catch(() => undefined);
            setOutbox({
              lifecycle: "denied",
              intent,
              receipt,
              errorMessage:
                receipt.denial_code === "conflicting_revision"
                  ? "State changed before your action was applied (conflicting_revision). Review the updated values and decide again."
                  : `Denied: ${receipt.denial_code ?? receipt.disposition ?? "rejected"}`,
              quarantineNotice: null,
            });
            await loadMeta();
            setRefreshSeq((n) => n + 1);
            return "denied";
          }
          setOutbox({
            lifecycle: "accepted",
            intent,
            receipt,
            errorMessage: null,
            quarantineNotice: null,
          });
          return "accepted";
        } catch (err) {
          if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
            await deletePendingOutboxApi().catch(() => undefined);
            if (err.status === 401) {
              clearData(err.message, "unauthorized");
              return "denied";
            }
            setOutbox({
              lifecycle: "denied",
              intent,
              receipt: null,
              errorMessage: err.message,
              quarantineNotice: null,
            });
            return "denied";
          }
          // Transport timeout / 5xx -> request outcome is unknown; retain board-pending file!
          setOutbox({
            lifecycle: "unknown",
            intent,
            receipt: null,
            errorMessage:
              err instanceof Error
                ? `Connection interrupted after send (${err.message}). Check original request before retrying.`
                : "Outcome unknown. Check original request before retrying.",
            quarantineNotice: null,
          });
          return "unknown";
        }
      } finally {
        submittingRef.current = false;
      }
    },
    [clearData, loadMeta]
  );

  const lookupUnknownCommand = useCallback(async () => {
    const intent = outbox.intent;
    if (!intent) return;
    try {
      const receipt = await lookupCommandReceiptApi(
        intent.command_id,
        intent.command_type
      );
      if (receipt.status === "not_found" || (!receipt.state && receipt.status !== "found")) {
        setOutbox({
          lifecycle: "not_found_retry_choice",
          intent,
          receipt,
          errorMessage:
            "Original request was not recorded on the daemon. Choose whether to retry the exact original request or discard it.",
          quarantineNotice: null,
        });
        return;
      }
      if (receipt.state === "applied") {
        try {
          await deletePendingOutboxApi();
        } catch {
          setOutbox({
            lifecycle: "unknown",
            intent,
            receipt,
            errorMessage: "Applied on daemon, but failed to remove pending file.",
            quarantineNotice: null,
          });
          return;
        }
        setOutbox({
          lifecycle: "applied",
          intent,
          receipt,
          errorMessage: null,
          quarantineNotice: null,
        });
        await loadMeta();
        setRefreshSeq((n) => n + 1);
      } else if (receipt.state === "denied") {
        await deletePendingOutboxApi().catch(() => undefined);
        setOutbox({
          lifecycle: "denied",
          intent,
          receipt,
          errorMessage: receipt.denial_code ?? "Denied",
          quarantineNotice: null,
        });
        await loadMeta();
        setRefreshSeq((n) => n + 1);
      } else {
        setOutbox({
          lifecycle: "accepted",
          intent,
          receipt,
          errorMessage: null,
          quarantineNotice: null,
        });
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        await deletePendingOutboxApi().catch(() => undefined);
        setOutbox({
          lifecycle: "idle",
          intent: null,
          receipt: null,
          errorMessage: null,
          quarantineNotice: "Stale unowned pending receipt returned 403 and was cleared.",
        });
        return;
      }
      setOutbox((prev) => ({
        ...prev,
        lifecycle: "unknown",
        errorMessage:
          err instanceof Error ? err.message : "Could not reach daemon to verify receipt.",
      }));
    }
  }, [loadMeta, outbox.intent]);

  const retryNotFoundCommand = useCallback(async () => {
    if (!outbox.intent) return;
    await dispatchCommand(outbox.intent);
  }, [dispatchCommand, outbox.intent]);

  const discardPendingCommand = useCallback(async () => {
    await deletePendingOutboxApi().catch(() => undefined);
    setOutbox({
      lifecycle: "idle",
      intent: null,
      receipt: null,
      errorMessage: null,
      quarantineNotice: null,
    });
  }, []);

  const dismissFinalOutbox = useCallback(() => {
    setOutbox((prev) =>
      prev.lifecycle === "applied" || prev.lifecycle === "denied" || prev.lifecycle === "idle"
        ? {
            lifecycle: "idle",
            intent: null,
            receipt: null,
            errorMessage: null,
            quarantineNotice: null,
          }
        : prev
    );
  }, []);

  const forceRefresh = useCallback(async (): Promise<boolean> => {
    const loaded = await loadMeta();
    setRefreshSeq((n) => n + 1);
    return loaded !== null;
  }, [loadMeta]);

  const controlsLocked =
    linkState !== "live" ||
    hostLocked ||
    outbox.lifecycle === "submitting" ||
    outbox.lifecycle === "unknown" ||
    outbox.lifecycle === "accepted" ||
    outbox.lifecycle === "not_found_retry_choice";

  const canWrite =
    !controlsLocked &&
    meta !== null &&
    !meta.board_access.bootstrap_locked &&
    meta.board_access.role === "board_control";

  const value = useMemo<AppContextValue>(
    () => ({
      route,
      routePathname,
      routeSearchParams,
      performanceMode,
      daemonMode,
      activeApprovalQuestionId,
      navigate,
      focusReturnId,
      clearFocusReturnId,
      pendingAskContext,
      setPendingAskContext,
      askCeo,
      registerEscapeBack,
      getLastSeenSeq,
      markSeenSeq,
      unreadCeoReply,
      markMessagesVisited,
      appearance,
      setAppearance,
      material,
      setMaterial,
      toasts,
      pushToast,
      dismissToast,
      meta,
      linkState,
      lastError,
      refreshSeq,
      lastEventSeq,
      recentEventTypes,
      hostLocked,
      setHostLocked,
      controlsLocked,
      canWrite,
      outbox,
      dispatchCommand,
      lookupUnknownCommand,
      retryNotFoundCommand,
      discardPendingCommand,
      dismissFinalOutbox,
      reportApiError,
      forceRefresh,
    }),
    [
      route,
      routePathname,
      routeSearchParams,
      performanceMode,
      daemonMode,
      activeApprovalQuestionId,
      navigate,
      focusReturnId,
      clearFocusReturnId,
      pendingAskContext,
      setPendingAskContext,
      askCeo,
      registerEscapeBack,
      getLastSeenSeq,
      markSeenSeq,
      unreadCeoReply,
      markMessagesVisited,
      appearance,
      setAppearance,
      material,
      setMaterial,
      toasts,
      pushToast,
      dismissToast,
      meta,
      linkState,
      lastError,
      refreshSeq,
      lastEventSeq,
      recentEventTypes,
      hostLocked,
      controlsLocked,
      canWrite,
      outbox,
      dispatchCommand,
      lookupUnknownCommand,
      retryNotFoundCommand,
      discardPendingCommand,
      dismissFinalOutbox,
      reportApiError,
      forceRefresh,
    ]
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) {
    throw new Error("useApp must be used inside AppProvider");
  }
  return ctx;
}

export function useEscapeBack(targetPath: string, focusId?: string): void {
  const { registerEscapeBack } = useApp();
  useEffect(
    () => registerEscapeBack(targetPath, focusId),
    [registerEscapeBack, targetPath, focusId]
  );
}
