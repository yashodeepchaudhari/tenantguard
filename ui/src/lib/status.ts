// Colour means status. These rules are the only place that decide green / amber / red.
import type { Level, Mode, ServerView, TenantView } from '../types.ts'

export type Status = 'ok' | 'warn' | 'bad' | 'idle'

export interface Thresholds {
  p95WarnMs: number
  p95BadMs: number
}

export function levelStatus(level: Level): Status {
  if (level === 'normal') return 'ok'
  if (level === 'warn') return 'warn'
  return 'bad'
}

/** A tenant tile is red if the tenant is acted on (throttle and above) or its p95 is bad,
 *  amber if under watch or slow, green otherwise, grey with no traffic. */
export function tenantStatus(t: Pick<TenantView, 'level' | 'p95_ms' | 'rps'>, th: Thresholds): Status {
  const byLevel = levelStatus(t.level)
  if (byLevel === 'bad') return 'bad'
  // p95_ms == 0 means nothing completed in the window: no data, not fast (contract QueueStat/TenantView)
  if (t.p95_ms <= 0) return byLevel === 'warn' ? 'warn' : 'idle'
  const byLatency: Status = t.p95_ms >= th.p95BadMs ? 'bad' : t.p95_ms >= th.p95WarnMs ? 'warn' : 'ok'
  return rank(byLatency) > rank(byLevel) ? byLatency : byLevel
}

const RANK: Record<Status, number> = { idle: 0, ok: 1, warn: 2, bad: 3 }
const rank = (s: Status) => RANK[s]

export function worst(statuses: Status[]): Status {
  return statuses.reduce<Status>((a, b) => (rank(b) > rank(a) ? b : a), 'idle')
}

/** Replica cell: slow = red, CPU >= 85 % of its cap = amber, drained (starting / draining) = grey, hatched. */
export function serverStatus(s: ServerView | undefined): Status {
  if (!s || s.status === 'drained') return 'idle'
  if (s.status === 'slow') return 'bad'
  return s.cpu_pct >= 85 ? 'warn' : 'ok'
}

/** Jain fairness: >= 0.9 good, >= 0.75 fair, else bad. */
export function fairnessStatus(f: number): Status {
  return f >= 0.9 ? 'ok' : f >= 0.75 ? 'warn' : 'bad'
}

/** Pool CPU utilization 0..1: headroom matters, so high is bad. */
export function utilizationStatus(u: number): Status {
  return u < 0.7 ? 'ok' : u < 0.85 ? 'warn' : 'bad'
}

export const STATUS_HEX: Record<Status, string> = {
  ok: '#22c55e',
  warn: '#f59e0b',
  bad: '#ef4444',
  idle: '#64748b',
}
export const ACCENT_HEX = '#38bdf8'
export const NEUTRAL_HEX = '#475569'

export const MODE_LABEL: Record<Mode, string> = { off: 'OFF', industry: 'Industry', tenantguard: 'TenantGuard' }

export const LEVEL_LABEL: Record<Level, string> = {
  normal: 'normal',
  warn: 'warn',
  throttle: 'throttle',
  deprioritize: 'deprioritize',
  quarantine: 'quarantine',
  blocked: 'blocked',
}
