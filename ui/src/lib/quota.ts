// Quota display for the Tenants page (pure). A tenant's quota comes from its project's tier
// (tenantguard.yaml tiers); the brain's ladder may scale it down (effective quota).
import type { Quota } from '../types.ts'

export type QuotaKey = keyof Quota
export const FIELDS: { key: QuotaKey; label: string; unit: string; int: boolean }[] = [
  { key: 'rate_rps', label: 'Rate', unit: 'rps', int: false },
  { key: 'burst', label: 'Burst', unit: 'req', int: true },
  { key: 'concurrency', label: 'Concurrency', unit: 'in-flight', int: true },
]

export const fmt = (v: number, int: boolean) => (int ? String(Math.round(v)) : Number.isInteger(v) ? String(v) : v.toFixed(1))
