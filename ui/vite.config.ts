/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: { host: '0.0.0.0', port: 5173, strictPort: true },
  build: {
    rollupOptions: {
      output: { manualChunks: { recharts: ['recharts'], react: ['react', 'react-dom', 'react-router-dom'] } },
    },
  },
  preview: { host: '0.0.0.0', port: 5173, strictPort: true },
  test: { environment: 'node', include: ['src/**/*.test.ts'] },
})
