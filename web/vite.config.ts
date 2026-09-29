/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const apiTarget = `http://localhost:${process.env.OCTOPUS_API_PORT || '8000'}`

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Two dev servers run at once during e2e (playwright.config.ts: one proxying
  // to the shared backend, one to the accounts backend). They share this
  // project, so they share `node_modules/.vite` — and each one's dependency
  // pre-bundling invalidates the other's, so both keep re-optimizing and
  // reloading every page they serve. A full `:fast` run took 9.8 minutes that
  // way instead of 45 seconds. A cache dir per server costs nothing and ends
  // it.
  cacheDir: process.env.VITE_CACHE_DIR || undefined,
  server: {
    proxy: {
      '/api': apiTarget,
      // Applications are served by the backend out of their own directories
      // (applications.md §3); the dev server has to forward them or the
      // in-app iframe 404s on :5173/:5174.
      '/apps': apiTarget,
      '/ws': { target: apiTarget, ws: true },
      '/health': apiTarget,
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test-setup.ts',
    exclude: ['e2e/**', 'node_modules/**'],
  },
})
