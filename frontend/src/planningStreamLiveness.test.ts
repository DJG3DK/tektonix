import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

/* A planning turn that ends while the operator's socket is silently dead.
 *
 * onclose is not a reliable death signal -- a half-open TCP (laptop sleep, NAT
 * idle-kill, a proxy dropping without a FIN) leaves the browser holding a
 * socket that will never fire another event. The turn's `error` and `closed`
 * events then go nowhere, and the UI sits on running:true forever: thinking
 * bubbles that never stop, a disabled composer, and no outcome banner -- that
 * banner only renders when NOT running, so the one thing that would explain
 * the ending is exactly what gets hidden. Observed live on the 2026-08-31
 * budget-exceeded turn.
 */

const getPlanningSession = vi.fn();
const sendPlanningMessage = vi.fn();

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getPlanningSession: (...a: unknown[]) => getPlanningSession(...a),
    sendPlanningMessage: (...a: unknown[]) => sendPlanningMessage(...a),
    planningStreamUrl: () => "ws://test/stream",
  };
});

const sockets: FakeSocket[] = [];

class FakeSocket {
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;
  constructor() {
    sockets.push(this);
    queueMicrotask(() => this.onopen?.());
  }
  close() {
    this.closed = true;
    this.onclose?.();
  }
}

beforeEach(() => {
  sockets.length = 0;
  getPlanningSession.mockReset();
  sendPlanningMessage.mockReset();
  sendPlanningMessage.mockResolvedValue({});
  getPlanningSession.mockResolvedValue({ log: [], running: false, meta: { plan_markdown: null, cost_usd: 0 } });
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function startTurn() {
  const { usePlanningStream } = await import("./usePlanningStream");
  const hook = renderHook(() => usePlanningStream("s1"));
  // Let the mount-time hydrate settle first: it writes `running` from the
  // server, and landing after sendMessage would stomp the true we just set.
  await act(async () => { await Promise.resolve(); });
  await act(async () => { await hook.result.current.sendMessage("go"); });
  return hook;
}

describe("planning socket liveness", () => {
  it("gives up on a socket that has gone quiet past three server pings", async () => {
    const hook = await startTurn();
    expect(hook.result.current.running).toBe(true);
    const ws = sockets[sockets.length - 1];

    await act(async () => { await vi.advanceTimersByTimeAsync(80_000); });
    // Closing is the recovery: it fires onclose, which reconnects and
    // re-hydrates `running` from the server -- the only authority on whether
    // a turn is actually still in flight.
    expect(ws.closed).toBe(true);
  });

  it("does not disturb a socket that is still hearing pings", async () => {
    const hook = await startTurn();
    const ws = sockets[sockets.length - 1];

    for (let i = 0; i < 5; i++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
      act(() => ws.onmessage?.({ data: JSON.stringify({ type: "ping" }) }));
    }
    expect(ws.closed).toBe(false);
    expect(hook.result.current.running).toBe(true);
  });

  it("clears running once the reconnect re-hydrates a finished turn", async () => {
    const hook = await startTurn();
    // The turn ended server-side while the socket was dead.
    getPlanningSession.mockResolvedValue({
      log: [], running: false, meta: { plan_markdown: null, cost_usd: 8.11 },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(80_000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    await waitFor(() => expect(hook.result.current.running).toBe(false));
    // And the real cost lands, rather than the stale zero.
    expect(hook.result.current.costUsd).toBe(8.11);
  });

  it("is not armed while the session is idle", async () => {
    const { usePlanningStream } = await import("./usePlanningStream");
    renderHook(() => usePlanningStream("s1"));
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    // No turn running means no socket to police and nothing to recover.
    expect(sockets.every((s) => !s.closed)).toBe(true);
  });

  it("drops an unparseable frame and keeps reading the stream", async () => {
    const hook = await startTurn();
    const ws = sockets[sockets.length - 1];
    const quiet = vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => act(() => ws.onmessage?.({ data: "not json at all" }))).not.toThrow();
    quiet.mockRestore();
    act(() => ws.onmessage?.({ data: JSON.stringify({ type: "closed" }) }));
    await waitFor(() => expect(hook.result.current.running).toBe(false));
  });

  it("still clears running on an ordinary in-band close", async () => {
    const hook = await startTurn();
    const ws = sockets[sockets.length - 1];
    act(() => ws.onmessage?.({ data: JSON.stringify({ type: "closed" }) }));
    await waitFor(() => expect(hook.result.current.running).toBe(false));
  });
});

describe("a session opened while a turn is in flight", () => {
  // The page hydrated and never connected, so a session opened mid-turn (a
  // reload, a second tab, a deep link) showed the snapshot and then nothing
  // until the watchdog noticed ~75s later (2026-09-29 audit, U4).
  it("opens a socket at once, without waiting for the watchdog", async () => {
    getPlanningSession.mockResolvedValue({ log: [], running: true, seq: 3, meta: { plan_markdown: null, cost_usd: 0 } });
    const { usePlanningStream } = await import("./usePlanningStream");
    const hook = renderHook(() => usePlanningStream("s1"));
    await waitFor(() => expect(sockets.length).toBe(1));
    expect(hook.result.current.running).toBe(true);
    const ws = sockets[0];
    act(() => ws.onmessage?.({ data: JSON.stringify({ type: "log_entry", seq: 4, entry: { id: "e4", kind: "agent", summary: "live", detail: "", timestamp: "t" } }) }));
    await waitFor(() => expect(hook.result.current.log.map((e) => e.summary)).toEqual(["live"]));
  });

  it("an event published between the socket opening and the snapshot is not lost, and one the snapshot holds is not doubled", async () => {
    // First call: the mount snapshot, which says a turn is running. Second
    // call: the socket-first snapshot, held open so frames can arrive on the
    // socket while it is in flight.
    let settle: (v: unknown) => void = () => {};
    const A = { id: "a", kind: "agent", summary: "A", detail: "", timestamp: "t" };
    const B = { id: "b", kind: "agent", summary: "B", detail: "", timestamp: "t" };
    const C = { id: "c", kind: "agent", summary: "C", detail: "", timestamp: "t" };
    getPlanningSession
      .mockResolvedValueOnce({ log: [A], running: true, seq: 1, meta: { plan_markdown: null, cost_usd: 0 } })
      .mockImplementationOnce(() => new Promise((r) => (settle = r)));
    const { usePlanningStream } = await import("./usePlanningStream");
    const hook = renderHook(() => usePlanningStream("s1"));
    await waitFor(() => expect(sockets.length).toBe(1));
    const ws = sockets[0];
    // B was published after the mount snapshot; the second snapshot has it.
    // C was published after that snapshot was taken. Both arrive on the
    // socket while the snapshot is in flight.
    act(() => ws.onmessage?.({ data: JSON.stringify({ type: "log_entry", seq: 2, entry: B }) }));
    act(() => ws.onmessage?.({ data: JSON.stringify({ type: "log_entry", seq: 3, entry: C }) }));
    expect(hook.result.current.log.map((e) => e.summary)).toEqual(["A"]);
    await act(async () => {
      settle({ log: [A, B], running: true, seq: 2, meta: { plan_markdown: null, cost_usd: 0.1 } });
      await Promise.resolve();
    });
    await waitFor(() => expect(hook.result.current.log.map((e) => e.summary)).toEqual(["A", "B", "C"]));
  });

  it("a socket replaced by the next turn does not start a reconnect", async () => {
    const hook = await startTurn();
    const first = sockets[sockets.length - 1];
    const before = getPlanningSession.mock.calls.length;
    await act(async () => { await hook.result.current.sendMessage("again"); });
    expect(first.closed).toBe(true);
    expect(sockets.length).toBe(2);
    // A reconnect re-hydrates; none was started for the superseded socket.
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(getPlanningSession.mock.calls.length).toBe(before);
    expect(sockets.length).toBe(2);
    expect(hook.result.current.running).toBe(true);
  });

  it("a reconnect merges by position, not by log length", async () => {
    const hook = await startTurn();
    const ws = sockets[sockets.length - 1];
    const X = { id: "x", kind: "agent", summary: "X", detail: "", timestamp: "t" };
    act(() => ws.onmessage?.({ data: JSON.stringify({ type: "log_entry", seq: 7, entry: X }) }));
    // The turn ended while the socket was dead. The server's snapshot is
    // SHORTER than what the page holds (its live buffer lost X to a restart)
    // -- the old length rule threw it away; the id keeps it.
    getPlanningSession.mockResolvedValue({
      log: [], running: false, seq: 0, meta: { plan_markdown: null, cost_usd: 1.5 },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(80_000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    await waitFor(() => expect(hook.result.current.running).toBe(false));
    expect(hook.result.current.log.filter((e) => e.id === "x")).toHaveLength(1);
  });
});

describe("a create_project proposal on the stream", () => {
  const proposal = { name: "my-app", description: "a store front", github: true };

  it("lands from turn_complete without waiting for a poll", async () => {
    const hook = await startTurn();
    const ws = sockets[sockets.length - 1];
    act(() => ws.onmessage?.({ data: JSON.stringify({ type: "turn_complete", plan_markdown: null, cost_usd: 0.2, new_project: proposal }) }));
    await waitFor(() => expect(hook.result.current.newProject).toEqual(proposal));
  });

  it("is cleared by a later turn that persisted none, and by clearNewProject", async () => {
    const hook = await startTurn();
    const ws = sockets[sockets.length - 1];
    act(() => ws.onmessage?.({ data: JSON.stringify({ type: "turn_complete", new_project: proposal }) }));
    await waitFor(() => expect(hook.result.current.newProject).toEqual(proposal));
    // The server sends the persisted value, null included -- a dismissed
    // proposal must not come back on the next turn.
    act(() => ws.onmessage?.({ data: JSON.stringify({ type: "turn_complete", new_project: null }) }));
    await waitFor(() => expect(hook.result.current.newProject).toBeNull());

    act(() => ws.onmessage?.({ data: JSON.stringify({ type: "turn_complete", new_project: proposal }) }));
    await waitFor(() => expect(hook.result.current.newProject).toEqual(proposal));
    act(() => hook.result.current.clearNewProject());
    await waitFor(() => expect(hook.result.current.newProject).toBeNull());
  });

  it("hydrates from the session meta", async () => {
    getPlanningSession.mockResolvedValue({
      log: [], running: false, meta: { plan_markdown: null, cost_usd: 0, new_project: proposal },
    });
    const { usePlanningStream } = await import("./usePlanningStream");
    const hook = renderHook(() => usePlanningStream("s1"));
    await waitFor(() => expect(hook.result.current.newProject).toEqual(proposal));
  });
});
