// Operator flows in the dashboard. Every effect is verified through the backend's own API,
// not just the UI. Anything the suite changes (mode, quota) is restored afterwards.
import type { APIRequestContext, Page } from '@playwright/test'
import { ALLOW_WRITES, TOKEN_HEADER, URLS, until, WRITES_OFF } from './env.ts'
import { expect, test } from './fixtures.ts'

const MODE_LABEL = { off: 'OFF', industry: 'Industry', tenantguard: 'TenantGuard' } as const
type Mode = keyof typeof MODE_LABEL

async function getMode(request: APIRequestContext): Promise<Mode> {
  return (await (await request.get(`${URLS.gateway}/admin/mode`, { headers: TOKEN_HEADER })).json()).mode
}

async function openControlRoom(page: Page) {
  await page.goto('/?view=tiles')
  await expect(page.getByTestId('tile-L')).toBeVisible({ timeout: 20_000 })
}

test('Control Room renders 12 tenant tiles, the 3x3 server grid and a status sentence', async ({ page }) => {
  await openControlRoom(page)
  await expect(page.locator('[data-testid^="tile-"]')).toHaveCount(12)
  await expect(page.locator('[data-testid^="server-w"]')).toHaveCount(9)
  await expect(page.getByTestId('status-sentence')).not.toBeEmpty()
  await expect(page.getByTestId('impact')).toBeVisible()
  // the header says the brain stream is live
  await expect(page.getByText('live', { exact: true })).toBeVisible()
})

test('picking a tenant tile outlines its barcode servers', async ({ page, request }) => {
  await openControlRoom(page)
  const barcode: string[] = (await (await request.get(`${URLS.gateway}/admin/tenants`, { headers: TOKEN_HEADER })).json())
    .find((t: { id: string }) => t.id === 'E')
    .servers.slice()
    .sort()
  await page.getByTestId('tile-E').click()
  await expect
    .poll(async () => (await page.locator('[data-testid^="server-w"][data-lit="1"]').evaluateAll((els) => els.map((e) => e.getAttribute('data-testid')!.slice(7)))).sort())
    .toEqual(barcode)
})

test('mode toggle round-trip is applied by the gateway', async ({ page, request, services }) => {
  test.skip(!ALLOW_WRITES, WRITES_OFF)
  test.skip(!!services.labRun, `lab scenario run ${services.labRun} in progress: not changing the mode`)
  await openControlRoom(page)
  const before = await getMode(request)
  const target: Mode = before === 'industry' ? 'off' : 'industry'
  try {
    await page.getByRole('radio', { name: MODE_LABEL[target], exact: true }).click()
    await expect.poll(() => getMode(request)).toBe(target)
    await expect(page.getByRole('radio', { name: MODE_LABEL[target], exact: true })).toHaveAttribute('aria-checked', 'true')
    await page.getByRole('radio', { name: MODE_LABEL[before], exact: true }).click()
    await expect.poll(() => getMode(request)).toBe(before)
  } finally {
    if ((await getMode(request)) !== before) await request.post(`${URLS.gateway}/admin/mode`, { headers: TOKEN_HEADER, data: { mode: before } })
  }
})

test('attack launcher offers exactly the five supported attacks', async ({ page }) => {
  await openControlRoom(page)
  const values = await page.locator('select[title] option').evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value))
  expect(values).toEqual(['flood', 'whale', 'hog', 'memory', 'retry_storm'])
})

test('attack launcher starts an attack, shows it running, and stops it', async ({ page, request, services }) => {
  test.skip(!ALLOW_WRITES, WRITES_OFF)
  test.skip(!services.lab, `lab not reachable at ${URLS.lab}`)
  test.skip(!!services.labRun, `lab scenario run ${services.labRun} in progress: not launching attacks`)
  await openControlRoom(page)
  const running = async () =>
    ((await (await request.get(`${URLS.lab}/lab/status`)).json()).attacks as { attack_id: string; attack: string; tenant: string; state: string }[]).filter(
      (a) => a.state === 'running',
    )
  const before = new Set((await running()).map((a) => a.attack_id))
  await page.locator('select[title]').selectOption('flood')
  await page.locator('select').nth(1).selectOption('B')
  await page.getByRole('button', { name: /^Launch on B/ }).click()
  const started = await until('flood on B running in the lab', async () => (await running()).find((a) => !before.has(a.attack_id) && a.attack === 'flood' && a.tenant === 'B'), 15_000)
  try {
    const row = page.locator('div', { has: page.locator('b', { hasText: /^flood$/ }) }).filter({ hasText: '→ B' }).last()
    await expect(row).toBeVisible({ timeout: 10_000 })
    await row.getByRole('button', { name: 'Stop' }).click()
    await until('attack stopped', async () => !(await running()).some((a) => a.attack_id === started.attack_id), 15_000)
  } finally {
    if ((await running()).some((a) => a.attack_id === started.attack_id)) await request.delete(`${URLS.lab}/lab/attack/${started.attack_id}`)
  }
})

test('Tenants page edits a quota through PATCH and the change is restored', async ({ page, request, services }) => {
  test.skip(!ALLOW_WRITES, WRITES_OFF)
  test.skip(!!services.labRun, `lab scenario run ${services.labRun} in progress: not editing quotas`)
  const quotaOf = async () =>
    (await (await request.get(`${URLS.gateway}/admin/tenants`, { headers: TOKEN_HEADER })).json()).find((t: { id: string }) => t.id === 'E').quota
  const original = await quotaOf()
  const rate = original.rate_rps === 33 ? 34 : 33
  try {
    await page.goto('/tenants')
    const row = page.getByTestId('row-E')
    await row.getByRole('button', { name: 'Edit quota' }).click()
    await page.getByLabel('E Rate').fill(String(rate))
    await row.getByRole('button', { name: 'Save' }).click()
    await expect.poll(async () => (await quotaOf()).rate_rps).toBe(rate)
    await expect(page.getByText(/Quota for E saved/)).toBeVisible()
    // invalid input never reaches the gateway
    await row.getByRole('button', { name: 'Edit quota' }).click()
    await page.getByLabel('E Rate').fill('-4')
    await row.getByRole('button', { name: 'Save' }).click()
    await expect(row.getByRole('alert')).toContainText('> 0')
    expect((await quotaOf()).rate_rps).toBe(rate)
  } finally {
    await request.patch(`${URLS.gateway}/admin/tenants/E/quota`, { headers: TOKEN_HEADER, data: original })
  }
  expect(await quotaOf()).toEqual(original)
})

test('incident timeline opens the Case File for that incident', async ({ page, request }) => {
  const latest = await until(
    'an incident on the brain',
    async () => ((await (await request.get(`${URLS.brain}/api/incidents?limit=20`)).json()) as { incident_id: string }[]).at(-1),
    45_000,
    1000,
  ).catch(() => null)
  test.skip(!latest, 'no incident on the brain yet')
  await openControlRoom(page)
  const row = page.getByTestId('timeline-row').first()
  await expect(row).toBeVisible({ timeout: 15_000 })
  const href = (await row.getAttribute('href'))!
  await row.click()
  await expect(page).toHaveURL(new RegExp(`${href.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`))
  await expect(page.getByTestId('case-steps')).toBeVisible()
  const id = decodeURIComponent(href.split('/case/')[1])
  const events = (await (await request.get(`${URLS.brain}/api/incidents?incident_id=${encodeURIComponent(id)}`)).json()) as unknown[]
  await expect.poll(() => page.getByTestId('case-log').locator('li').count(), { timeout: 15_000 }).toBeGreaterThanOrEqual(events.length)
  await expect(page.locator('[data-testid="report"], [data-testid="report-placeholder"]')).toBeVisible()
  await expect(page.getByTestId('case-steps').locator('li[data-reached="1"]').first()).toBeVisible()
})

test('Scorecard shows lab results by scenario and mode (or says why not)', async ({ page, request, services }) => {
  await page.goto('/scorecard')
  if (!services.lab) {
    await expect(page.getByRole('alert')).toContainText('Lab results unavailable')
    return
  }
  const results = (await (await request.get(`${URLS.lab}/lab/results`)).json()) as { scenario: string }[]
  const scenarios = new Set(results.map((r) => r.scenario)).size
  if (scenarios === 0) {
    await expect(page.getByText(/No runs yet/)).toBeVisible()
    return
  }
  await expect(page.getByTestId('score-table')).toBeVisible()
  await expect(page.locator('[data-testid^="score-"]:not([data-testid="score-table"])')).toHaveCount(scenarios)
  await expect(page.getByTestId('score-table').locator('tbody tr')).toHaveCount(scenarios * 3)
})

test('flow view toggle draws the live request flow and toggles back', async ({ page }) => {
  await openControlRoom(page)
  await page.getByTestId('view-flow').click()
  const canvas = page.getByTestId('flow-view').locator('canvas')
  await expect(canvas).toBeVisible()
  await expect
    .poll(
      () =>
        canvas.evaluate((c: HTMLCanvasElement) => {
          const d = c.getContext('2d')!.getImageData(0, 0, c.width, c.height).data
          let n = 0
          for (let i = 3; i < d.length; i += 16) if (d[i] > 0) n++
          return n
        }),
      { timeout: 10_000 },
    )
    .toBeGreaterThan(1000)
  await page.getByTestId('view-tiles').click()
  await expect(page.getByTestId('tile-L')).toBeVisible()
})

test('Cases page shows the handoff column when the bridge answers, or says it is not reachable', async ({ page, request, services }) => {
  await page.goto('/case')
  const status = page.getByTestId('handoff-status')
  if (!services.handoff) {
    await expect(status).toContainText('not reachable', { timeout: 12_000 })
    return
  }
  const list = (await (await request.get(`${URLS.handoff}/cases`, { headers: TOKEN_HEADER })).json()) as { case_id: string; incident_id: string; owner?: string }[]
  await expect(status).toContainText(`Web PS3 case`, { timeout: 12_000 })
  const last = list.at(-1)
  if (last) {
    const cell = page.getByTestId(`handoff-${last.incident_id}`)
    await expect(cell).toContainText(last.case_id, { timeout: 12_000 })
    if (last.owner) await expect(cell).toContainText(last.owner)
  }
})
