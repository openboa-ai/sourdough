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

export interface ToastItem {
  id: string;
  message: string;
}

export interface PreferencesContextValue {
  appearance: Appearance;
  setAppearance: (next: Appearance) => void;
  material: Material;
  setMaterial: (next: Material) => void;
  toasts: ToastItem[];
  pushToast: (message: string) => void;
  dismissToast: (id: string) => void;
  hostLocked: boolean;
  setHostLocked: (locked: boolean) => void;
  hostLockedRef: React.RefObject<boolean>;
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null);

export function PreferencesProvider({ children }: { children: ReactNode }) {
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
  const [hostLocked, setHostLocked] = useState(false);
  const hostLockedRef = useRef<boolean>(hostLocked);
  hostLockedRef.current = hostLocked;

  // Mount Yogurt agent bridge disabled by default (allow: () => false).
  useEffect(() => {
    try {
      createYogurtAgent({ allow: () => false });
    } catch {
      // Safe no-op in non-browser test hosts.
    }
  }, []);

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

  const value = useMemo<PreferencesContextValue>(
    () => ({
      appearance,
      setAppearance,
      material,
      setMaterial,
      toasts,
      pushToast,
      dismissToast,
      hostLocked,
      setHostLocked,
      hostLockedRef,
    }),
    [appearance, setAppearance, material, setMaterial, toasts, pushToast, dismissToast, hostLocked]
  );

  return (
    <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>
  );
}

export function usePreferences(): PreferencesContextValue {
  const ctx = useContext(PreferencesContext);
  if (!ctx) {
    throw new Error("usePreferences must be used inside PreferencesProvider");
  }
  return ctx;
}
