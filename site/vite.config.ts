import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The landing page. It builds to static HTML, CSS and images -- see
// scripts/render.mjs, which renders the page at build time and then strips
// the module script, because nothing on this page needs a runtime: the only
// control is the sign-in link, and a link is HTML.
//
// Kept as a Vite/React project rather than hand-written HTML so the page is
// still a component with real CSS bundling, asset hashing and tests -- the
// output is what is static, not the source.
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    // No hashed entry chunk to reason about: there is exactly one, and
    // render.mjs removes its tag from the HTML afterwards.
    assetsInlineLimit: 0,
  },
})
