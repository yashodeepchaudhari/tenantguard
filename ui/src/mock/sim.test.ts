import { describe, expect, it } from 'vitest'
import type { Mode } from '../types.ts'
import { MockPlatform, SimError } from './sim.ts'

function run(mode: Mode) {
  const p = new MockPlatform()
  p.setMode(mode)
  let t = 1000
  const levels: string[] = []
  let victimPeak = 0
  let last = p.tick(t).snapshot
  for (let i = 0; i < 200; i++) {
    t += 0.5
    if (i === 20) p.startAttack({ attack: 'whale', tenant: 'K', intensity: 2, duration_s: 40 }, t)
    const { snapshot, incidents } = p.tick(t)
    for (const e of incidents) if (e.tenant === 'K' && e.level) levels.push(e.level)
    if (i > 30 && i < 100) victimPeak = Math.max(victimPeak, snapshot.tenants.find((x) => x.tenant === 'A')!.p95_ms)
    last = snapshot
  }
  return { levels, victimPeak, last, p }
}

describe('mock platform', () => {
  it('emits contract-shaped snapshots for all 12 tenants', () => {
    const { snapshot } = new MockPlatform().tick(1)
    expect(snapshot.type).toBe('state')
    expect(snapshot.tenants).toHaveLength(12)
    for (const t of snapshot.tenants) expect(t.noisy_norm).toBeGreaterThanOrEqual(0)
  })

  it('off hurts victims most, industry less', () => {
    expect(run('off').victimPeak).toBeGreaterThan(run('industry').victimPeak * 2)
  })

  it('tenantguard walks the ladder on K and recovers after the attack', () => {
    const { levels, last } = run('tenantguard')
    expect(levels).toContain('throttle')
    expect(levels).toContain('quarantine')
    expect(levels[levels.length - 1]).toBe('normal')
    expect(last.tenants.find((x) => x.tenant === 'K')!.factor).toBe(1)
  })

  it('never flags an innocent tenant', () => {
    const { p } = run('off')
    for (const [id, s] of p.tenants) if (id !== 'K') expect(s.incident).toBeNull()
  })

  it('validates like the lab (422) and the gateway', () => {
    const p = new MockPlatform()
    expect(() => p.startAttack({ attack: 'whale', tenant: 'K', intensity: 6, duration_s: 10 }, 1)).toThrow(SimError)
    expect(() => p.startAttack({ attack: 'whale', tenant: 'Z', intensity: 1, duration_s: 10 }, 1)).toThrow(SimError)
    expect(() => p.patchQuota('A', { rate_rps: -1 })).toThrow(SimError)
    expect(p.patchQuota('A', { rate_rps: 7 }).quota.rate_rps).toBe(7)
  })
})
