import { useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { ms } from '../lib/format.ts'
import { useBackend } from '../lib/liveContext.ts'
import { groupResults, SCORE_MODES, slowdownStatus, type ScenarioRow } from '../lib/scorecard.ts'
import { MODE_LABEL, STATUS_HEX } from '../lib/status.ts'
import type { RunResult } from '../types.ts'

const AXIS = { stroke: '#64748b', fontSize: 14 }
const NEUTRAL = '#475569'
const NEUTRAL_LIGHT = '#64748b'

const secs = (v: number | null) => (v === null ? '–' : `${v.toFixed(1)} s`)
const count = (v: number | null) => (v === null ? '–' : `${Math.round(v).toLocaleString('en-IN')}`)

function ScenarioChart({ row }: { row: ScenarioRow }) {
  const data = SCORE_MODES.map((m) => {
    const c = row.cells[m]
    return { mode: MODE_LABEL[m], baseline: c.baseline, attack: c.attack, after: c.after, status: slowdownStatus(c.slowdown) }
  })
  return (
    <div className="panel flex h-[300px] flex-col" data-testid={`score-${row.scenario}`}>
      <div className="flex items-baseline justify-between">
        <div className="text-xl font-bold">
          {row.scenario} {row.attacker && <span className="text-base font-normal text-ink-300">· attacker {row.attacker}</span>}
        </div>
        <div className="text-sm text-ink-300">victim p99 · baseline / attack / after</div>
      </div>
      <div className="min-h-0 flex-1">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 22, right: 8, bottom: 0, left: 0 }} barGap={2}>
            <CartesianGrid stroke="#1a2233" vertical={false} />
            <XAxis dataKey="mode" {...AXIS} />
            <YAxis tickFormatter={(v) => (v >= 1000 ? `${v / 1000} s` : `${v} ms`)} width={64} {...AXIS} />
            <Tooltip
              contentStyle={{ background: '#111827', border: '1px solid #273248', borderRadius: 8, fontSize: 14 }}
              formatter={(v: number, n) => [ms(v), n]}
              cursor={{ fill: 'rgba(56,189,248,0.06)' }}
            />
            <Bar dataKey="baseline" name="baseline" fill={NEUTRAL} isAnimationActive={false} />
            <Bar dataKey="attack" name="during attack" isAnimationActive={false}>
              {data.map((d) => (
                <Cell key={d.mode} fill={STATUS_HEX[d.status]} />
              ))}
              <LabelList dataKey="attack" position="top" formatter={(v: number) => (v === null || v === undefined ? '' : ms(v))} fill="#f1f5f9" fontSize={14} />
            </Bar>
            <Bar dataKey="after" name="after" fill={NEUTRAL_LIGHT} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

export function Scorecard() {
  const backend = useBackend()
  const [results, setResults] = useState<RunResult[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = () =>
      backend
        .results()
        .then((r) => {
          if (!alive) return
          setResults(r)
          setError(null)
        })
        .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
    load()
    const id = setInterval(load, 10000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [backend])

  const rows = results ? groupResults(results) : []
  const fixture = backend.kind === 'mock'

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-4">
        <h1 className="text-3xl font-bold">Scorecard</h1>
        <span className="text-base text-ink-300">
          Victim p99 per scenario in OFF / Industry / TenantGuard mode · medians over runs · attack bar colour = slowdown vs baseline (≤ 1.5× green, ≤ 3× amber,
          worse red)
        </span>
      </div>
      {fixture && (
        <div className="rounded-lg border-2 border-warn bg-warn/10 px-3 py-2 text-lg font-bold text-warn" role="note" data-testid="fixture-banner">
          FIXTURE DATA: invented numbers for UI development, not measured. Never put these on a slide.
        </div>
      )}
      {error && (
        <div className="rounded-lg border border-bad bg-bad/10 px-3 py-2 text-lg text-bad" role="alert">
          Lab results unavailable: {error}
        </div>
      )}
      {results !== null && rows.length === 0 && !error && (
        <div className="panel text-lg text-ink-300">No runs yet. Run a scenario with the lab (POST /lab/run) and it appears here.</div>
      )}

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
        {rows.map((r) => (
          <ScenarioChart key={r.scenario} row={r} />
        ))}
      </div>

      {rows.length > 0 && (
        <div className="panel overflow-x-auto p-0">
          <table className="w-full min-w-[1100px] text-base" data-testid="score-table">
            <thead>
              <tr className="border-b border-ink-700 text-right text-sm uppercase tracking-wider text-ink-300">
                <th className="px-3 py-2 text-left">Scenario</th>
                <th className="px-2 py-2 text-left">Mode</th>
                <th className="px-2 py-2">Runs</th>
                <th className="px-2 py-2">p99 baseline</th>
                <th className="px-2 py-2">p99 attack</th>
                <th className="px-2 py-2">p99 after</th>
                <th className="px-2 py-2">Slowdown</th>
                <th className="px-2 py-2">Time to detect</th>
                <th className="px-2 py-2">Time to recover</th>
                <th className="px-2 py-2">Attacker 429s</th>
              </tr>
            </thead>
            <tbody>
              {rows.flatMap((r) =>
                SCORE_MODES.map((m, i) => {
                  const c = r.cells[m]
                  const st = slowdownStatus(c.slowdown)
                  return (
                    <tr key={`${r.scenario}-${m}`} className={`text-right ${i === 2 ? 'border-b-2 border-ink-700' : 'border-b border-ink-800'}`}>
                      <td className="px-3 py-1.5 text-left font-semibold">{i === 0 ? r.scenario : ''}</td>
                      <td className="px-2 py-1.5 text-left">{MODE_LABEL[m]}</td>
                      <td className="px-2 py-1.5 text-ink-300" title={c.runs.map((x) => x.file || x.run_id).join('\n')}>
                        {c.runs.length}
                      </td>
                      <td className="px-2 py-1.5">{c.baseline === null ? '–' : ms(c.baseline)}</td>
                      <td className="px-2 py-1.5 font-bold" style={{ color: st === 'idle' ? undefined : STATUS_HEX[st] }}>
                        {c.attack === null ? '–' : ms(c.attack)}
                      </td>
                      <td className="px-2 py-1.5">{c.after === null ? '–' : ms(c.after)}</td>
                      <td className="px-2 py-1.5">{c.slowdown === null ? '–' : `${c.slowdown.toFixed(1)} ×`}</td>
                      <td className="px-2 py-1.5">{secs(c.detect)}</td>
                      <td className="px-2 py-1.5">{secs(c.recover)}</td>
                      <td className="px-2 py-1.5">{count(c.attacker429)}</td>
                    </tr>
                  )
                }),
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
