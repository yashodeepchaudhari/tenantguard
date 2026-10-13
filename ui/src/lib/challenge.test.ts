import { describe, expect, it } from 'vitest'
import type { IncidentEvent, ServerView, StateSnapshot, TenantView } from '../types.ts'
import {
  cleared,
  eventKey,
  evidence,
  findAnswer,
  innocentsPunished,
  openIncidents,
  outcome,
  seal,
  sha256,
  stillHot,
  type Answer,
  type RoundStart,
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
  project: 'P1',
  ...over,
})

const sv = (server: string, status: ServerView['status'], project = 'P1'): ServerView => ({
  server,
  p95_ms: 20,
  peer_ratio: 1,
  cpu_pct: 30,
  status,
  project,
})
const at = (brainTs: number, seen: IncidentEvent[] = []): RoundStart => ({ before: new Set(seen.map(eventKey)), brainTs })
const ans = (tenant: string, also: string[] = []): Answer => ({ tenant, stage: 'named', detail: '', confidence: 0.9, also })

describe('sha256 and the seal', () => {
  it('matches the standard test vectors', () => {
    expect(sha256('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
    expect(sha256('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
    expect(sha256('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq')).toBe(
      '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1',
    )
    expect(sha256('a'.repeat(1000))).toBe('41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3')
  })

  it('seals target|kind|salt and changes with any part', () => {
    const s = seal('K', 'whale', '00ff')
    expect(s).toBe(sha256('K|whale|00ff'))
    expect(seal('J', 'whale', '00ff')).not.toBe(s)
    expect(seal('K', 'flood', '00ff')).not.toBe(s)
    expect(seal('K', 'whale', '00fe')).not.toBe(s)
  })
})

describe('events of the round', () => {
  it('ignores events seen before the start and events the brain stamped before it', () => {
    const old = ev({ incident_id: 'inc-old', stage: 'named', tenant: 'B', ts: 150 })
    const backfill = ev({ incident_id: 'inc-old2', stage: 'named', tenant: 'C', ts: 50 }) // arrives late, but is old
    expect(findAnswer([backfill, old], at(100, [old]))).toBeNull()
  })

  it('an earlier incident finishing its proof after the start does not count (stamped before, arrives after)', () => {
    const lateVerdict = ev({ incident_id: 'inc-k', stage: 'verdict', tenant: 'K', verdict: 'costly_pattern', ts: 98 })
    expect(findAnswer([lateVerdict], at(100), 'blame')).toBeNull()
  })

  it('takes the earliest naming event and lists the other culprits it names', () => {
    const throttle = ev({ stage: 'level_change', tenant: 'K', level: 'throttle', prev_level: 'warn', ts: 110 })
    const later = ev({ stage: 'named', tenant: 'J', ts: 130 })
    expect(findAnswer([later, throttle], at(100))?.tenant).toBe('K')
    const named = ev({
      stage: 'named',
      tenant: 'J',
      ts: 120,
      suspects: [
        { tenant: 'J', confidence: 0.7 },
        { tenant: 'K', confidence: 0.6 },
        { tenant: 'A', confidence: 0.1 },
      ],
      detail: 'J and K named',
    })
    expect(findAnswer([named], at(100))).toEqual({
      tenant: 'J',
      stage: 'named',
      detail: 'J and K named',
      confidence: 0.7,
      also: ['K'],
    })
  })

  it('does not count warnings, de-escalations, closing stages or events without a tenant', () => {
    const events = [
      ev({ stage: 'level_change', tenant: 'B', level: 'deprioritize', prev_level: 'quarantine', ts: 101 }),
      ev({ stage: 'level_change', tenant: 'K', level: 'warn', prev_level: 'normal', ts: 102 }),
      ev({ stage: 'detected', tenant: 'K', ts: 103 }),
      ev({ stage: 'named', tenant: null, ts: 104 }),
      ev({ stage: 'resolved', tenant: 'B', ts: 105 }),
    ]
    expect(findAnswer(events, at(100))).toBeNull()
  })

  it('naming is not blaming: named then cleared is fine, a blaming verdict or a throttle is blame', () => {
    const named = ev({ stage: 'named', tenant: 'J', ts: 110 })
    const honest = ev({ stage: 'verdict', tenant: 'J', ts: 120, verdict: 'honest_growth' })
    const costly = ev({ stage: 'verdict', tenant: 'J', ts: 130, verdict: 'costly_pattern' })
    expect(findAnswer([honest, named], at(100), 'name')?.tenant).toBe('J')
    expect(findAnswer([honest, named], at(100), 'blame')).toBeNull()
    expect(cleared([honest, named], at(100))).toEqual([{ tenant: 'J', verdict: 'honest_growth' }])
    expect(findAnswer([costly, honest, named], at(100), 'blame')?.stage).toBe('verdict')
    const throttle = ev({ stage: 'level_change', tenant: 'J', level: 'throttle', prev_level: 'warn', ts: 105 })
    expect(findAnswer([named, throttle], at(100), 'blame')?.stage).toBe('level_change')
  })

  it('keeps two verdicts of one incident in the same tick apart', () => {
    const a = ev({ stage: 'verdict', tenant: 'J', verdict: 'costly_pattern', ts: 120 })
    const b = ev({ stage: 'verdict', tenant: 'K', verdict: 'honest_growth', ts: 120 })
    expect(eventKey(a)).not.toBe(eventKey(b))
  })
})

describe('is the fleet calm', () => {
  it('open incidents: newest event not resolved or explained, and recent on the brain clock', () => {
    const events = [
      ev({ incident_id: 'a', stage: 'resolved', ts: 190 }),
      ev({ incident_id: 'a', stage: 'named', ts: 180 }),
      ev({ incident_id: 'b', stage: 'proving', ts: 185 }),
      ev({ incident_id: 'c', stage: 'named', ts: 100 }), // quiet for 100 s
      ev({ incident_id: 'd', stage: 'explained', ts: 195 }),
    ]
    expect(openIncidents(events, 200)).toEqual(['b'])
  })

  it('held back = a ladder action or a throttle factor', () => {
    const ts = [
      tv('K', { level: 'quarantine', factor: 0.1 }),
      tv('A', { level: 'throttle', factor: 0.5 }),
      tv('B'),
      tv('C', { factor: 0.8 }),
    ]
    expect(innocentsPunished(ts, 'K').map((t) => t.tenant)).toEqual(['A', 'C'])
    expect(stillHot(ts).map((t) => t.tenant)).toEqual(['K', 'A', 'C'])
  })
})

describe('outcome', () => {
  it('right only when the judge company is named first and nobody else is held back', () => {
    expect(outcome('K', ans('K'), [])).toEqual({ right: true, title: '✓ Found it' })
    expect(outcome('K', ans('K'), ['K'])).toEqual({ right: true, title: '✓ Found it' })
    expect(outcome('K', ans('K'), ['K', 'B']).right).toBe(false)
    expect(outcome('K', ans('J'), []).title).toBe('✕ Wrong company')
    expect(outcome('K', ans('J', ['K']), []).title).toBe('✕ Named J first (K too)')
    expect(outcome('K', null, [])).toEqual({ right: false, title: '✕ Not found' })
  })
})

describe('evidence', () => {
  const snap = {
    tenants: [
      tv('K', { noisy_score: 3.4, cpu_share: 0.31 }),
      tv('A', { p95_ms: 420 }),
      tv('B', { p95_ms: 90 }),
      tv('C', { p95_ms: 900, project: 'P2' }), // slow, but in another project
    ],
    servers: [sv('r1', 'slow'), sv('r2', 'healthy'), sv('r9', 'slow', 'P2')],
    noisy_thresholds: { warn: 1.5 },
  } as unknown as StateSnapshot

  it('reads the score, CPU share, its project neighbours over the SLO, and its slow replicas', () => {
    expect(evidence(snap, 'K', 150, 2)).toEqual({
      tenant: 'K',
      project: 'P1',
      score: 3.4,
      warn: 1.5,
      cpuShare: 0.31,
      neighbours: 2,
      neighboursOver: 1,
      sloMs: 150,
      slow: ['r1'],
    })
    expect(evidence(snap, 'Z', 150, 2)).toBeNull()
    expect(evidence(null, 'K', 150, 2)).toBeNull()
  })
})
