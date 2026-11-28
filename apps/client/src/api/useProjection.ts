import { useCallback, useEffect, useRef, useState } from "react";
import { fetchProjection } from "./client";
import { useControlPlane } from "../state/ControlPlaneContext";

interface CacheEntry {
  etag: string | null;
  data: unknown;
}

const projectionCache = new Map<string, CacheEntry>();

function cacheKeyFor(companyId: string | null | undefined, path: string): string {
  return `${companyId || "_anon"}::${path}`;
}

export function clearProjectionCache(): void {
  projectionCache.clear();
}

export function getCachedProjection<T>(
  path: string,
  companyId?: string | null
): { etag: string | null; data: T } | null {
  const hit = projectionCache.get(cacheKeyFor(companyId, path));
  if (!hit) return null;
  return { etag: hit.etag, data: hit.data as T };
}

export interface UseProjectionOptions {
  enabled?: boolean;
  /** Override the default refresh trigger (defaults to ControlPlane refreshSeq). */
  refreshKey?: number | string;
}

export interface UseProjectionResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  etag: string | null;
  notModifiedCount: number;
  refetch: () => void;
}

/**
 * Shared ETag / 304-aware projection hook with AbortController cancellation,
 * per-company cache isolation, and ControlPlane refreshSeq reactivity.
 */
export function useProjection<T>(
  path: string | null,
  opts?: UseProjectionOptions
): UseProjectionResult<T> {
  const { meta, metaReady, linkState, refreshSeq, reportApiError } = useControlPlane();
  const companyId = meta?.company_id ?? null;
  const enabled =
    (opts?.enabled ?? true) &&
    Boolean(path) &&
    metaReady &&
    linkState !== "unauthorized";
  const effectiveRefresh = opts?.refreshKey ?? refreshSeq;

  const [manualTick, setManualTick] = useState(0);
  const refetch = useCallback(() => setManualTick((n) => n + 1), []);

  const initialHit = path ? getCachedProjection<T>(path, companyId) : null;
  const [data, setData] = useState<T | null>(() => initialHit?.data ?? null);
  const [etag, setEtag] = useState<string | null>(() => initialHit?.etag ?? null);
  const [loading, setLoading] = useState<boolean>(() => Boolean(enabled && !initialHit));
  const [error, setError] = useState<string | null>(null);
  const [notModifiedCount, setNotModifiedCount] = useState<number>(0);

  const reqIdRef = useRef(0);

  // Sync from cache or reset immediately when path / company scope changes.
  useEffect(() => {
    if (!path) {
      setData(null);
      setEtag(null);
      setLoading(false);
      setError(null);
      return;
    }
    const cached = getCachedProjection<T>(path, companyId);
    if (cached) {
      setData(cached.data);
      setEtag(cached.etag);
      setLoading(false);
    } else {
      setData(null);
      setEtag(null);
      setLoading(enabled);
    }
    setError(null);
  }, [path, companyId, enabled]);

  useEffect(() => {
    if (!enabled || !path) {
      setLoading(false);
      return;
    }

    const reqId = ++reqIdRef.current;
    const controller = new AbortController();
    const key = cacheKeyFor(companyId, path);
    const cached = projectionCache.get(key);

    if (!cached) {
      setLoading(true);
    }

    void (async () => {
      try {
        const res = await fetchProjection<T>(path, {
          etag: cached?.etag ?? undefined,
          signal: controller.signal,
        });
        if (controller.signal.aborted || reqIdRef.current !== reqId) return;

        if (res.status === 304) {
          if (cached) {
            const nextEtag = res.etag ?? cached.etag;
            projectionCache.set(key, { etag: nextEtag, data: cached.data });
            setEtag(nextEtag);
            setData(cached.data as T);
            setNotModifiedCount((n) => n + 1);
          }
          setError(null);
          setLoading(false);
          return;
        }

        if (res.data !== null) {
          projectionCache.set(key, { etag: res.etag, data: res.data });
          setData(res.data);
          setEtag(res.etag);
        }
        setError(null);
        setLoading(false);
      } catch (err) {
        if (controller.signal.aborted || reqIdRef.current !== reqId) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Failed to load projection");
        setLoading(false);
      }
    })();

    return () => {
      controller.abort();
    };
  }, [enabled, path, companyId, effectiveRefresh, manualTick, reportApiError]);

  return { data, loading, error, etag, notModifiedCount, refetch };
}
