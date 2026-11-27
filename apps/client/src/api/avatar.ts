import { getAgentFamily, type AgentVariant } from "@openboa/yogurt";

const VALID_AVATAR_VARIANTS = new Set(Object.keys(getAgentFamily().variants));
const DEFAULT_AVATAR_VARIANT: AgentVariant = "wide-oval-top";

export function normalizeAvatarVariant(raw: string | null | undefined): AgentVariant {
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (VALID_AVATAR_VARIANTS.has(trimmed)) {
      return trimmed as AgentVariant;
    }
  }
  return DEFAULT_AVATAR_VARIANT;
}
