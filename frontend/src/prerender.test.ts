/**
 * The built index.html must contain the landing page's real text.
 *
 * This is the assertion that would have caught the original problem: before
 * 2026-09-16 the built page shipped `<div id="root"></div>` and nothing else,
 * so a crawler that does not execute JavaScript saw no body content at all —
 * the only occurrences of "coding agent" anywhere on the page were inside meta
 * tags. Google renders JS on a second, queued pass; Bing, DuckDuckGo and most
 * LLM crawlers largely do not.
 *
 * Asserts on CONTENT, not on the mechanism. If prerendering is ever replaced
 * by SSR or static generation, these still pass; if the build step is dropped
 * or silently stops emitting, they fail.
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const DIST_INDEX = path.resolve(__dirname, '../dist/index.html');
const built = existsSync(DIST_INDEX) ? readFileSync(DIST_INDEX, 'utf8') : null;

// Skipped rather than failed on a tree that has not been built: `npm test`
// runs on a fresh checkout in CI before any build, and a test that demands
// build output would fail for a reason that has nothing to do with the code.
const withBuild = built ? describe : describe.skip;

function bodyText(html: string): string {
  const marker = '<div id="root">';
  const body = html.slice(html.indexOf(marker) + marker.length);
  return body
    .replace(/<script[\s\S]*?<\/script>/g, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

withBuild('the built page is readable without JavaScript', () => {
  it('has a non-empty root, not just an empty mount point', () => {
    expect(built).not.toContain('<div id="root"></div>');
  });

  it('carries a substantial amount of real text', () => {
    // 7.6K at the time of writing. The floor is low enough that ordinary copy
    // edits never trip it, and high enough that an empty or half-rendered
    // root does.
    expect(bodyText(built!).length).toBeGreaterThan(3000);
  });

  it('states what the product is, in the body and not only in meta tags', () => {
    const text = bodyText(built!).toLowerCase();
    for (const phrase of ['autonomous coding agent', 'review gate', 'test suite']) {
      expect(text).toContain(phrase);
    }
  });

  it('names the category words someone would search for', () => {
    // These were measured at 0 occurrences on the live page before this work.
    const text = bodyText(built!).toLowerCase();
    expect(text).toContain('openrouter');
    expect(text).toContain('self-hosted');
  });

  it('keeps the heading structure a crawler reads for hierarchy', () => {
    expect(built).toMatch(/<h1[^>]*>/);
    expect(built).toMatch(/<h2[^>]*>/);
  });
});
