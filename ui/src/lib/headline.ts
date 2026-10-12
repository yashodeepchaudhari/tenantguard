// The one-line Control Room headline, built from structured data instead of the brain's long
// sentence (pure, unit tested). The brain sentence stays available as a tooltip.
//   "No attack · 12 tenants healthy"
//   "Attack: whale on K · 9 tenants protected"
//   "K throttled to 50% of plan · 9 tenants protected"
//   "Server w4 slow · 4 tenants moved away · no tenant blamed"
import type { OverridesView, StateSnapshot, TenantView } from '../types.ts'
import type { Scenario } from './scenario.ts'
import { tenantStatus, worst, type Status, type Thresholds } from './status.ts'

export interface Headline {
  tone: Status
  /** one short line */
  text: string
  /** small second line (truncated in the UI) */
  details: string
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

const ACTION: Record<string, string> = {
  warn: 'under watch',
  throttle: 'throttled',
  deprioritize: 'deprioritized',
  quarantine: 'quarantined',
  blocked: 'blocked',
}

function actionText(t: TenantView): string {
  const verb = ACTION[t.level] ?? t.level
  const pct = Math.round(t.factor * 100)
  return t.level !== 'warn' && pct < 100 ? `${verb} (${pct}% of plan)` : verb
}

export function buildHeadline(
  s: StateSnapshot,
  scenario: Scenario,
  th: Thresholds,
  barcodes: Record<string, string[]>,
  overrides: OverridesView | null,
  /** false when a scenario badge right next to the headline already names the attack */
  opts: { includeAttack?: boolean } = {},
): Headline {
  const status = new Map(s.tenants.map((t) => [t.tenant, tenantStatus(t, th)]))
  const flagged = s.tenants.filter((t) => t.level !== 'normal').sort((a, b) => b.noisy_score - a.noisy_score)
  const flaggedIds = new Set(flagged.map((t) => t.tenant))
  const others = s.tenants.filter((t) => !flaggedIds.has(t.tenant))
  const healthy = others.filter((t) => status.get(t.tenant) === 'ok').length
  const slow = s.servers.filter((x) => x.status === 'slow' && /^w\d+$/.test(x.server)).map((x) => x.server)
  const hurt = others.filter((t) => status.get(t.tenant) === 'warn' || status.get(t.tenant) === 'bad').length
  const idle = s.tenants.filter((t) => status.get(t.tenant) === 'idle').length
  const attack = scenario.attacks[0]
  const off = s.mode === 'off'
  const prefix = off ? 'Defences OFF · ' : ''
  const tail = hurt ? plural(hurt, 'tenant') + ' slowed' : plural(healthy, 'tenant') + (flagged.length || attack || slow.length ? ' protected' : ' healthy')

  const parts: string[] = []
  if (attack && opts.includeAttack !== false) parts.push(`Attack: ${attack.attack} on ${attack.tenant ?? attack.server}`)
  if (flagged.length === 1) parts.push(`${flagged[0].tenant} ${actionText(flagged[0])}`)
  else if (flagged.length > 1) {
    const top = flagged.slice(0, 3).map((t) => t.tenant).join(', ')
    parts.push(`${plural(flagged.length, 'tenant')} acted on (${top}${flagged.length > 3 ? ', …' : ''})`)
  }
  if (slow.length && !flagged.length) {
    const moved = overrides ? Object.keys(overrides.route_weights ?? {}).length : s.tenants.filter((t) => (barcodes[t.tenant] ?? []).some((sv) => slow.includes(sv))).length
    parts.push(`${slow.length === 1 ? `Server ${slow[0]}` : `Servers ${slow.join(', ')}`} slow`)
    if (moved) parts.push(`${plural(moved, 'tenant')} moved away`)
    if (!flagged.length) parts.push('no tenant blamed')
  }
  if (!parts.length) parts.push(scenario.kind === 'idle' ? 'No traffic' : 'No attack')
  parts.push(idle === s.tenants.length ? 'no tenant has traffic' : tail)

  const tone: Status =
    hurt || (off && attack) ? worst(others.map((t) => status.get(t.tenant) ?? 'idle')) : flagged.length || slow.length || attack ? 'warn' : idle === s.tenants.length ? 'idle' : 'ok'
  const details = [
    `mode ${s.mode === 'tenantguard' ? 'TenantGuard' : s.mode === 'industry' ? 'Industry' : 'OFF'}`,
    `fleet CPU ${Math.round(s.utilization * 100)} %`,
    `fairness ${s.fairness.toFixed(2)}`,
    ...(idle && idle < s.tenants.length ? [`${plural(idle, 'tenant')} without traffic`] : []),
  ].join(' · ')
  return { tone, text: prefix + parts.join(' · '), details }
}
