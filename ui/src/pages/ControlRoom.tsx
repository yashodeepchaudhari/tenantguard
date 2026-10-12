import { AttackLauncher } from '../components/AttackLauncher.tsx'
import { CpuShareChart, P95Chart } from '../components/Charts.tsx'
import { Gauge } from '../components/Gauge.tsx'
import { IncidentTimeline } from '../components/IncidentTimeline.tsx'
import { NoisyBars } from '../components/NoisyBars.tsx'
import { StatusDot } from '../components/StatusDot.tsx'
import { TenantTiles } from '../components/TenantTiles.tsx'
import { config } from '../config.ts'
import { useLive } from '../lib/liveContext.ts'
import { fairnessStatus, tenantStatus, utilizationStatus, worst } from '../lib/status.ts'

const SENTENCE: Record<string, string> = { ok: 'text-ink-100', warn: 'text-warn', bad: 'text-bad', idle: 'text-ink-300' }

export function ControlRoom() {
  const { snapshot, history, incidents, conn } = useLive()

  if (!snapshot) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-ink-300">
        <div className="text-3xl font-semibold">{conn === 'closed' ? 'Brain unreachable' : 'Waiting for the first state snapshot…'}</div>
        <div className="text-lg">
          Event stream: <code className="text-accent">{config.brainUrl.replace(/^http/, 'ws')}/ws/events</code>
        </div>
        <div className="text-base">Set VITE_BRAIN_URL in ui/.env, or run with ?mock=1 for fixture replay.</div>
      </div>
    )
  }

  const tenants = snapshot.tenants
  const overall = worst(tenants.map((t) => tenantStatus(t, config)))

  return (
    <div className="grid flex-1 grid-cols-12 gap-3 [grid-template-rows:auto_auto_minmax(260px,1fr)_minmax(250px,1fr)] xl:min-h-0 xl:[grid-template-rows:auto_auto_minmax(0,1fr)_minmax(0,1fr)]">
      {/* status sentence + gauges */}
      <section className="panel col-span-12 flex items-center gap-6 py-1">
        <StatusDot status={overall} className="h-5 w-5" />
        <h1 className={`flex-1 text-3xl font-bold leading-tight ${SENTENCE[overall]}`} data-testid="status-sentence">
          {snapshot.status_sentence || '—'}
        </h1>
        <Gauge
          label="Fairness (Jain)"
          value={snapshot.fairness}
          display={snapshot.fairness.toFixed(2)}
          status={fairnessStatus(snapshot.fairness)}
          hint="Jain index over completed rps / weight; 1.00 = perfectly fair"
        />
        <Gauge
          label="Fleet CPU"
          value={snapshot.utilization}
          display={(snapshot.utilization * 100).toFixed(0)}
          unit="%"
          status={utilizationStatus(snapshot.utilization)}
          hint="Share of the w0..w8 CPU limit in use"
        />
      </section>

      <section className="col-span-12">
        <TenantTiles tenants={tenants} />
      </section>

      <section className="panel col-span-12 xl:col-span-6">
        <P95Chart history={history} tenants={tenants} />
      </section>
      <section className="panel col-span-6 xl:col-span-3">
        <CpuShareChart history={history} tenants={tenants} />
      </section>
      <section className="panel col-span-6 xl:col-span-3">
        <NoisyBars tenants={tenants} />
      </section>

      <section className="panel col-span-12 min-h-0 xl:col-span-8">
        <IncidentTimeline events={incidents} />
      </section>
      <section className="panel col-span-12 min-h-0 xl:col-span-4">
        <AttackLauncher />
      </section>
    </div>
  )
}
