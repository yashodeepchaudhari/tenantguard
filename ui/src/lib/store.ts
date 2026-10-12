// Live state assembled from the brain stream: latest snapshot, a rolling 60 s history for
// charts, and the incident log. Framework-free so it can be unit tested.
import { TENANTS } from '../mock/fixture.ts'
import type { BrainMessage, IncidentEvent, LabStatus, Mode, OverridesView, StateSnapshot, TenantAdminView } from '../types.ts'
import type { ConnState } from './backend.ts'
import { emptyImpact, updateImpact, type ImpactState } from './impact.ts'

/** Layout fallback (ag23) until gateway /admin/tenants answers. */
const LAYOUT_BARCODES: Record<string, string[]> = Object.fromEntries(TENANTS.map((t) => [t.id, t.servers]))
const LAYOUT_REVENUE: Record<string, number> = Object.fromEntries(TENANTS.map((t) => [t.id, t.monthly_revenue_rs]))

export interface HistoryPoint {
  ts: number
  /** tenant -> end-to-end p95 ms */
  p95: Record<string, number>
  /** tenant -> CPU share 0..1 */
  cpu: Record<string, number>
}

export interface LiveState {
  conn: ConnState
  snapshot: StateSnapshot | null
  /** Last time a snapshot arrived (client clock, epoch s). */
  receivedAt: number | null
  history: HistoryPoint[]
  incidents: IncidentEvent[]
  /** Optimistic mode while a POST /admin/mode is in flight. */
  pendingMode: Mode | null
  /** gateway /admin/tenants (polled); null until it answers */
  admin: TenantAdminView[] | null
  /** gateway /admin/overrides (polled); null while unreachable */
  overrides: OverridesView | null
  /** tenant -> barcode servers (admin, else layout) */
  barcodes: Record<string, string[]>
  /** tenant -> monthly_revenue_rs (admin, else layout) */
  revenue: Record<string, number>
  impact: ImpactState
  /** lab GET /lab/status (polled); null until it answers or while unreachable */
  lab: LabStatus | null
  /** null = not asked yet, false = lab did not answer */
  labReachable: boolean | null
}

export interface StoreOptions {
  historyS?: number
  sloMs?: number
  warnFallback?: number
}


const MAX_INCIDENTS = 300

const eventKey = (e: IncidentEvent) => `${e.incident_id}|${e.stage}|${e.ts}|${e.level ?? ''}`

export class LiveStore {
  private state: LiveState
  private listeners = new Set<() => void>()
  private seen = new Set<string>()
  /** arrival order, to keep same-timestamp events newest-first */
  private order = new WeakMap<IncidentEvent, number>()
  private seq = 0
  private historyS: number
  private sloMs: number
  private warnFallback: number

  constructor(opts: StoreOptions | number = {}) {
    const o = typeof opts === 'number' ? { historyS: opts } : opts
    this.historyS = o.historyS ?? 60
    this.sloMs = o.sloMs ?? 150
    this.warnFallback = o.warnFallback ?? 1.5
    this.state = {
      conn: 'connecting',
      snapshot: null,
      receivedAt: null,
      history: [],
      incidents: [],
      pendingMode: null,
      admin: null,
      overrides: null,
      barcodes: LAYOUT_BARCODES,
      revenue: LAYOUT_REVENUE,
      impact: emptyImpact(),
      lab: null,
      labReachable: null,
    }
  }

  setLab(lab: LabStatus | null) {
    this.set({ lab, labReachable: lab !== null })
  }

  setAdmin(admin: TenantAdminView[]) {
    if (!admin.length) return
    this.set({
      admin,
      barcodes: Object.fromEntries(admin.map((t) => [t.id, t.servers])),
      revenue: Object.fromEntries(admin.map((t) => [t.id, t.monthly_revenue_rs ?? 0])),
    })
  }

  setOverrides(overrides: OverridesView | null) {
    this.set({ overrides })
  }

  resetImpact() {
    this.set({ impact: emptyImpact() })
  }

  getState = (): LiveState => this.state

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  private set(patch: Partial<LiveState>) {
    this.state = { ...this.state, ...patch }
    for (const l of this.listeners) l()
  }

  setConn(conn: ConnState) {
    if (conn !== this.state.conn) this.set({ conn })
  }

  setPendingMode(mode: Mode | null) {
    this.set({ pendingMode: mode })
  }

  apply(m: BrainMessage, receivedAt = Date.now() / 1000) {
    if (m.type === 'state') this.applyState(m, receivedAt)
    else this.addIncidents([m])
  }

  private applyState(s: StateSnapshot, receivedAt: number) {
    const point: HistoryPoint = { ts: s.ts, p95: {}, cpu: {} }
    for (const t of s.tenants) {
      point.p95[t.tenant] = t.p95_ms
      point.cpu[t.tenant] = t.cpu_share
    }
    const cutoff = s.ts - this.historyS
    const prev = this.state.history
    // A clock jump backwards (brain restart / replay) resets the window.
    const base = prev.length && prev[prev.length - 1].ts > s.ts ? [] : prev
    const history = base.filter((p) => p.ts >= cutoff)
    history.push(point)
    const pendingMode = this.state.pendingMode && this.state.pendingMode === s.mode ? null : this.state.pendingMode
    const impact = updateImpact(this.state.impact, s, {
      barcodes: this.state.barcodes,
      revenue: this.state.revenue,
      warn: s.noisy_thresholds?.warn ?? this.warnFallback,
      sloMs: this.sloMs,
      suspected: new Set(this.state.incidents.map((e) => e.tenant).filter((t): t is string => !!t)),
    })
    this.set({ snapshot: s, receivedAt, history, pendingMode, impact })
  }

  addIncidents(events: IncidentEvent[]) {
    const fresh = events.filter((e) => {
      const k = eventKey(e)
      if (this.seen.has(k)) return false
      this.seen.add(k)
      this.order.set(e, ++this.seq)
      return true
    })
    if (!fresh.length) return
    const ord = (e: IncidentEvent) => this.order.get(e) ?? 0
    const incidents = [...this.state.incidents, ...fresh].sort((a, b) => b.ts - a.ts || ord(b) - ord(a)).slice(0, MAX_INCIDENTS)
    if (this.seen.size > MAX_INCIDENTS * 4) this.seen = new Set(incidents.map(eventKey))
    this.set({ incidents })
  }
}
