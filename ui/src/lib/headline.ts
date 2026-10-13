// The Control Room headline, built from structured data instead of the brain's long sentence
// (pure, unit tested). It must always be readable in full (never an ellipsis), so the templates
// are short; the brain sentence stays available as a tooltip on the details line.
//   "No attack · 12 tenants healthy"
//   "whale on K · K throttled 50% · 11 protected"
//   "K watched · 11 protected"
//   "tg-media-1 slow · no one blamed"
import type { StateSnapshot, TenantView } from '../types.ts'
import type { Scenario } from './scenario.ts'
import { tenantStatus, worst, type Status, type Thresholds } from './status.ts'

export interface Headline {
  tone: Status
  /** short, always shown in full (wraps to at most two lines) */
  text: string
  /** small second line (truncated in the UI) */
  details: string
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

const ACTION: Record<string, string> = {
  warn: 'watched',
  throttle: 'throttled',
  deprioritize: 'deprioritized',
  quarantine: 'quarantined',
  blocked: 'blocked',
}

function actionText(t: TenantView): string {
  const verb = ACTION[t.level] ?? t.level
  const pct = Math.round(t.factor * 100)
  return t.level !== 'warn' && pct < 100 ? `${verb} ${pct}%` : verb
}

export function buildHeadline(
  s: StateSnapshot,
  scenario: Scenario,
  th: Thresholds,
  /** false when a scenario badge right next to the headline already names the attack */
  opts: { includeAttack?: boolean } = {},
): Headline {
  const status = new Map(s.tenants.map((t) => [t.tenant, tenantStatus(t, th)]))
  const flagged = s.tenants.filter((t) => t.level !== 'normal').sort((a, b) => b.noisy_score - a.noisy_score)
  const flaggedIds = new Set(flagged.map((t) => t.tenant))
  const others = s.tenants.filter((t) => !flaggedIds.has(t.tenant))
  const healthy = others.filter((t) => status.get(t.tenant) === 'ok').length
  const slow = s.servers.filter((x) => x.status === 'slow').map((x) => x.server)
  const hurt = others.filter((t) => status.get(t.tenant) === 'warn' || status.get(t.tenant) === 'bad').length
  const idle = s.tenants.filter((t) => status.get(t.tenant) === 'idle').length
  const attack = scenario.attacks[0]
  const off = s.mode === 'off'
  const prefix = off ? 'Defences OFF · ' : ''
  const acting = flagged.length > 0 || !!attack || slow.length > 0
  const tail = hurt ? `${hurt} slowed` : acting ? `${healthy} protected` : `${plural(healthy, 'tenant')} healthy`

  const parts: string[] = []
  if (attack && opts.includeAttack !== false) parts.push(`${attack.attack} on ${attack.tenant ?? attack.server}`)
  if (flagged.length === 1) parts.push(`${flagged[0].tenant} ${actionText(flagged[0])}`)
  else if (flagged.length > 1) {
    const top = flagged.slice(0, 3).map((t) => t.tenant).join(', ')
    parts.push(`${flagged.length} acted on: ${top}${flagged.length > 3 ? ` +${flagged.length - 3}` : ''}`)
  }
  if (slow.length && !flagged.length) {
    parts.push(`${slow.slice(0, 2).join(', ')}${slow.length > 2 ? ` +${slow.length - 2}` : ''} slow`)
    parts.push('no one blamed')
  }
  // with the lab offline we cannot know whether an attack is running: say what we observe
  if (!parts.length) parts.push(scenario.kind === 'idle' ? 'No traffic' : scenario.kind === 'unknown' ? 'No attack detected' : 'No attack')
  if (idle !== s.tenants.length) parts.push(tail)
  else if (!parts.includes('No traffic')) parts.push('no traffic')

  const tone: Status =
    hurt || (off && attack) ? worst(others.map((t) => status.get(t.tenant) ?? 'idle')) : flagged.length || slow.length || attack ? 'warn' : idle === s.tenants.length ? 'idle' : 'ok'
  const lent = (s.pool?.projects ?? []).reduce((a, p) => a + p.loan_cpu, 0)
  const details = [
    `mode ${s.mode === 'tenantguard' ? 'TenantGuard' : s.mode === 'industry' ? 'Industry' : 'OFF'}`,
    `pool CPU ${Math.round(s.utilization * 100)} %`,
    ...(lent > 0.005 ? [`${lent.toFixed(2)} cores lent`] : []),
    `fairness ${s.fairness.toFixed(2)}`,
    ...(idle && idle < s.tenants.length ? [`${plural(idle, 'tenant')} without traffic`] : []),
  ].join(' · ')
  return { tone, text: prefix + parts.join(' · '), details }
}
