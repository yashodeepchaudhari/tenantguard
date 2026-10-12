// Live state assembled from the brain stream: latest snapshot, a rolling 60 s history for
// charts, and the incident log. Framework-free so it can be unit tested.
import type { BrainMessage, IncidentEvent, Mode, StateSnapshot } from '../types.ts'
import type { ConnState } from './backend.ts'

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

  constructor(historyS = 60) {
    this.historyS = historyS
    this.state = { conn: 'connecting', snapshot: null, receivedAt: null, history: [], incidents: [], pendingMode: null }
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
    this.set({ snapshot: s, receivedAt, history, pendingMode })
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
