import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './theme.css'
import App from './App.tsx'
import { ErrorBoundary } from './ErrorBoundary.tsx'
import { registerServiceWorker } from './registerServiceWorker.ts'

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
