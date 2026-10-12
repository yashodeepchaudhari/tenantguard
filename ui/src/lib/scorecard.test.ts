import { describe, expect, it } from 'vitest'
import { RESULTS_FIXTURE } from '../mock/results.ts'
import type { RunResult } from '../types.ts'
import { groupResults, median, slowdownStatus } from './scorecard.ts'

const run = (o: Partial<RunResult>): RunResult => ({
  run_id: 'r',
  scenario: 'whale',
  mode: 'off',
  attacker: 'K',
  started_at: 1,
  victim_p99_ms: { baseline: 20, attack: 200, after: 25 },
  victim_p99_by_tenant: {},
  time_to_detect_s: null,
  time_to_recover_s: null,
  attacker_429: 0,
  attacker_503: 0,
  file: '',
  ...o,
})

describe('scorecard', () => {
  it('median ignores nulls', () => {
    expect(median([3, null, 1, 2])).toBe(2)
    expect(median([4, 1])).toBe(2.5)
    expect(median([null, undefined])).toBeNull()
  })

  it('groups by scenario x mode with medians and slowdown', () => {
    const rows = groupResults([
      run({ run_id: 'a', mode: 'off', victim_p99_ms: { baseline: 20, attack: 400, after: 30 } }),
      run({ run_id: 'b', mode: 'off', victim_p99_ms: { baseline: 20, attack: 200, after: 30 } }),
      run({ run_id: 'c', mode: 'tenantguard', time_to_detect_s: 3, victim_p99_ms: { baseline: 20, attack: 25, after: 21 } }),
      run({ run_id: 'd', scenario: 'flood', mode: 'industry' }),
    ])
    expect(rows.map((r) => r.scenario)).toEqual(['flood', 'whale'])
    const whale = rows[1]
    expect(whale.cells.off.runs).toHaveLength(2)
    expect(whale.cells.off.attack).toBe(300)
    expect(whale.cells.off.slowdown).toBe(15)
    expect(whale.cells.tenantguard.detect).toBe(3)
    expect(whale.cells.industry.runs).toHaveLength(0)
    expect(whale.cells.industry.attack).toBeNull()
  })

  it('colours protection by slowdown', () => {
    expect(slowdownStatus(1.2)).toBe('ok')
    expect(slowdownStatus(2.5)).toBe('warn')
    expect(slowdownStatus(10)).toBe('bad')
    expect(slowdownStatus(null)).toBe('idle')
  })

  it('fixtures cover every mode for each scenario', () => {
    for (const row of groupResults(RESULTS_FIXTURE)) for (const m of ['off', 'industry', 'tenantguard'] as const) expect(row.cells[m].runs.length).toBeGreaterThan(0)
  })
})
