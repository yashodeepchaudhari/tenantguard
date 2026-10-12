// Shared test fixtures:
// - `services`: which optional services answer (lab, handoff bridge, narrator), probed once per worker
// - `consoleGuard` (automatic): every UI test fails on an uncaught page error or a console error.
//   The only tolerated console errors are failed network loads to an optional service that is
//   known to be unreachable (the UI polls it and shows "not reachable"; Chromium logs each failure).
import { test as base, expect, type ConsoleMessage } from '@playwright/test'
import { probe, TOKEN_HEADER, URLS } from './env.ts'

export interface Services {
  lab: boolean
  handoff: boolean
  narrator: boolean
  /** id of a lab scenario run in progress (never disturb it), else null */
  labRun: string | null
}

type Fixtures = { consoleGuard: void }
type WorkerFixtures = { services: Services }

const origin = (u: string) => new URL(u).origin

export const test = base.extend<Fixtures, WorkerFixtures>({
  services: [
    // Playwright needs a destructuring pattern even when a fixture has no dependencies
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      const lab = await probe(`${URLS.lab}/lab/status`)
      const handoff = await probe(`${URLS.handoff}/cases`, TOKEN_HEADER)
      const narrator = await probe(`${URLS.narrator}/reports`)
      const run = lab?.status === 200 ? ((lab.body as { run?: string | null })?.run ?? null) : null
      await use({
        lab: lab?.status === 200,
        handoff: handoff?.status === 200,
        narrator: narrator?.status === 200,
        labRun: run,
      })
    },
    { scope: 'worker' },
  ],

  consoleGuard: [
    async ({ page, services }, use, testInfo) => {
      const problems: string[] = []
      const down = [
        ...(services.lab ? [] : [origin(URLS.lab)]),
        ...(services.handoff ? [] : [origin(URLS.handoff)]),
        ...(services.narrator ? [] : [origin(URLS.narrator)]),
      ]
      const tolerated = (m: ConsoleMessage) => {
        const url = m.location()?.url ?? ''
        return /Failed to load resource|net::ERR_/.test(m.text()) && down.some((o) => url.startsWith(o))
      }
      page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`))
      page.on('console', (m) => {
        if (m.type() === 'error' && !tolerated(m)) problems.push(`console.error: ${m.text()} @ ${m.location()?.url ?? ''}`)
      })
      await use()
      if (problems.length) await testInfo.attach('console-errors', { body: problems.join('\n'), contentType: 'text/plain' })
      expect(problems, 'no console errors or uncaught page errors').toEqual([])
    },
    { auto: true },
  ],
})

export { expect }
