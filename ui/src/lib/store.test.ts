import { describe, expect, it } from 'vitest'
import type { IncidentEvent, StateSnapshot } from '../types.ts'
import { LiveStore } from './store.ts'

const snap = (ts: number, p95 = 20): StateSnapshot => ({
  type: 'state',
  ts,
  mode: 'tenantguard',
  tenants: [
    {
      tenant: 'A',
      name: 'A',
      tier: 'pro',
      p95_ms: p95,
      rps: 1,
      rejected_rps: 0,
      cpu_share: 0.5,
      noisy_score: 1,
      noisy_norm: 0.25,
      level: 'normal',
      factor: 1,
      placement: 'normal',
    },
  ],
  servers: [],
  utilization: 0.3,
  fairness: 0.99,
  status_sentence: 'ok',
  brain_ok: true,
})

const inc = (ts: number, stage: IncidentEvent['stage'] = 'detected'): IncidentEvent => ({
  type: 'incident',
  incident_id: 'i1',
  stage,
  ts,
  suspects: [],
  detail: '',
})

describe('LiveStore', () => {
  it('keeps only the last 60 s of history', () => {
    const s = new LiveStore(60)
    for (let t = 0; t <= 100; t += 0.5) s.apply(snap(1000 + t))
    const h = s.getState().history
    expect(h[0].ts).toBeGreaterThanOrEqual(1100 - 60)
    expect(h[h.length - 1].ts).toBe(1100)
    expect(h[h.length - 1].p95.A).toBe(20)
    expect(h[h.length - 1].cpu.A).toBe(0.5)
  })

  it('resets history when the brain clock jumps backwards', () => {
    const s = new LiveStore(60)
    s.apply(snap(2000))
    s.apply(snap(2001))
    s.apply(snap(10))
    expect(s.getState().history.map((p) => p.ts)).toEqual([10])
  })

  it('dedupes incidents (WS + backfill) and sorts newest first', () => {
    const s = new LiveStore()
    s.apply(inc(5))
    s.addIncidents([inc(5), inc(3), inc(7, 'resolved')])
    expect(s.getState().incidents.map((e) => e.ts)).toEqual([7, 5, 3])
  })

  it('shows same-timestamp events newest-arrival first', () => {
    const s = new LiveStore()
    s.addIncidents([inc(5, 'detected'), inc(5, 'level_change')])
    expect(s.getState().incidents.map((e) => e.stage)).toEqual(['level_change', 'detected'])
  })

  it('clears the optimistic mode once a snapshot confirms it', () => {
    const s = new LiveStore()
    s.setPendingMode('tenantguard')
    s.apply(snap(1))
    expect(s.getState().pendingMode).toBeNull()
    s.setPendingMode('off')
    s.apply(snap(2))
    expect(s.getState().pendingMode).toBe('off')
  })

  it('notifies subscribers until unsubscribed', () => {
    const s = new LiveStore()
    let n = 0
    const un = s.subscribe(() => n++)
    s.apply(snap(1))
    un()
    s.apply(snap(2))
    expect(n).toBe(1)
  })
})
