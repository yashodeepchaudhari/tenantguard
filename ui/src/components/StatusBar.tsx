import { config } from '../config.ts'
import { buildHeadline } from '../lib/headline.ts'
import { useLive, useNow } from '../lib/liveContext.ts'
import { scenarioOf, type ScenarioKind } from '../lib/scenario.ts'
import type { Status } from '../lib/status.ts'
import type { StateSnapshot } from '../types.ts'
import { StatusDot } from './StatusDot.tsx'

const TONE: Record<Status, string> = { ok: 'text-ink-100', warn: 'text-warn', bad: 'text-bad', idle: 'text-ink-300' }

const BADGE: Record<ScenarioKind, string> = {
  attack: 'border-bad bg-bad/15 text-bad',
  run: 'border-accent text-accent',
  calm: 'border-ok/60 text-ok',
  idle: 'border-ink-700 text-ink-300',
  unknown: 'border-ink-700 text-ink-500',
}

/** Fixed-height headline: what traffic is running, what is happening, in one line each. */
export function StatusBar({ snapshot }: { snapshot: StateSnapshot }) {
  const { lab, labReachable, barcodes, overrides } = useLive()
  const now = useNow()
  const scenario = scenarioOf(lab, labReachable !== false && lab !== null, now)
  // the badge names the attack, so the headline does not repeat it (keeps the line short at 1366 px)
  const h = buildHeadline(snapshot, scenario, config, barcodes, overrides, { includeAttack: false })
  return (
    <div className="flex min-w-0 flex-1 items-center gap-3" data-testid="status-bar">
      <StatusDot status={h.tone} className="h-4 w-4" />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={`shrink-0 whitespace-nowrap rounded-md border px-2 py-0.5 text-sm font-bold ${BADGE[scenario.kind]}`}
            data-testid="scenario-badge"
            data-kind={scenario.kind}
          >
            {scenario.label}
          </span>
          <h1 className={`min-w-0 truncate text-2xl font-bold leading-tight ${TONE[h.tone]}`} data-testid="status-sentence" title={h.text}>
            {h.text}
          </h1>
        </div>
        <div className="mt-0.5 truncate text-sm text-ink-300" title={snapshot.status_sentence || h.details} data-testid="status-details">
          {h.details}
          {snapshot.status_sentence && <span className="text-ink-500"> · brain: {snapshot.status_sentence}</span>}
        </div>
      </div>
    </div>
  )
}
