import { useMemo, type ReactNode } from "react";
import type { Appearance, Material } from "@openboa/yogurt";
import { clearProjectionCache } from "../api/useProjection";
import type {
  CommandLifecycle,
  ContextRef,
  ControlPlaneMeta,
  LocalCommandIntent,
} from "../api/types";
import { decodeSingleSegment, splitRoute } from "../lib/routes";
import { ApprovalsCacheProvider, useApprovalsCache } from "./ApprovalsCacheContext";
import {
  ControlPlaneProvider,
  useControlPlane,
  type LinkState,
} from "./ControlPlaneContext";
import {
  OutboxProvider,
  useOutbox,
  type CommandOutboxState,
} from "./OutboxContext";
import {
  PreferencesProvider,
  usePreferences,
  type ToastItem,
} from "./PreferencesContext";
import { RouterProvider, useEscapeBack, useRouter } from "./RouterContext";

export {
  decodeSingleSegment,
  splitRoute,
  useApprovalsCache,
  useControlPlane,
  useEscapeBack,
  useOutbox,
  usePreferences,
  useRouter,
  type CommandOutboxState,
  type LinkState,
  type ToastItem,
};

export interface AppContextValue {
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

export function AppProvider({ children }: { children: ReactNode }) {
  return (
    <PreferencesProvider>
      <RouterProvider>
        <ControlPlaneProvider onClearCache={clearProjectionCache}>
          <OutboxProvider>
            <ApprovalsCacheProvider>{children}</ApprovalsCacheProvider>
          </OutboxProvider>
        </ControlPlaneProvider>
      </RouterProvider>
    </PreferencesProvider>
  );
}

/**
 * Backward-compatible composite hook. Prefer the narrow domain hooks
 * (`useRouter`, `usePreferences`, `useControlPlane`, `useOutbox`, `useApprovalsCache`)
 * in new and refactored components to avoid unnecessary re-renders on event ticks.
 */
export function useApp(): AppContextValue {
  const prefs = usePreferences();
  const router = useRouter();
  const cp = useControlPlane();
  const ob = useOutbox();

  return useMemo<AppContextValue>(
    () => ({
      route: router.route,
      routePathname: router.routePathname,
      routeSearchParams: router.routeSearchParams,
      activeApprovalQuestionId: router.activeApprovalQuestionId,
      navigate: router.navigate,
      focusReturnId: router.focusReturnId,
      clearFocusReturnId: router.clearFocusReturnId,
      pendingAskContext: router.pendingAskContext,
      setPendingAskContext: router.setPendingAskContext,
      askCeo: router.askCeo,
      registerEscapeBack: router.registerEscapeBack,
      getLastSeenSeq: cp.getLastSeenSeq,
      markSeenSeq: cp.markSeenSeq,
      unreadCeoReply: cp.unreadCeoReply,
      markMessagesVisited: cp.markMessagesVisited,
      appearance: prefs.appearance,
      setAppearance: prefs.setAppearance,
      material: prefs.material,
      setMaterial: prefs.setMaterial,
      toasts: prefs.toasts,
      pushToast: prefs.pushToast,
      dismissToast: prefs.dismissToast,
      meta: cp.meta,
      linkState: cp.linkState,
      lastError: cp.lastError,
      refreshSeq: cp.refreshSeq,
      lastEventSeq: cp.lastEventSeq,
      recentEventTypes: cp.recentEventTypes,
      hostLocked: prefs.hostLocked,
      setHostLocked: prefs.setHostLocked,
      controlsLocked: ob.controlsLocked,
      canWrite: ob.canWrite,
      outbox: ob.outbox,
      dispatchCommand: ob.dispatchCommand,
      lookupUnknownCommand: ob.lookupUnknownCommand,
      retryNotFoundCommand: ob.retryNotFoundCommand,
      discardPendingCommand: ob.discardPendingCommand,
      dismissFinalOutbox: ob.dismissFinalOutbox,
      reportApiError: cp.reportApiError,
      forceRefresh: cp.forceRefresh,
    }),
    [prefs, router, cp, ob]
  );
}
