import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { modelColor, relativeTime, shortModel } from "./format";

describe("format", () => {
  const NOW = Date.UTC(2026, 8, 23, 12, 0, 0);
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW); });
  afterEach(() => vi.useRealTimers());

  it("relativeTime takes the store's epoch seconds", () => {
    expect(relativeTime(NOW / 1000 - 30)).toBe("30s");
    expect(relativeTime(NOW / 1000 - 3 * 86400)).toBe("3d");
  });

  it("relativeTime takes a log entry's ISO timestamp, with the same units", () => {
    expect(relativeTime(new Date(NOW - 5 * 60_000).toISOString())).toBe("5m");
    expect(relativeTime(new Date(NOW - 30 * 3_600_000).toISOString())).toBe("1d");
  });

  it("a timestamp slightly in the future reads as now, not negative", () => {
    expect(relativeTime(NOW / 1000 + 5)).toBe("0s");
  });

  it("shortModel drops the provider and the date suffix", () => {
    expect(shortModel("deepseek/deepseek-v4-pro-0813")).toBe("deepseek-v4-pro");
    expect(shortModel("glm-5.3-flash")).toBe("glm-5.3-flash");
  });

  it("modelColor is per family, with a neutral fallback", () => {
    expect(modelColor("anthropic/claude-x")).toBe(modelColor("claude-y"));
    expect(modelColor("something-unknown")).toBe("#8b93a1");
  });
});
