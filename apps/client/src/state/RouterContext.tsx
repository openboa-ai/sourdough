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
import type { ContextRef } from "../api/types";
import { focusElementByDataId, hasOpenOverlay, isEditableElement } from "../lib/domFocus";
import {
  decodeSingleSegment,
  defaultEscapeBackTarget,
  isMessagesPathname,
  isTier1Path,
  normalizeRoute,
  parseAskContextFromSearch,
  sanitizeAskContext,
  splitRoute,
} from "../lib/routes";
import { usePreferences } from "./PreferencesContext";

export interface RouterContextValue {
  route: string;
  routePathname: string;
  routeSearchParams: URLSearchParams;
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
  routeRef: React.RefObject<string>;
  subscribePathChange: (listener: (nextPath: string, prevPath: string) => void) => () => void;
  clearPendingAskContextDirect: () => void;
}

const RouterContext = createContext<RouterContextValue | null>(null);

export function RouterProvider({ children }: { children: ReactNode }) {
  const { hostLockedRef } = usePreferences();

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

  const routeRef = useRef<string>(route);
  routeRef.current = route;
  const pendingReturnRef = useRef<{ path: string; focusId: string } | null>(null);
  const focusTargetPathRef = useRef<string | null>(null);
  const escapeRegRef = useRef<{ token: symbol; targetPath: string; focusId?: string } | null>(
    null
  );
  const pathListenersRef = useRef<Set<(nextPath: string, prevPath: string) => void>>(
    new Set()
  );

  const subscribePathChange = useCallback(
    (listener: (nextPath: string, prevPath: string) => void) => {
      pathListenersRef.current.add(listener);
      return () => {
        pathListenersRef.current.delete(listener);
      };
    },
    []
  );

  const notifyPathChange = useCallback((nextPath: string, prevPath: string) => {
    for (const listener of pathListenersRef.current) {
      listener(nextPath, prevPath);
    }
  }, []);

  const { pathname: routePathname, search: routeSearch } = useMemo(
    () => splitRoute(route),
    [route]
  );
  const routeSearchParams = useMemo(
    () => new URLSearchParams(routeSearch.startsWith("?") ? routeSearch.slice(1) : routeSearch),
    [routeSearch]
  );
  const activeApprovalQuestionId = useMemo(
    () => decodeSingleSegment(routePathname, "/approvals/"),
    [routePathname]
  );

  const clearPendingAskContextDirect = useCallback(() => {
    setPendingAskContextState(null);
  }, []);

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
        notifyPathChange(nextPath, prevPath);
      }
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [notifyPathChange]);

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
        notifyPathChange(nextPath, prevSplit.pathname);
        if (typeof window.scrollTo === "function") {
          window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
        }
      }
    },
    [notifyPathChange]
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

    const tryFocus = () => {
      if (!focusElementByDataId(focusReturnId)) return false;
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
  }, [hostLockedRef, navigate]);

  const value = useMemo<RouterContextValue>(
    () => ({
      route,
      routePathname,
      routeSearchParams,
      activeApprovalQuestionId,
      navigate,
      focusReturnId,
      clearFocusReturnId,
      pendingAskContext,
      setPendingAskContext,
      askCeo,
      registerEscapeBack,
      routeRef,
      subscribePathChange,
      clearPendingAskContextDirect,
    }),
    [
      route,
      routePathname,
      routeSearchParams,
      activeApprovalQuestionId,
      navigate,
      focusReturnId,
      clearFocusReturnId,
      pendingAskContext,
      setPendingAskContext,
      askCeo,
      registerEscapeBack,
      subscribePathChange,
      clearPendingAskContextDirect,
    ]
  );

  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

export function useRouter(): RouterContextValue {
  const ctx = useContext(RouterContext);
  if (!ctx) {
    throw new Error("useRouter must be used inside RouterProvider");
  }
  return ctx;
}

export function useEscapeBack(targetPath: string, focusId?: string): void {
  const { registerEscapeBack } = useRouter();
  useEffect(
    () => registerEscapeBack(targetPath, focusId),
    [registerEscapeBack, targetPath, focusId]
  );
}
