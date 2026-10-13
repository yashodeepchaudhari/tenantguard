import { describe, expect, it } from 'vitest'
import type { IncidentEvent, StateSnapshot, TenantView } from '../types.ts'
import {
  busiest,
  cleared,
  eventKey,
  evidence,
  findAnswer,
  innocentsPunished,
  outcome,
  seal,
  sha256,
  stillHot,
} from './challenge.ts'

const ev = (over: Partial<IncidentEvent>): IncidentEvent => ({
  type: 'incident',
  incident_id: 'inc-1',
  stage: 'detected',
  ts: 100,
  tenant: 'K',
  level: null,
  prev_level: null,
  suspects: [],
  verdict: null,
  action: null,
  detail: '',
  report: null,
  ...over,
})

const tv = (tenant: string, over: Partial<TenantView> = {}): TenantView => ({
  tenant,
  name: tenant,
  tier: 'pro',
  p95_ms: 20,
  rps: 10,
  rejected_rps: 0,
  cpu_share: 0.08,
  noisy_score: 1,
  noisy_norm: 0.1,
  level: 'normal',
  factor: 1,
  placement: 'normal',
  ...over,
})

describe('sha256', () => {
  it('matches the standard test vectors', () => {
    expect(sha256('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
    expect(sha256('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
    expect(sha256('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq')).toBe(
      '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1',
    )
    expect(sha256('a'.repeat(1000))).toBe('41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3')
  })

  it('seals tenant|attack|salt and changes with any part', () => {
    const s = seal('K', 'whale', '00ff')
    expect(s).toBe(sha256('K|whale|00ff'))
    expect(seal('J', 'whale', '00ff')).not.toBe(s)
    expect(seal('K', 'flood', '00ff')).not.toBe(s)
    expect(seal('K', 'whale', '00fe')).not.toBe(s)
  })
})

describe('findAnswer', () => {
  const old = ev({
    incident_id: 'inc-old',
    stage: 'named',
    tenant: 'B',
    ts: 50,
  })
  const before = new Set([eventKey(old)])

  it('ignores events from before the round', () => {
    expect(findAnswer([old], before)).toBeNull()
  })

  it('takes the decoder naming a tenant, with its confidence', () => {
    const named = ev({
      stage: 'named',
      tenant: 'K',
      ts: 120,
      suspects: [{ tenant: 'K', confidence: 0.91 }],
      detail: 'K named',
    })
    expect(findAnswer([named, old], before)).toEqual({
      tenant: 'K',
      stage: 'named',
      detail: 'K named',
      confidence: 0.91,
    })
  })

  it('takes the earliest blaming event (store keeps newest first)', () => {
    const first = ev({
      stage: 'level_change',
      tenant: 'K',
      level: 'throttle',
      prev_level: 'warn',
      ts: 110,
    })
    const later = ev({ stage: 'named', tenant: 'J', ts: 130 })
    expect(findAnswer([later, first, old], before)?.tenant).toBe('K')
  })

  it('does not count warnings, de-escalations of an old incident, or events without a tenant', () => {
    const events = [
      ev({
        stage: 'level_change',
        tenant: 'B',
        level: 'deprioritize',
        prev_level: 'quarantine',
        ts: 101,
      }),
      ev({
        stage: 'level_change',
        tenant: 'K',
        level: 'warn',
        prev_level: 'normal',
        ts: 102,
      }),
      ev({ stage: 'detected', tenant: 'K', ts: 103 }),
      ev({ stage: 'named', tenant: null, ts: 104 }),
      ev({ stage: 'resolved', tenant: 'B', ts: 105 }),
    ]
    expect(findAnswer(events, before)).toBeNull()
  })
})

describe('innocents', () => {
  it('lists other tenants that are held back, never the culprit', () => {
    const ts = [
      tv('K', { level: 'quarantine', factor: 0.1 }),
      tv('A', { level: 'throttle', factor: 0.5 }),
      tv('B'),
      tv('C', { factor: 0.8 }),
    ]
    expect(innocentsPunished(ts, 'K').map((t) => t.tenant)).toEqual(['A', 'C'])
    expect(stillHot(ts).map((t) => t.tenant)).toEqual(['K', 'A'])
  })
})

describe('outcome', () => {
  const named = (tenant: string) => ({
    tenant,
    stage: 'named' as const,
    detail: '',
    confidence: 0.9,
  })

  it("blame rounds: right only when the judge's company is named", () => {
    expect(outcome('whale', 'K', named('K'))).toEqual({
      right: true,
      title: '✓ Found it',
    })
    expect(outcome('blackbox_bomb', 'K', named('J')).right).toBe(false)
    expect(outcome('flood', 'K', null)).toEqual({
      right: false,
      title: '✕ Not found',
    })
  })

  it('nobody rounds: right only when no company is blamed, even the busy one', () => {
    expect(outcome('honest_spike', 'J', null)).toEqual({
      right: true,
      title: '✓ Blamed nobody',
    })
    expect(outcome('honest_spike', 'J', named('J')).right).toBe(false)
    expect(outcome('faulty_server', 'w4', named('A')).right).toBe(false)
    expect(outcome('faulty_server', 'w4', null).right).toBe(true)
  })
})

describe('evidence', () => {
  const barcodes = {
    K: ['w1', 'w3', 'w8'],
    A: ['w1', 'w2', 'w4'],
    B: ['w3', 'w5', 'w6'],
    C: ['w0', 'w2', 'w5'],
  }
  const snap = {
    tenants: [
      tv('K', { noisy_score: 3.4, cpu_share: 0.31 }),
      tv('A', { p95_ms: 420 }),
      tv('B', { p95_ms: 90 }),
      tv('C', { p95_ms: 900 }), // slow, but shares no server with K
    ],
    noisy_thresholds: { warn: 1.5 },
  } as unknown as StateSnapshot

  it('reads the score, CPU share and how many of its neighbours are over the SLO', () => {
    expect(evidence(snap, barcodes, 'K', 150, 2)).toEqual({
      tenant: 'K',
      score: 3.4,
      warn: 1.5,
      cpuShare: 0.31,
      neighbours: 2,
      neighboursOver: 1,
      sloMs: 150,
    })
    expect(evidence(snap, barcodes, 'Z', 150, 2)).toBeNull()
    expect(evidence(null, barcodes, 'K', 150, 2)).toBeNull()
  })

  it('names the busiest company', () => {
    expect(busiest(snap.tenants)?.tenant).toBe('K')
    expect(busiest([])).toBeNull()
  })
})

describe('naming versus blaming', () => {
  const before = new Set<string>()
  const named = ev({ stage: 'named', tenant: 'J', ts: 110 })
  const honest = ev({
    stage: 'verdict',
    tenant: 'J',
    ts: 120,
    verdict: 'honest_growth',
  })
  const costly = ev({
    stage: 'verdict',
    tenant: 'J',
    ts: 130,
    verdict: 'costly_pattern',
  })

  it('a company named and then cleared was not blamed', () => {
    expect(findAnswer([honest, named], before, 'name')?.tenant).toBe('J')
    expect(findAnswer([honest, named], before, 'blame')).toBeNull()
    expect(cleared([honest, named], before)).toEqual([{ tenant: 'J', verdict: 'honest_growth' }])
  })

  it('a blaming verdict or a throttle is blame', () => {
    expect(findAnswer([costly, honest, named], before, 'blame')?.stage).toBe('verdict')
    const throttle = ev({
      stage: 'level_change',
      tenant: 'J',
      level: 'throttle',
      prev_level: 'warn',
      ts: 105,
    })
    expect(findAnswer([named, throttle], before, 'blame')?.stage).toBe('level_change')
  })
})
