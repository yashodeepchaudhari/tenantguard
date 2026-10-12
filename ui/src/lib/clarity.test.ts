import { describe, expect, it } from 'vitest'
import type { AttackStatus, IncidentEvent, LabStatus, StateSnapshot, TenantView } from '../types.ts'
import { buildHeadline } from './headline.ts'
import { latestByTenant, tileReason } from './reason.ts'
import { mmss, scenarioOf } from './scenario.ts'

const th = { p95WarnMs: 150, p95BadMs: 400 }
const tv = (tenant: string, o: Partial<TenantView> = {}): TenantView => ({
  tenant, name: `T${tenant}`, tier: 'pro', p95_ms: 30, rps: 5, rejected_rps: 0, cpu_share: 0.08,
  noisy_score: 0.5, noisy_norm: 0.1, level: 'normal', factor: 1, placement: 'normal', ...o,
})
const ALL = 'ABCDEFGHIJKL'.split('')
const snap = (over: Record<string, Partial<TenantView>> = {}, o: Partial<StateSnapshot> = {}): StateSnapshot => ({
  type: 'state', ts: 1000, mode: 'tenantguard',
  tenants: ALL.map((t) => tv(t, over[t])),
  servers: ['w0', 'w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'w7', 'w8'].map((s) => ({ server: s, p95_ms: 10, peer_ratio: 1, cpu_pct: 20, status: 'healthy' as const })),
  utilization: 0.3, fairness: 0.97, status_sentence: 'long brain sentence', brain_ok: true, ...o,
})
const attack = (o: Partial<AttackStatus> = {}): AttackStatus => ({
  attack_id: 'a1', attack: 'whale', tenant: 'K', server: null, intensity: 2, state: 'running', started_at: 900, ends_at: 1042, ...o,
})
const lab = (o: Partial<LabStatus> = {}): LabStatus => ({ attacks: [], calm_running: true, run: null, ...o })

describe('scenario badge', () => {
  it('names the running attack with a countdown', () => {
    const s = scenarioOf(lab({ attacks: [attack()] }), true, 1000)
    expect(s.kind).toBe('attack')
    expect(s.label).toBe('Attack: whale on K (0:42 left)')
  })
  it('counts extra attacks, ignores stopped or expired ones', () => {
    const s = scenarioOf(lab({ attacks: [attack(), attack({ attack_id: 'a2', attack: 'flood', tenant: 'B', started_at: 950 }), attack({ attack_id: 'a3', state: 'stopped' }), attack({ attack_id: 'a4', ends_at: 999 })] }), true, 1000)
    expect(s.label).toBe('Attack: flood on B (0:42 left) +1 more')
    expect(s.attacks).toHaveLength(2)
  })
  it('calm, scenario run, no traffic, lab offline', () => {
    expect(scenarioOf(lab(), true, 1000).label).toBe('Calm traffic · no attack')
    expect(scenarioOf(lab({ run: 'run-1' }), true, 1000).kind).toBe('run')
    expect(scenarioOf(lab({ calm_running: false }), true, 1000).label).toBe('No traffic')
    expect(scenarioOf(null, false, 1000).kind).toBe('unknown')
  })
  it('mm:ss', () => {
    expect(mmss(42)).toBe('0:42')
    expect(mmss(125.4)).toBe('2:05')
    expect(mmss(-3)).toBe('0:00')
  })
})

describe('headline', () => {
  const calm = scenarioOf(lab(), true, 1000)
  it('calm and healthy', () => {
    const h = buildHeadline(snap(), calm, th, {}, null)
    expect(h.text).toBe('No attack · 12 tenants healthy')
    expect(h.tone).toBe('ok')
    expect(h.details).toBe('mode TenantGuard · fleet CPU 30 % · fairness 0.97')
  })
  it('attack on K with the culprit throttled and neighbours protected', () => {
    const h = buildHeadline(snap({ K: { level: 'throttle', factor: 0.5, noisy_score: 3 } }), scenarioOf(lab({ attacks: [attack()] }), true, 1000), th, {}, null)
    expect(h.text).toBe('Attack: whale on K · K throttled (50% of plan) · 11 tenants protected')
    expect(h.tone).toBe('warn')
  })
  it('next to the scenario badge the attack is not repeated', () => {
    const h = buildHeadline(snap({ K: { level: 'quarantine', factor: 0.1, noisy_score: 8 } }), scenarioOf(lab({ attacks: [attack()] }), true, 1000), th, {}, null, { includeAttack: false })
    expect(h.text).toBe('K quarantined (10% of plan) · 11 tenants protected')
  })

  it('victims slowed in OFF mode', () => {
    const h = buildHeadline(snap({ A: { p95_ms: 900 }, B: { p95_ms: 200 } }, { mode: 'off' }), scenarioOf(lab({ attacks: [attack()] }), true, 1000), th, {}, null)
    expect(h.text).toBe('Defences OFF · Attack: whale on K · 2 tenants slowed')
    expect(h.tone).toBe('bad')
  })
  it('a slow server with no culprit', () => {
    const s = snap()
    s.servers[4].status = 'slow'
    const ov = { limits: {}, blocks: {}, relocations: [], drains: {}, expires_at: {}, fail_static: false, route_weights: { B: {}, E: {}, G: {}, L: {} } }
    const h = buildHeadline(s, calm, th, {}, ov)
    expect(h.text).toBe('Server w4 slow · 4 tenants moved away · no tenant blamed · 12 tenants protected')
  })
  it('a tenant back at 100 % while its level catches up is not shown "throttled (100% of plan)"', () => {
    const h = buildHeadline(snap({ K: { level: 'throttle', factor: 0.999, noisy_score: 0.4 } }), calm, th, {}, null)
    expect(h.text).toBe('K throttled · 11 tenants protected')
    expect(tileReason(tv('K', { level: 'throttle', factor: 0.999, noisy_score: 0.4 }), undefined)?.text).toBe('recovering · back to its full plan')
    expect(tileReason(tv('K', { level: 'warn', noisy_score: 0.6 }), undefined)?.text).toBe('back within its fair share · still watched')
  })

  it('several tenants acted on are listed briefly', () => {
    const h = buildHeadline(snap({ A: { level: 'throttle', factor: 0.7 }, B: { level: 'throttle', factor: 0.6 }, G: { level: 'warn' } }), calm, th, {}, null)
    expect(h.text).toBe('3 tenants acted on (A, B, G) · 9 tenants protected')
  })
  it('lab offline: says what it observes, not what it assumes', () => {
    const h = buildHeadline(snap(), scenarioOf(null, false, 1000), th, {}, null)
    expect(h.text).toBe('No attack detected · 12 tenants healthy')
  })

  it('no traffic at all', () => {
    const idle = Object.fromEntries(ALL.map((t) => [t, { p95_ms: 0, rps: 0 }]))
    const h = buildHeadline(snap(idle), scenarioOf(lab({ calm_running: false }), true, 1000), th, {}, null)
    expect(h.text).toBe('No traffic · no tenant has traffic')
    expect(h.tone).toBe('idle')
  })
})

describe('tile reason', () => {
  const ev = (o: Partial<IncidentEvent>): IncidentEvent => ({ type: 'incident', incident_id: 'inc-1', stage: 'level_change', ts: 1, tenant: 'K', suspects: [], detail: '', ...o })
  it('normal tiles have no reason', () => {
    expect(tileReason(tv('A'), undefined)).toBeNull()
  })
  it('throttled: how much over its share and how far limits were cut, with the incident link', () => {
    const r = tileReason(tv('K', { level: 'throttle', factor: 0.5, noisy_score: 2.4 }), ev({ level: 'throttle', prev_level: 'warn', detail: 'Escalated warn -> throttle because score >= 2.0' }))
    expect(r).toEqual({ text: 'used 2.4× its fair share · limits cut to 50%', detail: 'Escalated warn -> throttle because score >= 2.0', incidentId: 'inc-1' })
  })
  it('warn: under watch', () => {
    expect(tileReason(tv('K', { level: 'warn', noisy_score: 1.7 }), undefined)?.text).toBe('using 1.7× its fair share · neighbours slower')
  })
  it('recovering after the attack (the case in the user screenshot)', () => {
    const r = tileReason(tv('A', { level: 'throttle', factor: 0.73, noisy_score: 0.4 }), ev({ tenant: 'A', level: 'throttle', prev_level: 'throttle', detail: 'Relaxed limits to 0.73 because calm for 10 s' }))
    expect(r?.text).toBe('recovering · limits back to 73%')
  })
  it('a quarantined attacker is not "recovering" just because its verdict says "until it calms down"', () => {
    const r = tileReason(tv('K', { level: 'quarantine', factor: 0.1, noisy_score: 8.8 }), ev({ stage: 'verdict', level: null, prev_level: null, detail: 'Verdict costly_pattern: quarantined until it calms down.' }))
    expect(r?.text).toBe('used 8.8× its fair share · limits cut to 10%')
  })

  it('a badge always has a reason, even before the incident log loads', () => {
    const r = tileReason(tv('G', { level: 'throttle', factor: 0.5, noisy_score: 2.1 }), undefined)
    expect(r?.text).toBe('used 2.1× its fair share · limits cut to 50%')
    expect(r?.incidentId).toBeNull()
    expect(r?.detail).toContain('throttle')
  })
  it('latest event per tenant from a newest-first log', () => {
    const m = latestByTenant([ev({ ts: 3, tenant: 'K', detail: 'new' }), ev({ ts: 2, tenant: 'K', detail: 'old' }), ev({ ts: 1, tenant: 'A' }), ev({ ts: 0, tenant: null })])
    expect(m.get('K')?.detail).toBe('new')
    expect([...m.keys()]).toEqual(['K', 'A'])
  })
})
