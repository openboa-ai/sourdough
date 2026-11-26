import type { IconName } from "@openboa/yogurt";

export interface DomainNavItem {
  id: string;
  label: string;
  path: string;
  icon: IconName;
  shortcut?: string;
  keywords: string[];
  matches: (pathname: string) => boolean;
}

export interface DomainModuleDefinition {
  /** Logical module id (e.g. "trading", future "content", "saas"). */
  id: string;
  /** Backend active_domains token activating this module (e.g. "paper" for Trading). */
  domainKey: string;
  /** Single-word sidebar section label. */
  label: string;
  /** Plain description shown in Settings -> Installed Modules. */
  description: string;
  /** Landing path when this module is the sole active business module. */
  defaultPath: string;
  /** Navigation items rendered inside this module's sidebar section. */
  navItems: DomainNavItem[];
}

export const TRADING_DOMAIN_MODULE: DomainModuleDefinition = {
  id: "trading",
  domainKey: "paper",
  label: "Trading",
  description:
    "Live production trading, multi-book company paper trading, and strategy release evaluation.",
  defaultPath: "/",
  navItems: [
    {
      id: "trading-live",
      label: "Live Trading",
      path: "/",
      icon: "chart",
      shortcut: "G L",
      keywords: [
        "Live",
        "Trading",
        "Production",
        "Home",
        "Promotion",
        "Qualification",
        "Binance",
      ],
      matches: (p) =>
        p === "/" ||
        p === "/trading/live" ||
        p === "/company/execution-tiers",
    },
    {
      id: "trading-paper",
      label: "Paper Trading",
      path: "/trading/paper",
      icon: "clock",
      shortcut: "G P",
      keywords: [
        "Paper",
        "Trading",
        "Portfolio",
        "Simulation",
        "Orders",
        "Positions",
        "Performance",
        "PnL",
      ],
      matches: (p) =>
        p === "/trading/paper" ||
        p === "/paper" ||
        p === "/performance" ||
        p.startsWith("/orders/"),
    },
    {
      id: "trading-strategies",
      label: "Strategies",
      path: "/trading/strategies",
      icon: "puzzle",
      shortcut: "G S",
      keywords: [
        "Strategies",
        "Releases",
        "Trials",
        "Challenger",
        "Deployments",
        "History",
        "Rollback",
      ],
      matches: (p) =>
        p === "/trading/strategies" || p === "/company/strategy-trials",
    },
  ],
};

/** Canonical registry of pluggable business workload modules mounted above Company OS. */
export const REGISTERED_DOMAIN_MODULES: readonly DomainModuleDefinition[] = [
  TRADING_DOMAIN_MODULE,
];

export function resolveActiveDomainModules(
  activeDomains: readonly string[] | null | undefined
): DomainModuleDefinition[] {
  if (!activeDomains || activeDomains.length === 0) {
    return [];
  }
  const activeSet = new Set(activeDomains.map((d) => d.toLowerCase()));
  return REGISTERED_DOMAIN_MODULES.filter((mod) =>
    activeSet.has(mod.domainKey.toLowerCase()) || activeSet.has(mod.id.toLowerCase())
  );
}

export type HomeResolutionMode = "single_module" | "multi_overview" | "core_only";

export function resolveHomeMode(
  modules: readonly DomainModuleDefinition[]
): HomeResolutionMode {
  if (modules.length === 0) return "core_only";
  if (modules.length === 1) return "single_module";
  return "multi_overview";
}
