import { useEffect, useLayoutEffect, useRef } from "react";

/** Keep a scrolling log pinned to its newest entry while the reader is at the
 * bottom, and leave it alone once they scroll up to read.
 *
 * "At the bottom" is decided by the READER's scrolling, not re-measured after
 * each new entry. The old check ran after the entry was already in the DOM,
 * so anything taller than its 120px margin -- a tool result, a diff, a long
 * reply, which is most of an agent's output -- read as "the user scrolled
 * away" and the view stopped following. Smooth scrolling made it worse: an
 * entry arriving mid-animation measured the half-way position and gave up.
 *
 * So: a scroll event records whether the reader is at the bottom, and any
 * growth of the content (a new row, a row that grows in place, an image that
 * loads) re-pins instantly if they were. Instant, not smooth, so there is no
 * in-between position for the next scroll event to misread.
 *
 * `resetKey` re-pins when the view switches to a different log. */
const AT_BOTTOM_PX = 40;

export function useStickToBottom(
  containerRef: React.RefObject<HTMLElement | null>,
  entryCount: number,
  resetKey?: unknown,
) {
  const stuck = useRef(true);

  const pin = () => {
    const c = containerRef.current;
    if (c && stuck.current) c.scrollTop = c.scrollHeight;
  };

  useEffect(() => {
    const c = containerRef.current;
    if (!c) return;
    const onScroll = () => {
      stuck.current = c.scrollHeight - c.scrollTop - c.clientHeight <= AT_BOTTOM_PX;
    };
    c.addEventListener("scroll", onScroll, { passive: true });
    // Growth that changes no entry count: a streaming row, a late image, a
    // phone keyboard shrinking the pane.
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => pin());
    ro?.observe(c);
    for (const child of Array.from(c.children)) ro?.observe(child);
    return () => {
      c.removeEventListener("scroll", onScroll);
      ro?.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [containerRef]);

  useLayoutEffect(() => {
    stuck.current = true;
    pin();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  // Before paint, so a new entry never shows for a frame above the fold.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useLayoutEffect(pin, [entryCount]);
}
