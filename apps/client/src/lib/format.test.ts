import { describe, expect, it } from "vitest";
import {
  formatDeadlineNs,
  formatMinorCurrency,
  formatNsClock,
  formatRelativeNs,
  formatSignedPct,
  formatTimestamp,
  newCommandId,
  normalizeToNs,
} from "./format";

describe("lib/format", () => {
  it("never coerces null, undefined, or non-finite values to zero", () => {
    expect(formatMinorCurrency(null)).toBe("Unavailable");
    expect(formatMinorCurrency(undefined)).toBe("Unavailable");
    expect(formatMinorCurrency(Number.NaN)).toBe("Unavailable");
    expect(formatMinorCurrency(Number.POSITIVE_INFINITY)).toBe("Unavailable");
    expect(formatSignedPct(null)).toBe("Unavailable");
    expect(formatSignedPct(undefined)).toBe("Unavailable");
    expect(formatSignedPct(Number.NaN)).toBe("Unavailable");
  });

  it("formats signed and unsigned minor-unit currencies honestly", () => {
    expect(formatMinorCurrency(0)).toBe("$0.00");
    expect(formatMinorCurrency(123456)).toBe("$1,234.56");
    expect(formatMinorCurrency(123456, "USD", { signed: true })).toBe("+$1,234.56");
    expect(formatMinorCurrency(-5025, "USD", { signed: true })).toBe("-$50.25");
    expect(formatMinorCurrency(1000, "INVALID")).toBe("$10.00");
  });

  it("formats signed percentages accurately", () => {
    expect(formatSignedPct(0)).toBe("0.00%");
    expect(formatSignedPct(4.25)).toBe("+4.25%");
    expect(formatSignedPct(-1.5)).toBe("-1.50%");
  });

  it("normalizes seconds, milliseconds, and nanoseconds to ns", () => {
    expect(normalizeToNs(null)).toBeNull();
    expect(normalizeToNs(0)).toBeNull();
    expect(normalizeToNs(1_700_000_000)).toBe(1_700_000_000_000_000_000);
    expect(normalizeToNs(1_700_000_000_000)).toBe(1_700_000_000_000_000_000);
    expect(normalizeToNs(1_700_000_000_000_000_000)).toBe(1_700_000_000_000_000_000);
  });

  it("handles missing temporal readings safely", () => {
    expect(formatRelativeNs(null)).toBe("Not scheduled");
    expect(formatNsClock(null)).toBe("No reading");
    expect(formatDeadlineNs(null)).toBe("No deadline");
    expect(formatTimestamp(null)).toBe("—");
  });

  it("generates unique prefixed command ids", () => {
    const a = newCommandId("paper");
    const b = newCommandId("paper");
    expect(a.startsWith("paper-")).toBe(true);
    expect(b.startsWith("paper-")).toBe(true);
    expect(a).not.toBe(b);
  });
});
