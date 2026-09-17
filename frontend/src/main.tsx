import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './theme.css'
import App from './App.tsx'
import { ErrorBoundary } from './ErrorBoundary.tsx'
import { registerServiceWorker } from './registerServiceWorker.ts'
import { applyTheme, storedTheme } from './themes.ts'

// BEFORE createRoot, not in an effect: the account's real scheme arrives with
// /api/auth/me, which is a round trip, so anyone not on the default would
// otherwise watch the app repaint itself on every load. App reconciles this
// with the server's answer the moment it has one.
applyTheme(storedTheme())

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)

// After render, never before: registration is for the NEXT launch (and for the
// install prompt), so it must not compete with the first paint for bandwidth.
registerServiceWorker()
