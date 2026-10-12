import { useMemo } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { config } from '../config.ts'
import { ms } from '../lib/format.ts'
import type { HistoryPoint } from '../lib/store.ts'
import { NEUTRAL_HEX, STATUS_HEX, tenantStatus } from '../lib/status.ts'
import type { TenantView } from '../types.ts'

const AXIS = { stroke: '#64748b', fontSize: 14 }
const TOOLTIP = {
  contentStyle: { background: '#111827', border: '1px solid #273248', borderRadius: 8, fontSize: 14 },
  labelStyle: { color: '#cbd5e1' },
}
const LOG_TICKS = [10, 30, 100, 300, 1000, 3000]

/** Line colour = tenant's current status; healthy tenants stay neutral so trouble stands out. */
function lineColour(t: TenantView) {
  const s = tenantStatus(t, config)
  return s === 'ok' || s === 'idle' ? NEUTRAL_HEX : STATUS_HEX[s]
}

function useRows(history: HistoryPoint[], key: 'p95' | 'cpu') {
  return useMemo(() => {
    if (!history.length) return []
    const last = history[history.length - 1].ts
    return history.map((p) => {
      const row: Record<string, number> = { t: Math.round((p.ts - last) * 10) / 10 }
      for (const [k, v] of Object.entries(p[key])) row[k] = key === 'cpu' ? v * 100 : Math.max(1, v)
      return row
    })
  }, [history, key])
}

export function P95Chart({ history, tenants }: { history: HistoryPoint[]; tenants: TenantView[] }) {
  const rows = useRows(history, 'p95')
  // draw healthy tenants first so flagged lines sit on top
  const order = [...tenants].sort((a, b) => Number(lineColour(a) !== NEUTRAL_HEX) - Number(lineColour(b) !== NEUTRAL_HEX))
  const hotAll = tenants.filter((t) => lineColour(t) !== NEUTRAL_HEX).sort((a, b) => b.p95_ms - a.p95_ms)
  const hot = hotAll.slice(0, 6)
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-baseline justify-between">
        <div className="panel-title">p95 latency per tenant · last 60 s</div>
        <div className="flex flex-wrap gap-x-3 text-sm">
          {hot.length === 0 ? (
            <span className="text-ink-300">all within SLO</span>
          ) : (
            <>
              {hot.map((t) => (
                <span key={t.tenant} style={{ color: lineColour(t) }} className="font-semibold">
                  {t.tenant} {ms(t.p95_ms)}
                </span>
              ))}
              {hotAll.length > hot.length && <span className="text-ink-300">+{hotAll.length - hot.length} more</span>}
            </>
          )}
        </div>
      </div>
      <div className="min-h-0 flex-1">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 6, right: 12, bottom: 0, left: 4 }}>
            <CartesianGrid stroke="#1a2233" />
            <XAxis dataKey="t" type="number" domain={[-60, 0]} ticks={[-60, -45, -30, -15, 0]} tickFormatter={(v) => `${v} s`} {...AXIS} />
            <YAxis
              scale="log"
              domain={[10, 3000]}
              allowDataOverflow
              ticks={LOG_TICKS}
              tickFormatter={(v) => (v >= 1000 ? `${v / 1000} s` : `${v} ms`)}
              width={64}
              {...AXIS}
            />
            <ReferenceLine y={config.p95WarnMs} stroke={STATUS_HEX.warn} strokeDasharray="6 4" strokeOpacity={0.6} />
            <ReferenceLine y={config.p95BadMs} stroke={STATUS_HEX.bad} strokeDasharray="6 4" strokeOpacity={0.6} />
            <Tooltip {...TOOLTIP} labelFormatter={(v) => `${v} s`} formatter={(v: number, name) => [ms(v), name]} />
            {order.map((t) => {
              const c = lineColour(t)
              const hotLine = c !== NEUTRAL_HEX
              return (
                <Line
                  key={t.tenant}
                  dataKey={t.tenant}
                  stroke={c}
                  strokeWidth={hotLine ? 3 : 1.5}
                  strokeOpacity={hotLine ? 1 : 0.7}
                  dot={false}
                  isAnimationActive={false}
                />
              )
            })}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

const GREYS = ['#334155', '#3f4d63', '#475569', '#52627a']

export function CpuShareChart({ history, tenants }: { history: HistoryPoint[]; tenants: TenantView[] }) {
  const rows = useRows(history, 'cpu')
  const top = [...tenants].sort((a, b) => b.cpu_share - a.cpu_share)[0]
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-baseline justify-between">
        <div className="panel-title">CPU share · stacked</div>
        {top && (
          <span className="text-sm text-ink-300">
            top: <b className="text-ink-100">{top.tenant}</b> {(top.cpu_share * 100).toFixed(0)} %
          </span>
        )}
      </div>
      <div className="min-h-0 flex-1">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
            <XAxis dataKey="t" type="number" domain={[-60, 0]} ticks={[-60, -30, 0]} tickFormatter={(v) => `${v} s`} {...AXIS} />
            <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} tickFormatter={(v) => `${v} %`} width={52} {...AXIS} />
            <Tooltip {...TOOLTIP} labelFormatter={(v) => `${v} s`} formatter={(v: number, name) => [`${v.toFixed(1)} %`, name]} />
            {tenants.map((t, i) => {
              const c = lineColour(t)
              const fill = c === NEUTRAL_HEX ? GREYS[i % GREYS.length] : c
              return (
                <Area
                  key={t.tenant}
                  dataKey={t.tenant}
                  stackId="cpu"
                  stroke="#0b0f17"
                  strokeWidth={1}
                  fill={fill}
                  fillOpacity={c === NEUTRAL_HEX ? 0.9 : 0.85}
                  isAnimationActive={false}
                />
              )
            })}
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
