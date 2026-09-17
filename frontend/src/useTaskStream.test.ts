import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useTaskStream } from "./useTaskStream";

// The invariant this hook exists for: a WebSocket only carries events from
// the moment it opens, so it can never replay what already happened. The REST
// snapshot is what makes a page opened (or reconnected) mid-task show real
// history instead of starting blank. Audit H6 records that every reconnect
// path once called connect() directly, so only the FIRST connection ever
// fetched a snapshot — a network blip left the UI permanently behind.

const getTask = vi.fn();
const getMe = vi.fn(async () => ({ id: 1 }));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getTask: (...a: unknown[]) => getTask(...a),
    getMe: () => getMe(),
    taskStreamUrl: (id: string) => `ws://test/stream/${id}`,
  };
});

/** A WebSocket stand-in whose lifecycle the test drives by hand. */
class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code?: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 0;
  sent: string[] = [];
  url: string;

  // Explicit field rather than a constructor parameter property: this repo
  // sets erasableSyntaxOnly, which rejects the shorthand.
  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000 });
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  drop(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

const snapshot = (over: Record<string, unknown> = {}) => ({
  meta: { task_id: "t1", status: "running", repo: "webapp", goal: "g", budget_usd: 5, created_at: 0 },
  state: { execution_log: [{ node: "work", summary: "already happened", detail: "", cost_usd: 0, timestamp: new Date().toISOString(), step_id: null }], plan: [] },
  orphaned: false,
  ...over,
});

beforeEach(() => {
  FakeSocket.instances = [];
  getTask.mockReset();
  getTask.mockResolvedValue(snapshot());
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
});

describe("useTaskStream", () => {
  it("does nothing without a task", () => {
    renderHook(() => useTaskStream(null, null));
    expect(getTask).not.toHaveBeenCalled();
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it("fetches the REST snapshot so history opened mid-task is not blank", async () => {
    const { result } = renderHook(() => useTaskStream("t1", "webapp"));
    await waitFor(() => expect(getTask).toHaveBeenCalledWith("t1", "webapp"));
    await waitFor(() => expect(result.current.log.length).toBeGreaterThan(0));
    expect(result.current.log[0].summary).toBe("already happened");
  });

  it("opens a socket for the task", async () => {
    renderHook(() => useTaskStream("t1", "webapp"));
    await waitFor(() => expect(FakeSocket.instances.length).toBeGreaterThan(0));
    expect(FakeSocket.instances[0].url).toContain("t1");
  });

  it("re-fetches the snapshot on every reconnect, not just the first connect", async () => {
    // Regression for audit H6.
    renderHook(() => useTaskStream("t1", "webapp"));
    await waitFor(() => expect(FakeSocket.instances.length).toBeGreaterThan(0));
    await waitFor(() => expect(getTask).toHaveBeenCalledTimes(1));

    FakeSocket.instances[0].open();
    FakeSocket.instances[0].drop(1006); // unexpected drop, not a clean close

    await waitFor(() => expect(getTask.mock.calls.length).toBeGreaterThan(1), { timeout: 5000 });
  });

  it("retries a failing snapshot rather than giving up silently", async () => {
    getTask.mockRejectedValueOnce(new Error("boom")).mockResolvedValue(snapshot());
    renderHook(() => useTaskStream("t1", "webapp"));
    await waitFor(() => expect(getTask.mock.calls.length).toBeGreaterThan(1), { timeout: 5000 });
  });

  it("stops when the task changes and starts fresh for the new one", async () => {
    const { rerender } = renderHook(({ id }) => useTaskStream(id, "webapp"), {
      initialProps: { id: "t1" },
    });
    await waitFor(() => expect(getTask).toHaveBeenCalledWith("t1", "webapp"));

    rerender({ id: "t2" });
    await waitFor(() => expect(getTask).toHaveBeenCalledWith("t2", "webapp"));
  });

  it("surfaces an orphaned task so it can be resumed rather than looking busy forever", async () => {
    // The store says "running" but nothing is driving it — a backend restart
    // mid-run. Without this the task sits there looking active indefinitely.
    getTask.mockResolvedValue(snapshot({ orphaned: true }));
    const { result } = renderHook(() => useTaskStream("t1", "webapp"));
    await waitFor(() => expect(result.current.orphaned).toBe(true));
  });

  it("applies events that arrive over the socket", async () => {
    const { result } = renderHook(() => useTaskStream("t1", "webapp"));
    await waitFor(() => expect(FakeSocket.instances.length).toBeGreaterThan(0));
    const ws = FakeSocket.instances[0];
    ws.open();

    ws.onmessage?.({
      data: JSON.stringify({
        execution_log: [
          {
            node: "work",
            summary: "arrived live",
            detail: "",
            cost_usd: 0,
            timestamp: new Date().toISOString(),
            step_id: null,
          },
        ],
      }),
    });

    await waitFor(() =>
      expect(result.current.log.some((e) => e.summary === "arrived live")).toBe(true),
    );
  });

  it("ignores a malformed frame instead of tearing the stream down", async () => {
    const { result } = renderHook(() => useTaskStream("t1", "webapp"));
    await waitFor(() => expect(FakeSocket.instances.length).toBeGreaterThan(0));
    const ws = FakeSocket.instances[0];
    ws.open();

    // The frame is discarded and the stream carries on; an unguarded
    // JSON.parse threw a bare SyntaxError into the event loop instead.
    expect(() => ws.onmessage?.({ data: "not json at all" })).not.toThrow();

    ws.onmessage?.({
      data: JSON.stringify({
        execution_log: [
          { node: "work", summary: "still working", detail: "", cost_usd: 0, timestamp: new Date().toISOString(), step_id: null },
        ],
      }),
    });
    await waitFor(() =>
      expect(result.current.log.some((e) => e.summary === "still working")).toBe(true),
    );
  });
});
