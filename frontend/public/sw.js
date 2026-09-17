/* Tektonix's service worker.
 *
 * It exists for two reasons, in this order:
 *
 *   1. Installability. Chrome will not offer "Install app" (and will not mint
 *      the WebAPK that gives Android a real launcher icon and a standalone
 *      window) unless the origin has a service worker with a fetch handler.
 *   2. A cold start on a bad connection shows the app, not the browser's
 *      offline page.
 *
 * It is deliberately NOT an offline mode. This is a console onto a live agent
 * -- running tasks, streaming logs, a review gate -- and every screen worth
 * looking at is a network answer. Cached agent state would be a screen that
 * lies, which is worse than a screen that says it cannot reach the box.
 *
 * Three rules, and the whole file is just these:
 *
 *   /api/* is never touched. Not cached, not inspected, not retried. That
 *   covers the session cookie, every mutation, the WebSocket upgrades for
 *   task and planning streams, and the SSE-ish log feeds. A service worker
 *   that caches an authenticated API response is how one account ends up
 *   reading another's data out of a shared disk cache.
 *
 *   Hashed bundles under /assets/ are cache-first and kept forever, because
 *   their names change when their contents do (Vite) -- that is what makes a
 *   relaunch instant.
 *
 *   Navigations are network-first with the cached shell as a fallback. Not
 *   cache-first: index.html names the hashed bundles of the deploy it came
 *   from, so a stale shell served while the network was fine would ask for
 *   /assets/index-OLD.js, get the SPA fallback's index.html back as
 *   JavaScript, and white-screen the app after a deploy. Fresh whenever the
 *   network answers; the copy is only for when it does not.
 */

// Bump to evict every previous cache on activate. The version is the contract:
// an old worker's entries are never merged into a new one's.
const VERSION = 'tektonix-v1';
const SHELL = `${VERSION}-shell`;
const ASSETS = `${VERSION}-assets`;
const KEEP = [SHELL, ASSETS];

self.addEventListener('install', (event) => {
  // Only the shell is precached. The hashed bundles are not listed here on
  // purpose: their names are build output, this file is static, and a
  // hand-maintained list would be wrong the first time someone forgot to
  // update it. They get cached on first use instead.
  event.waitUntil(
    caches.open(SHELL).then((c) => c.add(new Request('/', { cache: 'reload' })))
      // A failed precache must not fail the install -- the worker is still
      // worth having for rules 1 and 2, and the shell will be cached by the
      // first navigation anyway.
      .catch(() => undefined)
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !KEEP.includes(k)).map((k) => caches.delete(k))))
      // Claim immediately so a deploy takes effect at the next launch rather
      // than whenever the last installed window happens to be closed. A
      // dashboard is left open for days.
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('message', (event) => {
  // The page asks for this after it has told the operator an update is ready.
  if (event.data === 'skip-waiting') self.skipWaiting();
});

function isApi(url) {
  return url.pathname === '/api' || url.pathname.startsWith('/api/');
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  // Another origin's response is not ours to store, and an opaque one cannot
  // even be inspected for whether it succeeded.
  if (url.origin !== self.location.origin) return;
  if (isApi(url)) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(SHELL).then((c) => c.put('/', copy)).catch(() => undefined);
          }
          return res;
        })
        .catch(() => caches.match('/', { cacheName: SHELL }).then(
          (hit) => hit || new Response(
            '<!doctype html><meta charset="utf-8"><title>Tektonix</title>'
            + '<body style="background:#11151a;color:#edf0f4;font:16px system-ui;'
            + 'display:grid;place-items:center;height:100vh;margin:0;text-align:center">'
            + '<div><p>Tektonix is unreachable.</p>'
            + '<p style="color:#a3adbb;font-size:14px">This device is offline, or the box is down.</p></div>',
            { status: 503, headers: { 'content-type': 'text/html; charset=utf-8' } },
          ),
        )),
    );
    return;
  }

  // Content-hashed build output: the name IS the version, so a hit is always
  // correct and a miss is a one-time fetch.
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(
      caches.match(request, { cacheName: ASSETS }).then((hit) => hit || fetch(request).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(ASSETS).then((c) => c.put(request, copy)).catch(() => undefined);
        }
        return res;
      })),
    );
    return;
  }

  // Everything else at the root -- icons, the manifest -- has a stable name,
  // so it is revalidated rather than trusted: serve the cached copy at once
  // and replace it in the background. A replaced icon is then one launch
  // stale instead of a cache lifetime stale.
  if (/\.(png|ico|svg|webmanifest)$/.test(url.pathname)) {
    event.respondWith(
      caches.match(request, { cacheName: ASSETS }).then((hit) => {
        const live = fetch(request).then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(ASSETS).then((c) => c.put(request, copy)).catch(() => undefined);
          }
          return res;
        }).catch(() => hit);
        return hit || live;
      }),
    );
  }
});
