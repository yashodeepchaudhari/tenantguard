// What traffic is running right now, from lab GET /lab/status (pure, unit tested).
import type { AttackStatus, LabStatus } from '../types.ts'

export type ScenarioKind = 'attack' | 'run' | 'calm' | 'idle' | 'unknown'

export interface Scenario {
  kind: ScenarioKind
  /** short badge text, e.g. "Attack: whale on K (0:42 left)" */
  label: string
  /** running attacks, newest first */
  attacks: AttackStatus[]
}

/** m:ss for a countdown */
export function mmss(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/**
 * @param lab latest lab status, null when unknown
 * @param reachable false when the lab did not answer (badge says so instead of guessing)
 * @param now epoch seconds
 */
export function scenarioOf(lab: LabStatus | null, reachable: boolean, now: number): Scenario {
  if (!reachable || !lab) return { kind: 'unknown', label: 'Lab offline: traffic unknown', attacks: [] }
  const attacks = lab.attacks.filter((a) => a.state === 'running' && a.ends_at > now).sort((a, b) => b.started_at - a.started_at)
  if (attacks.length) {
    const a = attacks[0]
    const target = a.tenant ?? a.server ?? '?'
    const more = attacks.length > 1 ? ` +${attacks.length - 1} more` : ''
    return { kind: 'attack', label: `Attack: ${a.attack} on ${target} (${mmss(a.ends_at - now)} left)${more}`, attacks }
  }
  if (lab.run) return { kind: 'run', label: `Scenario run ${lab.run} in progress`, attacks }
  if (lab.calm_running) return { kind: 'calm', label: 'Calm traffic · no attack', attacks }
  return { kind: 'idle', label: 'No traffic', attacks }
}
