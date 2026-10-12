import { summarizeImpact } from '../lib/impact.ts'
import { useLive, useStore } from '../lib/liveContext.ts'

function rs(v: number): string {
  if (v < 100) return `₹${v.toFixed(2)}`
  return `₹${Math.round(v).toLocaleString('en-IN')}`
}

function lakh(v: number): string {
  if (v >= 1e7) return `₹${(v / 1e7).toFixed(2)} Cr`
  if (v >= 1e5) return `₹${(v / 1e5).toFixed(1)} L`
  return `₹${Math.round(v).toLocaleString('en-IN')}`
}

const dur = (s: number) => (s >= 120 ? `${(s / 60).toFixed(1)} min` : `${Math.round(s)} s`)

const TIP =
  'Since this page opened. Revenue protected = Σ (seconds a threatened customer stayed within SLO in TenantGuard mode) × its monthly revenue ÷ 30 days. ' +
  'Threatened = shares a server with a flagged tenant, sits on a slow server, or is being routed around one. ' +
  'Innocents penalised = tenants throttled while their own noisy score was below warn.'

/** ₹ impact: money and customers protected, innocents penalised (must stay 0). */
export function ImpactPanel() {
  const { impact, revenue } = useLive()
  const store = useStore()
  const s = summarizeImpact(impact, revenue)
  const innocentOk = s.innocents.length === 0
  return (
    <div className="flex items-stretch gap-4 border-l border-ink-700 pl-4" title={TIP} data-testid="impact">
      <div className="flex flex-col justify-center">
        <div className="text-xs font-semibold uppercase tracking-wider text-ink-300">Revenue protected</div>
        <div className="text-3xl font-black leading-tight text-ok" data-testid="impact-revenue">
          {rs(s.revenueRs)}
        </div>
        <div className="text-xs text-ink-300">
          {s.customers.length} customer{s.customers.length === 1 ? '' : 's'} shielded · {lakh(s.customersMonthlyRs)}/month
        </div>
      </div>
      <div className="flex flex-col justify-center">
        <div className="text-xs font-semibold uppercase tracking-wider text-ink-300">SLO breach avoided</div>
        <div className="text-3xl font-black leading-tight text-ink-100" data-testid="impact-avoided">
          {dur(s.avoidedS)}
        </div>
        <div className="text-xs text-ink-300">suffered {dur(s.sufferedS)} · over {dur(s.sinceS)}</div>
      </div>
      <div className="flex flex-col justify-center">
        <div className="text-xs font-semibold uppercase tracking-wider text-ink-300">Innocents penalised</div>
        <div className={`text-3xl font-black leading-tight ${innocentOk ? 'text-ok' : 'text-bad'}`} data-testid="impact-innocents">
          {s.innocents.length}
        </div>
        <button className="text-left text-xs text-ink-300 hover:text-accent" onClick={() => store.resetImpact()} title="Restart the impact counters">
          {innocentOk ? 'nobody punished · reset' : `${s.innocents.join(' ')} · reset`}
        </button>
      </div>
    </div>
  )
}
