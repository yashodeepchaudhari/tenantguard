import { describe, expect, it } from 'vitest'
import type { IncidentEvent } from '../types.ts'
import { caseIndex, caseSteps, reportSections, splitRounds, stepTime, summarizeCase } from './case.ts'

const ev = (o: Partial<IncidentEvent>): IncidentEvent => ({
  type: 'incident',
  incident_id: 'inc-1',
  stage: 'detected',
  ts: 1,
  suspects: [],
  detail: '',
  ...o,
})

describe('case file', () => {
  const events = [
    ev({ stage: 'resolved', ts: 30, detail: 'back to baseline', suspects: [{ tenant: 'K', confidence: 0.1 }] }),
    ev({ stage: 'detected', ts: 10, tenant: 'K', detail: 'K is noisy', suspects: [{ tenant: 'K', confidence: 0.4 }] }),
    ev({ stage: 'named', ts: 12, suspects: [{ tenant: 'E', confidence: 0.2 }, { tenant: 'K', confidence: 0.9 }] }),
    ev({ stage: 'level_change', ts: 14, level: 'throttle', prev_level: 'warn', action: 'limits factor=0.5' }),
    ev({ stage: 'verdict', ts: 16, verdict: 'costly_pattern' }),
    ev({ incident_id: 'other', stage: 'detected', ts: 50 }),
  ]

  it('folds events into one case, oldest first', () => {
    const c = summarizeCase('inc-1', events)
    expect(c.events.map((e) => e.ts)).toEqual([10, 12, 14, 16, 30])
    expect(c.tenant).toBe('K')
    expect(c.openedAt).toBe(10)
    expect(c.closed).toBe(true)
    expect(c.reached).toMatchObject({ detected: 10, named: 12, verdict: 16, resolved: 30 })
    expect(c.reached.proving).toBeUndefined()
    expect(c.suspects.map((s) => s.tenant)).toEqual(['K', 'E']) // latest naming-stage list, by confidence
    expect(c.suspects[0].confidence).toBe(0.9) // not the post-recovery 0.1 of 'resolved'
    expect(c.verdict).toBe('costly_pattern')
    expect(c.action).toBe('limits factor=0.5')
    expect(c.level).toBe('throttle')
    expect(c.detail).toBe('back to baseline')
  })

  it("a brain that only sends 'closed' still completes the Resolved step", () => {
    const c = summarizeCase('x', [ev({ incident_id: 'x', stage: 'detected', ts: 1 }), ev({ incident_id: 'x', stage: 'closed', ts: 9 })])
    expect(c.closed).toBe(true)
    expect(stepTime(c, 'resolved')).toEqual({ ts: 9, via: 'closed' })
    expect(stepTime(c, 'detected')).toEqual({ ts: 1, via: 'detected' })
    expect(stepTime(c, 'verdict')).toBeNull()
  })

  it('reads the hash split rounds of an unattributed-work verdict (real brain shape)', () => {
    const report = {
      verdict: 'costly_pattern',
      project: 'free',
      rounds: [
        { round: 1, unattributed_ms_per_s: { 'tg-free-1': 12.5, 'tg-free-2': 580.2 }, halves: [['C'], ['F', 'I']], kept: ['F', 'I'] },
        { round: 2, unattributed_ms_per_s: { 'tg-free-1': 3.1, 'tg-free-2': 590 }, halves: [['I'], ['F']], kept: ['F'] },
      ],
    }
    const r = splitRounds(report)
    expect(r.map((x) => x.kept)).toEqual([['F', 'I'], ['F']])
    expect(r[0].unattributed['tg-free-2']).toBe(580.2)
    expect(r[1].halves).toEqual([['I'], ['F']])
    expect(splitRounds({ summary: 'x' })).toEqual([])
    const c = summarizeCase('inc-F', [
      ev({ incident_id: 'inc-F', stage: 'verdict', ts: 2, tenant: 'F', report }),
      ev({ incident_id: 'inc-F', stage: 'level_change', ts: 3, report: { note: 'later report without rounds' } }),
    ])
    expect(c.split).toHaveLength(2) // kept from the verdict's report
  })

  it('an open case is not closed', () => {
    expect(summarizeCase('other', events).closed).toBe(false)
  })

  it('indexes cases newest first', () => {
    expect(caseIndex(events).map((c) => c.id)).toEqual(['other', 'inc-1'])
  })

  it('renders a report dict as sections', () => {
    const s = reportSections({
      verdict: 'costly_pattern',
      confidence: 0.912,
      summary: 'KaamKaro ran very heavy searches that took most of project mobility’s CPU.',
      evidence: ['E p95 4x baseline', 'G p95 3x baseline'],
      numbers: { victim_p95_before_ms: 240, victim_p95_after_ms: 31 },
    })
    expect(s[0]).toEqual({ title: 'Facts', kind: 'table', rows: [['Verdict', 'costly_pattern'], ['Confidence', '0.91']] })
    expect(s.map((x) => x.kind)).toEqual(['table', 'text', 'list', 'table'])
    expect(s[3]).toMatchObject({ title: 'Numbers', rows: [['Victim p95 before ms', '240'], ['Victim p95 after ms', '31']] })
    expect(reportSections({ unattributed_ms_per_s: { 'tg-free-2': 590, K: 0.1 } })[0]).toMatchObject({ title: 'Unattributed ms per s', rows: [['tg-free-2', '590'], ['K', '0.10']] })
  })
})

describe('case kinds', () => {
  it('tells a tenant case, a hash split and a host-wide slowdown apart, each with its own steps', () => {
    expect(summarizeCase('inc-1', [ev({ tenant: 'K' })]).kind).toBe('tenant')
    const split = summarizeCase('srv-1-3', [
      ev({ incident_id: 'srv-1-3', stage: 'detected', report: { project: 'health', suspects: ['A', 'D'] } }),
      ev({ incident_id: 'srv-1-3', stage: 'proving', ts: 9, report: { project: 'health', round: 1, kept: ['D'] } }),
    ])
    expect(split.kind).toBe('split')
    expect(caseSteps(split.kind).map((s) => s.label)).toEqual(['Hidden CPU', 'Split rounds', 'Verdict', 'Closed'])
    const host = summarizeCase('srv-1-1', [
      ev({ incident_id: 'srv-1-1', stage: 'detected', report: { projects: ['health', 'media'] } }),
      ev({ incident_id: 'srv-1-1', stage: 'verdict', report: { verdict: 'host_fault', projects: ['health', 'media'] } }),
      ev({ incident_id: 'srv-1-1', stage: 'closed', ts: 40 }),
    ])
    expect(host.kind).toBe('host')
    expect(host.verdict).toBe('host_fault')
    expect(caseSteps(host.kind).every((s) => stepTime(host, s.stage) !== null)).toBe(true)   // a finished story
  })
})
