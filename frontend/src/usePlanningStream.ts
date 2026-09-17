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

/**
 * Unlike a task's WS (one continuous connection for the task's whole
 * lifetime), a planning session's WS closes at the end of EVERY turn --
 * server.py's stream_planning_session mirrors stream_task's "closed = this
 * run finished" convention exactly, and here every turn is its own run (a
 * fresh POST .../message kicks off a fresh background task). So sending a
 * message here always (re)connects the WS and waits for it to open BEFORE
 * posting, guaranteeing no live event is lost to a race between "the turn
 * started" and "the socket subscribed" -- there is no separate resumption
 * poller the way useTaskStream needs, since the only thing that ever starts
 * a new turn is this same hook's own sendMessage.
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
  const lastMessageAt = useRef(Date.now());

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    if (prevSessionId.current !== sessionId) {
      prevSessionId.current = sessionId;
      setState(EMPTY_STATE);
    }

    async function hydrate() {
      try {
        const { log, running, meta } = await getPlanningSession(sessionId!);
        if (cancelled) return;
        setState((s) => ({
          ...s, log, running, planMarkdown: meta.plan_markdown, newProject: meta.new_project ?? null,
          costUsd: meta.cost_usd, hydrateError: null,
        }));
      } catch (err) {
        if (cancelled) return;
        setState((s) => ({ ...s, hydrateError: err instanceof Error ? err.message : "failed to load session" }));
      }
    }

    hydrate();
    deliberateClose.current = false;
    return () => {
      cancelled = true;
      deliberateClose.current = true;
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [sessionId]);

  const connect = useCallback((): Promise<WebSocket> => {
    return new Promise((resolve, reject) => {
      if (!sessionId) {
        reject(new Error("no active planning session"));
        return;
      }
      // audit H-15 (secondary): close any previous socket before replacing the
      // ref, so an orphaned socket can't keep appending to state.
      if (wsRef.current) {
        try { wsRef.current.close(); } catch { /* already closing */ }
      }
      const ws = new WebSocket(planningStreamUrl(sessionId));
      wsRef.current = ws;
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
        const event: PlanningStreamEvent = JSON.parse(ev.data);
        if (event.type === "ping") return; // server heartbeat, not content
        if (event.type === "cost") {
          // Live per-call spend, same contract as the task stream.
          setState((s) => ({ ...s, costUsd: event.cost_usd ?? s.costUsd }));
          return;
        }
        if (event.type === "log_entry" && event.entry) {
          setState((s) => ({ ...s, log: [...s.log, event.entry!] }));
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
          sawClosed = true;
          setState((s) => ({ ...s, running: false }));
        }
      };
      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        // A drop without an in-band `closed` is unexpected (NAT idle kill,
        // proxy blip, backend restart). First response: reconnect + re-hydrate
        // automatically -- the manual "send again" banner (audit H-15) is now
        // the LAST resort after the retry budget, not the first.
        if (!sawClosed && !deliberateClose.current) {
          reconnectRef.current();
        }
      };
    });
  }, [sessionId]);

  const reconnect = useCallback(async () => {
    if (reconnecting.current || !sessionId) return;
    reconnecting.current = true;
    try {
      for (let attempt = 0; attempt < 5; attempt++) {
        await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** attempt, 8000)));
        if (deliberateClose.current) return; // session switched/unmounted while waiting
        try {
          // Snapshot FIRST, then open the socket. Connecting first meant live
          // events could arrive before the snapshot landed, and the snapshot
          // (older, but usually longer) then replaced them wholesale. The
          // length guard below stops the log from shrinking, but ordering the
          // fetch first removes the race instead of surviving it -- and it
          // matches useTaskStream's hydrate-then-connect order.
          const { log, running, meta } = await getPlanningSession(sessionId);
          await connect();
          setState((s) => ({
            ...s,
            log: log.length >= s.log.length ? log : s.log,
            running,
            planMarkdown: meta.plan_markdown ?? s.planMarkdown,
            newProject: meta.new_project ?? null,
            costUsd: meta.cost_usd ?? s.costUsd,
            sendError: null,
          }));
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
  }, [connect, sessionId]);
  reconnectRef.current = reconnect;

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
