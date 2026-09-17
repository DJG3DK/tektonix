/* The SSR entry scripts/render.mjs builds. Separate from main.tsx because
 * that one is the CLIENT entry: it imports the stylesheets so Vite emits
 * them, and a server build must not pull CSS through a browser loader. */
import { renderToStaticMarkup } from "react-dom/server";
import { LandingPage } from "./LandingPage";

export function render(): string {
  return renderToStaticMarkup(<LandingPage />);
}
