// Scorecard aggregation (pure, unit tested): RunResult files grouped by scenario × mode.
import type { AttackName, Mode, RunResult } from '../types.ts'
import type { Status } from './status.ts'

export const SCORE_MODES: Mode[] = ['off', 'industry', 'tenantguard']

export interface Cell {
  scenario: AttackName
  mode: Mode
  runs: RunResult[]
  /** medians over the runs (null when no run has the value) */
  baseline: number | null
  attack: number | null
  after: number | null
  detect: number | null
  recover: number | null
  attacker429: number | null
  /** attack-phase victim p99 / baseline */
  slowdown: number | null
}

export interface ScenarioRow {
  scenario: AttackName
  attacker: string | null
  cells: Record<Mode, Cell>
}

export function median(xs: (number | null | undefined)[]): number | null {
  const v = xs.filter((x): x is number => typeof x === 'number' && Number.isFinite(x)).sort((a, b) => a - b)
  if (!v.length) return null
  const m = Math.floor(v.length / 2)
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2
}

export function groupResults(results: RunResult[]): ScenarioRow[] {
  const byScenario = new Map<AttackName, RunResult[]>()
  for (const r of results) {
    const list = byScenario.get(r.scenario) ?? []
    list.push(r)
    byScenario.set(r.scenario, list)
  }
  const rows: ScenarioRow[] = []
  for (const [scenario, runs] of byScenario) {
    const cells = {} as Record<Mode, Cell>
    for (const mode of SCORE_MODES) {
      const rs = runs.filter((r) => r.mode === mode).sort((a, b) => a.started_at - b.started_at)
      const baseline = median(rs.map((r) => r.victim_p99_ms?.baseline))
      const attack = median(rs.map((r) => r.victim_p99_ms?.attack))
      cells[mode] = {
        scenario,
        mode,
        runs: rs,
        baseline,
        attack,
        after: median(rs.map((r) => r.victim_p99_ms?.after)),
        detect: median(rs.map((r) => r.time_to_detect_s)),
        recover: median(rs.map((r) => r.time_to_recover_s)),
        attacker429: median(rs.map((r) => r.attacker_429)),
        slowdown: baseline && attack !== null ? attack / baseline : null,
      }
    }
    const attacker = runs.find((r) => r.attacker)?.attacker ?? null
    rows.push({ scenario, attacker, cells })
  }
  return rows.sort((a, b) => a.scenario.localeCompare(b.scenario))
}

/** Victim protection status from the attack-phase slowdown (p99 attack / baseline). */
export function slowdownStatus(x: number | null): Status {
  if (x === null) return 'idle'
  return x <= 1.5 ? 'ok' : x <= 3 ? 'warn' : 'bad'
}
