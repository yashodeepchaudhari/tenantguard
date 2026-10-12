// Quota edit helpers for the Tenants page (pure, unit tested).
import type { Quota, QuotaPatch } from '../types.ts'

export type QuotaKey = keyof Quota
export const FIELDS: { key: QuotaKey; label: string; unit: string; int: boolean }[] = [
  { key: 'rate_rps', label: 'Rate', unit: 'rps', int: false },
  { key: 'burst', label: 'Burst', unit: 'req', int: true },
  { key: 'cost_ms_per_s', label: 'CPU cost', unit: 'ms/s', int: false },
  { key: 'concurrency', label: 'Concurrency', unit: 'in-flight', int: true },
]

export const fmt = (v: number, int: boolean) => (int ? String(Math.round(v)) : Number.isInteger(v) ? String(v) : v.toFixed(1))

/** Validate the edit form; returns the minimal patch (changed fields only) or an error. */
export function buildPatch(current: Quota, draft: Record<QuotaKey, string>): { patch: QuotaPatch } | { error: string } {
  const patch: QuotaPatch = {}
  for (const f of FIELDS) {
    const raw = draft[f.key].trim()
    const v = Number(raw)
    if (raw === '' || !Number.isFinite(v) || v <= 0) return { error: `${f.label} must be a number > 0` }
    if (f.int && !Number.isInteger(v)) return { error: `${f.label} must be a whole number` }
    if (v !== current[f.key]) patch[f.key] = v
  }
  return { patch }
}
