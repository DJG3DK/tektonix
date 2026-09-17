import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    // dangerouslyIgnoreUnhandledErrors stays off: an unhandled rejection in a
    // page that ships no JavaScript would mean something is very wrong.
    dangerouslyIgnoreUnhandledErrors: false,
  },
})
