import { useState } from 'react'
import { AttackLauncher } from '../components/AttackLauncher.tsx'
import { CpuShareChart, P95Chart } from '../components/Charts.tsx'
import { FlowView } from '../components/FlowView.tsx'
import { Gauge } from '../components/Gauge.tsx'
import { ImpactPanel } from '../components/ImpactPanel.tsx'
import { IncidentTimeline } from '../components/IncidentTimeline.tsx'
import { NoisyBars } from '../components/NoisyBars.tsx'
import { ServerGrid } from '../components/ServerGrid.tsx'
import { StatusDot } from '../components/StatusDot.tsx'
import { TenantTiles } from '../components/TenantTiles.tsx'
import { config } from '../config.ts'
import { useLive } from '../lib/liveContext.ts'
import { noisyThresholds } from '../lib/thresholds.ts'
import { useBarcodes } from '../lib/useBarcodes.ts'
import { fairnessStatus, tenantStatus, utilizationStatus, worst } from '../lib/status.ts'

const SENTENCE: Record<string, string> = { ok: 'text-ink-100', warn: 'text-warn', bad: 'text-bad', idle: 'text-ink-300' }

type View = 'tiles' | 'flow'
const VIEW_KEY = 'tenantguard.controlRoom.view'

function initialView(): View {
  const q = new URLSearchParams(window.location.search).get('view')
  if (q === 'flow' || q === 'tiles') return q
  try {
    return window.localStorage.getItem(VIEW_KEY) === 'flow' ? 'flow' : 'tiles'
  } catch {
    return 'tiles'
  }
}

function ViewToggle({ view, onChange }: { view: View; onChange: (v: View) => void }) {
  return (
    <div className="flex flex-col gap-1" role="radiogroup" aria-label="Control Room view">
      {(['tiles', 'flow'] as const).map((v) => (
        <button
          key={v}
          role="radio"
          aria-checked={view === v}
          data-testid={`view-${v}`}
          onClick={() => onChange(v)}
          className={`rounded-md px-2 py-0.5 text-sm font-bold ${view === v ? 'bg-accent text-ink-950' : 'border border-ink-700 text-ink-300 hover:text-ink-100'}`}
        >
          {v === 'tiles' ? 'Tiles' : 'Flow'}
        </button>
      ))}
    </div>
  )
}

export function ControlRoom() {
  const { snapshot, history, incidents, conn, overrides } = useLive()
  const barcodes = useBarcodes()
  const [picked, setPicked] = useState<string | null>(null)
  const [view, setViewState] = useState<View>(initialView)
  const setView = (v: View) => {
    setViewState(v)
    try {
      window.localStorage.setItem(VIEW_KEY, v)
    } catch {
      /* storage unavailable: the toggle still works for this visit */
    }
  }

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
  // Highlight the picked tenant's barcode; otherwise follow the noisiest flagged tenant.
  const flagged = [...tenants].filter((t) => t.level !== 'normal').sort((a, b) => b.noisy_score - a.noisy_score)[0]
  const focus = picked ?? flagged?.tenant ?? null
  const focusName = tenants.find((t) => t.tenant === focus)?.name

  return (
    <div className="grid flex-1 grid-cols-12 gap-3 [grid-template-rows:auto_auto_minmax(260px,1fr)_minmax(250px,1fr)] xl:min-h-0 xl:[grid-template-rows:auto_auto_minmax(0,1fr)_minmax(0,1fr)]">
      {/* status sentence + gauges */}
      <section className="panel col-span-12 flex items-center gap-6 py-1">
        <StatusDot status={overall} className="h-5 w-5" />
        <h1 className={`flex-1 text-3xl font-bold leading-tight ${SENTENCE[overall]}`} data-testid="status-sentence">
          {snapshot.status_sentence || '—'}
        </h1>
        <ImpactPanel />
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
        <ViewToggle view={view} onChange={setView} />
      </section>

      {view === 'flow' ? (
        <>
          <section className="panel col-span-12 row-span-2 min-h-[480px] xl:col-span-9 xl:min-h-0">
            <FlowView snapshot={snapshot} overrides={overrides} barcodes={barcodes} />
          </section>
          <section className="panel col-span-12 row-span-2 xl:col-span-3">
            <NoisyBars tenants={tenants} thresholds={noisyThresholds(snapshot)} />
          </section>
        </>
      ) : (
        <>
          <section className="col-span-12">
            <TenantTiles tenants={tenants} selected={picked} onSelect={(id) => setPicked((p) => (p === id ? null : id))} />
          </section>

          <section className="panel col-span-12 xl:col-span-5">
            <P95Chart history={history} tenants={tenants} />
          </section>
          <section className="panel col-span-6 xl:col-span-4">
            <ServerGrid
              servers={snapshot.servers}
              highlight={focus ? (barcodes[focus] ?? []) : []}
              highlightLabel={focus ? `${focus}${focusName ? ` ${focusName}` : ''} · barcode ${(barcodes[focus] ?? []).join(' ')}` : undefined}
              compact
            />
          </section>
          <section className="panel col-span-6 xl:col-span-3">
            <NoisyBars tenants={tenants} thresholds={noisyThresholds(snapshot)} />
          </section>
        </>
      )}

      <section className="panel col-span-12 min-h-0 xl:col-span-5">
        <IncidentTimeline events={incidents} />
      </section>
      <section className="panel col-span-6 min-h-0 xl:col-span-3">
        <CpuShareChart history={history} tenants={tenants} />
      </section>
      <section className="panel col-span-6 min-h-0 xl:col-span-4">
        <AttackLauncher />
      </section>
    </div>
  )
}
