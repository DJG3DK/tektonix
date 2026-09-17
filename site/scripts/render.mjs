/* Render the landing page to static HTML, then remove the JavaScript.
 *
 * Runs after `vite build`. Two steps:
 *
 *   1. SSR-build LandingPage and inject its markup into dist/index.html. A
 *      crawler with JavaScript off sees the page, which is the whole point of
 *      a landing page: Google renders JS on a second, queued pass, and Bing,
 *      DuckDuckGo and most LLM crawlers largely do not.
 *
 *   2. Strip the module <script> tag. Nothing on this page needs a runtime --
 *      the only control is a sign-in link -- so shipping React to render
 *      markup that is already in the file would be pure cost. The CSS <link>
 *      stays.
 *
 * renderToStaticMarkup, not renderToString: there is no hydration, so the
 * react-* attributes would be dead weight.
 */
import { build } from 'vite';
import react from '@vitejs/plugin-react';
import { readFile, writeFile, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

const ROOT = path.resolve(import.meta.dirname, '..');
const INDEX = path.join(ROOT, 'dist', 'index.html');
const ROOT_DIV = '<div id="root"></div>';

function fail(msg) {
  console.error(`[render] ${msg}`);
  process.exit(1);
}

if (!existsSync(INDEX)) fail('dist/index.html is missing -- run vite build first');

const outDir = path.join(ROOT, '.render-tmp');
try {
  await build({
    root: ROOT,
    logLevel: 'error',
    plugins: [react()],
    build: {
      ssr: path.join(ROOT, 'src', 'entry.tsx'),
      outDir,
      emptyOutDir: true,
      // The CSS is already emitted by the client build; this pass only needs
      // the markup.
      cssCodeSplit: false,
      rollupOptions: { output: { entryFileNames: 'entry.js' } },
    },
  });

  const mod = await import(path.join(outDir, 'entry.js'));
  const markup = mod.render();
  if (!markup || markup.length < 2000) {
    fail(`rendered markup is suspiciously small (${markup?.length ?? 0} chars)`);
  }

  let html = await readFile(INDEX, 'utf8');
  if (!html.includes(ROOT_DIV)) fail(`could not find ${ROOT_DIV} in dist/index.html`);
  html = html.replace(ROOT_DIV, `<div id="root">${markup}</div>`);

  // Every asset the markup references must exist in the build, or the page
  // ships with broken images and nothing says so.
  for (const m of markup.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)) {
    if (!existsSync(path.join(ROOT, 'dist', m[1].slice(1)))) {
      fail(`markup references ${m[1]}, which the build did not emit`);
    }
  }

  const before = html;
  const orphans = [...html.matchAll(/<script type="module"[^>]*src="([^"]+)"/g)].map((m) => m[1]);
  html = html.replace(/\s*<script type="module"[^>]*><\/script>/g, '');
  if (html === before) fail('no module script tag found to strip -- did the build change?');
  if (/<script[^>]+src=/.test(html)) fail('a script tag survived; the page must ship no JavaScript');

  await writeFile(INDEX, html, 'utf8');

  // The entry chunk existed only to pull the component's stylesheet and
  // images into the graph. Nothing references it now, so shipping it would be
  // a file served to no one and a puzzle for whoever looks in dist next.
  for (const src of orphans) {
    await rm(path.join(ROOT, 'dist', src.replace(/^\//, '')), { force: true });
    console.log(`[render] removed the unreferenced entry chunk ${src}`);
  }
  const hash = createHash('sha1').update(markup).digest('hex').slice(0, 8);
  console.log(`[render] ${markup.length} chars of static HTML, no JavaScript shipped (${hash})`);
} finally {
  await rm(outDir, { recursive: true, force: true });
}
