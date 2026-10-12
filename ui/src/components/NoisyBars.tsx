import type { NoisyThresholds } from '../lib/thresholds.ts'
import { levelStatus, STATUS_HEX } from '../lib/status.ts'
import type { TenantView } from '../types.ts'

/** Bars use noisy_norm (0..1); the raw score is printed as text. Bar colour = level status. */
export function NoisyBars({ tenants, thresholds }: { tenants: TenantView[]; thresholds: NoisyThresholds }) {
  const q = thresholds.quarantine
  const marks = [
    { at: thresholds.warn / q, label: `warn ${thresholds.warn}×`, colour: STATUS_HEX.warn },
    { at: thresholds.throttle / q, label: `throttle ${thresholds.throttle}×`, colour: STATUS_HEX.bad },
  ]
  const sorted = [...tenants].sort((a, b) => b.noisy_norm - a.noisy_norm || a.tenant.localeCompare(b.tenant))
  return (
    <div className="flex h-full flex-col">
      <div className="panel-title">Noisy score · × fair share</div>
      <div className="relative flex min-h-0 flex-1 flex-col justify-between overflow-hidden">
        {sorted.map((t) => {
          const s = levelStatus(t.level)
          const colour = s === 'ok' ? '#475569' : STATUS_HEX[s]
          const w = Math.max(0, Math.min(1, t.noisy_norm)) * 100
          return (
            <div key={t.tenant} className="flex min-h-0 items-center gap-2 leading-none" data-testid={`noisy-${t.tenant}`}>
              <span className="w-5 text-base font-bold leading-none text-ink-100">{t.tenant}</span>
              <div className="relative h-3 flex-1 rounded bg-ink-800">
                <div className="h-full rounded transition-[width] duration-300" style={{ width: `${w}%`, background: colour }} />
                {marks.map((m) => (
                  <div key={m.label} className="absolute top-[-2px] h-4 w-px" style={{ left: `${m.at * 100}%`, background: m.colour, opacity: 0.8 }} />
                ))}
              </div>
              <span className="w-14 text-right text-sm font-semibold leading-none" style={{ color: s === 'ok' ? '#cbd5e1' : colour }}>
                {t.noisy_score.toFixed(1)} ×
              </span>
            </div>
          )
        })}
      </div>
      <div className="mt-1 flex gap-3 truncate whitespace-nowrap pl-7 text-xs text-ink-300">
        {marks.map((m) => (
          <span key={m.label}>
            <span className="mr-1 inline-block h-2.5 w-px align-middle" style={{ background: m.colour }} />
            {m.label}
          </span>
        ))}
        <span>full bar = quarantine {q}×</span>
      </div>
    </div>
  )
}
