// Stand-in brain + gateway + lab + handoff bridge + narrator, driven by src/mock/sim.ts,
// speaking the real wire contracts over HTTP/WS with CORS. Lets the UI run in REAL mode
// (VITE_MOCK=0) before the services exist, exercises the actual fetch / WebSocket code paths,
// and is the default target of the Playwright suite (ui/e2e).
//
//   node scripts/mock-ws-server.mjs [--brain 18200] [--gateway 18000] [--lab 18300] [--handoff 18400] [--narrator 18700] [--no-script]
//
// Then point ui/.env at it: VITE_BRAIN_URL=http://127.0.0.1:18200 etc. Requires Node >= 23.6.
import http from 'node:http'
import { WebSocketServer } from 'ws'
import { mockCases } from '../src/mock/handoff.ts'
import { RESULTS_FIXTURE } from '../src/mock/results.ts'
import { MockPlatform, SimError } from '../src/mock/sim.ts'

const arg = (name, def) => {
  const i = process.argv.indexOf(`--${name}`)
  return i > 0 ? Number(process.argv[i + 1]) : def
}
const PORTS = {
  brain: arg('brain', 18200),
  gateway: arg('gateway', 18000),
  lab: arg('lab', 18300),
  handoff: arg('handoff', 18400),
  narrator: arg('narrator', 18700),
}
const HOST = process.env.MOCK_HOST || '0.0.0.0'
const SCRIPT = !process.argv.includes('--no-script')

const sim = new MockPlatform()
const now = () => Date.now() / 1000
const incidents = []
let latest = null

function send(res, status, body, extra = {}) {
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, X-TenantGuard-Control-Token',
    ...extra,
  })
  res.end(body === undefined ? '' : JSON.stringify(body))
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let data = ''
    req.on('data', (c) => (data += c))
    req.on('end', () => {
      try {
        resolve(data ? JSON.parse(data) : {})
      } catch {
        reject(new SimError(422, 'invalid JSON body'))
      }
    })
  })
}

const CONTROL_TOKEN = process.env.TENANTGUARD_CONTROL_TOKEN || 'dev'

/** A route handler may return a value (200 JSON) or a `Reply` for another status / extra headers. */
class Reply {
  constructor(status, body, headers = {}) {
    this.status = status
    this.body = body
    this.headers = headers
  }
}

// `tokenPaths`: the control token is required on these path prefixes only (like the gateway:
// /admin and /control need it, /api and /healthz do not).
function serve(name, port, routes, { tokenPaths = [] } = {}) {
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x')
    if (process.env.MOCK_QUIET !== '1') console.log(`${name} ${req.method} ${url.pathname}`)
    if (req.method === 'OPTIONS') return send(res, 204)
    if (tokenPaths.some((p) => url.pathname.startsWith(p)) && req.headers['x-tenantguard-control-token'] !== CONTROL_TOKEN) {
      return send(res, 401, { detail: 'missing or wrong X-TenantGuard-Control-Token' })
    }
    for (const [method, re, fn] of routes) {
      const m = (method === 'ANY' || req.method === method) && url.pathname.match(re)
      if (!m) continue
      try {
        const body = req.method === 'GET' || req.method === 'DELETE' || req.method === 'HEAD' ? undefined : await readJson(req)
        const out = await fn(m, body, url, req)
        if (out instanceof Reply) return send(res, out.status, out.body, out.headers)
        return send(res, 200, out)
      } catch (e) {
        if (e instanceof SimError) return send(res, e.status, { detail: e.message })
        console.error(e)
        return send(res, 500, { detail: String(e) })
      }
    }
    send(res, 404, { detail: 'Not Found' })
  })
  server.listen(port, HOST, () => console.log(`mock ${name} on http://${HOST}:${port}`))
  return server
}

// ---- brain ----
const brain = serve('brain', PORTS.brain, [
  ['GET', /^\/api\/state$/, () => latest],
  [
    'GET',
    /^\/api\/incidents$/,
    (_m, _b, url) => {
      const id = url.searchParams.get('incident_id')
      const tenant = url.searchParams.get('tenant')
      const limit = Number(url.searchParams.get('limit') || 200)
      return incidents.filter((e) => (!id || e.incident_id === id) && (!tenant || e.tenant === tenant)).slice(-limit)
    },
  ],
  ['GET', /^\/healthz$/, () => ({ ok: true })],
  // v2: the brain owns the registry (pool -> projects -> tenants)
  ['GET', /^\/api\/pool$/, () => sim.poolView()],
  ['PUT', /^\/admin\/pool$/, (_m, b) => sim.setPool(b)],
  ['POST', /^\/admin\/projects$/, (_m, b) => sim.createProject(b)],
  ['PATCH', /^\/admin\/projects\/([^/]+)$/, (m, b) => sim.patchProject(decodeURIComponent(m[1]), b)],
  ['DELETE', /^\/admin\/projects\/([^/]+)$/, (m) => sim.deleteProject(decodeURIComponent(m[1]))],
  ['POST', /^\/admin\/projects\/([^/]+)\/tenants$/, (m, b) => sim.addTenant(decodeURIComponent(m[1]), b)],
  ['DELETE', /^\/admin\/tenants\/([^/]+)$/, (m) => (sim.removeTenant(decodeURIComponent(m[1])), { ok: true })],
], { tokenPaths: ['/admin', '/control'] })
const wss = new WebSocketServer({ server: brain, path: '/ws/events' })
wss.on('connection', (ws, req) => {
  console.log(`brain WS connect from ${req.socket.remoteAddress} (${wss.clients.size} clients)`)
  if (latest) ws.send(JSON.stringify(latest))
})

// ---- gateway admin ----
serve('gateway', PORTS.gateway, [
  ['GET', /^\/admin\/mode$/, () => ({ mode: sim.mode })],
  ['POST', /^\/admin\/mode$/, (_m, b) => sim.setMode(b.mode)],
  ['GET', /^\/admin\/tenants$/, () => sim.tenantsAdmin()],
  ['GET', /^\/admin\/tenants\/([^/]+)$/, (m) => sim.adminView(decodeURIComponent(m[1]))],
  ['GET', /^\/admin\/overrides$/, () => sim.overrides(now())],
  ['GET', /^\/healthz$/, () => ({ ok: true })],
  // tenant traffic: identity from X-API-Key (demo keys tk_<ID>_demo), CPU header like a worker
  [
    'ANY',
    /^\/api\/(.+)$/,
    (m, _b, _u, req) => {
      const key = req.headers['x-api-key']
      const tenant = typeof key === 'string' ? /^tk_([A-Za-z0-9_-]+?)_demo$/.exec(key)?.[1] : undefined
      if (!tenant || !sim.tenants.has(tenant)) return new Reply(401, { error: 'unauthorized' })
      const cpu = (0.4 + Math.random() * 0.6).toFixed(3)
      const replica = sim.adminView(tenant).replicas[0] ?? 'none'
      return new Reply(200, { ok: true, tenant, path: `/api/${m[1]}` }, { 'X-TenantGuard-Cpu-Ms': cpu, 'X-TenantGuard-Server': replica })
    },
  ],
], { tokenPaths: ['/admin', '/control'] })

// ---- lab ----
serve('lab', PORTS.lab, [
  ['GET', /^\/lab\/status$/, () => sim.labStatus(now())],
  ['POST', /^\/lab\/tenants$/, (_m, b) => sim.labAddTenant(b)],
  ['GET', /^\/lab\/personas$/, () => ({ personas: sim.tenantsAdmin().map((t) => ({ tenant: t.id })) })],
  ['DELETE', /^\/lab\/tenants\/([^/]+)$/, (m) => (sim.labRemoveTenant(decodeURIComponent(m[1])), { ok: true })],
  ['POST', /^\/lab\/attack$/, (_m, b) => sim.startAttack(b, now())],
  ['DELETE', /^\/lab\/attack\/([^/]+)$/, (m) => sim.stopAttack(decodeURIComponent(m[1]), now())],
  ['GET', /^\/lab\/results$/, () => RESULTS_FIXTURE],
])

// ---- handoff bridge ----
serve('handoff', PORTS.handoff, [
  ['GET', /^\/cases$/, () => mockCases(incidents, now())],
  ['GET', /^\/cases\/([^/]+)$/, (m) => {
    const c = mockCases(incidents, now()).find((x) => x.case_id === decodeURIComponent(m[1]))
    if (!c) throw new SimError(404, 'unknown case')
    return c
  }],
])

// ---- narrator (ReportView, template text built from the incident log) ----
function reportFor(id) {
  const evs = incidents.filter((e) => e.incident_id === id)
  const trigger = [...evs].reverse().find((e) => ['named', 'verdict', 'closed', 'resolved', 'explained'].includes(e.stage))
  if (!trigger) return null
  const tenant = evs.find((e) => e.tenant)?.tenant ?? null
  const hhmmss = (ts) => new Date(ts * 1000).toISOString().slice(11, 19)
  return {
    incident_id: id,
    tenant,
    trigger_stage: trigger.stage === 'resolved' || trigger.stage === 'explained' ? 'closed' : trigger.stage,
    version: evs.length,
    report: {
      headline: `${tenant ? `Tenant ${tenant}` : 'A server'} slowed its neighbours`,
      timeline: evs.map((e) => `${hhmmss(e.ts)} ${e.stage}: ${e.detail}`),
      cause: trigger.detail,
      evidence_points: evs.filter((e) => e.action).map((e) => e.action),
      action_taken: evs.filter((e) => e.action).map((e) => e.action).pop() ?? 'none',
      operator_recommendation: 'No action needed: TenantGuard recovered the tenant automatically.',
    },
    customer_message: { message: 'Some requests were slower for a short time; service is back to normal.' },
    evidence: { stages: evs.map((e) => e.stage) },
    created_at: evs[0].ts,
    updated_at: evs[evs.length - 1].ts,
  }
}
serve('narrator', PORTS.narrator, [
  ['GET', /^\/healthz$/, () => ({ ok: true })],
  ['GET', /^\/reports$/, () => [...new Set(incidents.map((e) => e.incident_id))].map(reportFor).filter(Boolean).reverse()],
  [
    'GET',
    /^\/reports\/([^/]+)$/,
    (m) => {
      const r = reportFor(decodeURIComponent(m[1]))
      if (!r) throw new SimError(404, 'no report')
      return r
    },
  ],
])

// ---- 2 Hz tick ----
const started = now()
let scripted = !SCRIPT
setInterval(() => {
  if (!scripted && now() - started > 4) {
    scripted = true
    sim.startAttack({ attack: 'whale', tenant: 'K', intensity: 2, duration_s: 45 }, now())
    console.log('scripted: whale on K for 45 s')
  }
  const { snapshot, incidents: evs } = sim.tick(now())
  latest = snapshot
  for (const e of evs) {
    incidents.push(e)
    if (process.env.MOCK_QUIET !== '1') console.log(`incident ${e.incident_id} ${e.stage} ${e.prev_level ?? ''}->${e.level ?? ''} ${e.detail}`)
  }
  if (incidents.length > 500) incidents.splice(0, incidents.length - 500)
  const frames = [...evs.map((e) => JSON.stringify(e)), JSON.stringify(snapshot)]
  for (const ws of wss.clients) if (ws.readyState === 1) for (const f of frames) ws.send(f)
}, 500)
