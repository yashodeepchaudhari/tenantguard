import type { KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'
import { config } from '../config.ts'
import { ms, rps } from '../lib/format.ts'
import { tileReason } from '../lib/reason.ts'
import { tenantStatus, type Status } from '../lib/status.ts'
import type { IncidentEvent, TenantView } from '../types.ts'
import { LevelBadge } from './LevelBadge.tsx'

const FRAME: Record<Status, string> = {
  ok: 'border-ok/60 bg-ok/[0.07]',
  warn: 'border-warn bg-warn/[0.12]',
  bad: 'border-bad bg-bad/[0.16] shadow-[0_0_24px_-6px_rgba(239,68,68,0.7)]',
  idle: 'border-ink-700 bg-ink-900',
}
const NUMBER: Record<Status, string> = { ok: 'text-ink-100', warn: 'text-warn', bad: 'text-bad', idle: 'text-ink-500' }

function Tile({ t, latest, selected, onSelect }: { t: TenantView; latest?: IncidentEvent; selected: boolean; onSelect?: (id: string) => void }) {
  const status = tenantStatus(t, config)
  const [p95Num, p95Unit] = ms(t.p95_ms).split(' ')
  const why = tileReason(t, latest)
  const select = () => onSelect?.(t.tenant)
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      select()
    }
  }
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={select}
      onKeyDown={onKey}
      aria-pressed={selected}
      title="Show this tenant's servers on the grid"
      className={`flex h-[132px] min-w-0 cursor-pointer flex-col overflow-hidden rounded-xl border-2 px-3 py-1.5 text-left transition-colors ${FRAME[status]} ${
        selected ? 'outline outline-[3px] outline-offset-1 outline-accent' : ''
      }`}
      data-testid={`tile-${t.tenant}`}
    >
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-black leading-tight text-ink-100">{t.tenant}</span>
        <span className="min-w-0 flex-1 truncate text-base font-semibold text-ink-100" title={t.name}>
          {t.name}
        </span>
        <span className="text-xs uppercase tracking-wider text-ink-300">{t.tier}</span>
      </div>
      <div className="flex items-baseline justify-between gap-2">
        {t.p95_ms > 0 ? (
          <span className={`whitespace-nowrap text-3xl font-bold leading-tight ${NUMBER[status]}`}>
            {p95Num}
            <span className="unit">{p95Unit} p95</span>
          </span>
        ) : (
          <span className="text-2xl font-bold leading-tight text-ink-500" title="no completed requests in the last 5 s">
            no traffic
          </span>
        )}
        <span className="truncate text-right text-sm leading-tight text-ink-300">
          {rps(t.rps)}
          {t.rejected_rps >= 0.5 && <span title="429 + 503 per second"> · {rps(t.rejected_rps).replace('rps', 'rej/s')}</span>}
        </span>
      </div>
      <div className="mt-0.5 flex items-center justify-between gap-1">
        <LevelBadge level={t.level} />
        {Math.round(t.factor * 100) < 100 && <span className="whitespace-nowrap text-sm font-semibold text-ink-100">{Math.round(t.factor * 100)} % quota</span>}
        {t.placement !== 'normal' && <span className="truncate text-sm text-ink-300">→ {t.placement}</span>}
      </div>
      {why ? (
        <div className="mt-auto flex min-w-0 items-baseline gap-1 text-xs leading-tight" data-testid={`why-${t.tenant}`} title={why.detail}>
          <span className="min-w-0 flex-1 truncate text-ink-100">
            <b className="text-ink-300">why:</b> {why.text}
          </span>
          {why.incidentId && (
            <Link
              to={`/case/${encodeURIComponent(why.incidentId)}`}
              onClick={(e) => e.stopPropagation()}
              className="shrink-0 font-semibold text-accent hover:underline"
              data-testid={`why-link-${t.tenant}`}
            >
              case →
            </Link>
          )}
        </div>
      ) : (
        <div className="mt-auto truncate text-xs leading-tight text-ink-500">{t.p95_ms > 0 ? 'within its fair share' : 'no requests in the last 5 s'}</div>
      )}
    </div>
  )
}

export function TenantTiles({
  tenants,
  latest,
  selected,
  onSelect,
}: {
  tenants: TenantView[]
  /** latest IncidentEvent per tenant (lib/reason.latestByTenant) */
  latest?: Map<string, IncidentEvent>
  selected?: string | null
  onSelect?: (id: string) => void
}) {
  return (
    <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 lg:grid-cols-6">
      {tenants.map((t) => (
        <Tile key={t.tenant} t={t} latest={latest?.get(t.tenant)} selected={selected === t.tenant} onSelect={onSelect} />
      ))}
    </div>
  )
}
