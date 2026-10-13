// A small, deterministic-ish simulation of the whole v2 platform (gateway + replicas + brain + lab)
// that produces contract-shaped messages. It is NOT the real control loop: it only has to look
// plausible so the UI can be built and demoed without a backend.
//   pool -> projects (floor, cap, tier) -> tenants; floors first, idle CPU lent by tier weight;
//   replicas scale with demand (starting -> ready, draining -> gone); a tenant only reaches its own
//   project's replicas; the ladder judges a tenant against its project peers.
//
// Pure TS with erasable syntax only, so Node can run it directly (scripts/mock-ws-server.mjs).
import type {
  AttackName,
  AttackReq,
  AttackStatus,
  IncidentEvent,
  LabStatus,
  Level,
  Mode,
  OverridesView,
  PoolSpec,
  PoolView,
  ProjectPatch,
  ProjectSpec,
  ProjectView,
  Quota,
  ReplicaView,
  ServerView,
  StateSnapshot,
  TenantAdminView,
  TenantCreate,
  TenantCreated,
  LabTenantJoin,
  LabTenantJoined,
  TenantView,
  Tier,
} from '../types.ts'
import { LEVELS } from '../types.ts'
import { NOISY, POOL, PROJECTS, replicaMemoryMb, TENANTS, TIERS, type FixtureTenant } from './fixture.ts'

const BASE_LAT_MS = 6
const CPU_PER_REQ_MS = 7
const BASE_RPS: Record<Tier, number> = { enterprise: 24, pro: 15, free: 8 }
const TTL_S = 60
const REPLICA_MAX_CPU = 1.0 // cores per replica (one Python process)
const TARGET_UTIL = 0.8
const START_S = 2 // a new replica answers its health check after this long
const DRAIN_S = 1
const DOWN_TICKS = 20 // ticks of lower demand before one replica is removed

interface AttackProfile {
  rps: (base: number) => number // extra requests/s at intensity 1
  cpu: number // CPU ms per request
  legit?: boolean
}

const PROFILES: Record<AttackName, AttackProfile> = {
  flood: { rps: (b) => b * 10, cpu: 2 },
  whale: { rps: () => 2, cpu: 400 },
  hog: { rps: () => 4, cpu: 150 },
  memory: { rps: () => 10, cpu: 30 },
  retry_storm: { rps: (b) => b * 8, cpu: 5 },
  background_bomb: { rps: () => 3, cpu: 250 },
  blackbox_bomb: { rps: () => 2, cpu: 350 },
  cache_flood: { rps: (b) => b * 4, cpu: 20 },
  faulty_server: { rps: () => 0, cpu: 0 },
  honest_spike: { rps: (b) => b * 2, cpu: CPU_PER_REQ_MS, legit: true },
}

export class SimError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

interface TState {
  id: string
  /** per-incident story for the Case File: when the incident opened, the peak, what was done */
  story: { openedAt: number; peakScore: number; peakFactor: number; actions: string[]; victimPeak: number } | null
  level: Level
  factor: number
  weightFactor: number
  blocked: boolean
  p95: number
  rps: number
  rejected: number
  cpuShare: number
  score: number
  aboveSince: number | null
  belowSince: number | null
  lastStep: number
  incident: string | null
  overrideExpires: number | null
}

interface SimReplica {
  id: string
  project: string
  state: 'starting' | 'ready' | 'draining'
  since: number
  nanoCpus: number
  borrowed: boolean
  memMb: number
  tenants: string[]
  load: number // CPU ms/s it got last tick
}

interface PState {
  demand: number // cores (EWMA)
  base: number
  loan: number
  used: number
  next: number // next replica index
  down: number // ticks with fewer replicas needed
}

// Small seeded PRNG so screenshots are stable-ish.
function mulberry32(seed: number) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const clamp = (x: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, x))

/** FNV-1a: stand-in for the gateway's rendezvous hash (stable per tenant and replica). */
function fnv(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193) >>> 0
  return h
}

/** Floors first, then the surplus by weight; a project never gets more than it wants. */
export function waterFill(surplus: number, wants: Record<string, number>, weights: Record<string, number>): Record<string, number> {
  const out: Record<string, number> = Object.fromEntries(Object.keys(wants).map((k) => [k, 0]))
  let active = Object.keys(wants).filter((k) => wants[k] > 1e-9)
  let left = surplus
  while (left > 1e-9 && active.length) {
    const tw = active.reduce((a, k) => a + weights[k], 0)
    const capped = active.filter((k) => wants[k] - out[k] <= (left * weights[k]) / tw + 1e-9)
    if (!capped.length) {
      for (const k of active) out[k] += (left * weights[k]) / tw
      return out
    }
    for (const k of capped) {
      left -= wants[k] - out[k]
      out[k] = wants[k]
    }
    active = active.filter((k) => !capped.includes(k))
  }
  return out
}

export class MockPlatform {
  mode: Mode = 'tenantguard'
  pool: PoolSpec = { ...POOL }
  projects: ProjectSpec[] = PROJECTS.map((p) => ({ ...p }))
  tenantDefs: FixtureTenant[] = TENANTS.map((t) => ({ ...t }))
  tenants: Map<string, TState> = new Map()
  replicas: SimReplica[] = []
  pstate: Map<string, PState> = new Map()
  attacks: AttackStatus[] = []
  serverViews: ServerView[] = []
  utilization = 0
  fairness = 1
  private rand: () => number
  private incSeq = 0
  private lastTick = 0
  private baseline: Map<string, number> = new Map()

  constructor(seed = 7) {
    this.rand = mulberry32(seed)
    for (const t of this.tenantDefs) this.tenants.set(t.id, this.freshTenant(t.id))
    for (const p of this.projects) this.pstate.set(p.id, { demand: 0, base: 0, loan: 0, used: 0, next: 1, down: 0 })
  }

  private freshTenant(id: string): TState {
    return {
      id,
      level: 'normal',
      factor: 1,
      weightFactor: 1,
      blocked: false,
      p95: 25,
      rps: 0,
      rejected: 0,
      cpuShare: 0,
      score: 0,
      aboveSince: null,
      belowSince: null,
      lastStep: 0,
      incident: null,
      overrideExpires: null,
      story: null,
    }
  }

  private project(id: string): ProjectSpec {
    const p = this.projects.find((x) => x.id === id)
    if (!p) throw new SimError(404, `unknown project ${id}`)
    return p
  }

  private tierOf(tenant: string): Tier {
    const t = this.tenantDefs.find((x) => x.id === tenant)
    return t ? this.project(t.project).tier : 'free'
  }

  private peers(id: string): string[] {
    const p = this.tenantDefs.find((t) => t.id === id)?.project
    return this.tenantDefs.filter((t) => t.id !== id && t.project === p).map((t) => t.id)
  }

  private usable(): { cpu: number; mem: number } {
    const keep = 1 - this.pool.headroom_ratio
    return { cpu: this.pool.cpu_total * keep, mem: Math.floor(this.pool.mem_total_mb * keep) }
  }

  // ---------------- brain admin API ----------------
  private check(pool: PoolSpec, projects: ProjectSpec[], tenants: FixtureTenant[]) {
    if (!(pool.cpu_total > 0 && pool.mem_total_mb > 0 && pool.headroom_ratio >= 0 && pool.headroom_ratio < 1)) throw new SimError(422, 'bad pool')
    const keep = 1 - pool.headroom_ratio
    for (const p of projects)
      if (!(p.floor_cpu > 0 && p.floor_cpu <= p.cap_cpu && p.cap_cpu <= pool.cpu_total)) throw new SimError(422, `project ${p.id}: need 0 < floor <= cap <= cpu_total`)
    const floors = projects.reduce((a, p) => a + p.floor_cpu, 0)
    if (floors > pool.cpu_total * keep + 1e-9) throw new SimError(422, `sum of floors ${floors.toFixed(2)} cores > usable ${(pool.cpu_total * keep).toFixed(3)} cores`)
    const own = (p: ProjectSpec) => Math.max(p.warm_min, Math.ceil(p.floor_cpu / (REPLICA_MAX_CPU * TARGET_UTIL) - 1e-9))
    const mem = projects.reduce((a, p) => a + own(p) * replicaMemoryMb(tenants.filter((t) => t.project === p.id).length), 0)
    if (mem > Math.floor(pool.mem_total_mb * keep)) throw new SimError(422, `reserved replica memory ${mem} MB > usable ${Math.floor(pool.mem_total_mb * keep)} MB`)
  }

  setPool(spec: PoolSpec): PoolView {
    this.check(spec, this.projects, this.tenantDefs)
    this.pool = { ...spec }
    return this.poolView()
  }

  createProject(spec: ProjectSpec): PoolView {
    if (!/^[a-z0-9-]{1,32}$/.test(spec.id)) throw new SimError(422, 'project id must match ^[a-z0-9-]{1,32}$')
    if (this.projects.some((p) => p.id === spec.id)) throw new SimError(409, `project ${spec.id} already exists`)
    const next = [...this.projects, { ...spec }]
    this.check(this.pool, next, this.tenantDefs)
    this.projects = next
    this.pstate.set(spec.id, { demand: 0, base: 0, loan: 0, used: 0, next: 1, down: 0 })
    return this.poolView()
  }

  patchProject(id: string, patch: ProjectPatch): PoolView {
    const cur = this.project(id)
    const next = this.projects.map((p) => (p.id === id ? { ...cur, ...Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined && v !== null)) } : p))
    this.check(this.pool, next, this.tenantDefs)
    this.projects = next
    return this.poolView()
  }

  deleteProject(id: string): PoolView {
    this.project(id)
    if (this.tenantDefs.some((t) => t.project === id)) throw new SimError(409, `project ${id} still has tenants`)
    this.projects = this.projects.filter((p) => p.id !== id)
    this.replicas = this.replicas.filter((r) => r.project !== id)
    this.pstate.delete(id)
    return this.poolView()
  }

  addTenant(project: string, req: TenantCreate): TenantCreated {
    this.project(project)
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/.test(req.id)) throw new SimError(422, 'bad tenant id')
    if (this.tenantDefs.some((t) => t.id.toLowerCase() === req.id.toLowerCase())) throw new SimError(409, `tenant ${req.id} already exists`)
    const def: FixtureTenant = { id: req.id, name: req.name, project, monthly_revenue_rs: req.monthly_revenue_rs ?? 0, protected: !!req.protected }
    this.check(this.pool, this.projects, [...this.tenantDefs, def])
    this.tenantDefs.push(def)
    this.tenants.set(def.id, this.freshTenant(def.id))
    return { tenant: def.id, project, api_key: `tk_${def.id}_${Math.floor(this.rand() * 1e9).toString(36)}` }
  }

  removeTenant(id: string): void {
    if (!this.tenants.has(id)) throw new SimError(404, `unknown tenant ${id}`)
    this.tenantDefs = this.tenantDefs.filter((t) => t.id !== id)
    this.tenants.delete(id)
  }

  /** The simulated lab already drives every tenant the sim knows; this only checks the tenant and reports a rate. */
  labAddTenant(j: LabTenantJoin): LabTenantJoined {
    if (!this.tenants.has(j.tenant)) throw new SimError(404, `unknown tenant ${j.tenant}`)
    return { tenant: j.tenant, rps: 1, effective_rps: 1, calm_running: true }
  }

  labRemoveTenant(id: string): void {
    if (this.tenants.has(id)) throw new SimError(409, `tenant ${id} still exists in the platform`)
  }

  // ---------------- gateway /admin ----------------
  setMode(mode: Mode) {
    if (!['off', 'industry', 'tenantguard'].includes(mode)) throw new SimError(422, `bad mode ${mode}`)
    this.mode = mode
    if (mode !== 'tenantguard') {
      // brain overrides only exist in tenantguard mode
      for (const s of this.tenants.values()) {
        s.factor = 1
        s.weightFactor = 1
        s.overrideExpires = null
        if (s.level !== 'normal' && s.level !== 'warn') s.level = 'warn'
      }
    }
    return { mode: this.mode }
  }

  tenantsAdmin(): TenantAdminView[] {
    return this.tenantDefs.map((t) => this.adminView(t.id))
  }

  private quota(id: string): Quota {
    return { ...TIERS[this.tierOf(id)].quota }
  }

  adminView(id: string): TenantAdminView {
    const t = this.tenantDefs.find((x) => x.id === id)
    const s = this.tenants.get(id)
    if (!t || !s) throw new SimError(404, `unknown tenant ${id}`)
    const f = s.factor
    const q = this.quota(id)
    const ready = this.replicas.filter((r) => r.project === t.project && r.state === 'ready').map((r) => r.id)
    return {
      id: t.id,
      name: t.name,
      project: t.project,
      tier: this.tierOf(id),
      replicas: ready.sort((a, b) => fnv(`${id}|${b}`) - fnv(`${id}|${a}`)),
      weight: 1,
      quota: q,
      effective_quota: { rate_rps: q.rate_rps * f, burst: Math.max(1, Math.round(q.burst * f)), concurrency: Math.max(1, Math.round(q.concurrency * f)) },
      factor: f,
      weight_factor: s.weightFactor,
      blocked: s.blocked,
      pinned_to: null,
      override_expires_at: s.overrideExpires,
      monthly_revenue_rs: t.monthly_revenue_rs,
      protected: t.protected,
    }
  }

  overrides(now: number): OverridesView {
    const out: OverridesView = { limits: {}, blocks: {}, pins: {}, expires_at: {}, snapshot_version: Math.floor(now), last_snapshot_age_s: 0.3, fail_static: false }
    for (const s of this.tenants.values()) {
      if (s.factor < 1 || s.weightFactor < 1) {
        out.limits[s.id] = { tenant: s.id, factor: s.factor, weight_factor: s.weightFactor, ttl_s: TTL_S, incident_id: s.incident }
        out.expires_at[`limits:${s.id}`] = s.overrideExpires ?? now + TTL_S
      }
    }
    return out
  }

  // ---------------- lab ----------------
  startAttack(req: AttackReq, now: number): AttackStatus {
    if (!PROFILES[req.attack]) throw new SimError(422, `unknown attack ${req.attack}`)
    const intensity = req.intensity ?? 1
    const duration = req.duration_s ?? 60
    if (!(intensity >= 0.1 && intensity <= 5)) throw new SimError(422, 'intensity must be 0.1..5')
    if (!(duration > 0 && duration <= 600)) throw new SimError(422, 'duration_s must be in (0, 600]')
    if (req.attack === 'faulty_server') {
      if (!req.server || !this.replicas.some((r) => r.id === req.server)) throw new SimError(422, 'faulty_server needs a replica id')
    } else if (!req.tenant || !this.tenants.has(req.tenant)) {
      throw new SimError(422, 'tenant required')
    }
    const a: AttackStatus = {
      attack_id: `atk-${Math.floor(now * 1000).toString(36)}-${this.attacks.length}`,
      attack: req.attack,
      tenant: req.attack === 'faulty_server' ? null : req.tenant,
      server: req.attack === 'faulty_server' ? req.server : null,
      intensity,
      state: 'running',
      started_at: now,
      ends_at: now + duration,
    }
    this.attacks.push(a)
    return { ...a }
  }

  stopAttack(id: string, now: number): AttackStatus {
    const a = this.attacks.find((x) => x.attack_id === id)
    if (!a) throw new SimError(404, `unknown attack ${id}`)
    if (a.state === 'running') {
      a.state = 'stopped'
      a.ends_at = now
    }
    return { ...a }
  }

  labStatus(now: number): LabStatus {
    this.expireAttacks(now)
    return { attacks: this.attacks.slice(-20).map((a) => ({ ...a })), calm_running: true, run: null }
  }

  private expireAttacks(now: number) {
    for (const a of this.attacks) if (a.state === 'running' && now >= a.ends_at) a.state = 'done'
  }

  // ---------------- control loop: allocation and replicas ----------------
  private allocate(demand: Map<string, number>) {
    const { cpu } = this.usable()
    const base: Record<string, number> = {}
    const wants: Record<string, number> = {}
    const weights: Record<string, number> = {}
    for (const p of this.projects) {
      const d = demand.get(p.id) ?? 0
      base[p.id] = this.mode === 'industry' ? p.floor_cpu : Math.min(d, p.floor_cpu)
      wants[p.id] = this.mode === 'tenantguard' ? Math.max(0, Math.min(d, p.cap_cpu) - base[p.id]) : 0
      weights[p.id] = TIERS[p.tier].weight
    }
    const surplus = Math.max(0, cpu - Object.values(base).reduce((a, b) => a + b, 0))
    const loan = waterFill(surplus, wants, weights)
    for (const p of this.projects) {
      const st = this.pstate.get(p.id)!
      st.base = base[p.id]
      st.loan = loan[p.id] ?? 0
    }
  }

  private scale(now: number) {
    for (const p of this.projects) {
      const st = this.pstate.get(p.id)!
      const alloc = st.base + st.loan
      const own = Math.max(p.warm_min, Math.ceil(p.floor_cpu / (REPLICA_MAX_CPU * TARGET_UTIL) - 1e-9))
      const usable = Math.min(st.demand, Math.max(alloc, p.floor_cpu))
      let need = this.mode === 'tenantguard' ? Math.ceil(usable / (REPLICA_MAX_CPU * TARGET_UTIL) - 1e-9) : own
      need = clamp(Math.max(need, Math.max(1, p.warm_min)), 1, Math.max(1, Math.ceil(p.cap_cpu / REPLICA_MAX_CPU)))
      const live = this.replicas.filter((r) => r.project === p.id && r.state !== 'draining')
      const tenants = this.tenantDefs.filter((t) => t.project === p.id).map((t) => t.id)
      const cores = this.mode === 'off' ? 0 : this.mode === 'industry' ? p.floor_cpu / need : Math.min(REPLICA_MAX_CPU, Math.max(alloc, p.floor_cpu))
      if (need > live.length) {
        st.down = 0
        for (let i = live.length; i < need; i++) {
          this.replicas.push({ id: `tg-${p.id}-${st.next++}`, project: p.id, state: 'starting', since: now, nanoCpus: Math.round(cores * 1e9), borrowed: i >= own, memMb: replicaMemoryMb(tenants.length), tenants, load: 0 })
        }
      } else if (need < live.length && ++st.down >= DOWN_TICKS) {
        st.down = 0
        const victim = [...live].sort((a, b) => Number(b.borrowed) - Number(a.borrowed) || b.since - a.since)[0]
        victim.state = 'draining'
        victim.since = now
      } else if (need >= live.length) st.down = 0
      for (const r of live) r.nanoCpus = Math.round(cores * 1e9)
    }
    for (const r of this.replicas) if (r.state === 'starting' && now - r.since >= START_S) r.state = 'ready'
    this.replicas = this.replicas.filter((r) => !(r.state === 'draining' && now - r.since >= DRAIN_S))
  }

  private route(tenant: string, project: string): SimReplica | null {
    const ready = this.replicas.filter((r) => r.project === project && r.state === 'ready')
    if (!ready.length) return null
    return ready.reduce((best, r) => (fnv(`${tenant}|${r.id}`) > fnv(`${tenant}|${best.id}`) ? r : best))
  }

  // ---------------- simulation step ----------------
  tick(now: number): { snapshot: StateSnapshot; incidents: IncidentEvent[] } {
    const dt = this.lastTick ? clamp(now - this.lastTick, 0.05, 2) : 0.5
    this.lastTick = now
    this.expireAttacks(now)
    const running = this.attacks.filter((a) => a.state === 'running')
    const enforce = this.mode !== 'off'
    const events: IncidentEvent[] = []

    // 1. demand per tenant, admitted by its tier quota x the ladder's factor
    interface Load {
      demandRps: number
      demandCpu: number
      rps: number
      cpu: number
      heavyCpuPerReq: number
      heavyCpu: number
      legit: boolean
    }
    const loads = new Map<string, Load>()
    for (const t of this.tenantDefs) {
      const s = this.tenants.get(t.id)!
      const tier = this.tierOf(t.id)
      const base = BASE_RPS[tier] * (0.92 + 0.16 * this.rand())
      let dRps = base
      let dCpu = base * CPU_PER_REQ_MS
      let heavyCpuPerReq = 0
      let heavyCpu = 0
      let legit = true
      for (const a of running) {
        if (a.tenant !== t.id) continue
        const p = PROFILES[a.attack]
        const r = p.rps(BASE_RPS[tier]) * a.intensity
        dRps += r
        dCpu += r * p.cpu
        if (p.cpu >= 50) {
          heavyCpuPerReq = Math.max(heavyCpuPerReq, p.cpu)
          heavyCpu += r * p.cpu
        }
        if (!p.legit) legit = false
      }
      // rate quota for everything; the concurrency cap (also x factor) is what holds back heavy requests
      const rateAdmit = enforce ? Math.min(1, (TIERS[tier].quota.rate_rps * s.factor) / Math.max(dRps, 1e-9)) : 1
      const heavyAdmit = enforce ? Math.min(rateAdmit, s.factor) : 1
      const admitBlocked = s.blocked ? 0 : 1
      const lightCpu = dCpu - heavyCpu
      loads.set(t.id, {
        demandRps: dRps,
        demandCpu: dCpu,
        rps: dRps * rateAdmit * admitBlocked,
        cpu: (lightCpu * rateAdmit + heavyCpu * heavyAdmit) * admitBlocked,
        heavyCpuPerReq,
        heavyCpu: heavyCpu * heavyAdmit * admitBlocked,
        legit,
      })
    }

    // 2. control loop: demand per project -> allocation -> replicas
    const demand = new Map<string, number>()
    for (const p of this.projects) {
      const st = this.pstate.get(p.id)!
      const raw = this.tenantDefs.filter((t) => t.project === p.id).reduce((a, t) => a + loads.get(t.id)!.demandCpu, 0) / 1000
      st.demand = clamp(0.5 * raw + 0.5 * st.demand, 0, p.cap_cpu)
      demand.set(p.id, st.demand)
    }
    this.allocate(demand)
    this.scale(now)

    // 3. per-replica load and latency (a tenant only reaches its own project's replicas)
    const faults = new Map<string, number>()
    for (const a of running) if (a.attack === 'faulty_server' && a.server) faults.set(a.server, 1 + 4 * a.intensity)
    const where = new Map<string, SimReplica | null>()
    for (const r of this.replicas) r.load = 0
    for (const t of this.tenantDefs) {
      const r = this.route(t.id, t.project)
      where.set(t.id, r)
      if (r) r.load += loads.get(t.id)!.cpu
    }
    const capMs = (r: SimReplica) => (r.nanoCpus ? r.nanoCpus / 1e6 : 1000)
    const latOn = (tenantId: string, r: SimReplica) => {
      const rho = clamp(r.load / capMs(r), 0, 0.97)
      const base = BASE_LAT_MS * (faults.get(r.id) ?? 1)
      let lat = (base / (1 - rho)) * (this.mode === 'off' ? 1.6 : 1.3)
      if (r.load > capMs(r)) lat += (r.load / capMs(r) - 1) * (this.mode === 'off' ? 1800 : 600)
      // head-of-line blocking behind heavy requests of project peers on the same replica
      for (const o of this.peers(tenantId)) {
        if (where.get(o) !== r) continue
        const l = loads.get(o)!
        if (l.heavyCpuPerReq < 50) continue
        const wf = this.tenants.get(o)!.weightFactor
        lat += (l.heavyCpuPerReq / 2) * clamp(l.heavyCpu / capMs(r) / 0.08, 0, 1) * (this.mode === 'off' ? 1.5 : wf)
      }
      return lat
    }

    // 4. tenant views
    const totalCpu = [...loads.values()].reduce((a, l) => a + l.cpu, 0)
    for (const t of this.tenantDefs) {
      const s = this.tenants.get(t.id)!
      const l = loads.get(t.id)!
      const r = where.get(t.id)
      if (!r) {
        s.p95 = 0
        s.rps = s.rps * 0.5
        s.rejected = s.rejected * 0.5 + l.demandRps * 0.5
        continue
      }
      const own = l.heavyCpuPerReq > 0 ? l.heavyCpuPerReq * 1.2 : CPU_PER_REQ_MS * 1.5
      const p95 = clamp((latOn(t.id, r) + own) * (0.94 + 0.12 * this.rand()), 1, 8000)
      s.p95 = s.p95 > 0 ? s.p95 * 0.55 + p95 * 0.45 : p95
      s.rps = s.rps * 0.5 + l.rps * 0.5
      s.rejected = s.rejected * 0.5 + (l.demandRps - l.rps) * 0.5
      s.cpuShare = totalCpu > 0 ? l.cpu / totalCpu : 0
      if (!this.baseline.has(t.id) || (running.length === 0 && s.level === 'normal')) {
        const b = this.baseline.get(t.id) ?? s.p95
        this.baseline.set(t.id, b * 0.9 + s.p95 * 0.1)
      }
    }

    // 5. noisy score inside each project + ladder (the "brain")
    for (const t of this.tenantDefs) {
      const s = this.tenants.get(t.id)!
      const l = loads.get(t.id)!
      const team = [t.id, ...this.peers(t.id)]
      const projDemand = team.reduce((a, id) => a + loads.get(id)!.demandCpu, 0)
      const usage = projDemand > 0 ? (l.demandCpu / projDemand) * team.length : 0
      let impact = 0
      const others = this.peers(t.id)
      for (const o of others) {
        const so = this.tenants.get(o)!
        if (so.p95 > 0) impact += clamp(so.p95 / (this.baseline.get(o) ?? so.p95) - 1, 0, 3)
      }
      impact = others.length ? impact / others.length : 0
      if (usage < NOISY.warn) impact = Math.min(impact, 0.2)
      s.score = s.score * 0.6 + usage * (1 + impact) * 0.4
      // flag only a tenant that itself over-uses AND whose project peers are hurt
      const hurting = usage >= NOISY.warn && (impact > 0.15 || !l.legit)
      this.ladder(t.id, s, now, hurting, usage < NOISY.warn, events, dt)
    }

    // 6. replicas, pool
    this.serverViews = this.replicas.map((r) => {
      const peers = this.replicas.filter((x) => x.project === r.project && x.id !== r.id && x.state === 'ready')
      const p95 = (BASE_LAT_MS * (faults.get(r.id) ?? 1)) / (1 - clamp(r.load / capMs(r), 0, 0.97))
      const peerP95 = peers.length ? peers.map((x) => BASE_LAT_MS / (1 - clamp(x.load / capMs(x), 0, 0.97))).sort((a, b) => a - b)[Math.floor(peers.length / 2)] : p95
      const ratio = peerP95 > 0 ? p95 / peerP95 : 1
      const status: ServerView['status'] = r.state !== 'ready' ? 'drained' : faults.has(r.id) || ratio >= 1.6 ? 'slow' : 'healthy'
      return { server: r.id, project: r.project, p95_ms: p95, peer_ratio: ratio, cpu_pct: clamp((r.load / capMs(r)) * 100, 0, 100), status }
    })
    for (const p of this.projects) {
      this.pstate.get(p.id)!.used = this.replicas.filter((r) => r.project === p.id).reduce((a, r) => a + Math.min(r.load, capMs(r)), 0) / 1000
    }
    this.utilization = clamp([...this.pstate.values()].reduce((a, st) => a + st.used, 0) / this.pool.cpu_total, 0, 1)
    const xs = this.tenantDefs.map((t) => this.tenants.get(t.id)!.rps)
    const sum = xs.reduce((a, b) => a + b, 0)
    const sq = xs.reduce((a, b) => a + b * b, 0)
    this.fairness = sq > 0 ? (sum * sum) / (xs.length * sq) : 1

    return { snapshot: this.snapshot(now), incidents: events }
  }

  private ladder(id: string, s: TState, now: number, hurting: boolean, calm: boolean, events: IncidentEvent[], dt: number) {
    const name = this.tenantDefs.find((t) => t.id === id)!.name
    const act = this.mode === 'tenantguard'
    const set = (level: Level, detail: string, action: string | null) => {
      const prev = s.level
      if (prev === level) return
      s.level = level
      s.lastStep = now
      if (!s.incident) {
        s.incident = `inc-${id}-${++this.incSeq}`
        events.push(this.ev(s, 'detected', now, { detail: `${name} (${id}) uses ${s.score.toFixed(1)}x its fair share of its project while its project peers slow down.` }))
      }
      events.push(this.ev(s, 'level_change', now, { level, prev_level: prev, action, detail }))
      this.storyStep(s, level, prev, action, now, events, name)
    }
    const held = (since: number | null, hold: number) => since !== null && now - since >= hold

    if (s.score >= NOISY.warn && hurting) {
      s.aboveSince ??= now
      s.belowSince = null
    } else if (s.score < NOISY.recover_below || calm) {
      s.belowSince ??= now
      s.aboveSince = null
    } else {
      s.aboveSince = null
      s.belowSince = null
    }

    const score = s.score.toFixed(1)
    if (s.aboveSince !== null) {
      if (s.level === 'normal' && held(s.aboveSince, NOISY.hold_s.warn)) {
        set('warn', `Noisy score ${score} >= warn ${NOISY.warn}; ${act ? 'watching' : `observe only (mode ${this.mode})`}.`, null)
      } else if (!act) {
        // off / industry: detect only
      } else if (s.level === 'warn' && s.score >= NOISY.throttle && now - s.lastStep >= NOISY.hold_s.throttle) {
        s.factor = NOISY.throttle_factor
        s.overrideExpires = now + TTL_S
        set('throttle', `Score ${score} >= throttle ${NOISY.throttle} for ${NOISY.hold_s.throttle} s: quota x${s.factor}.`, `LimitsCmd factor=${s.factor}`)
      } else if (s.level === 'throttle' && s.score >= NOISY.throttle && now - s.lastStep >= NOISY.hold_s.throttle) {
        s.factor = Math.max(0.1, s.factor * NOISY.throttle_factor)
        s.overrideExpires = now + TTL_S
        s.lastStep = now
        if (s.factor <= 0.25) {
          s.weightFactor = 0.25
          set('deprioritize', `Still ${score} after halving: quota x${s.factor}, scheduler weight x0.25.`, `LimitsCmd factor=${s.factor} weight_factor=0.25`)
        } else {
          events.push(this.ev(s, 'level_change', now, { level: 'throttle', prev_level: 'throttle', action: `LimitsCmd factor=${s.factor}`, detail: `Score ${score} still high: quota halved again to x${s.factor}.` }))
        }
      } else if (s.level === 'deprioritize' && s.score >= NOISY.quarantine && now - s.lastStep >= NOISY.hold_s.quarantine) {
        s.factor = 0.1
        s.weightFactor = 0.1
        s.overrideExpires = now + TTL_S
        set('quarantine', `Score ${score} >= quarantine ${NOISY.quarantine} for ${NOISY.hold_s.quarantine} s: quota x0.1, weight x0.1.`, 'LimitsCmd factor=0.1 weight_factor=0.1')
      } else if (s.factor < 1) {
        s.overrideExpires = now + TTL_S // the snapshot renews the TTL while needed
      }
    } else if (s.belowSince !== null && s.level !== 'normal') {
      if (!act || s.factor >= 1) {
        if (held(s.belowSince, NOISY.hold_s.recover / 2)) this.recover(s, now, events, name)
      } else if (held(s.belowSince, NOISY.hold_s.recover) && now - s.lastStep >= 1) {
        s.factor = Math.min(1, s.factor + NOISY.recover_step * Math.max(1, dt))
        s.lastStep = now
        const next: Level = s.factor >= 1 ? 'warn' : s.factor >= 0.5 ? 'throttle' : s.factor >= 0.25 ? 'deprioritize' : 'quarantine'
        if (s.factor >= 0.25) s.weightFactor = 1
        if (next !== s.level) {
          set(next, `Usage back under its fair share for ${NOISY.hold_s.recover} s: recovering, quota x${s.factor.toFixed(1)}.`, `LimitsCmd factor=${s.factor.toFixed(1)}`)
        }
        if (s.factor >= 1) this.recover(s, now, events, name)
      }
    }
  }

  private recover(s: TState, now: number, events: IncidentEvent[], name: string) {
    const prev = s.level
    s.level = 'normal'
    s.factor = 1
    s.weightFactor = 1
    s.overrideExpires = null
    s.lastStep = now
    events.push(this.ev(s, 'level_change', now, { level: 'normal', prev_level: prev, action: 'override cleared', detail: `${name} back under its fair share; override cleared.` }))
    events.push(this.ev(s, 'resolved', now, { detail: `The other tenants of ${name}'s project are back to their baseline p95.` }))
    const st = s.story
    if (st && st.actions.length) {
      const project = this.tenantDefs.find((t) => t.id === s.id)?.project ?? '?'
      const victims = this.peers(s.id)
      events.push(
        this.ev(s, 'explained', now, {
          verdict: 'costly_pattern',
          detail: `${name} sent a few very expensive requests that took most of its project's CPU; throttling it restored its project peers.`,
          report: {
            verdict: 'costly_pattern',
            attacker: s.id,
            project,
            confidence: Math.min(1, st.peakScore / NOISY.quarantine),
            duration_s: Math.round(now - st.openedAt),
            summary: `${name} (${s.id}) ran heavy requests that used up to ${st.peakScore.toFixed(1)}x its fair share of project ${project}. ${victims.length} project peers slowed down (worst p95 ${Math.round(st.victimPeak)} ms). TenantGuard throttled ${s.id} step by step down to ${Math.round(st.peakFactor * 100)} % of its plan, its peers recovered, and the quota was restored once ${s.id} calmed down. Other projects were never affected: each runs on its own replicas with its own CPU floor.`,
            actions: st.actions,
            evidence: {
              project,
              peers_hurt: victims.join(' '),
              peak_noisy_score: st.peakScore,
              lowest_quota_factor: st.peakFactor,
              worst_peer_p95_ms: Math.round(st.victimPeak),
            },
          },
        }),
      )
    }
    s.story = null
    s.incident = null
  }

  /** Case File stages as the ladder climbs: named -> proving -> verdict. */
  private storyStep(s: TState, level: Level, prev: Level, action: string | null, now: number, events: IncidentEvent[], name: string) {
    if (prev === 'normal' && level === 'warn') {
      s.story = { openedAt: now, peakScore: s.score, peakFactor: 1, actions: [], victimPeak: 0 }
    }
    const st = s.story
    if (!st) return
    st.peakScore = Math.max(st.peakScore, s.score)
    st.peakFactor = Math.min(st.peakFactor, s.factor)
    for (const v of this.peers(s.id)) st.victimPeak = Math.max(st.victimPeak, this.tenants.get(v)!.p95)
    if (action && level !== 'normal') st.actions.push(`${level}: ${action}`)
    const rank = (l: Level) => LEVELS.indexOf(l)
    if (this.mode !== 'tenantguard' || rank(level) <= rank(prev)) return // story stages only while escalating
    const conf = clamp(s.score / NOISY.quarantine, 0.5, 0.97)
    const project = this.tenantDefs.find((t) => t.id === s.id)?.project ?? '?'
    if (level === 'throttle' && prev === 'warn') {
      events.push(this.ev(s, 'named', now, { suspects: [{ tenant: s.id, confidence: conf }], detail: `${name} named: it holds most of project ${project}'s CPU while its peers slow down.` }))
    } else if (level === 'deprioritize') {
      events.push(this.ev(s, 'proving', now, { suspects: [{ tenant: s.id, confidence: conf }], detail: `Proving: halving ${s.id}'s quota cut its project peers' p95; the effect follows ${s.id}.` }))
    } else if (level === 'quarantine') {
      events.push(this.ev(s, 'verdict', now, { verdict: 'costly_pattern', action, suspects: [{ tenant: s.id, confidence: Math.max(conf, 0.9) }], detail: `Verdict costly_pattern: ${s.id} sends requests far heavier than its plan; quarantined until it calms down.` }))
    }
  }

  private ev(s: TState, stage: IncidentEvent['stage'], now: number, extra: Partial<IncidentEvent>): IncidentEvent {
    return {
      type: 'incident',
      incident_id: s.incident ?? `inc-${s.id}-${this.incSeq}`,
      stage,
      ts: now,
      tenant: s.id,
      level: null,
      prev_level: null,
      suspects: [{ tenant: s.id, confidence: clamp(s.score / NOISY.quarantine, 0, 1) }],
      verdict: null,
      action: null,
      detail: '',
      report: null,
      ...extra,
    }
  }

  poolView(): PoolView {
    const { cpu, mem } = this.usable()
    const projects: ProjectView[] = this.projects.map((p) => {
      const st = this.pstate.get(p.id)!
      const reps = this.replicas.filter((r) => r.project === p.id)
      const view = (r: SimReplica): ReplicaView => ({
        id: r.id,
        project: r.project,
        url: `http://${r.id}:8100`,
        state: r.state,
        borrowed: r.borrowed,
        nano_cpus: r.nanoCpus,
        cpu_shares: Math.round((10240 * p.floor_cpu) / Math.max(1, reps.length)),
        mem_mb: r.memMb,
        started_at: r.since,
        tenants: r.tenants,
      })
      const live = reps.filter((r) => r.state !== 'draining').length
      return {
        id: p.id,
        name: p.name,
        tier: p.tier,
        weight: TIERS[p.tier].weight,
        floor_cpu: p.floor_cpu,
        cap_cpu: p.cap_cpu,
        warm_min: p.warm_min,
        tenants: this.tenantDefs.filter((t) => t.project === p.id).map((t) => t.id),
        demand_cpu: st.demand,
        used_cpu: st.used,
        base_cpu: st.base,
        loan_cpu: st.loan,
        alloc_cpu: st.base + st.loan,
        state: live === 0 ? 'asleep' : st.demand > 0.5 ? 'busy' : 'warm',
        replicas: reps.map(view),
      }
    })
    return {
      pool: { ...this.pool },
      usable_cpu: cpu,
      usable_mem_mb: mem,
      floors_cpu: this.projects.reduce((a, p) => a + p.floor_cpu, 0),
      reserved_mem_mb: this.replicas.reduce((a, r) => a + r.memMb, 0),
      projects,
    }
  }

  snapshot(now: number): StateSnapshot {
    const tenants: TenantView[] = this.tenantDefs.map((t) => {
      const s = this.tenants.get(t.id)!
      return {
        tenant: t.id,
        name: t.name,
        tier: this.tierOf(t.id),
        p95_ms: s.p95,
        rps: s.rps,
        rejected_rps: Math.max(0, s.rejected),
        cpu_share: s.cpuShare,
        noisy_score: s.score,
        noisy_norm: clamp(s.score / NOISY.quarantine, 0, 1),
        level: s.level,
        factor: s.factor,
        project: t.project,
      }
    })
    return {
      type: 'state',
      ts: now,
      mode: this.mode,
      tenants,
      servers: this.serverViews,
      utilization: this.utilization,
      fairness: this.fairness,
      status_sentence: this.sentence(tenants),
      brain_ok: true,
      noisy_thresholds: { warn: NOISY.warn, throttle: NOISY.throttle, quarantine: NOISY.quarantine },
      pool: this.poolView(),
    }
  }

  private sentence(tenants: TenantView[]): string {
    const flagged = tenants.filter((t) => t.level !== 'normal').sort((a, b) => b.noisy_score - a.noisy_score)
    const baseline = (t: TenantView) => this.baseline.get(t.tenant) ?? t.p95_ms
    const hurt = tenants.filter((t) => t.level === 'normal' && t.p95_ms > 2 * baseline(t))
    const util = Math.round(this.utilization * 100)
    if (flagged.length === 0 && hurt.length === 0) {
      return `All ${tenants.length} tenants healthy · pool CPU ${util}% · fairness ${this.fairness.toFixed(2)}`
    }
    const n = flagged[0]
    const who = n ? `${n.name} (${n.tenant})` : 'An unidentified tenant'
    const hurtTxt = hurt.length ? `${hurt.length} project peer${hurt.length > 1 ? 's' : ''} slowed` : 'project peers protected'
    if (this.mode === 'off') return `Defences OFF: ${who} is starving its project peers · ${hurtTxt}`
    if (!n) return `${hurtTxt} · looking for the cause`
    if (this.mode === 'industry') return `${who} is noisy (${n.noisy_score.toFixed(1)}x fair share) · static quotas only · ${hurtTxt}`
    const what =
      n.level === 'warn' ? 'under watch' : n.level === 'normal' ? 'recovered' : `${n.level === 'throttle' ? 'throttled' : n.level === 'deprioritize' ? 'deprioritized' : n.level} to ${Math.round(n.factor * 100)}% of plan`
    return `${who} is noisy (${n.noisy_score.toFixed(1)}x fair share of project ${n.project}) · ${what} · ${hurtTxt}`
  }
}
