/**
 * Post-build: put the landing page's real HTML inside <div id="root">.
 *
 * Run after `vite build`. Builds an SSR bundle of src/prerender-entry.tsx,
 * renders it, and injects the markup into dist/index.html.
 *
 * Why this exists: the dashboard is a React SPA, so the built index.html ships
 * an empty root div. Measured on the live site before this script, a crawler
 * with JavaScript disabled saw no body text at all -- the only occurrences of
 * "coding agent" on the page were inside meta tags. Google renders JS on a
 * second, queued pass; Bing, DuckDuckGo and most LLM crawlers largely do not.
 *
 * Asset URLs: Vite hashes emitted assets by CONTENT, so the logo and
 * screenshots referenced from the SSR bundle land on the same filenames the
 * client build produced. That is checked rather than assumed -- see
 * assertAssetsResolve below, which fails the build if a prerendered <img>
 * points at a file that is not in dist.
 */
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const DIST = path.join(ROOT, 'dist');
const INDEX = path.join(DIST, 'index.html');
const ROOT_DIV = '<div id="root"></div>';

function fail(msg) {
  console.error(`[prerender] ${msg}`);
  process.exit(1);
}

/** Every src/href the markup points at must exist in dist, or it 404s. */
function assertAssetsResolve(html) {
  const missing = [];
  for (const m of html.matchAll(/(?:src|href)="(\/[^"]+)"/g)) {
    const url = m[1];
    if (url.startsWith('//') || url.startsWith('/#')) continue;
    const onDisk = path.join(DIST, url.replace(/^\//, '').split('?')[0]);
    if (!existsSync(onDisk)) missing.push(url);
  }
  if (missing.length) {
    fail(
      `prerendered markup references files that are not in dist: ${[...new Set(missing)].join(', ')}\n` +
        '           Asset hashing diverged between the client and SSR builds.',
    );
  }
}

// Inside the project, not /tmp: the SSR bundle keeps react-dom external, so
// Node has to be able to resolve node_modules by walking up from the output
// directory. Under node_modules/.cache it is already gitignored and already
// swept by a clean install.
const outDir = path.join(ROOT, 'node_modules/.cache/tektonix-prerender');
await mkdir(outDir, { recursive: true });
try {
  await build({
    root: ROOT,
    logLevel: 'error',
    build: {
      ssr: path.join(ROOT, 'src/prerender-entry.tsx'),
      outDir,
      emptyOutDir: true,
      // Same naming as the client build, so content-hashed assets line up.
      assetsDir: 'assets',
    },
  });

  const mod = await import(path.join(outDir, 'prerender-entry.js'));
  const markup = mod.render();

  if (!markup || markup.length < 2000) {
    fail(`rendered markup is suspiciously small (${markup?.length ?? 0} chars) -- did LandingPage render?`);
  }
  assertAssetsResolve(markup);

  const html = await readFile(INDEX, 'utf8');
  if (!html.includes(ROOT_DIV)) {
    fail(`could not find ${ROOT_DIV} in dist/index.html -- the mount point changed`);
  }
  await writeFile(INDEX, html.replace(ROOT_DIV, `<div id="root">${markup}</div>`), 'utf8');

  const hash = createHash('sha1').update(markup).digest('hex').slice(0, 8);
  console.log(`[prerender] injected ${markup.length} chars of landing-page HTML (${hash})`);
} finally {
  await rm(outDir, { recursive: true, force: true });
}
