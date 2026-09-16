/**
 * SSR entry used only at build time by scripts/prerender.mjs.
 *
 * Renders the landing page -- the one public page -- to static HTML so a
 * crawler that does not execute JavaScript sees the actual content instead of
 * `<div id="root"></div>`. Google runs JS eventually, on a second queued pass;
 * Bing, DuckDuckGo and most LLM crawlers largely do not.
 *
 * renderToStaticMarkup, not renderToString: nothing hydrates this. main.tsx
 * calls createRoot().render(), which REPLACES the container's children, so the
 * prerendered DOM is discarded the moment the bundle runs. That is deliberate
 * -- it means there is no hydration contract to violate and no mismatch
 * warnings, at the cost of the markup being for crawlers rather than for
 * first paint.
 *
 * LandingPage is safe to render this way because it is purely presentational:
 * no state, no effects, no data fetching. If that ever stops being true this
 * build step will start lying, so the prerender test asserts on real content.
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { LandingPage } from './components/LandingPage';

export function render(): string {
  // onSignIn is a no-op here: the static copy has no event handlers, and the
  // real one arrives with the bundle.
  return renderToStaticMarkup(<LandingPage onSignIn={() => {}} />);
}
