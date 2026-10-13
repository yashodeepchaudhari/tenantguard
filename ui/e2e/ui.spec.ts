// Operator flows in the dashboard. Every effect is verified through the backend's own API,
// not just the UI. Anything the suite changes (mode, projects, tenants) is restored afterwards.
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

test('Control Room renders a tile per tenant, the replica grid by project and a status sentence', async ({ page, request }) => {
  await openControlRoom(page)
  // 12 seeded tenants, plus any added on the Pool page
  const tenants = ((await (await request.get(`${URLS.brain}/api/state`)).json()).tenants as unknown[]).length
  expect(tenants).toBeGreaterThanOrEqual(12)
  await expect(page.locator('[data-testid^="tile-"]')).toHaveCount(tenants)
  const projects = ((await (await request.get(`${URLS.brain}/api/pool`)).json()).projects as { id: string }[]).map((p) => p.id)
  await expect(page.locator('[data-testid^="project-"]')).toHaveCount(projects.length)
  await expect.poll(() => page.locator('[data-testid^="replica-tg-"][data-state="ready"]').count(), { timeout: 20_000 }).toBeGreaterThanOrEqual(projects.length)
  await expect(page.getByTestId('status-sentence')).not.toBeEmpty()
  await expect(page.getByTestId('impact')).toBeVisible()
  // the header says the brain stream is live
  await expect(page.getByText('live', { exact: true })).toBeVisible()
})

test('picking a tenant tile outlines its project on the replica grid', async ({ page, request }) => {
  await openControlRoom(page)
  const project: string = (await (await request.get(`${URLS.gateway}/admin/tenants`, { headers: TOKEN_HEADER })).json()).find(
    (t: { id: string }) => t.id === 'E',
  ).project
  await page.getByTestId('tile-E').click()
  await expect
    .poll(async () => page.locator('[data-testid^="project-"][data-lit="1"]').evaluateAll((els) => els.map((e) => e.getAttribute('data-testid'))))
    .toEqual([`project-${project}`])
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

test('Pool page creates a project and a tenant, hands the key to the lab, and deletes both from the page', async ({ page, request, services }) => {
  test.skip(!ALLOW_WRITES, WRITES_OFF)
  test.skip(!!services.labRun, `lab scenario run ${services.labRun} in progress: not changing the registry`)
  const pool = async () => (await (await request.get(`${URLS.brain}/api/pool`)).json()) as { projects: { id: string; tenants: string[]; replicas: unknown[] }[] }
  const id = `e2e${Date.now() % 100000}`
  try {
    await page.goto('/pool')
    await expect(page.getByTestId('pool-bar')).toBeVisible({ timeout: 20_000 })
    const np = page.getByTestId('new-project')
    await np.getByLabel('Id', { exact: true }).fill(id)
    await np.getByLabel('Name', { exact: true }).fill('E2E')
    await np.getByLabel(/^Tier/).selectOption('free')
    await np.getByRole('button', { name: 'Create project' }).click()
    await expect.poll(async () => (await pool()).projects.some((p) => p.id === id)).toBe(true)
    await expect(page.getByTestId(`pool-row-${id}`)).toBeVisible()
    // a floor that would break the pool is refused by the brain, with its reason
    await np.getByLabel('Id', { exact: true }).fill(`${id}x`)
    await np.getByLabel('Name', { exact: true }).fill('Too big')
    await np.getByLabel('Floor cores').fill('99')
    await np.getByLabel('Cap cores').fill('99')
    await np.getByRole('button', { name: 'Create project' }).click()
    await expect(np.getByRole('alert')).toBeVisible()
    expect((await pool()).projects.some((p) => p.id === `${id}x`)).toBe(false)
    // a tenant in the new project: the key is shown once
    const nt = page.getByTestId('new-tenant')
    await nt.getByLabel(/^Project/).selectOption(id)
    await nt.getByLabel('Tenant id').fill('Q9')
    await nt.getByLabel('Name', { exact: true }).fill('Quiz')
    await nt.getByRole('button', { name: 'Add tenant' }).click()
    await expect(page.getByTestId('api-key')).toContainText('tk_')
    await expect.poll(async () => (await pool()).projects.find((p) => p.id === id)?.tenants).toEqual(['Q9'])
    // the page hands the key to the lab at once, so the new tenant gets calm traffic
    const labTenants = async () =>
      ((await (await request.get(`${URLS.lab}/lab/personas`)).json()) as { personas: { tenant: string }[] }).personas.map((x) => x.tenant)
    if (services.lab) {
      await expect(page.getByTestId('lab-traffic')).toContainText('calm traffic for Q9')
      expect(await labTenants()).toContain('Q9')
    } else {
      await expect(page.getByTestId('lab-traffic')).toContainText('Lab did not take the key')
    }
    // delete from the page: the tenant (×), then the project (Delete -> Yes, delete)
    const row = page.getByTestId(`pool-row-${id}`)
    await expect(row.getByRole('button', { name: 'Delete' })).toBeDisabled()
    await row.getByRole('button', { name: 'remove tenant Q9' }).click()
    await expect.poll(async () => (await pool()).projects.find((p) => p.id === id)?.tenants).toEqual([])
    if (services.lab) await expect.poll(labTenants).not.toContain('Q9')
    await row.getByRole('button', { name: 'Delete' }).click()
    await row.getByRole('button', { name: 'Yes, delete' }).click()
    await expect.poll(async () => (await pool()).projects.some((p) => p.id === id)).toBe(false)
    await expect(row).toHaveCount(0)
  } finally {
    await request.delete(`${URLS.brain}/admin/tenants/Q9`, { headers: TOKEN_HEADER })
    await request.delete(`${URLS.brain}/admin/projects/${id}`, { headers: TOKEN_HEADER })
  }
  expect((await pool()).projects.some((p) => p.id === id)).toBe(false)
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
  // the timeline is live: a newer incident can push this row down, so click the link by its href
  const href = (await row.getAttribute('href'))!
  await page.locator(`[data-testid="timeline-row"][href="${href}"]`).first().click()
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

test('flow view toggle draws the live request flow and toggles back', async ({ page, request, services }) => {
  await openControlRoom(page)
  await page.getByTestId('view-flow').click()
  // during an attack the flow view says what the brain is doing to routing (pins, lending, refusals)
  const attackRunning =
    services.lab &&
    ((await (await request.get(`${URLS.lab}/lab/status`)).json()).attacks as { state: string }[]).some((a) => a.state === 'running')
  if (attackRunning) await expect(page.getByTestId('flow-hint')).toHaveAttribute('data-kind', /split|lending|refused|off/)
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
