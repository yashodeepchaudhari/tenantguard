import { describe, expect, it } from 'vitest'
import type { Level, Mode, StateSnapshot, TenantView } from '../types.ts'
import { emptyImpact, MONTH_S, summarizeImpact, updateImpact, type ImpactContext } from './impact.ts'

const tv = (tenant: string, o: Partial<TenantView> = {}): TenantView => ({
  tenant, name: tenant, tier: 'pro', p95_ms: 20, rps: 5, rejected_rps: 0, cpu_share: 0.1,
  noisy_score: 0.5, noisy_norm: 0.1, level: 'normal' as Level, factor: 1, placement: 'normal', ...o,
})
const snap = (ts: number, tenants: TenantView[], mode: Mode = 'tenantguard', slow: string[] = []): StateSnapshot => ({
  type: 'state', ts, mode, tenants,
  servers: ['w0', 'w1', 'w2'].map((s) => ({ server: s, p95_ms: 10, peer_ratio: 1, cpu_pct: 20, status: slow.includes(s) ? 'slow' : 'healthy' })),
  utilization: 0.3, fairness: 0.9, status_sentence: '', brain_ok: true,
})
const ctx: ImpactContext = {
  barcodes: { K: ['w1'], A: ['w0', 'w1'], B: ['w2'], C: ['w2'] },
  revenue: { A: 2_592_000, B: 1000, C: 0, K: 5 },
  rerouted: new Set(),
  warn: 1.5,
  sloMs: 150,
}

function run(steps: StateSnapshot[], c = ctx) {
  return steps.reduce((st, s) => updateImpact(st, s, c), emptyImpact())
}

describe('impact meter', () => {
  it("counts a neighbour kept within SLO while K is flagged, pro rata on its revenue", () => {
    const k = tv('K', { level: 'throttle', factor: 0.5, noisy_score: 3 })
    const st = run([snap(100, [k, tv('A'), tv('B')]), snap(101, [k, tv('A'), tv('B')]), snap(102, [k, tv('A'), tv('B')])])
    expect(st.avoidedS).toEqual({ A: 2 }) // B does not share a server with K
    expect(st.revenueRs).toBeCloseTo((2_592_000 * 2) / MONTH_S) // = 2
    const sum = summarizeImpact(st, ctx.revenue)
    expect(sum.customers).toEqual(['A'])
    expect(sum.innocents).toEqual([]) // K was over warn: not innocent
    expect(sum.sinceS).toBe(2)
  })

  it('OFF mode protects nobody; a victim over SLO is suffered time', () => {
    const k = tv('K', { level: 'warn', noisy_score: 3 })
    const st = run([snap(0, [k, tv('A', { p95_ms: 900 })], 'off'), snap(1, [k, tv('A', { p95_ms: 900 })], 'off')])
    expect(st.avoidedS).toEqual({})
    expect(st.sufferedS).toEqual({ A: 1 })
  })

  it('a tenant acted on below warn is an innocent penalised', () => {
    const st = run([snap(0, [tv('B', { level: 'throttle', factor: 0.5, noisy_score: 0.4 })]), snap(1, [tv('B', { level: 'throttle', factor: 0.5, noisy_score: 0.4 })])])
    expect(st.innocents).toEqual(['B'])
  })

  it('slow servers and rerouting threaten their tenants; p95 0 is no data', () => {
    const st = run(
      [snap(0, [tv('A'), tv('B', { p95_ms: 0 }), tv('C')], 'tenantguard', ['w0']), snap(1, [tv('A'), tv('B', { p95_ms: 0 }), tv('C')], 'tenantguard', ['w0'])],
      { ...ctx, rerouted: new Set(['C']) },
    )
    expect(st.avoidedS).toEqual({ A: 1, C: 1 }) // B: rerouted? no; p95 0 anyway
  })

  it('caps a stalled interval at 2 s and survives a clock jump back', () => {
    const k = tv('K', { level: 'warn', noisy_score: 3 })
    let st = run([snap(0, [k, tv('A')]), snap(60, [k, tv('A')])])
    expect(st.avoidedS.A).toBe(2)
    st = updateImpact(st, snap(5, [k, tv('A')]), ctx)
    expect(st.avoidedS.A).toBe(2)
    expect(st.lastTs).toBe(5)
  })
})
