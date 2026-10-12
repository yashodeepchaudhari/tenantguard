// A small, deterministic-ish simulation of the whole platform (gateway + workers + brain + lab)
// that produces contract-shaped messages. It is NOT the real detector: it only has to look
// plausible so the UI can be built and demoed without a backend.
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
  Quota,
  QuotaPatch,
  ServerView,
  StateSnapshot,
  TenantAdminView,
  TenantView,
} from '../types.ts'
import { LEVELS } from '../types.ts'
import { NOISY, SERVERS, TENANTS, TIERS } from './fixture.ts'

const SERVER_CAP_MS = 500 // 0.5 core = 500 CPU-ms per second
const BASE_LAT_MS = 6
const CPU_PER_REQ_MS = 7
const BASE_RPS = { enterprise: 24, pro: 15, free: 8 } as const
const TTL_S = 60

interface AttackProfile {
  rps: (base: number) => number // extra requests/s at intensity 1
  cpu: number // CPU ms per request
  bypassCostBucket?: boolean // black-box CPU the gateway cannot attribute
  legit?: boolean
}

const PROFILES: Record<AttackName, AttackProfile> = {
  flood: { rps: (b) => b * 10, cpu: 2 },
  whale: { rps: () => 2, cpu: 400 },
  hog: { rps: () => 4, cpu: 150 },
  memory: { rps: () => 10, cpu: 30 },
  retry_storm: { rps: (b) => b * 8, cpu: 5 },
  background_bomb: { rps: () => 3, cpu: 250 },
  blackbox_bomb: { rps: () => 2, cpu: 350, bypassCostBucket: true },
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
  quota: Quota
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

export class MockPlatform {
  mode: Mode = 'tenantguard'
  tenants: Map<string, TState> = new Map()
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
    for (const t of TENANTS) {
      this.tenants.set(t.id, {
        id: t.id,
        quota: { ...TIERS[t.tier].quota },
        level: 'normal',
        factor: 1,
        weightFactor: 1,
        blocked: false,
        p95: 25,
        rps: BASE_RPS[t.tier],
        rejected: 0,
        cpuShare: 0,
        score: 0,
        aboveSince: null,
        belowSince: null,
        lastStep: 0,
        incident: null,
        overrideExpires: null,
        story: null,
      })
    }
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
    return TENANTS.map((t) => this.adminView(t.id))
  }

  adminView(id: string): TenantAdminView {
    const t = TENANTS.find((x) => x.id === id)
    const s = this.tenants.get(id)
    if (!t || !s) throw new SimError(404, `unknown tenant ${id}`)
    const f = s.factor
    const q = s.quota
    return {
      id: t.id,
      name: t.name,
      tier: t.tier,
      servers: [...t.servers],
      weight: TIERS[t.tier].weight,
      quota: { ...q },
      effective_quota: {
        rate_rps: q.rate_rps * f,
        burst: Math.max(1, Math.round(q.burst * f)),
        cost_ms_per_s: q.cost_ms_per_s * f,
        concurrency: Math.max(1, Math.round(q.concurrency * f)),
      },
      factor: f,
      weight_factor: s.weightFactor,
      blocked: s.blocked,
      placement: 'normal',
      override_expires_at: s.overrideExpires,
      monthly_revenue_rs: t.monthly_revenue_rs,
      protected: t.protected,
    }
  }

  patchQuota(id: string, patch: QuotaPatch): TenantAdminView {
    const s = this.tenants.get(id)
    if (!s) throw new SimError(404, `unknown tenant ${id}`)
    for (const k of ['rate_rps', 'burst', 'cost_ms_per_s', 'concurrency'] as const) {
      const v = patch[k]
      if (v === undefined || v === null) continue
      if (typeof v !== 'number' || !Number.isFinite(v) || v <= 0) throw new SimError(422, `${k} must be > 0`)
      s.quota[k] = k === 'burst' || k === 'concurrency' ? Math.round(v) : v
    }
    return this.adminView(id)
  }

  overrides(now: number): OverridesView {
    const out: OverridesView = {
      limits: {},
      blocks: {},
      relocations: [],
      route_weights: {},
      drains: {},
      expires_at: {},
      last_heartbeat_age_s: 0.3,
      fail_static: false,
    }
    // rescue (like the brain): in tenantguard mode route tenants away from slow servers
    if (this.mode === 'tenantguard') {
      const slow = new Set(this.serverViews.filter((v) => v.status === 'slow').map((v) => v.server))
      for (const t of TENANTS) {
        const st = this.tenants.get(t.id)!
        if (st.level !== 'normal' || !t.servers.some((sv) => slow.has(sv)) || t.servers.every((sv) => slow.has(sv))) continue
        out.route_weights[t.id] = Object.fromEntries(t.servers.map((sv) => [sv, slow.has(sv) ? 0 : 1]))
        out.expires_at[`route_weights:${t.id}`] = now + 120
      }
    }
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
      if (!req.server || !SERVERS.includes(req.server)) throw new SimError(422, 'faulty_server needs server w0..w8')
    } else if (!req.tenant || !this.tenants.has(req.tenant)) {
      throw new SimError(422, 'tenant required (A..L)')
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

  // ---------------- simulation step ----------------
  tick(now: number): { snapshot: StateSnapshot; incidents: IncidentEvent[] } {
    const dt = this.lastTick ? clamp(now - this.lastTick, 0.05, 2) : 0.5
    this.lastTick = now
    this.expireAttacks(now)
    const running = this.attacks.filter((a) => a.state === 'running')
    const enforce = this.mode !== 'off'
    const events: IncidentEvent[] = []

    // 1. demand per tenant
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
    for (const t of TENANTS) {
      const s = this.tenants.get(t.id)!
      const base = BASE_RPS[t.tier] * (0.92 + 0.16 * this.rand())
      let dRps = base
      let dCpu = base * CPU_PER_REQ_MS
      let heavyCpuPerReq = 0
      let heavyCpu = 0
      let bypass = 0
      let legit = true
      for (const a of running) {
        if (a.tenant !== t.id) continue
        const p = PROFILES[a.attack]
        const r = p.rps(BASE_RPS[t.tier]) * a.intensity
        dRps += r
        const c = r * p.cpu
        if (p.bypassCostBucket) bypass += c
        else dCpu += c
        if (p.cpu >= 50) {
          heavyCpuPerReq = Math.max(heavyCpuPerReq, p.cpu)
          heavyCpu += c
        }
        if (!p.legit) legit = false
      }
      let admit = 1
      if (enforce) {
        const capCpu = s.quota.cost_ms_per_s * s.factor
        const capRps = s.quota.rate_rps * s.factor
        admit = Math.min(1, capCpu / Math.max(dCpu, 1e-9), capRps / Math.max(dRps, 1e-9))
      }
      if (s.blocked) admit = 0
      loads.set(t.id, {
        demandRps: dRps,
        demandCpu: dCpu + bypass,
        rps: dRps * admit,
        cpu: dCpu * admit + bypass,
        heavyCpuPerReq,
        heavyCpu: heavyCpu * admit,
        legit,
      })
    }

    // 2. per-server load and latency
    const faults = new Map<string, number>()
    for (const a of running) if (a.attack === 'faulty_server' && a.server) faults.set(a.server, 1 + 4 * a.intensity)
    const serverLoad = new Map<string, number>(SERVERS.map((sv) => [sv, 0]))
    for (const t of TENANTS) {
      const l = loads.get(t.id)!
      for (const sv of t.servers) serverLoad.set(sv, serverLoad.get(sv)! + l.cpu / t.servers.length)
    }
    const latOn = (tenantId: string, sv: string) => {
      const others = TENANTS.filter((x) => x.id !== tenantId && x.servers.includes(sv))
      const load = serverLoad.get(sv)!
      const rho = clamp(load / SERVER_CAP_MS, 0, 0.97)
      const base = BASE_LAT_MS * (faults.get(sv) ?? 1)
      let lat = (base / (1 - rho)) * (this.mode === 'off' ? 1.6 : 1.3)
      if (load > SERVER_CAP_MS) lat += (load / SERVER_CAP_MS - 1) * (this.mode === 'off' ? 1800 : 600)
      // head-of-line blocking behind co-located heavy requests
      for (const o of others) {
        const l = loads.get(o.id)!
        if (l.heavyCpuPerReq < 50) continue
        const occ = l.heavyCpu / o.servers.length / SERVER_CAP_MS
        const wf = this.tenants.get(o.id)!.weightFactor
        lat += (l.heavyCpuPerReq / 2) * clamp(occ / 0.08, 0, 1) * (this.mode === 'off' ? 1.5 : wf)
      }
      return lat
    }

    // 3. tenant views
    const totalCpu = [...loads.values()].reduce((a, l) => a + l.cpu, 0)
    const totalDemand = [...loads.values()].reduce((a, l) => a + l.demandCpu, 0)
    const totalWeight = TENANTS.reduce((a, t) => a + TIERS[t.tier].weight, 0)
    for (const t of TENANTS) {
      const s = this.tenants.get(t.id)!
      const l = loads.get(t.id)!
      const worst = Math.max(...t.servers.map((sv) => latOn(t.id, sv)))
      const own = l.heavyCpuPerReq > 0 ? l.heavyCpuPerReq * 1.2 : CPU_PER_REQ_MS * 1.5
      let p95 = worst + own
      p95 = clamp(p95 * (0.94 + 0.12 * this.rand()), 1, 8000)
      s.p95 = s.p95 * 0.55 + p95 * 0.45
      s.rps = s.rps * 0.5 + l.rps * 0.5
      s.rejected = s.rejected * 0.5 + (l.demandRps - l.rps) * 0.5
      s.cpuShare = totalCpu > 0 ? l.cpu / totalCpu : 0
      if (!this.baseline.has(t.id) || (running.length === 0 && s.level === 'normal')) {
        const b = this.baseline.get(t.id) ?? s.p95
        this.baseline.set(t.id, b * 0.9 + s.p95 * 0.1)
      }
    }

    // 4. noisy score + ladder (the "brain")
    for (const t of TENANTS) {
      const s = this.tenants.get(t.id)!
      const l = loads.get(t.id)!
      const fair = TIERS[t.tier].weight / totalWeight
      const share = totalDemand > 0 ? l.demandCpu / totalDemand : 0
      const usage = share / fair
      let impact = 0
      for (const o of TENANTS) {
        if (o.id === t.id) continue
        const so = this.tenants.get(o.id)!
        impact += clamp(so.p95 / (this.baseline.get(o.id) ?? so.p95) - 1, 0, 3)
      }
      impact /= TENANTS.length - 1
      if (usage < NOISY.warn) impact = Math.min(impact, 0.2)
      const score = usage * (1 + impact)
      s.score = s.score * 0.6 + score * 0.4
      // flag only a tenant that itself over-uses AND whose neighbours are hurt
      const hurting = usage >= NOISY.warn && (impact > 0.15 || !l.legit)
      this.ladder(t.id, s, now, hurting, usage < NOISY.warn, events, dt)
    }

    // 5. servers, fleet
    const sp95 = new Map(
      SERVERS.map((sv) => {
        const load = serverLoad.get(sv)!
        const others = TENANTS.filter((t) => t.servers.includes(sv)).map((t) => loads.get(t.id)!)
        const heavy = Math.max(0, ...others.map((l) => (l.heavyCpuPerReq >= 50 ? clamp(l.heavyCpu / 3 / SERVER_CAP_MS / 0.08, 0, 1) * l.heavyCpuPerReq * 0.5 : 0)))
        return [sv, (BASE_LAT_MS * (faults.get(sv) ?? 1)) / (1 - clamp(load / SERVER_CAP_MS, 0, 0.97)) + heavy]
      }),
    )
    const med = (xs: number[]) => {
      const v = [...xs].sort((x, y) => x - y)
      return v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2
    }
    this.serverViews = SERVERS.map((sv) => {
      const load = serverLoad.get(sv)!
      const p95 = sp95.get(sv)!
      const peer = med(SERVERS.filter((x) => x !== sv).map((x) => sp95.get(x)!))
      const ratio = peer > 0 ? p95 / peer : 1
      const slow = faults.has(sv) || (ratio >= 1.6 && p95 >= 8)
      return { server: sv, p95_ms: p95, peer_ratio: ratio, cpu_pct: clamp((load / SERVER_CAP_MS) * 100, 0, 100), status: slow ? 'slow' : 'healthy' }
    })
    for (const sv of ['spare', 'quarantine']) this.serverViews.push({ server: sv, p95_ms: 0, peer_ratio: 1, cpu_pct: 1, status: 'healthy' })
    this.utilization = clamp(totalCpu / (SERVER_CAP_MS * SERVERS.length), 0, 1)
    const xs = TENANTS.map((t) => this.tenants.get(t.id)!.rps / TIERS[t.tier].weight)
    const sum = xs.reduce((a, b) => a + b, 0)
    const sq = xs.reduce((a, b) => a + b * b, 0)
    this.fairness = sq > 0 ? (sum * sum) / (xs.length * sq) : 1

    return { snapshot: this.snapshot(now), incidents: events }
  }

  private ladder(id: string, s: TState, now: number, hurting: boolean, calm: boolean, events: IncidentEvent[], dt: number) {
    const name = TENANTS.find((t) => t.id === id)!.name
    const act = this.mode === 'tenantguard'
    const set = (level: Level, detail: string, action: string | null) => {
      const prev = s.level
      if (prev === level) return
      s.level = level
      s.lastStep = now
      if (!s.incident) {
        s.incident = `inc-${id}-${++this.incSeq}`
        events.push(this.ev(s, 'detected', now, { detail: `${name} (${id}) uses ${s.score.toFixed(1)}x its fair share while neighbours slow down.` }))
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
        set('throttle', `Score ${score} >= throttle ${NOISY.throttle} for ${NOISY.hold_s.throttle} s: quota x${s.factor}.`, `limits factor=${s.factor}`)
      } else if (s.level === 'throttle' && s.score >= NOISY.throttle && now - s.lastStep >= NOISY.hold_s.throttle) {
        s.factor = Math.max(0.1, s.factor * NOISY.throttle_factor)
        s.overrideExpires = now + TTL_S
        s.lastStep = now
        if (s.factor <= 0.25) {
          s.weightFactor = 0.25
          set('deprioritize', `Still ${score} after halving: quota x${s.factor}, scheduler weight x0.25.`, `limits factor=${s.factor} weight_factor=0.25`)
        } else {
          events.push(this.ev(s, 'level_change', now, { level: 'throttle', prev_level: 'throttle', action: `limits factor=${s.factor}`, detail: `Score ${score} still high: quota halved again to x${s.factor}.` }))
        }
      } else if (s.level === 'deprioritize' && s.score >= NOISY.quarantine && now - s.lastStep >= NOISY.hold_s.quarantine) {
        s.factor = 0.1
        s.weightFactor = 0.1
        s.overrideExpires = now + TTL_S
        set('quarantine', `Score ${score} >= quarantine ${NOISY.quarantine} for ${NOISY.hold_s.quarantine} s: quota x0.1, weight x0.1.`, 'limits factor=0.1 weight_factor=0.1')
      } else if (s.factor < 1) {
        s.overrideExpires = now + TTL_S // renew TTL while needed
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
          set(next, `Usage back under its fair share for ${NOISY.hold_s.recover} s: recovering, quota x${s.factor.toFixed(1)}.`, `limits factor=${s.factor.toFixed(1)}`)
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
    events.push(this.ev(s, 'level_change', now, { level: 'normal', prev_level: prev, action: 'limits cleared', detail: `${name} back under its fair share; override cleared.` }))
    events.push(this.ev(s, 'resolved', now, { detail: `Neighbours of ${name} back to baseline p95.` }))
    const st = s.story
    if (st && st.actions.length) {
      const victims = this.coTenants(s.id)
      events.push(
        this.ev(s, 'explained', now, {
          verdict: 'costly_pattern',
          detail: `${name} sent a few very expensive requests that took most of the CPU of its servers; throttling it restored its neighbours.`,
          report: {
            verdict: 'costly_pattern',
            attacker: s.id,
            confidence: Math.min(1, st.peakScore / NOISY.quarantine),
            duration_s: Math.round(now - st.openedAt),
            summary: `${name} (${s.id}) ran heavy requests that used up to ${st.peakScore.toFixed(1)}x its fair share on ${this.barcode(s.id).join(', ')}. ${victims.length} neighbours sharing those servers slowed down (worst p95 ${Math.round(st.victimPeak)} ms). TenantGuard throttled ${s.id} step by step down to ${Math.round(st.peakFactor * 100)} % of its plan, the neighbours recovered, and the quota was restored once ${s.id} calmed down.`,
            actions: st.actions,
            evidence: {
              barcode: this.barcode(s.id).join(' '),
              neighbours_hurt: victims.join(' '),
              peak_noisy_score: st.peakScore,
              lowest_quota_factor: st.peakFactor,
              worst_victim_p95_ms: Math.round(st.victimPeak),
            },
          },
        }),
      )
    }
    s.story = null
    s.incident = null
  }

  private barcode(id: string): string[] {
    return TENANTS.find((t) => t.id === id)?.servers ?? []
  }

  private coTenants(id: string): string[] {
    const b = new Set(this.barcode(id))
    return TENANTS.filter((t) => t.id !== id && t.servers.some((sv) => b.has(sv))).map((t) => t.id)
  }

  /** Wave-2-style stages for the Case File: named -> proving -> verdict as the ladder climbs. */
  private storyStep(s: TState, level: Level, prev: Level, action: string | null, now: number, events: IncidentEvent[], name: string) {
    if (prev === 'normal' && level === 'warn') {
      s.story = { openedAt: now, peakScore: s.score, peakFactor: 1, actions: [], victimPeak: 0 }
    }
    const st = s.story
    if (!st) return
    st.peakScore = Math.max(st.peakScore, s.score)
    st.peakFactor = Math.min(st.peakFactor, s.factor)
    for (const v of this.coTenants(s.id)) st.victimPeak = Math.max(st.victimPeak, this.tenants.get(v)!.p95)
    if (action && level !== 'normal') st.actions.push(`${level}: ${action}`)
    const rank = (l: Level) => LEVELS.indexOf(l)
    if (this.mode !== 'tenantguard' || rank(level) <= rank(prev)) return // story stages only while escalating
    const others = this.coTenants(s.id)
      .map((id) => ({ tenant: id, confidence: clamp(this.tenants.get(id)!.score / NOISY.quarantine / 3, 0.02, 0.3) }))
      .sort((a, b) => b.confidence - a.confidence)
      .slice(0, 2)
    const conf = clamp(s.score / NOISY.quarantine, 0.5, 0.97)
    if (level === 'throttle' && prev === 'warn') {
      events.push(this.ev(s, 'named', now, { suspects: [{ tenant: s.id, confidence: conf }, ...others], detail: `${name} named: its barcode ${this.barcode(s.id).join(' ')} covers every slow server.` }))
    } else if (level === 'deprioritize') {
      events.push(this.ev(s, 'proving', now, { suspects: [{ tenant: s.id, confidence: conf }, ...others], detail: `Proving: halving ${s.id}'s quota cut its neighbours' p95; the effect follows ${s.id}.` }))
    } else if (level === 'quarantine') {
      events.push(this.ev(s, 'verdict', now, { verdict: 'costly_pattern', action: action, suspects: [{ tenant: s.id, confidence: Math.max(conf, 0.9) }, ...others], detail: `Verdict costly_pattern: ${s.id} pays for a plan but sends requests far heavier than it; quarantined until it calms down.` }))
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

  snapshot(now: number): StateSnapshot {
    const tenants: TenantView[] = TENANTS.map((t) => {
      const s = this.tenants.get(t.id)!
      return {
        tenant: t.id,
        name: t.name,
        tier: t.tier,
        p95_ms: s.p95,
        rps: s.rps,
        rejected_rps: Math.max(0, s.rejected),
        cpu_share: s.cpuShare,
        noisy_score: s.score,
        noisy_norm: clamp(s.score / NOISY.quarantine, 0, 1),
        level: s.level,
        factor: s.factor,
        placement: 'normal',
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
    }
  }

  private sentence(tenants: TenantView[]): string {
    const flagged = tenants.filter((t) => t.level !== 'normal').sort((a, b) => b.noisy_score - a.noisy_score)
    const baseline = (t: TenantView) => this.baseline.get(t.tenant) ?? t.p95_ms
    const hurt = tenants.filter((t) => t.level === 'normal' && t.p95_ms > 2 * baseline(t))
    const util = Math.round(this.utilization * 100)
    if (flagged.length === 0 && hurt.length === 0) {
      return `All ${tenants.length} tenants healthy · fleet CPU ${util}% · fairness ${this.fairness.toFixed(2)}`
    }
    const n = flagged[0]
    const who = n ? `${n.name} (${n.tenant})` : 'An unidentified tenant'
    const hurtTxt = hurt.length ? `${hurt.length} neighbour${hurt.length > 1 ? 's' : ''} slowed` : 'neighbours protected'
    if (this.mode === 'off') return `Defences OFF: ${who} is starving its neighbours · ${hurtTxt}`
    if (!n) return `${hurtTxt} · looking for the cause`
    if (this.mode === 'industry') return `${who} is noisy (${n.noisy_score.toFixed(1)}x fair share) · quotas only · ${hurtTxt}`
    const what =
      n.level === 'warn' ? 'under watch' : n.level === 'normal' ? 'recovered' : `${n.level === 'throttle' ? 'throttled' : n.level === 'deprioritize' ? 'deprioritized' : n.level} to ${Math.round(n.factor * 100)}% of plan`
    return `${who} is noisy (${n.noisy_score.toFixed(1)}x fair share) · ${what} · ${hurtTxt}`
  }
}
