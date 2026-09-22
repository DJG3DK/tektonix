import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

const getGitHubInbox = vi.fn();
vi.mock("./api", () => ({ getGitHubInbox: () => getGitHubInbox() }));

import { useGitHubInboxCount } from "./useGitHubInboxCount";

function item(state: string) {
  return { key: `k${Math.random()}`, repo: "demo", state, title: "t", url: "u", updated_at: 0 };
}

beforeEach(() => {
  getGitHubInbox.mockReset();
  vi.useRealTimers();
});

describe("useGitHubInboxCount", () => {
  it("counts only the items waiting on a decision", async () => {
    // A badge that counted `seen` would be lit permanently on a busy project,
    // and a badge that is always on is a badge nobody reads. `snoozed` was
    // deferred on purpose; `task_created` is already actioned.
    getGitHubInbox.mockResolvedValue({
      items: [item("proposed"), item("proposed"), item("seen"), item("snoozed"),
              item("task_created"), item("dismissed"), item("resolved")],
      last_poll: null,
    });
    const { result } = renderHook(() => useGitHubInboxCount());
    await waitFor(() => expect(result.current).toBe(2));
  });

  it("reports zero when nothing is waiting, so the badge hides", async () => {
    getGitHubInbox.mockResolvedValue({ items: [item("seen"), item("resolved")], last_poll: null });
    const { result } = renderHook(() => useGitHubInboxCount());
    await waitFor(() => expect(result.current).toBe(0));
  });

  it("shows no badge rather than a zero when the request fails", async () => {
    // null and 0 are different answers: one is "nothing waiting", the other
    // is "we could not tell", and only the first should render a confident 0.
    getGitHubInbox.mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useGitHubInboxCount());
    await waitFor(() => expect(result.current).toBeNull());
  });

  it("re-reads when the key changes, so acting on an item clears it promptly", async () => {
    getGitHubInbox.mockResolvedValue({ items: [item("proposed")], last_poll: null });
    const { result, rerender } = renderHook(({ k }) => useGitHubInboxCount(k),
                                            { initialProps: { k: "github" } });
    await waitFor(() => expect(result.current).toBe(1));

    getGitHubInbox.mockResolvedValue({ items: [], last_poll: null });
    rerender({ k: "task" });
    await waitFor(() => expect(result.current).toBe(0));
  });

  it("does not set state after unmount", async () => {
    // A poll landing after the sidebar is gone is a React warning and a leak.
    let resolve!: (v: unknown) => void;
    getGitHubInbox.mockReturnValue(new Promise((r) => { resolve = r; }));
    const { unmount } = renderHook(() => useGitHubInboxCount());
    unmount();
    resolve({ items: [item("proposed")], last_poll: null });
    await new Promise((r) => setTimeout(r, 10));   // no unhandled update
  });
});
