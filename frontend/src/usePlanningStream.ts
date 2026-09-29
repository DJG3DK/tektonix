import { useCallback, useEffect, useRef, useState } from "react";
import type { AttachmentEntry } from "./api";
import { getPlanningSession, planningStreamUrl, sendPlanningMessage } from "./api";
import type { NewProjectProposal, PlanningLogEntry, PlanningStreamEvent } from "./types";

interface PlanningStreamState {
  log: PlanningLogEntry[];
  planMarkdown: string | null;
  /** The unanswered create_project proposal, from the hydrate and from
   *  turn_complete -- the same two sources planMarkdown has. */
  newProject: NewProjectProposal | null;
  costUsd: number;
  running: boolean;
  hydrateError: string | null;
  sendError: string | null;
}

type Snapshot = Awaited<ReturnType<typeof getPlanningSession>>;

/* The server pings every 20s (stream_planning_session's sender loop), so
   three missed pings means the socket is dead however healthy it looks. */
const SOCKET_SILENCE_LIMIT_MS = 70_000;
const SOCKET_WATCHDOG_POLL_MS = 15_000;

const EMPTY_STATE: PlanningStreamState = {
  log: [],
  planMarkdown: null,
  newProject: null,
  costUsd: 0,
  running: false,
  hydrateError: null,
  sendError: null,
};

/** One log from a snapshot and what this page already held.
 *
 * The snapshot is the authority for everything up to its `seq`, so it keeps
 * its order and position. An entry the page holds that the snapshot lacks is
 * kept only when it carries a server id -- it came down the socket and the
 * snapshot lost it (a restart between the two). The one entry without an id
 * is the operator's own message, appended locally by sendMessage; the
 * snapshot has the server's copy of it, so the local one is dropped rather
 * than doubled. Replaces "keep whichever list is longer" (2026-09-29 audit,
 * U4), which could neither merge nor tell newer from longer.
 */
export function mergePlanningLog(snapshot: PlanningLogEntry[], held: PlanningLogEntry[]): PlanningLogEntry[] {
  const ids = new Set(snapshot.map((e) => e.id).filter(Boolean));
  const out = [...snapshot];
  for (const e of held) {
    if (!e.id || ids.has(e.id)) continue;
    ids.add(e.id);
    out.push(e);
  }
  return out;
}

/**
 * Unlike a task's WS (one continuous connection for the task's whole
 * lifetime), a planning session's WS closes at the end of EVERY turn --
 * server.py's stream_planning_session mirrors stream_task's "closed = this
 * run finished" convention exactly, and here every turn is its own run (a
 * fresh POST .../message kicks off a fresh background task). So sending a
 * message here always (re)connects the WS and waits for it to open BEFORE
 * posting, guaranteeing no live event is lost to a race between "the turn
 * started" and "the socket subscribed".
 *
 * A turn can also already be in flight when the page opens (a reload, a
 * second tab, a deep link). Then the socket is opened FIRST and the snapshot
 * taken second: the server subscribes the socket on accept, so anything it
 * publishes after that is either in the snapshot or in the frames buffered
 * while the snapshot was in flight -- the same socket-first order as
 * useTaskStream, with the server's per-event `seq` saying which is which.
 * Until 2026-09-29 the page hydrated and never connected, and sat frozen
 * until the watchdog noticed ~75s later (audit, U4).
 */
export function usePlanningStream(sessionId: string | null) {
  const [state, setState] = useState<PlanningStreamState>(EMPTY_STATE);
  const wsRef = useRef<WebSocket | null>(null);
  const prevSessionId = useRef<string | null>(null);
  // Auto-reconnect machinery (2026-08-28): a NAT/middlebox can kill a quiet
  // socket mid-turn (reported live -- "the stream stalls, refresh fixes it").
  // The task stream already reconnects with backoff; this brings planning to
  // parity. reconnectRef breaks the connect<->onclose definition cycle;
  // deliberateClose marks unmount/session-switch closes so they never spawn
  // a reconnect loop for a session nobody is viewing.
  const reconnectRef = useRef<() => void>(() => {});
  const reconnecting = useRef(false);
  const deliberateClose = useRef(false);
  // Sockets this hook replaced on purpose (a new turn, a reconnect). Their
  // onclose is not a drop and must not start a reconnect that would then
  // replace the socket that replaced them.
  const superseded = useRef(new WeakSet<WebSocket>());
  // Liveness watchdog (2026-08-31). onclose is not a reliable death signal:
  // a half-open TCP -- laptop sleep, NAT idle-kill, a proxy dropping the
  // connection without a FIN -- leaves the browser holding a socket it will
  // never hear from again, and no event ever fires. The turn then ends
  // server-side and its `error`/`closed` events go to a socket nobody is
  // listening on, so the UI sits on `running: true` forever: thinking
  // bubbles that never stop, composer disabled, and -- because the outcome
  // banner only renders when NOT running -- no sign of why the turn ended.
  // Observed live on the $8 budget-exceeded turn of 2026-08-31.
  //
  // The server pings every 20s, so silence past three pings is death. The
  // recovery is just close(): that fires onclose, which reconnects and
  // re-hydrates, and hydration reads `running` from the server -- which is
  // the authority on whether a turn is actually in flight.
  //
  // Set when a socket is opened, not at render: a render is not a message.
  const lastMessageAt = useRef(0);
  // Socket-first hydrate: frames that arrive while a snapshot is in flight
  // wait in `pending` and are applied afterwards, skipping anything at or
  // below the snapshot's position.
  const hydrating = useRef(false);
  const pending = useRef<PlanningStreamEvent[]>([]);
  const lastAppliedSeq = useRef(0);

  const applyEvent = useCallback((event: PlanningStreamEvent) => {
    // A frame already folded into the snapshot, or replayed twice from the
    // buffer, is dropped rather than double-counted. Adopted, not raised: the
    // server's counter restarts with its process (see useTaskStream).
    if (typeof event.seq === "number") {
      if (event.seq <= lastAppliedSeq.current) return;
      lastAppliedSeq.current = event.seq;
    }
    if (event.type === "cost") {
      // Live per-call spend, same contract as the task stream.
      setState((s) => ({ ...s, costUsd: event.cost_usd ?? s.costUsd }));
      return;
    }
    if (event.type === "log_entry" && event.entry) {
      const entry = event.entry;
      setState((s) => (
        // A server too old to number its frames can replay one the snapshot
        // already holds; the entry id catches that.
        entry.id && s.log.some((e) => e.id === entry.id) ? s : { ...s, log: [...s.log, entry] }
      ));
    } else if (event.type === "turn_complete") {
      setState((s) => ({
        ...s,
        planMarkdown: event.plan_markdown ?? s.planMarkdown,
        // The server sends the persisted value, null included: a turn
        // that ended with no proposal must not resurrect a dismissed one.
        newProject: event.new_project === undefined ? s.newProject : event.new_project,
        costUsd: event.cost_usd ?? s.costUsd,
      }));
    } else if (event.type === "error") {
      setState((s) => ({ ...s, running: false, sendError: event.message ?? "planning turn failed" }));
    } else if (event.type === "stopped") {
      // The operator pressed Stop. `closed` always follows and clears
      // `running`, but the cancelled turn reports the cost it actually spent
      // and that would otherwise be dropped — a stopped turn still cost money.
      setState((s) => ({ ...s, costUsd: event.cost_usd ?? s.costUsd, sendError: null }));
    } else if (event.type === "closed") {
      setState((s) => ({ ...s, running: false }));
    }
  }, []);

  /** Fold a REST snapshot in. `reconnect` snapshots also clear the send
   *  error, since the connection they follow up on is back. */
  const applySnapshot = useCallback(({ log, running, meta, seq }: Snapshot, reason: "mount" | "reconnect") => {
    if (typeof seq === "number") lastAppliedSeq.current = seq;
    setState((s) => ({
      ...s,
      log: mergePlanningLog(log, s.log),
      running,
      planMarkdown: reason === "mount" ? meta.plan_markdown : (meta.plan_markdown ?? s.planMarkdown),
      newProject: meta.new_project ?? null,
      costUsd: meta.cost_usd ?? s.costUsd,
      hydrateError: null,
      sendError: reason === "reconnect" ? null : s.sendError,
    }));
  }, []);

  const connect = useCallback((): Promise<WebSocket> => {
    return new Promise((resolve, reject) => {
      if (!sessionId || prevSessionId.current !== sessionId) {
        reject(new Error("no active planning session"));
        return;
      }
      // audit H-15 (secondary): close any previous socket before replacing the
      // ref, so an orphaned socket can't keep appending to state.
      const prev = wsRef.current;
      if (prev) {
        superseded.current.add(prev);
        try { prev.close(); } catch { /* already closing */ }
      }
      const ws = new WebSocket(planningStreamUrl(sessionId));
      wsRef.current = ws;
      lastMessageAt.current = Date.now();
      // Tracks whether an in-band `closed` event arrived for THIS socket, so
      // onclose can tell a clean end-of-turn from a dropped connection.
      let sawClosed = false;
      ws.onopen = () => {
        lastMessageAt.current = Date.now();
        resolve(ws);
      };
      ws.onerror = () => reject(new Error("connection failed"));
      ws.onmessage = (ev) => {
        lastMessageAt.current = Date.now();
        let event: PlanningStreamEvent;
        try {
          event = JSON.parse(ev.data);
        } catch {
          // Same as the task stream: one bad frame is dropped, not a thrown
          // handler that leaves the turn looking stuck.
          console.error("planning stream: discarding unparseable frame");
          return;
        }
        if (event.type === "ping") return; // server heartbeat, not content
        if (event.type === "closed") sawClosed = true;
        if (hydrating.current) {
          pending.current.push(event);
          return;
        }
        applyEvent(event);
      };
      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        if (superseded.current.has(ws)) return;
        // A drop without an in-band `closed` is unexpected (NAT idle kill,
        // proxy blip, backend restart). First response: reconnect + re-hydrate
        // automatically -- the manual "send again" banner (audit H-15) is now
        // the LAST resort after the retry budget, not the first.
        if (!sawClosed && !deliberateClose.current) {
          reconnectRef.current();
        }
      };
    });
  }, [sessionId, applyEvent]);

  /** Open the socket, then take the snapshot, then replay what arrived
   *  meanwhile. Opening first is what closes the window a snapshot-first
   *  order leaves between "snapshot taken" and "socket subscribed". */
  const connectThenHydrate = useCallback(async (reason: "mount" | "reconnect") => {
    if (deliberateClose.current) throw new Error("planning session closed");
    hydrating.current = true;
    pending.current = [];
    try {
      await connect();
      const snapshot = await getPlanningSession(sessionId!);
      if (deliberateClose.current || prevSessionId.current !== sessionId) return;
      applySnapshot(snapshot, reason);
      hydrating.current = false;
      const buffered = pending.current;
      pending.current = [];
      for (const event of buffered) applyEvent(event);
    } finally {
      hydrating.current = false;
      pending.current = [];
    }
  }, [connect, sessionId, applySnapshot, applyEvent]);

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    if (prevSessionId.current !== sessionId) {
      prevSessionId.current = sessionId;
      lastAppliedSeq.current = 0;
      setState(EMPTY_STATE);
    }
    deliberateClose.current = false;

    async function hydrate() {
      try {
        const snapshot = await getPlanningSession(sessionId!);
        if (cancelled) return;
        applySnapshot(snapshot, "mount");
        // A turn is in flight: subscribe now, socket-first, and take a second
        // snapshot behind it so nothing between the two is missed. An idle
        // session opens no socket -- sendMessage does that when a turn starts.
        if (snapshot.running) await connectThenHydrate("mount");
      } catch (err) {
        if (cancelled) return;
        setState((s) => ({ ...s, hydrateError: err instanceof Error ? err.message : "failed to load session" }));
      }
    }

    hydrate();
    return () => {
      cancelled = true;
      deliberateClose.current = true;
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [sessionId, applySnapshot, connectThenHydrate]);

  const reconnect = useCallback(async () => {
    if (reconnecting.current || !sessionId) return;
    reconnecting.current = true;
    try {
      for (let attempt = 0; attempt < 5; attempt++) {
        await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** attempt, 8000)));
        if (deliberateClose.current) return; // session switched/unmounted while waiting
        try {
          await connectThenHydrate("reconnect");
          return;
        } catch {
          /* next attempt */
        }
      }
      setState((s) =>
        s.running
          ? { ...s, running: false, sendError: "connection dropped mid-turn -- send again to continue" }
          : s,
      );
    } finally {
      reconnecting.current = false;
    }
  }, [connectThenHydrate, sessionId]);
  useEffect(() => {
    reconnectRef.current = reconnect;
  }, [reconnect]);

  // Only armed while a turn is in flight -- an idle session has no socket to
  // watch and nothing to recover.
  useEffect(() => {
    if (!state.running) return;
    const id = window.setInterval(() => {
      if (Date.now() - lastMessageAt.current < SOCKET_SILENCE_LIMIT_MS) return;
      const ws = wsRef.current;
      lastMessageAt.current = Date.now(); // don't re-fire while the retry runs
      if (ws) {
        try { ws.close(); } catch { /* already gone */ }
      } else {
        // No socket at all and still "running" -- reconnect directly, since
        // there is no onclose coming to do it for us.
        reconnectRef.current();
      }
    }, SOCKET_WATCHDOG_POLL_MS);
    return () => window.clearInterval(id);
  }, [state.running]);

  const sendMessage = useCallback(
    async (text: string, attachments?: AttachmentEntry[]) => {
      if (!sessionId) return;
      const userEntry: PlanningLogEntry = {
        kind: "user",
        summary: text,
        detail: text,
        timestamp: new Date().toISOString(),
      };
      setState((s) => ({ ...s, log: [...s.log, userEntry], running: true, sendError: null }));
      try {
        await connect();
        await sendPlanningMessage(sessionId, text, attachments);
      } catch (err) {
        setState((s) => ({ ...s, running: false, sendError: err instanceof Error ? err.message : "failed to send" }));
      }
    },
    [sessionId, connect],
  );

  // The confirm/dismiss route answers with the session meta, which the
  // view hands up to App; this drops the hook's own copy so a proposal the
  // operator has answered cannot outlive the answer.
  const clearNewProject = useCallback(() => {
    setState((s) => (s.newProject ? { ...s, newProject: null } : s));
  }, []);

  return { ...state, sendMessage, clearNewProject };
}
