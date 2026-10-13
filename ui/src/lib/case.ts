// Case File logic (pure, unit tested): fold an incident's events into one case.
import type { IncidentEvent, Level, Stage, Suspect } from '../types.ts'

/** The story the Case File tells, in order. level_change / closed are shown in the log, not as steps. */
export const CASE_STEPS: Stage[] = ['detected', 'rescued', 'named', 'proving', 'verdict', 'resolved', 'explained']

/** tenant: the ladder's case about one tenant; split: a hash split test of one project (hidden CPU);
 *  host: a host-wide slowdown nobody is blamed for. The brain gives the last two `srv-` ids. */
export type CaseKind = 'tenant' | 'split' | 'host'

export interface CaseStep {
  stage: Stage
  label: string
}

const STEPS: Record<CaseKind, CaseStep[]> = {
  tenant: [
    { stage: 'detected', label: 'Detected' },
    { stage: 'rescued', label: 'Rescued' },
    { stage: 'named', label: 'Named' },
    { stage: 'proving', label: 'Proving' },
    { stage: 'verdict', label: 'Verdict' },
    { stage: 'resolved', label: 'Resolved' },
    { stage: 'explained', label: 'Explained' },
  ],
  split: [
    { stage: 'detected', label: 'Hidden CPU' },
    { stage: 'proving', label: 'Split rounds' },
    { stage: 'verdict', label: 'Verdict' },
    { stage: 'resolved', label: 'Closed' },
  ],
  host: [
    { stage: 'detected', label: 'Slowdown' },
    { stage: 'verdict', label: 'No blame' },
    { stage: 'resolved', label: 'Recovered' },
  ],
}

export const caseSteps = (kind: CaseKind): CaseStep[] => STEPS[kind]

export function caseKind(id: string, events: IncidentEvent[]): CaseKind {
  if (!id.startsWith('srv-')) return 'tenant'
  const host = events.some((e) => e.report?.verdict === 'host_fault' || Array.isArray(e.report?.projects))
  return host ? 'host' : 'split'
}

/** Stages whose suspect list is the investigation's answer (later stages may carry a post-recovery score). */
const NAMING = new Set<Stage>(['detected', 'named', 'proving', 'verdict'])

export interface CaseSummary {
  id: string
  kind: CaseKind
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
  /** the hash split's rounds, from the latest report that has them */
  split: SplitRound[]
  level: Level | null
  events: IncidentEvent[]
}

export interface SplitRound {
  round: number
  halves: string[][]
  kept: string[]
  /** replica -> unexplained CPU ms/s measured in this round */
  unattributed: Record<string, number>
}

const isMap = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const strs = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])

/** The brain's hash-split evidence ({rounds: [{round, halves, kept, unattributed_ms_per_s}]}), oldest first. */
export function splitRounds(report: Record<string, unknown> | null | undefined): SplitRound[] {
  const rounds = report?.rounds
  if (!Array.isArray(rounds)) return []
  return rounds.filter(isMap).map((r, i) => ({
    round: typeof r.round === 'number' ? r.round : i + 1,
    halves: Array.isArray(r.halves) ? r.halves.map(strs) : [],
    kept: strs(r.kept),
    unattributed: isMap(r.unattributed_ms_per_s)
      ? Object.fromEntries(Object.entries(r.unattributed_ms_per_s).filter((e): e is [string, number] => typeof e[1] === 'number'))
      : {},
  }))
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
  let split: SplitRound[] = []
  let level: Level | null = null
  let tenant: string | null = null
  for (const e of evs) {
    if (reached[e.stage] === undefined) reached[e.stage] = e.ts
    if (e.suspects?.length) {
      suspects = e.suspects
      if (NAMING.has(e.stage)) namedSuspects = e.suspects
    }
    if (e.verdict) verdict = e.verdict
    else if (typeof e.report?.verdict === 'string') verdict = e.report.verdict // findings carry it in the report
    if (e.action) action = e.action
    if (e.detail) detail = e.detail
    if (e.report && typeof e.report === 'object') {
      report = e.report
      const rounds = splitRounds(e.report)
      if (rounds.length) split = rounds
    }
    if (e.level) level = e.level
    if (e.tenant) tenant = e.tenant
  }
  if (!tenant && suspects.length) tenant = [...suspects].sort((a, b) => b.confidence - a.confidence)[0].tenant
  return {
    id,
    kind: caseKind(id, evs),
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
    split,
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

/** Group incidents (newest first) into cases for the case index. */
export function caseIndex(events: IncidentEvent[]): CaseSummary[] {
  const ids = [...new Set(events.map((e) => e.incident_id))]
  return ids.map((id) => summarizeCase(id, events)).sort((a, b) => (b.lastAt ?? 0) - (a.lastAt ?? 0))
}

export type ReportSection =
  | { title: string; kind: 'text'; text: string }
  | { title: string; kind: 'list'; items: string[] }
  | { title: string; kind: 'table'; rows: [string, string][] }

// ids (tg-media-1, K) stay as they are; snake_case keys become words
const label = (k: string) => (/^tg-|^[A-Z]$/.test(k) ? k : k.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase()))

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
