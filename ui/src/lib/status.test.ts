import { describe, expect, it } from 'vitest'
import { fairnessStatus, levelStatus, tenantStatus, utilizationStatus, worst } from './status.ts'

const th = { p95WarnMs: 150, p95BadMs: 400 }

describe('status colours', () => {
  it('maps levels to green / amber / red', () => {
    expect(levelStatus('normal')).toBe('ok')
    expect(levelStatus('warn')).toBe('warn')
    for (const l of ['throttle', 'deprioritize', 'quarantine', 'blocked'] as const) expect(levelStatus(l)).toBe('bad')
  })

  it('a hurt victim at normal level goes amber then red by latency', () => {
    expect(tenantStatus({ level: 'normal', p95_ms: 30, rps: 10 }, th)).toBe('ok')
    expect(tenantStatus({ level: 'normal', p95_ms: 200, rps: 10 }, th)).toBe('warn')
    expect(tenantStatus({ level: 'normal', p95_ms: 900, rps: 10 }, th)).toBe('bad')
  })

  it('a throttled tenant is red even when its latency is fine', () => {
    expect(tenantStatus({ level: 'throttle', p95_ms: 20, rps: 10 }, th)).toBe('bad')
  })

  it('no traffic is idle (grey), not green', () => {
    expect(tenantStatus({ level: 'normal', p95_ms: 0, rps: 0 }, th)).toBe('idle')
  })

  it('worst / gauges', () => {
    expect(worst(['ok', 'warn', 'idle'])).toBe('warn')
    expect(worst([])).toBe('idle')
    expect(fairnessStatus(0.99)).toBe('ok')
    expect(fairnessStatus(0.8)).toBe('warn')
    expect(fairnessStatus(0.5)).toBe('bad')
    expect(utilizationStatus(0.3)).toBe('ok')
    expect(utilizationStatus(0.9)).toBe('bad')
  })
})
