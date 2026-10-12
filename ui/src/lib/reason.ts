// The short "why" shown on every non-normal tenant tile (pure, unit tested). A level badge is
// never shown without a reason: the latest IncidentEvent for the tenant gives the incident link
// and the full explanation (tooltip); the visible text is built from the tenant's own numbers.
import type { IncidentEvent, Level, TenantView } from '../types.ts'
import { LEVELS } from '../types.ts'

export interface TileReason {
  text: string
  /** full explanation for the tooltip (latest event detail) */
  detail: string
  incidentId: string | null
}

const rank = (l: Level | null | undefined) => (l ? LEVELS.indexOf(l) : -1)

/** Latest event for each tenant (events are newest first, as in the store). */
export function latestByTenant(events: IncidentEvent[]): Map<string, IncidentEvent> {
  const out = new Map<string, IncidentEvent>()
  for (const e of events) if (e.tenant && !out.has(e.tenant)) out.set(e.tenant, e)
  return out
}

export function tileReason(t: TenantView, latest: IncidentEvent | undefined): TileReason | null {
  if (t.level === 'normal') return null
  const pct = Math.round(t.factor * 100)
  const limited = pct < 100
  const score = `${t.noisy_score.toFixed(1)}× its fair share`
  // recovering = the ladder is stepping down (level fell, or the brain's detail starts with a relax/recover verb);
  // words like "calms down" inside a verdict sentence must not count
  const recovering = !!latest && (rank(latest.prev_level) > rank(latest.level) || /^(relax|recover|cleared|back to)/i.test((latest.detail ?? '').trim()))
  const calmNow = t.noisy_score < 1
  let text: string
  if (t.level === 'warn') text = calmNow ? 'back within its fair share · still watched' : `using ${score} · neighbours slower`
  else if (recovering || calmNow || !limited) text = limited ? `recovering · limits back to ${pct}%` : 'recovering · back to its full plan'
  else text = `used ${score} · limits cut to ${pct}%`
  return {
    text,
    detail: latest?.detail || `${t.name} is at level ${t.level}: noisy score ${t.noisy_score.toFixed(2)}.`,
    incidentId: latest?.incident_id ?? null,
  }
}
