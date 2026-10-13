// ₹ impact meter (pure, unit tested). Derived client-side from the snapshot stream until the
// brain owns it (proposed contract field: StateSnapshot.impact, see ui/README.md).
//
// Definitions (per snapshot interval dt, capped at 2 s so a stalled tab does not inflate them):
// - a tenant is THREATENED when it shares a project (and so its replicas) with a flagged tenant
//   (level >= warn), or when one of its project's replicas is slow with a p95 that would breach the SLO
//   (a replica merely 1.6x its peers at 20 ms would not hurt anyone)
// - SLO-violation time AVOIDED: a threatened tenant stays within SLO (p95 < sloMs) while TenantGuard
//   mode is on. Each such second counts once per tenant.
// - SLO-violation time SUFFERED: any tenant over SLO (any mode), so OFF-mode damage is visible too
// - revenue protected (₹) = Σ avoided seconds × that tenant's monthly_revenue_rs / seconds in a month
// - customers shielded = distinct tenants with avoided time > 0, never counting a tenant that was
//   itself noisy in this session (a culprit is not a protected customer)
// - innocents penalised = distinct tenants acted on (level >= throttle or factor < 1) that were
//   were never noisy in this session (own score >= warn) and that no brain incident names. A culprit
//   still recovering on the ladder (factor < 1, score back to 0) is not an innocent. Must stay 0.
import type { Level, StateSnapshot } from '../types.ts'

export const MONTH_S = 30 * 24 * 3600

export interface ImpactContext {
  projectOf: Record<string, string> // tenant -> project
  revenue: Record<string, number> // tenant -> monthly_revenue_rs
  warn: number // noisy warn threshold
  sloMs: number
  /** tenants the brain opened an incident against (IncidentEvent.tenant) */
  suspected: Set<string>
}

export interface ImpactState {
  since: number | null
  lastTs: number | null
  avoidedS: Record<string, number>
  sufferedS: Record<string, number>
  innocents: string[]
  /** tenants seen noisy (own score >= warn) at any point this session */
  noisy: string[]
  revenueRs: number
}

export const emptyImpact = (): ImpactState => ({ since: null, lastTs: null, avoidedS: {}, sufferedS: {}, innocents: [], noisy: [], revenueRs: 0 })

export const ACTED: Level[] = ['throttle', 'deprioritize', 'quarantine', 'blocked']

export function updateImpact(prev: ImpactState, s: StateSnapshot, ctx: ImpactContext): ImpactState {
  const noisy = new Set(prev.noisy)
  for (const t of s.tenants) if (t.noisy_score >= ctx.warn) noisy.add(t.tenant)
  const culprit = (id: string) => noisy.has(id) || ctx.suspected.has(id)
  if (prev.lastTs === null || s.ts <= prev.lastTs) {
    // first snapshot (or a clock jump back): start the clock, nothing to integrate yet
    return { ...prev, since: prev.since ?? s.ts, lastTs: s.ts, noisy: [...noisy].sort() }
  }
  const dt = Math.min(2, s.ts - prev.lastTs)
  const avoidedS = { ...prev.avoidedS }
  const sufferedS = { ...prev.sufferedS }
  const innocents = new Set(prev.innocents)
  let revenueRs = prev.revenueRs

  const slowProjects = new Set(s.servers.filter((x) => x.status === 'slow' && x.p95_ms >= ctx.sloMs).map((x) => x.project))
  const flaggedProjects = new Set<string>()
  const flagged = new Set<string>()
  for (const t of s.tenants) {
    if (t.level !== 'normal') {
      flagged.add(t.tenant)
      flaggedProjects.add(ctx.projectOf[t.tenant] ?? t.project)
    }
  }

  for (const t of s.tenants) {
    const id = t.tenant
    const hasData = t.p95_ms > 0 // p95 0 = nothing completed: no data, skip
    if (hasData && t.p95_ms >= ctx.sloMs) sufferedS[id] = (sufferedS[id] ?? 0) + dt
    const project = ctx.projectOf[id] ?? t.project
    const threatened = !flagged.has(id) && !culprit(id) && (flaggedProjects.has(project) || slowProjects.has(project))
    if (s.mode === 'tenantguard' && threatened && hasData && t.p95_ms < ctx.sloMs) {
      avoidedS[id] = (avoidedS[id] ?? 0) + dt
      revenueRs += ((ctx.revenue[id] ?? 0) * dt) / MONTH_S
    }
    if ((ACTED.includes(t.level) || t.factor < 1) && !culprit(id)) innocents.add(id)
  }
  return { since: prev.since ?? s.ts, lastTs: s.ts, avoidedS, sufferedS, innocents: [...innocents].sort(), noisy: [...noisy].sort(), revenueRs }
}

export interface ImpactSummary {
  revenueRs: number
  customers: string[]
  customersMonthlyRs: number
  avoidedS: number
  sufferedS: number
  innocents: string[]
  sinceS: number
}

export function summarizeImpact(st: ImpactState, revenue: Record<string, number>): ImpactSummary {
  const customers = Object.keys(st.avoidedS).filter((k) => st.avoidedS[k] > 0).sort()
  const sum = (o: Record<string, number>) => Object.values(o).reduce((a, b) => a + b, 0)
  return {
    revenueRs: st.revenueRs,
    customers,
    customersMonthlyRs: customers.reduce((a, c) => a + (revenue[c] ?? 0), 0),
    avoidedS: sum(st.avoidedS),
    sufferedS: sum(st.sufferedS),
    innocents: st.innocents,
    sinceS: st.since !== null && st.lastTs !== null ? st.lastTs - st.since : 0,
  }
}
