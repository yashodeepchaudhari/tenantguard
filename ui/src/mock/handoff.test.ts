import { describe, expect, it } from 'vitest'
import { MockPlatform } from './sim.ts'
import { mockCases } from './handoff.ts'
import type { IncidentEvent } from '../types.ts'

describe('mock handoff bridge', () => {
  it('opens one case per acted-on incident, with owner, pending action and evidence link', () => {
    const p = new MockPlatform()
    const events: IncidentEvent[] = []
    let t = 1000
    for (let i = 0; i < 60; i++) {
      t += 0.5
      if (i === 5) p.startAttack({ attack: 'whale', tenant: 'K', intensity: 2, duration_s: 60 }, t)
      events.push(...p.tick(t).incidents)
    }
    const cases = mockCases(events, t, 'http://ui:5173')
    expect(cases).toHaveLength(1)
    const c = cases[0]
    expect(c.case_id).toBe('INC-0001')
    expect(c.tenant).toBe('K')
    expect(c.owner).toBeTruthy()
    expect(c.actions[0].state).toBe('pending')
    expect(c.actions[0].deadline_ist).toMatch(/^\d\d:\d\d:\d\d$/)
    expect(c.context.evidence_link).toBe(`http://ui:5173/case/${c.incident_id}`)
  })

  it('a plain warn opens no case', () => {
    const warnOnly: IncidentEvent[] = [
      { type: 'incident', incident_id: 'i', stage: 'detected', ts: 1, suspects: [], detail: '' },
      { type: 'incident', incident_id: 'i', stage: 'level_change', ts: 1, level: 'warn', prev_level: 'normal', suspects: [], detail: '' },
    ]
    expect(mockCases(warnOnly, 10)).toEqual([])
  })
})
