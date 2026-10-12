// End-to-end verification of the running system from a user's point of view.
//   npm run e2e                      mock stack (started automatically), everything except @live
//   E2E_UI_URL=... E2E_GATEWAY_URL=... npm run e2e      a real stack (UI must already point at it)
//   E2E_LIVE=1 ...                   also run the @live attack scenario (host only)
// System Edge, headless, one worker: the laptop running it has very little free memory.
import { defineConfig } from '@playwright/test'
import { MOCK, MOCK_PORTS, URLS } from './e2e/env.ts'

export default defineConfig({
  testDir: 'e2e',
  outputDir: 'e2e-results',
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['list'], ['json', { outputFile: 'e2e-results/results.json' }]],
  grepInvert: process.env.E2E_LIVE === '1' ? undefined : /@live/,
  use: {
    channel: 'msedge',
    headless: true,
    baseURL: URLS.ui,
    viewport: { width: 1920, height: 1080 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: MOCK
    ? [
        {
          command: 'node scripts/mock-ws-server.mjs',
          url: `http://127.0.0.1:${MOCK_PORTS.gateway}/healthz`,
          reuseExistingServer: true,
          env: { MOCK_QUIET: '1' },
          timeout: 30_000,
        },
        {
          command: `npx vite --port ${MOCK_PORTS.ui} --strictPort`,
          url: URLS.ui,
          reuseExistingServer: true,
          timeout: 60_000,
          // process env wins over ui/.env and ui/.env.local, so the UI talks to the mock stack
          env: {
            VITE_MOCK: '0',
            VITE_BRAIN_URL: URLS.brain,
            VITE_GATEWAY_URL: URLS.gateway,
            VITE_LAB_URL: URLS.lab,
            VITE_HANDOFF_URL: URLS.handoff,
            VITE_CONTROL_TOKEN: URLS.token,
          },
        },
      ]
    : undefined,
})
