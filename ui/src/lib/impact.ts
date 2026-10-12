// ₹ impact meter (pure, unit tested). Derived client-side from the snapshot stream until the
// brain owns it (proposed contract field: StateSnapshot.impact, see ui/README.md).
//
// Definitions (per snapshot interval dt, capped at 2 s so a stalled tab does not inflate them):
// - a tenant is THREATENED when it shares a server with a flagged tenant (level >= warn), when one
//   of its barcode servers is slow, or when the gateway is routing it around a server (route weights)
// - SLO-violation time AVOIDED: a threatened tenant stays within SLO (p95 < sloMs) while TenantGuard
//   mode is on. Each such second counts once per tenant.
// - SLO-violation time SUFFERED: any tenant over SLO (any mode), so OFF-mode damage is visible too
// - revenue protected (₹) = Σ avoided seconds × that tenant's monthly_revenue_rs / seconds in a month
// - customers shielded = distinct tenants with avoided time > 0
// - innocents penalised = distinct tenants acted on (level >= throttle or factor < 1) while their
//   own noisy score was below warn. TenantGuard's promise is that this stays 0.
import type { Level, StateSnapshot } from '../types.ts'

export const MONTH_S = 30 * 24 * 3600

export interface ImpactContext {
  barcodes: Record<string, string[]>
  revenue: Record<string, number> // tenant -> monthly_revenue_rs
  rerouted: Set<string> // tenants with active route weights / relocation
  warn: number // noisy warn threshold
  sloMs: number
}

export interface ImpactState {
  since: number | null
  lastTs: number | null
  avoidedS: Record<string, number>
  sufferedS: Record<string, number>
  innocents: string[]
  revenueRs: number
}

export const emptyImpact = (): ImpactState => ({ since: null, lastTs: null, avoidedS: {}, sufferedS: {}, innocents: [], revenueRs: 0 })

const ACTED: Level[] = ['throttle', 'deprioritize', 'quarantine', 'blocked']

export function updateImpact(prev: ImpactState, s: StateSnapshot, ctx: ImpactContext): ImpactState {
  if (prev.lastTs === null || s.ts <= prev.lastTs) {
    // first snapshot (or a clock jump back): start the clock, nothing to integrate yet
    return { ...prev, since: prev.since ?? s.ts, lastTs: s.ts }
  }
  const dt = Math.min(2, s.ts - prev.lastTs)
  const avoidedS = { ...prev.avoidedS }
  const sufferedS = { ...prev.sufferedS }
  const innocents = new Set(prev.innocents)
  let revenueRs = prev.revenueRs

  const slow = new Set(s.servers.filter((x) => x.status === 'slow').map((x) => x.server))
  const flaggedServers = new Set<string>()
  const flagged = new Set<string>()
  for (const t of s.tenants) {
    if (t.level !== 'normal') {
      flagged.add(t.tenant)
      for (const sv of ctx.barcodes[t.tenant] ?? []) flaggedServers.add(sv)
    }
  }

  for (const t of s.tenants) {
    const id = t.tenant
    const hasData = t.p95_ms > 0 // p95 0 = nothing completed: no data, skip
    if (hasData && t.p95_ms >= ctx.sloMs) sufferedS[id] = (sufferedS[id] ?? 0) + dt
    const barcode = ctx.barcodes[id] ?? []
    const threatened =
      !flagged.has(id) && (barcode.some((sv) => flaggedServers.has(sv) || slow.has(sv)) || ctx.rerouted.has(id))
    if (s.mode === 'tenantguard' && threatened && hasData && t.p95_ms < ctx.sloMs) {
      avoidedS[id] = (avoidedS[id] ?? 0) + dt
      revenueRs += ((ctx.revenue[id] ?? 0) * dt) / MONTH_S
    }
    if ((ACTED.includes(t.level) || t.factor < 1) && t.noisy_score < ctx.warn) innocents.add(id)
  }
  return { since: prev.since ?? s.ts, lastTs: s.ts, avoidedS, sufferedS, innocents: [...innocents].sort(), revenueRs }
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
