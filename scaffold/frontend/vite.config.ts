import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The app always calls the API at a relative /api path, so there is no CORS in any
// environment: in dev this proxy handles it, in docker nginx proxies it, and on
// App Platform the platform routes /api to the service and / to the static site.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // SSE must not be buffered by the dev proxy or updates arrive in bursts.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['x-accel-buffering'] = 'no'
          })
        },
      },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
