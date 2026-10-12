import { STATUS_HEX, type Status } from '../lib/status.ts'

interface Props {
  label: string
  /** 0..1 */
  value: number | null
  display: string
  unit?: string
  status: Status
  hint?: string
}

/** Semicircle gauge. Arc colour = status. */
export function Gauge({ label, value, display, unit, status, hint }: Props) {
  const v = value === null || !Number.isFinite(value) ? 0 : Math.max(0, Math.min(1, value))
  const r = 52
  const len = Math.PI * r
  return (
    <div className="flex flex-col items-center" title={hint}>
      <svg viewBox="0 0 128 72" className="h-16 w-32" role="img" aria-label={`${label} ${display}${unit ?? ''}`}>
        <path d="M 12 66 A 52 52 0 0 1 116 66" fill="none" stroke="#273248" strokeWidth="12" strokeLinecap="round" />
        <path
          d="M 12 66 A 52 52 0 0 1 116 66"
          fill="none"
          stroke={value === null ? STATUS_HEX.idle : STATUS_HEX[status]}
          strokeWidth="12"
          strokeLinecap="round"
          strokeDasharray={`${len * v} ${len}`}
          style={{ transition: 'stroke-dasharray 400ms ease' }}
        />
        <text x="64" y="62" textAnchor="middle" className="fill-ink-100" style={{ fontSize: 26, fontWeight: 700 }}>
          {display}
          {unit && (
            <tspan className="fill-ink-300" style={{ fontSize: 14, fontWeight: 500 }}>
              {' '}
              {unit}
            </tspan>
          )}
        </text>
      </svg>
      <div className="-mt-1 text-sm font-semibold uppercase tracking-wider text-ink-300">{label}</div>
    </div>
  )
}
