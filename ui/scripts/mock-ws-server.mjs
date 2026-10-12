// Stand-in brain + gateway admin + lab, driven by src/mock/sim.ts, speaking the real wire
// contracts over HTTP/WS with CORS. Lets the UI run in REAL mode (VITE_MOCK=0) before the
// services exist, and exercises the actual fetch / WebSocket code paths.
//
//   node scripts/mock-ws-server.mjs [--brain 18200] [--gateway 18000] [--lab 18300] [--no-script]
//
// Then point ui/.env at it: VITE_BRAIN_URL=http://127.0.0.1:18200 etc. Requires Node >= 23.6.
import http from 'node:http'
import { WebSocketServer } from 'ws'
import { MockPlatform, SimError } from '../src/mock/sim.ts'

const arg = (name, def) => {
  const i = process.argv.indexOf(`--${name}`)
  return i > 0 ? Number(process.argv[i + 1]) : def
}
const PORTS = { brain: arg('brain', 18200), gateway: arg('gateway', 18000), lab: arg('lab', 18300) }
const HOST = process.env.MOCK_HOST || '0.0.0.0'
const SCRIPT = !process.argv.includes('--no-script')

const sim = new MockPlatform()
const now = () => Date.now() / 1000
const incidents = []
let latest = null

function send(res, status, body) {
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,PATCH,DELETE,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
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

function serve(name, port, routes) {
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x')
    console.log(`${name} ${req.method} ${url.pathname}`)
    if (req.method === 'OPTIONS') return send(res, 204)
    for (const [method, re, fn] of routes) {
      const m = req.method === method && url.pathname.match(re)
      if (!m) continue
      try {
        const body = method === 'GET' || method === 'DELETE' ? undefined : await readJson(req)
        return send(res, 200, await fn(m, body))
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
  ['GET', /^\/api\/incidents$/, () => incidents.slice(-200)],
  ['GET', /^\/healthz$/, () => ({ ok: true })],
])
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
  ['PATCH', /^\/admin\/tenants\/([^/]+)\/quota$/, (m, b) => sim.patchQuota(decodeURIComponent(m[1]), b)],
  ['GET', /^\/admin\/overrides$/, () => sim.overrides(now())],
])

// ---- lab ----
serve('lab', PORTS.lab, [
  ['GET', /^\/lab\/status$/, () => sim.labStatus(now())],
  ['POST', /^\/lab\/attack$/, (_m, b) => sim.startAttack(b, now())],
  ['DELETE', /^\/lab\/attack\/([^/]+)$/, (m) => sim.stopAttack(decodeURIComponent(m[1]), now())],
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
    console.log(`incident ${e.incident_id} ${e.stage} ${e.prev_level ?? ''}->${e.level ?? ''} ${e.detail}`)
  }
  if (incidents.length > 500) incidents.splice(0, incidents.length - 500)
  const frames = [...evs.map((e) => JSON.stringify(e)), JSON.stringify(snapshot)]
  for (const ws of wss.clients) if (ws.readyState === 1) for (const f of frames) ws.send(f)
}, 500)
