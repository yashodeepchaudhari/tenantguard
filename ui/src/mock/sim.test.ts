import { describe, expect, it } from 'vitest'
import type { Mode } from '../types.ts'
import { MockPlatform, SimError } from './sim.ts'

function run(mode: Mode) {
  const p = new MockPlatform()
  p.setMode(mode)
  let t = 1000
  const levels: string[] = []
  let victimPeak = 0
  let otherPeak = 0
  let last = p.tick(t).snapshot
  for (let i = 0; i < 200; i++) {
    t += 0.5
    if (i === 20) p.startAttack({ attack: 'whale', tenant: 'K', intensity: 2, duration_s: 40 }, t)
    const { snapshot, incidents } = p.tick(t)
    for (const e of incidents) if (e.tenant === 'K' && e.level) levels.push(e.level)
    if (i > 30 && i < 100) {
      victimPeak = Math.max(victimPeak, snapshot.tenants.find((x) => x.tenant === 'E')!.p95_ms) // K's project peer
      otherPeak = Math.max(otherPeak, snapshot.tenants.find((x) => x.tenant === 'A')!.p95_ms) // another project
    }
    last = snapshot
  }
  return { levels, victimPeak, otherPeak, last, p }
}

describe('mock platform', () => {
  it('emits contract-shaped snapshots for all 12 tenants', () => {
    const { snapshot } = new MockPlatform().tick(1)
    expect(snapshot.type).toBe('state')
    expect(snapshot.tenants).toHaveLength(12)
    for (const t of snapshot.tenants) expect(t.noisy_norm).toBeGreaterThanOrEqual(0)
  })

  it("off hurts K's project peers most; industry's static floors do not shield peers inside the project", () => {
    expect(run('off').victimPeak).toBeGreaterThan(run('industry').victimPeak)
    expect(run('industry').victimPeak).toBeGreaterThan(200) // only the ladder (tenantguard mode) acts inside a project
  })

  it('another project never feels the whale (own replicas, own floor)', () => {
    const { otherPeak, victimPeak } = run('off')
    expect(otherPeak).toBeLessThan(100)
    expect(victimPeak).toBeGreaterThan(otherPeak * 3)
  })

  it('every project starts one warm replica, and the snapshot carries the pool', () => {
    const p = new MockPlatform()
    let s = p.tick(1000).snapshot
    for (let i = 1; i <= 6; i++) s = p.tick(1000 + i * 0.5).snapshot
    expect(s.pool?.projects.map((x) => x.replicas.filter((r) => r.state === 'ready').length)).toEqual([1, 1, 1, 1, 1])
    expect(s.servers.every((x) => x.project && x.server.startsWith('tg-'))).toBe(true)
    expect(s.pool?.floors_cpu).toBeCloseTo(4.5)
  })

  it('tenantguard walks the ladder on K and recovers after the attack', () => {
    const { levels, last } = run('tenantguard')
    expect(levels).toContain('throttle')
    expect(levels).toContain('quarantine')
    expect(levels[levels.length - 1]).toBe('normal')
    expect(last.tenants.find((x) => x.tenant === 'K')!.factor).toBe(1)
  })

  it('tells the case story once, in order, ending with an explained report', () => {
    const p = new MockPlatform()
    let t = 1000
    const stages: string[] = []
    let report: Record<string, unknown> | null | undefined = null
    for (let i = 0; i < 200; i++) {
      t += 0.5
      if (i === 20) p.startAttack({ attack: 'whale', tenant: 'K', intensity: 2, duration_s: 40 }, t)
      for (const e of p.tick(t).incidents) {
        if (e.stage !== 'level_change') stages.push(e.stage)
        if (e.stage === 'explained') report = e.report
      }
    }
    expect(stages).toEqual(['detected', 'named', 'proving', 'verdict', 'resolved', 'explained'])
    expect(report).toMatchObject({ verdict: 'costly_pattern', attacker: 'K' })
  })

  it('an honest burst of a whole project borrows idle CPU and scales out', () => {
    const p = new MockPlatform()
    let t = 1000
    let s = p.tick(t).snapshot
    for (let i = 0; i < 60; i++) {
      t += 0.5
      if (i === 2) for (const id of ['E', 'G', 'K']) p.startAttack({ attack: 'honest_spike', tenant: id, intensity: 5, duration_s: 60 }, t)
      s = p.tick(t).snapshot
    }
    const mob = s.pool!.projects.find((x) => x.id === 'mobility')!
    expect(mob.loan_cpu).toBeGreaterThan(0)
    expect(mob.alloc_cpu).toBeGreaterThan(mob.floor_cpu)
    expect(mob.replicas.filter((r) => r.state === 'ready').length).toBeGreaterThanOrEqual(2)
    expect(mob.replicas.some((r) => r.borrowed)).toBe(true)
  })

  it('never flags an innocent tenant', () => {
    const { p } = run('off')
    for (const [id, s] of p.tenants) if (id !== 'K') expect(s.incident).toBeNull()
  })

  it('validates like the lab (422) and the gateway', () => {
    const p = new MockPlatform()
    expect(() => p.startAttack({ attack: 'whale', tenant: 'K', intensity: 6, duration_s: 10 }, 1)).toThrow(SimError)
    expect(() => p.startAttack({ attack: 'whale', tenant: 'Z', intensity: 1, duration_s: 10 }, 1)).toThrow(SimError)
    // the brain's registry rules: sum of floors, ids, duplicates, a project with tenants
    expect(() => p.createProject({ id: 'big', name: 'Big', tier: 'pro', floor_cpu: 1, cap_cpu: 2, warm_min: 1 })).toThrow(/sum of floors/)
    expect(() => p.createProject({ id: 'Bad Id', name: 'x', tier: 'pro', floor_cpu: 0.1, cap_cpu: 1, warm_min: 1 })).toThrow(SimError)
    expect(() => p.addTenant('media', { id: 'a', name: 'dup' })).toThrow(/already exists/)
    expect(() => p.deleteProject('free')).toThrow(/still has tenants/)
    p.createProject({ id: 'games', name: 'Games', tier: 'free', floor_cpu: 0.25, cap_cpu: 1, warm_min: 0 })
    const k = p.addTenant('games', { id: 'Q', name: 'Quiz' })
    expect(k.project).toBe('games')
    expect(p.adminView('Q').quota.rate_rps).toBe(20) // the free tier's quota
    p.removeTenant('Q')
    expect(p.deleteProject('games').projects.map((x) => x.id)).not.toContain('games')
  })
})
