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
  QuotaPatch,
  TenantAdminView,
} from '../types.ts'

export type ConnState = 'connecting' | 'open' | 'closed'

export interface Backend {
  readonly kind: 'real' | 'mock'
  /** Subscribe to the brain event stream. Returns an unsubscribe function. */
  subscribe(onMessage: (m: BrainMessage) => void, onConn: (s: ConnState) => void): () => void
  /** Recent incidents for backfill when the page loads (may be empty). */
  incidents(): Promise<IncidentEvent[]>
  getMode(): Promise<Mode>
  setMode(mode: Mode): Promise<Mode>
  tenants(): Promise<TenantAdminView[]>
  patchQuota(id: string, patch: QuotaPatch): Promise<TenantAdminView>
  overrides(): Promise<OverridesView>
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
          const loc = Array.isArray(x?.loc) ? x.loc.filter((p: unknown) => p !== 'body').join('.') : ''
          return loc ? `${loc}: ${x?.msg ?? ''}` : String(x?.msg ?? '')
        })
        .join('; ')
    }
  }
  return fallback
}

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
      const body = await http<unknown>(config.brainUrl, '/api/incidents')
      const list = Array.isArray(body) ? body : (body as { incidents?: unknown })?.incidents
      return Array.isArray(list) ? (list.filter((e) => e?.type === 'incident') as IncidentEvent[]) : []
    } catch {
      return []
    }
  },

  async getMode() {
    return (await http<ModeBody>(config.gatewayUrl, '/admin/mode')).mode
  },
  async setMode(mode) {
    const body = await http<Partial<ModeBody> | undefined>(config.gatewayUrl, '/admin/mode', json('POST', { mode }))
    return body?.mode ?? mode
  },
  tenants: () => http<TenantAdminView[]>(config.gatewayUrl, '/admin/tenants'),
  patchQuota: (id, patch) =>
    http<TenantAdminView>(config.gatewayUrl, `/admin/tenants/${encodeURIComponent(id)}/quota`, json('PATCH', patch)),
  overrides: () => http<OverridesView>(config.gatewayUrl, '/admin/overrides'),
  labStatus: () => http<LabStatus>(config.labUrl, '/lab/status'),
  startAttack: (req) => http<AttackStatus>(config.labUrl, '/lab/attack', json('POST', req)),
  stopAttack: (id) => http<AttackStatus>(config.labUrl, `/lab/attack/${encodeURIComponent(id)}`, { method: 'DELETE' }),
}
