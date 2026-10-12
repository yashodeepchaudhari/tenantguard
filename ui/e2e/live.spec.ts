// @live: the real detection loop on the Docker host. Run only with E2E_LIVE=1 against a stack
// with calm traffic available (lab). calm -> whale on K -> a K incident within 60 s -> stop the
// whale -> K recovers (normal, full quota) within 90 s.
import { ALLOW_WRITES, TOKEN_HEADER, URLS, until, WRITES_OFF } from './env.ts'
import { expect, test } from './fixtures.ts'

interface TenantView {
  tenant: string
  level: string
  factor: number
  noisy_score: number
}

test('@live whale on K is detected, acted on, and recovers', async ({ page, request, services }) => {
  test.setTimeout(240_000)
  test.skip(!ALLOW_WRITES, WRITES_OFF)
  test.skip(!services.lab, `lab not reachable at ${URLS.lab}`)
  test.skip(!!services.labRun, `lab scenario run ${services.labRun} in progress`)

  const state = async () => (await (await request.get(`${URLS.brain}/api/state`)).json()) as { mode: string; tenants: TenantView[] }
  const k = async () => (await state()).tenants.find((t) => t.tenant === 'K')!
  const modeBefore = (await (await request.get(`${URLS.gateway}/admin/mode`, { headers: TOKEN_HEADER })).json()).mode
  const startedAt = Date.now() / 1000
  let attackId: string | null = null
  let calmStarted = false
  try {
    if (modeBefore !== 'tenantguard') await request.post(`${URLS.gateway}/admin/mode`, { headers: TOKEN_HEADER, data: { mode: 'tenantguard' } })
    const lab = await (await request.get(`${URLS.lab}/lab/status`)).json()
    if (!lab.calm_running) {
      await request.post(`${URLS.lab}/lab/calm/start`)
      calmStarted = true
    }
    // calm first: K must start normal
    await until('K normal before the attack', async () => (await k()).level === 'normal', 60_000, 1000)

    const r = await request.post(`${URLS.lab}/lab/attack`, { data: { attack: 'whale', tenant: 'K', intensity: 2, duration_s: 120 } })
    expect(r.status(), await r.text()).toBe(200)
    attackId = (await r.json()).attack_id

    // detection: a K incident event and a level above normal within 60 s
    const incident = await until(
      'K incident within 60 s',
      async () => {
        const evs = (await (await request.get(`${URLS.brain}/api/incidents?tenant=K&limit=50`)).json()) as { ts: number; tenant: string; level?: string }[]
        return evs.find((e) => e.tenant === 'K' && e.ts >= startedAt)
      },
      60_000,
      1000,
    )
    expect(incident.tenant).toBe('K')
    const flagged = await until('K above normal', async () => {
      const t = await k()
      return t.level !== 'normal' && t
    }, 60_000, 1000)
    expect(flagged.noisy_score).toBeGreaterThan(1)

    // the dashboard shows it: K's tile is no longer NORMAL
    await page.goto('/?view=tiles')
    await expect(page.getByTestId('tile-K')).not.toContainText('NORMAL', { timeout: 20_000 })

    // stop the whale; K must recover to normal at full quota within 90 s
    await request.delete(`${URLS.lab}/lab/attack/${attackId}`)
    attackId = null
    await until('K recovered (normal, factor 1)', async () => {
      const t = await k()
      return t.level === 'normal' && t.factor >= 0.999
    }, 90_000, 1000)
    await expect(page.getByTestId('tile-K')).toContainText('NORMAL', { timeout: 20_000 })
  } finally {
    if (attackId) await request.delete(`${URLS.lab}/lab/attack/${attackId}`)
    if (calmStarted) await request.post(`${URLS.lab}/lab/calm/stop`)
    if (modeBefore !== 'tenantguard') await request.post(`${URLS.gateway}/admin/mode`, { headers: TOKEN_HEADER, data: { mode: modeBefore } })
  }
})
