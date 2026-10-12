// Case File logic (pure, unit tested): fold an incident's events into one case.
import type { IncidentEvent, Level, Stage, Suspect } from '../types.ts'

/** The story the Case File tells, in order. level_change / closed are shown in the log, not as steps. */
export const CASE_STEPS: Stage[] = ['detected', 'rescued', 'named', 'proving', 'verdict', 'resolved', 'explained']

/** Stages whose suspect list is the investigation's answer (later stages may carry a post-recovery score). */
const NAMING = new Set<Stage>(['detected', 'named', 'proving', 'verdict'])

export interface CaseSummary {
  id: string
  tenant: string | null
  openedAt: number | null
  lastAt: number | null
  closed: boolean
  /** first time each stage was reached */
  reached: Partial<Record<Stage, number>>
  suspects: Suspect[]
  verdict: string | null
  action: string | null
  detail: string
  report: Record<string, unknown> | null
  level: Level | null
  events: IncidentEvent[]
}

export function summarizeCase(id: string, events: IncidentEvent[]): CaseSummary {
  const evs = events.filter((e) => e.incident_id === id).sort((a, b) => a.ts - b.ts)
  const reached: Partial<Record<Stage, number>> = {}
  let suspects: Suspect[] = []
  let namedSuspects: Suspect[] = []
  let verdict: string | null = null
  let action: string | null = null
  let detail = ''
  let report: Record<string, unknown> | null = null
  let level: Level | null = null
  let tenant: string | null = null
  for (const e of evs) {
    if (reached[e.stage] === undefined) reached[e.stage] = e.ts
    if (e.suspects?.length) {
      suspects = e.suspects
      if (NAMING.has(e.stage)) namedSuspects = e.suspects
    }
    if (e.verdict) verdict = e.verdict
    if (e.action) action = e.action
    if (e.detail) detail = e.detail
    if (e.report && typeof e.report === 'object') report = e.report
    if (e.level) level = e.level
    if (e.tenant) tenant = e.tenant
  }
  if (!tenant && suspects.length) tenant = [...suspects].sort((a, b) => b.confidence - a.confidence)[0].tenant
  return {
    id,
    tenant,
    openedAt: evs[0]?.ts ?? null,
    lastAt: evs[evs.length - 1]?.ts ?? null,
    closed: reached.resolved !== undefined || reached.closed !== undefined,
    reached,
    suspects: [...(namedSuspects.length ? namedSuspects : suspects)].sort((a, b) => b.confidence - a.confidence),
    verdict,
    action,
    detail,
    report,
    level,
    events: evs,
  }
}

/** When a step was reached. The Wave 1 brain ends incidents with `closed` and never sends
 *  `resolved`, so `closed` also completes the Resolved step. */
export function stepTime(c: Pick<CaseSummary, 'reached'>, step: Stage): { ts: number; via: Stage } | null {
  const ts = c.reached[step]
  if (ts !== undefined) return { ts, via: step }
  if (step === 'resolved' && c.reached.closed !== undefined) return { ts: c.reached.closed, via: 'closed' }
  return null
}

/** How many of the slow servers are in the suspect's barcode (its 3 servers). */
export function barcodeMatch(barcode: string[], slow: string[]): { matched: string[]; missing: string[]; score: number } {
  const b = new Set(barcode)
  const matched = slow.filter((s) => b.has(s))
  const missing = slow.filter((s) => !b.has(s))
  return { matched, missing, score: slow.length ? matched.length / slow.length : 0 }
}

/** Group incidents (newest first) into cases for the case index. */
export function caseIndex(events: IncidentEvent[]): CaseSummary[] {
  const ids = [...new Set(events.map((e) => e.incident_id))]
  return ids.map((id) => summarizeCase(id, events)).sort((a, b) => (b.lastAt ?? 0) - (a.lastAt ?? 0))
}

export type ReportSection =
  | { title: string; kind: 'text'; text: string }
  | { title: string; kind: 'list'; items: string[] }
  | { title: string; kind: 'table'; rows: [string, string][] }

const label = (k: string) => k.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())

function scalar(v: unknown): string {
  if (v === null || v === undefined) return '–'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(2)
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

/** Render an arbitrary report dict as readable sections: strings → paragraphs, arrays → lists,
 *  objects → key/value tables, loose scalars gathered into one "Facts" table. */
export function reportSections(report: Record<string, unknown>): ReportSection[] {
  const out: ReportSection[] = []
  const facts: [string, string][] = []
  for (const [k, v] of Object.entries(report)) {
    if (typeof v === 'string' && v.length > 40) out.push({ title: label(k), kind: 'text', text: v })
    else if (Array.isArray(v)) out.push({ title: label(k), kind: 'list', items: v.map(scalar) })
    else if (v && typeof v === 'object') out.push({ title: label(k), kind: 'table', rows: Object.entries(v).map(([a, b]) => [label(a), scalar(b)]) })
    else facts.push([label(k), scalar(v)])
  }
  if (facts.length) out.unshift({ title: 'Facts', kind: 'table', rows: facts })
  return out
}
