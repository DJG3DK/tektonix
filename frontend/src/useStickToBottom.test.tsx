import { render } from "@testing-library/react";
import { useRef } from "react";
import { describe, expect, it } from "vitest";
import { useStickToBottom } from "./useStickToBottom";

// jsdom does no layout, so the container's geometry is set by hand:
// a 400px-tall viewport over `height` px of content.
function geometry(el: HTMLElement, height: number) {
  Object.defineProperty(el, "scrollHeight", { configurable: true, value: height });
  Object.defineProperty(el, "clientHeight", { configurable: true, value: 400 });
}

function Log({ count }: { count: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useStickToBottom(ref, count);
  return <div data-testid="log" ref={ref}><div /></div>;
}

function setup() {
  const view = render(<Log count={1} />);
  const el = view.getByTestId("log");
  const grow = (height: number, count: number) => {
    geometry(el, height);
    view.rerender(<Log count={count} />);
  };
  const scrollTo = (top: number) => {
    el.scrollTop = top;
    el.dispatchEvent(new Event("scroll"));
  };
  return { el, grow, scrollTo };
}

describe("useStickToBottom", () => {
  it("follows a new entry taller than any near-bottom margin", () => {
    // The bug: a 600px tool result measured as "scrolled away" and the view
    // stopped following.
    const { el, grow, scrollTo } = setup();
    grow(1000, 2);
    scrollTo(600);                     // reader at the bottom
    grow(1600, 3);                     // one tall entry arrives
    expect(el.scrollTop).toBe(1600);
    grow(2400, 4);                     // and the next
    expect(el.scrollTop).toBe(2400);
  });

  it("leaves the reader alone once they scroll up", () => {
    const { el, grow, scrollTo } = setup();
    grow(1000, 2);
    scrollTo(200);                     // reading earlier output
    grow(1600, 3);
    expect(el.scrollTop).toBe(200);
  });

  it("picks up following again when the reader returns to the bottom", () => {
    const { el, grow, scrollTo } = setup();
    grow(1000, 2);
    scrollTo(200);
    grow(1600, 3);
    scrollTo(1200);                    // back at the bottom (1600 - 400)
    grow(2000, 4);
    expect(el.scrollTop).toBe(2000);
  });
});
