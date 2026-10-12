// Targets for the end-to-end suite. Defaults are the mock stack (scripts/mock-ws-server.mjs +
// a Vite dev server pointed at it), so the suite runs anywhere; set E2E_* to test a real stack.
const env = process.env

export const MOCK_PORTS = { ui: 5174, gateway: 18000, brain: 18200, lab: 18300, handoff: 18400, narrator: 18700 }
const local = (port: number) => `http://127.0.0.1:${port}`

/** true when no E2E_UI_URL is given: Playwright starts the mock stack itself */
export const MOCK = !env.E2E_UI_URL

const strip = (u: string) => u.replace(/\/+$/, '')

export const URLS = {
  ui: strip(env.E2E_UI_URL || local(MOCK_PORTS.ui)),
  gateway: strip(env.E2E_GATEWAY_URL || local(MOCK_PORTS.gateway)),
  brain: strip(env.E2E_BRAIN_URL || local(MOCK_PORTS.brain)),
  lab: strip(env.E2E_LAB_URL || local(MOCK_PORTS.lab)),
  handoff: strip(env.E2E_HANDOFF_URL || local(MOCK_PORTS.handoff)),
  narrator: strip(env.E2E_NARRATOR_URL || local(MOCK_PORTS.narrator)),
  token: env.E2E_CONTROL_TOKEN || 'dev',
}

export const TOKEN_HEADER = { 'X-TenantGuard-Control-Token': URLS.token }

/** Tests that change state (mode, quotas, attacks) run against the mock stack, and against a real
 *  stack only when E2E_ALLOW_WRITES=1 is set on purpose: a shared host may be mid-measurement. */
export const ALLOW_WRITES = MOCK || env.E2E_ALLOW_WRITES === '1'
export const WRITES_OFF = 'real stack: state-changing test skipped (set E2E_ALLOW_WRITES=1 to allow)'

/** Demo API key for a tenant (layouts/ag23.json). */
export const apiKey = (tenant: string) => `tk_${tenant}_demo`

/** GET with a short timeout; null when the service is unreachable. */
export async function probe(url: string, headers: Record<string, string> = {}): Promise<{ status: number; body: unknown } | null> {
  try {
    const r = await fetch(url, { headers, signal: AbortSignal.timeout(4000) })
    const text = await r.text()
    let body: unknown = text
    try {
      body = JSON.parse(text)
    } catch {
      /* not JSON */
    }
    return { status: r.status, body }
  } catch {
    return null
  }
}

/** Poll until fn returns a truthy value or the timeout passes (then throws). */
export async function until<T>(what: string, fn: () => Promise<T | null | undefined | false>, timeoutMs = 10_000, everyMs = 500): Promise<T> {
  const t0 = Date.now()
  for (;;) {
    const v = await fn()
    if (v) return v
    if (Date.now() - t0 > timeoutMs) throw new Error(`timed out after ${timeoutMs} ms waiting for: ${what}`)
    await new Promise((r) => setTimeout(r, everyMs))
  }
}
