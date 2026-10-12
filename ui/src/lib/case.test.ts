import { describe, expect, it } from 'vitest'
import type { IncidentEvent } from '../types.ts'
import { barcodeMatch, caseIndex, reportSections, summarizeCase } from './case.ts'

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

  it('an open case is not closed', () => {
    expect(summarizeCase('other', events).closed).toBe(false)
  })

  it('indexes cases newest first', () => {
    expect(caseIndex(events).map((c) => c.id)).toEqual(['other', 'inc-1'])
  })

  it('barcode match counts slow servers inside the barcode', () => {
    expect(barcodeMatch(['w1', 'w3', 'w8'], ['w1', 'w3'])).toEqual({ matched: ['w1', 'w3'], missing: [], score: 1 })
    expect(barcodeMatch(['w1', 'w3', 'w8'], ['w1', 'w4']).score).toBe(0.5)
    expect(barcodeMatch(['w1'], []).score).toBe(0)
  })

  it('renders a report dict as sections', () => {
    const s = reportSections({
      verdict: 'costly_pattern',
      confidence: 0.912,
      summary: 'KaamKaro ran very heavy searches that took most of the CPU of w1, w3 and w8.',
      evidence: ['w1 p95 4x peers', 'w3 p95 3x peers'],
      numbers: { victim_p95_before_ms: 240, victim_p95_after_ms: 31 },
    })
    expect(s[0]).toEqual({ title: 'Facts', kind: 'table', rows: [['Verdict', 'costly_pattern'], ['Confidence', '0.91']] })
    expect(s.map((x) => x.kind)).toEqual(['table', 'text', 'list', 'table'])
    expect(s[3]).toMatchObject({ title: 'Numbers', rows: [['Victim p95 before ms', '240'], ['Victim p95 after ms', '31']] })
  })
})
