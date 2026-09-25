import { useEffect, useState } from "react";
import { getGitHubInbox } from "./api";

/** How many GitHub items are waiting on a decision from you.
 *
 * `proposed` and nothing else, deliberately. The inbox also carries `seen`
 * (listed because the project's mode is off -- informational, no decision to
 * make), `task_created` (already actioned), `snoozed` (deferred on purpose)
 * and the terminal states. Counting those would light the badge permanently
 * on a busy project, and a badge that is always on is a badge nobody reads.
 *
 * Polls rather than subscribing: the inbox itself is a poller on the server
 * side, refreshed every couple of minutes, so a websocket would deliver the
 * same staleness at more cost. `refreshKey` lets a caller force a re-read --
 * the sidebar passes the current view, so acting on an item and navigating
 * away updates the badge immediately instead of up to a poll later.
 *
 * A failure is not an error state here. This is a badge; if the request
 * fails, the honest thing is to show no badge rather than a zero, an
 * exclamation mark, or a stale count from before.
 */
const INBOX_POLL_MS = 45_000;

export function useGitHubInboxCount(refreshKey?: unknown): number | null {
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const data = await getGitHubInbox();
        if (live) setCount(data.items.filter((i) => i.state === "proposed").length);
      } catch {
        if (live) setCount(null);
      }
    };
    void load();
    const timer = setInterval(() => void load(), INBOX_POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [refreshKey]);

  return count;
}
