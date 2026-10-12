// API smoke: each service answers with the contract shapes the dashboard depends on.
import { apiKey, TOKEN_HEADER, URLS } from './env.ts'
import { expect, test } from './fixtures.ts'

const isNum = (v: unknown) => typeof v === 'number' && Number.isFinite(v)
const LEVELS = ['normal', 'warn', 'throttle', 'deprioritize', 'quarantine', 'blocked']
const MODES = ['off', 'industry', 'tenantguard']

test.describe('gateway', () => {
  test('healthz answers', async ({ request }) => {
    const r = await request.get(`${URLS.gateway}/healthz`)
    expect(r.status()).toBe(200)
  })

  test('tenant traffic without an API key is refused with 401', async ({ request }) => {
    const r = await request.get(`${URLS.gateway}/api/light`)
    expect(r.status()).toBe(401)
  })

  test('admin API without the control token is refused with 401', async ({ request }) => {
    const r = await request.get(`${URLS.gateway}/admin/mode`)
    expect(r.status()).toBe(401)
    const ok = await request.get(`${URLS.gateway}/admin/mode`, { headers: TOKEN_HEADER })
    expect(ok.status()).toBe(200)
    expect(MODES).toContain((await ok.json()).mode)
  })

  test('a valid tenant request returns 200 with the measured CPU header', async ({ request }) => {
    const r = await request.get(`${URLS.gateway}/api/light`, { headers: { 'X-API-Key': apiKey('A') } })
    expect(r.status()).toBe(200)
    const cpu = r.headers()['x-tenantguard-cpu-ms']
    expect(cpu, 'X-TenantGuard-Cpu-Ms header').toBeTruthy()
    expect(Number(cpu)).toBeGreaterThanOrEqual(0)
  })

  test('admin tenants list has 12 tenants with barcodes and quotas', async ({ request }) => {
    const r = await request.get(`${URLS.gateway}/admin/tenants`, { headers: TOKEN_HEADER })
    expect(r.status()).toBe(200)
    const list = await r.json()
    expect(list).toHaveLength(12)
    for (const t of list) {
      expect(t.servers).toHaveLength(3)
      for (const k of ['rate_rps', 'burst', 'cost_ms_per_s', 'concurrency']) expect(isNum(t.quota[k]), `${t.id} quota.${k}`).toBe(true)
      expect(t).not.toHaveProperty('api_key')
    }
  })
})

test.describe('brain', () => {
  test('/api/state is a valid StateSnapshot', async ({ request }) => {
    const r = await request.get(`${URLS.brain}/api/state`)
    expect(r.status()).toBe(200)
    const s = await r.json()
    expect(s.type).toBe('state')
    expect(isNum(s.ts)).toBe(true)
    expect(MODES).toContain(s.mode)
    expect(s.tenants).toHaveLength(12)
    for (const t of s.tenants) {
      for (const k of ['p95_ms', 'rps', 'rejected_rps', 'cpu_share', 'noisy_score', 'noisy_norm', 'factor']) expect(isNum(t[k]), `${t.tenant}.${k}`).toBe(true)
      expect(LEVELS).toContain(t.level)
      expect(t.cpu_share).toBeGreaterThanOrEqual(0)
      expect(t.cpu_share).toBeLessThanOrEqual(1)
      expect(t.noisy_norm).toBeGreaterThanOrEqual(0)
      expect(t.noisy_norm).toBeLessThanOrEqual(1)
    }
    const fleet = s.servers.filter((x: { server: string }) => /^w\d$/.test(x.server))
    expect(fleet).toHaveLength(9)
    for (const x of s.servers) expect(['healthy', 'slow', 'drained']).toContain(x.status)
    expect(s.utilization).toBeGreaterThanOrEqual(0)
    expect(s.utilization).toBeLessThanOrEqual(1)
    expect(s.fairness).toBeGreaterThanOrEqual(0)
    expect(s.fairness).toBeLessThanOrEqual(1)
    expect(typeof s.status_sentence).toBe('string')
    expect(typeof s.noisy_thresholds).toBe('object')
  })

  test('/api/incidents is a list of IncidentEvents', async ({ request }) => {
    const r = await request.get(`${URLS.brain}/api/incidents?limit=50`)
    expect(r.status()).toBe(200)
    const list = await r.json()
    expect(Array.isArray(list)).toBe(true)
    for (const e of list) {
      expect(e.type).toBe('incident')
      expect(typeof e.incident_id).toBe('string')
      expect(isNum(e.ts)).toBe(true)
    }
  })
})

test.describe('optional services', () => {
  test('lab status', async ({ request, services }) => {
    test.skip(!services.lab, `lab not reachable at ${URLS.lab}`)
    const r = await request.get(`${URLS.lab}/lab/status`)
    expect(r.status()).toBe(200)
    const s = await r.json()
    expect(Array.isArray(s.attacks)).toBe(true)
    expect(typeof s.calm_running).toBe('boolean')
  })

  test('handoff bridge lists CaseViews', async ({ request, services }) => {
    test.skip(!services.handoff, `handoff bridge not reachable at ${URLS.handoff}`)
    const r = await request.get(`${URLS.handoff}/cases`, { headers: TOKEN_HEADER })
    expect(r.status()).toBe(200)
    const list = await r.json()
    expect(Array.isArray(list)).toBe(true)
    for (const c of list) {
      expect(c.case_id).toMatch(/^INC-\d+$/)
      expect(typeof c.incident_id).toBe('string')
      expect(c.context.evidence_link).toContain(`/case/`)
    }
  })

  test('narrator lists ReportViews', async ({ request, services }) => {
    test.skip(!services.narrator, `narrator not reachable at ${URLS.narrator}`)
    const r = await request.get(`${URLS.narrator}/reports`)
    expect(r.status()).toBe(200)
    const list = await r.json()
    expect(Array.isArray(list)).toBe(true)
    for (const v of list) {
      expect(typeof v.incident_id).toBe('string')
      expect(typeof v.report.headline).toBe('string')
      expect(Array.isArray(v.report.timeline)).toBe(true)
      expect(typeof v.customer_message.message).toBe('string')
    }
  })
})
