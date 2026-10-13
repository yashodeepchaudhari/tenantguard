// One interface for every backend call the UI makes. `realBackend` talks HTTP/WS to the
// brain, gateway and lab; `mockBackend` (src/mock) runs the in-browser simulation.
import { config, wsUrl } from '../config.ts'
import type {
  AttackReq,
  AttackStatus,
  BrainMessage,
  IncidentEvent,
  LabStatus,
  Mode,
  ModeBody,
  OverridesView,
  CaseView,
  PoolSpec,
  PoolView,
  ProjectPatch,
  ProjectSpec,
  RunResult,
  TenantAdminView,
  TenantCreate,
  TenantCreated,
} from '../types.ts'

export type ConnState = 'connecting' | 'open' | 'closed'

export interface Backend {
  readonly kind: 'real' | 'mock'
  /** Subscribe to the brain event stream. Returns an unsubscribe function. */
  subscribe(onMessage: (m: BrainMessage) => void, onConn: (s: ConnState) => void): () => void
  /** Recent incidents for backfill when the page loads (may be empty). */
  incidents(): Promise<IncidentEvent[]>
  /** Every event of one incident, oldest first (brain GET /api/incidents?incident_id=). */
  incident(id: string): Promise<IncidentEvent[]>
  /** Measured scenario runs (lab GET /lab/results). */
  results(): Promise<RunResult[]>
  /** Handoff bridge GET /cases (CaseView list); rejects when the bridge is unreachable. */
  handoffCases(): Promise<CaseView[]>
  getMode(): Promise<Mode>
  setMode(mode: Mode): Promise<Mode>
  tenants(): Promise<TenantAdminView[]>
  overrides(): Promise<OverridesView>
  /** brain GET /api/pool */
  pool(): Promise<PoolView>
  /** brain admin API (control token): the registry of pool, projects and tenants */
  setPool(spec: PoolSpec): Promise<PoolView>
  createProject(spec: ProjectSpec): Promise<PoolView>
  patchProject(id: string, patch: ProjectPatch): Promise<PoolView>
  deleteProject(id: string): Promise<PoolView>
  addTenant(project: string, req: TenantCreate): Promise<TenantCreated>
  removeTenant(id: string): Promise<void>
  labStatus(): Promise<LabStatus>
  startAttack(req: AttackReq): Promise<AttackStatus>
  stopAttack(id: string): Promise<AttackStatus>
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/** Turn a FastAPI error body ({detail: str | [{msg, loc}]}) into one readable line. */
export function errorText(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail
    if (typeof d === 'string') return d
    if (Array.isArray(d)) {
      return d
        .map((x) => {
          if (typeof x === 'string') return x
          const loc = Array.isArray(x?.loc) ? x.loc.filter((p: unknown) => p !== 'body').join('.') : ''
          return loc ? `${loc}: ${x?.msg ?? ''}` : String(x?.msg ?? '')
        })
        .filter(Boolean)
        .join('; ')
    }
  }
  return fallback
}

/** Header required on gateway and brain /admin/* and /control/* (contracts.H_CONTROL_TOKEN). */
export const H_CONTROL_TOKEN = 'X-TenantGuard-Control-Token'
const admin = (init: RequestInit = {}): RequestInit => ({ ...init, headers: { ...init.headers, [H_CONTROL_TOKEN]: config.controlToken } })

async function http<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(base + path, {
      ...init,
      headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers },
      signal: init?.signal ?? AbortSignal.timeout(5000),
    })
  } catch (e) {
    throw new ApiError(0, `${base} unreachable (${e instanceof Error ? e.message : String(e)})`)
  }
  const text = await res.text()
  let body: unknown = undefined
  try {
    body = text ? JSON.parse(text) : undefined
  } catch {
    body = text
  }
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${errorText(body, res.statusText || 'error')}`)
  return body as T
}

const json = (method: string, data: unknown): RequestInit => ({ method, body: JSON.stringify(data) })

/** Accepts a bare list or {incidents: [...]}. */
export function incidentList(body: unknown): IncidentEvent[] {
  const list = Array.isArray(body) ? body : (body as { incidents?: unknown })?.incidents
  return Array.isArray(list) ? (list.filter((e) => e?.type === 'incident') as IncidentEvent[]) : []
}

/** Accepts a bare list or {results: [...]}; drops entries without the RunResult essentials. */
export function resultList(body: unknown): RunResult[] {
  const list = Array.isArray(body) ? body : (body as { results?: unknown })?.results
  if (!Array.isArray(list)) return []
  return list.filter((r) => r && typeof r.run_id === 'string' && typeof r.scenario === 'string' && r.victim_p99_ms) as RunResult[]
}

/** Accepts a bare list or {cases: [...]}; drops entries without case_id / incident_id. */
export function caseViewList(body: unknown): CaseView[] {
  const list = Array.isArray(body) ? body : (body as { cases?: unknown })?.cases
  if (!Array.isArray(list)) return []
  return list
    .filter((c) => c && typeof c.case_id === 'string' && typeof c.incident_id === 'string')
    .map((c) => ({ ...c, actions: Array.isArray(c.actions) ? c.actions : [], history: Array.isArray(c.history) ? c.history : [] })) as CaseView[]
}

export function parseBrainMessage(raw: string): BrainMessage | null {
  try {
    const m = JSON.parse(raw)
    if (m && (m.type === 'state' || m.type === 'incident')) return m as BrainMessage
  } catch {
    /* ignore malformed frames */
  }
  return null
}

export const realBackend: Backend = {
  kind: 'real',

  subscribe(onMessage, onConn) {
    let ws: WebSocket | null = null
    let stopped = false
    let retry = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const connect = () => {
      if (stopped) return
      onConn('connecting')
      ws = new WebSocket(wsUrl(config.brainUrl, '/ws/events'))
      ws.onopen = () => {
        retry = 0
        onConn('open')
      }
      ws.onmessage = (ev) => {
        const m = parseBrainMessage(typeof ev.data === 'string' ? ev.data : '')
        if (m) onMessage(m)
      }
      ws.onclose = () => {
        onConn('closed')
        if (stopped) return
        retry = Math.min(retry + 1, 5)
        timer = setTimeout(connect, 500 * 2 ** (retry - 1)) // 0.5 s .. 8 s
      }
      ws.onerror = () => ws?.close()
    }
    connect()
    return () => {
      stopped = true
      clearTimeout(timer)
      ws?.close()
    }
  },

  async incidents() {
    try {
      return incidentList(await http<unknown>(config.brainUrl, '/api/incidents'))
    } catch {
      return []
    }
  },

  async incident(id) {
    const list = incidentList(await http<unknown>(config.brainUrl, `/api/incidents?incident_id=${encodeURIComponent(id)}`))
    // older brains ignore the filter: filter here too
    return list.filter((e) => e.incident_id === id).sort((a, b) => a.ts - b.ts)
  },

  async results() {
    return resultList(await http<unknown>(config.labUrl, '/lab/results'))
  },

  async handoffCases() {
    return caseViewList(await http<unknown>(config.handoffUrl, '/cases', admin()))
  },

  async getMode() {
    return (await http<ModeBody>(config.gatewayUrl, '/admin/mode', admin())).mode
  },
  async setMode(mode) {
    const body = await http<Partial<ModeBody> | undefined>(config.gatewayUrl, '/admin/mode', admin(json('POST', { mode })))
    return body?.mode ?? mode
  },
  tenants: () => http<TenantAdminView[]>(config.gatewayUrl, '/admin/tenants', admin()),
  overrides: () => http<OverridesView>(config.gatewayUrl, '/admin/overrides', admin()),
  pool: () => http<PoolView>(config.brainUrl, '/api/pool'),
  setPool: (spec) => http<PoolView>(config.brainUrl, '/admin/pool', admin(json('PUT', spec))),
  createProject: (spec) => http<PoolView>(config.brainUrl, '/admin/projects', admin(json('POST', spec))),
  patchProject: (id, patch) => http<PoolView>(config.brainUrl, `/admin/projects/${encodeURIComponent(id)}`, admin(json('PATCH', patch))),
  deleteProject: (id) => http<PoolView>(config.brainUrl, `/admin/projects/${encodeURIComponent(id)}`, admin({ method: 'DELETE' })),
  addTenant: (project, req) =>
    http<TenantCreated>(config.brainUrl, `/admin/projects/${encodeURIComponent(project)}/tenants`, admin(json('POST', req))),
  removeTenant: async (id) => {
    await http<unknown>(config.brainUrl, `/admin/tenants/${encodeURIComponent(id)}`, admin({ method: 'DELETE' }))
  },
  labStatus: () => http<LabStatus>(config.labUrl, '/lab/status'),
  startAttack: (req) => http<AttackStatus>(config.labUrl, '/lab/attack', json('POST', req)),
  stopAttack: (id) => http<AttackStatus>(config.labUrl, `/lab/attack/${encodeURIComponent(id)}`, { method: 'DELETE' }),
}
