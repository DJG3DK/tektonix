import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

/* public/sw.js run against a stand-in `self`: a notification opens whatever
 * URL the push payload carried, so it must only ever be a path on this
 * origin. */
const ORIGIN = "https://tektonix.example";

type Listener = (event: Record<string, unknown>) => void;

function loadWorker() {
  const listeners: Record<string, Listener> = {};
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type: string, fn: Listener) => { listeners[type] = fn; },
    registration: { showNotification: vi.fn(async (_title: string, _opts: { data: { url: string } }) => undefined) },
    clients: {
      matchAll: vi.fn(async (): Promise<Array<Record<string, unknown>>> => []),
      openWindow: vi.fn(async (_url: string) => undefined),
      claim: vi.fn(),
    },
    skipWaiting: vi.fn(),
  };
  const src = readFileSync(join(__dirname, "..", "public", "sw.js"), "utf8");
  new Function("self", "caches", "fetch", src)(self, {}, () => Promise.reject(new Error("offline")));
  return { self, listeners };
}

async function click(url: unknown) {
  const { self, listeners } = loadWorker();
  let pending: Promise<unknown> = Promise.resolve();
  listeners.notificationclick({
    notification: { close: () => undefined, data: { url } },
    waitUntil: (p: Promise<unknown>) => { pending = p; },
  });
  await pending;
  return self.clients.openWindow.mock.calls[0]?.[0];
}

describe("service worker notification targets", () => {
  it.each([
    ["https://evil.example/login", "/"],
    ["//evil.example/login", "/"],
    ["javascript:alert(1)", "/"],
    ["data:text/html,<script>1</script>", "/"],
    [undefined, "/"],
    ["/tasks/abc?tab=log#end", "/tasks/abc?tab=log#end"],
    [`${ORIGIN}/review/7`, "/review/7"],
  ])("a click on %s opens %s", async (url, expected) => {
    expect(await click(url)).toBe(expected);
  });

  it("a click on a task target moves the window the operator has open, rather than opening another", async () => {
    // agent/notify.py sends /task/<id> (it sent "/" for every alert until
    // 2026-09-29, audit U6); the worker takes the open window there.
    const { self, listeners } = loadWorker();
    const win = { url: `${ORIGIN}/planning/abc`, navigate: vi.fn(), focus: vi.fn(async () => undefined) };
    self.clients.matchAll.mockResolvedValue([win]);
    let pending: Promise<unknown> = Promise.resolve();
    listeners.notificationclick({
      notification: { close: () => undefined, data: { url: "/task/t1" } },
      waitUntil: (p: Promise<unknown>) => { pending = p; },
    });
    await pending;
    expect(win.navigate).toHaveBeenCalledWith("/task/t1");
    expect(win.focus).toHaveBeenCalled();
    expect(self.clients.openWindow).not.toHaveBeenCalled();
  });

  it("the notification itself stores only a same-origin path", async () => {
    const { self, listeners } = loadWorker();
    let pending: Promise<unknown> = Promise.resolve();
    listeners.push({
      data: { json: () => ({ title: "t", url: "https://evil.example/" }) },
      waitUntil: (p: Promise<unknown>) => { pending = p; },
    });
    await pending;
    expect(self.registration.showNotification.mock.calls[0][1].data.url).toBe("/");
  });
});
