import { useState } from 'react'
import { AttackLauncher } from '../components/AttackLauncher.tsx'
import { CpuShareChart, P95Chart } from '../components/Charts.tsx'
import { FlowView } from '../components/FlowView.tsx'
import { Gauge } from '../components/Gauge.tsx'
import { ImpactPanel } from '../components/ImpactPanel.tsx'
import { IncidentTimeline } from '../components/IncidentTimeline.tsx'
import { NoisyBars } from '../components/NoisyBars.tsx'
import { ServerGrid } from '../components/ServerGrid.tsx'
import { StatusBar } from '../components/StatusBar.tsx'
import { TenantTiles } from '../components/TenantTiles.tsx'
import { config } from '../config.ts'
import { useLive } from '../lib/liveContext.ts'
import { latestByTenant } from '../lib/reason.ts'
import { noisyThresholds } from '../lib/thresholds.ts'
import { useBarcodes } from '../lib/useBarcodes.ts'
import { fairnessStatus, utilizationStatus } from '../lib/status.ts'

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
  const latest = latestByTenant(incidents)
  // Highlight the picked tenant's barcode; otherwise follow the noisiest flagged tenant.
  const flagged = [...tenants].filter((t) => t.level !== 'normal').sort((a, b) => b.noisy_score - a.noisy_score)[0]
  const focus = picked ?? flagged?.tenant ?? null
  const focusName = tenants.find((t) => t.tenant === focus)?.name

  // Fixed panel heights and normal page scroll: nothing is squeezed to fit the window, so nothing
  // can collapse or overlap whatever the window size (1366x768 and 1920x1080 are tested).
  return (
    <div className="flex flex-col gap-3" data-testid="control-room">
      <section className="panel flex h-[92px] shrink-0 items-center gap-4 py-1" data-panel="status">
        <StatusBar snapshot={snapshot} />
        <ImpactPanel />
        {/* below 1536 px the gauges' numbers stay readable in the status details line */}
        <div className="hidden shrink-0 items-center gap-4 2xl:flex" data-testid="gauges">
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
        </div>
        <ViewToggle view={view} onChange={setView} />
      </section>

      {view === 'flow' ? (
        <div className="grid grid-cols-12 gap-3">
          <section className="panel col-span-12 h-[560px] xl:col-span-9" data-panel="flow">
            <FlowView snapshot={snapshot} overrides={overrides} barcodes={barcodes} />
          </section>
          <section className="panel col-span-12 h-[560px] xl:col-span-3" data-panel="noisy">
            <NoisyBars tenants={tenants} thresholds={noisyThresholds(snapshot)} />
          </section>
        </div>
      ) : (
        <>
          <section data-panel="tiles">
            <TenantTiles tenants={tenants} latest={latest} selected={picked} onSelect={(id) => setPicked((p) => (p === id ? null : id))} />
            <p className="mt-1 truncate text-xs text-ink-300" data-testid="legend">
              <b>p95</b> = the slowest 5 % of responses · <b>THROTTLE</b> = its limits are reduced · <b>fair share</b> = its tier&apos;s portion of the
              servers it shares · click a tile to see its 3 servers
            </p>
          </section>

          <div className="grid grid-cols-12 gap-3">
            <section className="panel col-span-12 h-[270px] xl:col-span-5" data-panel="p95">
              <P95Chart history={history} tenants={tenants} />
            </section>
            <section className="panel col-span-6 h-[270px] xl:col-span-4" data-panel="servers">
              <ServerGrid
                servers={snapshot.servers}
                highlight={focus ? (barcodes[focus] ?? []) : []}
                highlightLabel={focus ? `${focus}${focusName ? ` ${focusName}` : ''} · barcode ${(barcodes[focus] ?? []).join(' ')}` : undefined}
                compact
              />
            </section>
            <section className="panel col-span-6 h-[270px] xl:col-span-3" data-panel="noisy">
              <NoisyBars tenants={tenants} thresholds={noisyThresholds(snapshot)} />
            </section>
          </div>
        </>
      )}

      <div className="grid grid-cols-12 gap-3">
        <section className="panel col-span-12 h-[300px] xl:col-span-5" data-panel="timeline">
          <IncidentTimeline events={incidents} />
        </section>
        <section className="panel col-span-6 h-[300px] xl:col-span-3" data-panel="cpu">
          <CpuShareChart history={history} tenants={tenants} />
        </section>
        <section className="panel col-span-6 h-[300px] xl:col-span-4" data-panel="launcher">
          <AttackLauncher />
        </section>
      </div>
    </div>
  )
}
