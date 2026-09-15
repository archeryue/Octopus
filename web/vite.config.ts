/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const apiTarget = `http://localhost:${process.env.OCTOPUS_API_PORT || '8000'}`

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
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
