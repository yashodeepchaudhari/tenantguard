import { Link } from 'react-router-dom'
import { clock } from '../lib/format.ts'
import { levelStatus, STATUS_HEX } from '../lib/status.ts'
import type { IncidentEvent, Level, Stage } from '../types.ts'
import { LevelBadge } from './LevelBadge.tsx'

const STAGE_LABEL: Record<Stage, string> = {
  detected: 'Detected',
  level_change: 'Level',
  rescued: 'Rescued',
  named: 'Named',
  proving: 'Proving',
  verdict: 'Verdict',
  resolved: 'Resolved',
  explained: 'Explained',
  closed: 'Closed',
}

function stageColour(e: IncidentEvent): string {
  if (e.stage === 'resolved' || e.stage === 'closed' || e.stage === 'rescued') return STATUS_HEX.ok
  if (e.stage === 'level_change' && e.level) {
    const s = levelStatus(e.level)
    return s === 'ok' ? STATUS_HEX.ok : STATUS_HEX[s]
  }
  if (e.stage === 'detected') return STATUS_HEX.warn
  return '#cbd5e1'
}

function Transition({ prev, next }: { prev?: Level | null; next?: Level | null }) {
  if (!next) return null
  return (
    <span className="flex items-center gap-1">
      {prev && prev !== next && (
        <>
          <LevelBadge level={prev} className="opacity-60" />
          <span className="text-ink-300">→</span>
        </>
      )}
      <LevelBadge level={next} />
    </span>
  )
}

export function IncidentTimeline({ events, max = 40 }: { events: IncidentEvent[]; max?: number }) {
  const shown = events.slice(0, max)
  const open = new Set<string>()
  const closed = new Set<string>()
  for (const e of events) {
    if (e.stage === 'resolved' || e.stage === 'closed') closed.add(e.incident_id)
    else if (!closed.has(e.incident_id)) open.add(e.incident_id)
  }
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-baseline justify-between">
        <div className="panel-title">Incident timeline</div>
        <span className="text-sm text-ink-300">
          {open.size} open · {events.length} events
        </span>
      </div>
      {shown.length === 0 ? (
        <div className="flex flex-1 items-center justify-center text-lg text-ink-500">No incidents. Every tenant is within its fair share.</div>
      ) : (
        <ol className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
          {shown.map((e) => (
            <li key={`${e.incident_id}-${e.stage}-${e.ts}-${e.level ?? ''}`}>
              <Link
                to={`/case/${encodeURIComponent(e.incident_id)}`}
                title={`Open case file ${e.incident_id}`}
                data-testid="timeline-row"
                className="grid grid-cols-[5.5rem_2rem_6.5rem_1fr] items-start gap-2 rounded-lg border-l-4 bg-ink-800/60 px-2 py-1.5 hover:bg-ink-700/70"
                style={{ borderLeftColor: stageColour(e) }}
              >
              <span className="font-mono text-sm text-ink-300">{clock(e.ts)}</span>
              <span className="text-lg font-black leading-tight text-ink-100">{e.tenant ?? '–'}</span>
              <span className="text-sm font-bold uppercase leading-6" style={{ color: stageColour(e) }}>
                {STAGE_LABEL[e.stage] ?? e.stage}
              </span>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  {e.stage === 'level_change' && <Transition prev={e.prev_level} next={e.level} />}
                  {e.verdict && <span className="rounded bg-ink-700 px-1.5 text-sm font-semibold">{e.verdict}</span>}
                  {e.action && <code className="rounded bg-ink-950 px-1.5 text-sm text-accent">{e.action}</code>}
                </div>
                {e.detail && <div className="text-base leading-snug text-ink-100">{e.detail}</div>}
              </div>
              </Link>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
