import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { LevelBadge } from '../components/LevelBadge.tsx'
import { ServerGrid } from '../components/ServerGrid.tsx'
import { barcodeMatch, CASE_STEPS, reportSections, summarizeCase, type CaseSummary, type ReportSection } from '../lib/case.ts'
import { clock } from '../lib/format.ts'
import { useBackend, useLive, useNow } from '../lib/liveContext.ts'
import { STATUS_HEX } from '../lib/status.ts'
import { useBarcodes } from '../lib/useBarcodes.ts'
import type { IncidentEvent, OverridesView, Stage } from '../types.ts'

const STEP_LABEL: Partial<Record<Stage, string>> = {
  detected: 'Detected',
  rescued: 'Rescued',
  named: 'Named',
  proving: 'Proving',
  verdict: 'Verdict',
  resolved: 'Resolved',
  explained: 'Explained',
}

const key = (e: IncidentEvent) => `${e.stage}|${e.ts}|${e.level ?? ''}`

function Stepper({ c }: { c: CaseSummary }) {
  const t0 = c.openedAt ?? 0
  return (
    <ol className="grid grid-cols-7 gap-2" data-testid="case-steps">
      {CASE_STEPS.map((s, i) => {
        const ts = c.reached[s]
        const done = ts !== undefined
        return (
          <li
            key={s}
            data-reached={done ? '1' : '0'}
            className={`rounded-lg border-2 px-3 py-2 ${done ? 'border-accent bg-accent/10' : 'border-dashed border-ink-700 text-ink-500'}`}
          >
            <div className="text-xs font-semibold uppercase tracking-wider text-ink-300">step {i + 1}</div>
            <div className={`text-lg font-bold ${done ? 'text-ink-100' : ''}`}>{STEP_LABEL[s]}</div>
            <div className="font-mono text-sm">{done ? `${clock(ts)} · +${(ts - t0).toFixed(1)} s` : 'not reached'}</div>
          </li>
        )
      })}
    </ol>
  )
}

function Suspects({ c }: { c: CaseSummary }) {
  if (!c.suspects.length) return <div className="text-base text-ink-500">No suspects named yet.</div>
  return (
    <ul className="space-y-2" data-testid="suspects">
      {c.suspects.map((s, i) => {
        const pct = Math.round(Math.max(0, Math.min(1, s.confidence)) * 100)
        const colour = i === 0 ? STATUS_HEX.bad : '#64748b'
        return (
          <li key={s.tenant} className="flex items-center gap-3">
            <span className="w-6 text-2xl font-black">{s.tenant}</span>
            <div className="h-4 flex-1 rounded bg-ink-800">
              <div className="h-full rounded" style={{ width: `${pct}%`, background: colour }} />
            </div>
            <span className="w-16 text-right text-lg font-bold" style={{ color: i === 0 ? colour : undefined }}>
              {pct} %
            </span>
          </li>
        )
      })}
    </ul>
  )
}

function Report({ sections }: { sections: ReportSection[] }) {
  return (
    <div className="space-y-3" data-testid="report">
      {sections.map((sec) => (
        <section key={sec.title}>
          <h3 className="mb-1 text-sm font-semibold uppercase tracking-wider text-ink-300">{sec.title}</h3>
          {sec.kind === 'text' && <p className="text-lg leading-snug">{sec.text}</p>}
          {sec.kind === 'list' && (
            <ul className="list-disc space-y-0.5 pl-5 text-base">
              {sec.items.map((it, i) => (
                <li key={i}>{it}</li>
              ))}
            </ul>
          )}
          {sec.kind === 'table' && (
            <table className="w-full text-base">
              <tbody>
                {sec.rows.map(([k, v]) => (
                  <tr key={k} className="border-b border-ink-800">
                    <td className="py-0.5 pr-3 text-ink-300">{k}</td>
                    <td className="py-0.5 font-semibold">{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ))}
    </div>
  )
}

/** What the template narrator would say when the brain has not written a report yet. */
function Placeholder({ c, name }: { c: CaseSummary; name: string }) {
  const steps = CASE_STEPS.filter((s) => c.reached[s] !== undefined).map((s) => STEP_LABEL[s]?.toLowerCase())
  return (
    <div className="space-y-2 text-lg leading-snug text-ink-300" data-testid="report-placeholder">
      <p>
        No report yet: the brain writes one when the case reaches <b>explained</b>.
      </p>
      <p className="text-ink-100">
        {name} {c.closed ? 'was' : 'is'} under investigation since {c.openedAt ? clock(c.openedAt) : '–'}
        {c.level ? `, currently at level ${c.level}` : ''}. Steps so far: {steps.join(' → ') || 'none'}.
        {c.verdict ? ` Verdict: ${c.verdict}.` : ''}
        {c.action ? ` Last action: ${c.action}.` : ''}
      </p>
    </div>
  )
}

function ttlFor(ov: OverridesView | null, tenant: string | null): { key: string; expires: number } | null {
  if (!ov || !tenant) return null
  const hits = Object.entries(ov.expires_at).filter(([k]) => k.endsWith(`:${tenant}`) || k.includes(`:${tenant}/`))
  if (!hits.length) return null
  const [k, expires] = hits.sort((a, b) => b[1] - a[1])[0]
  return { key: k, expires }
}

export function CaseFile() {
  const { incidentId = '' } = useParams()
  const backend = useBackend()
  const { incidents, snapshot } = useLive()
  const barcodes = useBarcodes()
  const now = useNow()
  const [fetched, setFetched] = useState<IncidentEvent[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ov, setOv] = useState<OverridesView | null>(null)

  useEffect(() => {
    let alive = true
    const load = () =>
      backend
        .incident(incidentId)
        .then((evs) => {
          if (!alive) return
          setFetched(evs)
          setError(null)
        })
        .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
    const loadOv = () =>
      backend
        .overrides()
        .then((o) => alive && setOv(o))
        .catch(() => alive && setOv(null))
    load()
    loadOv()
    const a = setInterval(load, 3000)
    const b = setInterval(loadOv, 2000)
    return () => {
      alive = false
      clearInterval(a)
      clearInterval(b)
    }
  }, [backend, incidentId])

  // REST history + anything that arrived on the live stream since
  const events = useMemo(() => {
    const seen = new Set<string>()
    return [...(fetched ?? []), ...incidents.filter((e) => e.incident_id === incidentId)].filter((e) => {
      const k = key(e)
      if (seen.has(k)) return false
      seen.add(k)
      return true
    })
  }, [fetched, incidents, incidentId])

  const c = summarizeCase(incidentId, events)

  if (!c.events.length) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-ink-300">
        <div className="text-3xl font-semibold">{fetched === null && !error ? 'Loading case…' : `No events for incident ${incidentId}`}</div>
        {error && <div className="text-lg text-bad">Brain: {error}</div>}
        <Link to="/case" className="btn-ghost">
          All cases
        </Link>
      </div>
    )
  }

  const tenantView = snapshot?.tenants.find((t) => t.tenant === c.tenant)
  const name = tenantView ? `${tenantView.name} (${c.tenant})` : (c.tenant ?? 'unknown tenant')
  const barcode = c.tenant ? (barcodes[c.tenant] ?? []) : []
  const slow = (snapshot?.servers ?? []).filter((s) => s.status === 'slow').map((s) => s.server)
  const match = barcodeMatch(barcode, slow)
  const ttl = ttlFor(ov, c.tenant)
  const duration = (c.closed ? (c.lastAt ?? now) : now) - (c.openedAt ?? now)

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-4">
        <Link to="/case" className="text-base text-ink-300 hover:text-accent">
          ← all cases
        </Link>
        <h1 className="text-3xl font-bold">
          Case file <span className="font-mono text-2xl text-ink-300">{c.id}</span>
        </h1>
        <span
          data-testid="case-status"
          className={`rounded-lg border-2 px-3 py-0.5 text-lg font-bold uppercase ${c.closed ? 'border-ok text-ok' : 'border-bad text-bad'}`}
        >
          {c.closed ? 'resolved' : 'open'}
        </span>
        <span className="text-lg">
          Suspect <b className="text-2xl">{name}</b>
        </span>
        {c.level && <LevelBadge level={c.level} />}
        <span className="ml-auto text-base text-ink-300">
          opened {c.openedAt ? clock(c.openedAt) : '–'} · {Math.max(0, Math.round(duration))} s {c.closed ? 'total' : 'so far'}
        </span>
      </div>

      <Stepper c={c} />

      <div className="grid grid-cols-12 gap-3">
        <section className="panel col-span-12 flex flex-col gap-3 xl:col-span-4">
          <div>
            <div className="panel-title">Verdict & action</div>
            <div className="text-3xl font-bold" data-testid="verdict">
              {c.verdict ?? <span className="text-ink-500">pending</span>}
            </div>
            <div className="mt-1 text-base">
              <span className="text-ink-300">last action </span>
              {c.action ? <code className="rounded bg-ink-950 px-1.5 text-accent">{c.action}</code> : <span className="text-ink-500">no action yet</span>}
            </div>
            <div className="mt-1 text-lg" data-testid="ttl">
              {ttl ? (
                <>
                  Override <span className="font-mono text-base text-ink-300">{ttl.key}</span> expires in{' '}
                  <b className={ttl.expires - now < 10 ? 'text-warn' : ''}>{Math.max(0, Math.round(ttl.expires - now))} s</b>
                </>
              ) : (
                <span className="text-ink-500">no active override for {c.tenant ?? 'this tenant'}</span>
              )}
            </div>
          </div>
          <div>
            <div className="panel-title">Suspects · confidence</div>
            <Suspects c={c} />
          </div>
          <div>
            <div className="panel-title">Why (latest detail)</div>
            <p className="text-lg leading-snug" data-testid="detail">
              {c.detail || '–'}
            </p>
          </div>
        </section>

        <section className="panel col-span-12 flex min-h-[420px] flex-col md:col-span-6 xl:col-span-4">
          <ServerGrid
            servers={snapshot?.servers ?? []}
            highlight={barcode}
            highlightLabel={c.tenant ? `${c.tenant}'s barcode ${barcode.join(' ')}` : undefined}
            title="Barcode match · live grid"
          />
          <div className="mt-2 text-lg" data-testid="barcode-match">
            {slow.length === 0 ? (
              <span className="text-ink-300">No server is slow right now{c.closed ? ' (case resolved)' : ''}.</span>
            ) : (
              <>
                Slow now: <b>{slow.join(' ')}</b> ·{' '}
                <b className={match.score >= 0.99 ? 'text-bad' : 'text-warn'}>
                  {match.matched.length} of {slow.length}
                </b>{' '}
                inside {c.tenant}&apos;s barcode
                {match.missing.length > 0 && <span className="text-ink-300"> (not: {match.missing.join(' ')})</span>}
              </>
            )}
          </div>
        </section>

        <section className="panel col-span-12 md:col-span-6 xl:col-span-4">
          <div className="panel-title">Report</div>
          {c.report ? <Report sections={reportSections(c.report)} /> : <Placeholder c={c} name={name} />}
        </section>
      </div>

      <section className="panel">
        <div className="panel-title">Event log · {c.events.length} events</div>
        <ol className="space-y-1" data-testid="case-log">
          {c.events.map((e) => (
            <li key={key(e)} className="grid grid-cols-[6rem_5rem_7.5rem_1fr] items-start gap-2 rounded bg-ink-800/60 px-2 py-1">
              <span className="font-mono text-sm text-ink-300">{clock(e.ts)}</span>
              <span className="font-mono text-sm text-ink-300">+{(e.ts - (c.openedAt ?? e.ts)).toFixed(1)} s</span>
              <span className="text-sm font-bold uppercase">{e.stage.replace('_', ' ')}</span>
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  {e.prev_level && e.level && e.prev_level !== e.level && (
                    <>
                      <LevelBadge level={e.prev_level} className="opacity-60" /> → <LevelBadge level={e.level} />
                    </>
                  )}
                  {e.verdict && <span className="rounded bg-ink-700 px-1.5 text-sm font-semibold">{e.verdict}</span>}
                  {e.action && <code className="rounded bg-ink-950 px-1.5 text-sm text-accent">{e.action}</code>}
                </div>
                {e.detail && <div className="text-base">{e.detail}</div>}
              </div>
            </li>
          ))}
        </ol>
      </section>
    </div>
  )
}
