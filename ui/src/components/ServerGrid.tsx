import { ms } from '../lib/format.ts'
import { FLEET, serverStatus, type Status } from '../lib/status.ts'
import type { ServerView } from '../types.ts'

const FRAME: Record<Status, string> = {
  ok: 'border-ok/50 bg-ok/[0.06]',
  warn: 'border-warn bg-warn/[0.12]',
  bad: 'border-bad bg-bad/[0.18]',
  idle: 'border-ink-700 bg-ink-900',
}
const TEXT: Record<Status, string> = { ok: 'text-ink-100', warn: 'text-warn', bad: 'text-bad', idle: 'text-ink-500' }
const BAR: Record<Status, string> = { ok: 'bg-ok', warn: 'bg-warn', bad: 'bg-bad', idle: 'bg-ink-500' }
const HATCH = { backgroundImage: 'repeating-linear-gradient(45deg, rgba(100,116,139,0.35) 0 6px, transparent 6px 12px)' }

interface Props {
  servers: ServerView[]
  /** servers to outline in the accent colour (a tenant's barcode) */
  highlight?: string[]
  highlightLabel?: string
  compact?: boolean
  title?: string
  /** extra line per server, e.g. the decoder's P(slow) */
  annotate?: Record<string, string>
}

function Cell({ sv, s, lit, compact, note }: { sv: string; s: ServerView | undefined; lit: boolean; compact: boolean; note?: string }) {
  const st = serverStatus(s)
  const drained = s?.status === 'drained'
  return (
    <div
      data-testid={`server-${sv}`}
      data-status={s?.status ?? 'unknown'}
      data-lit={lit ? '1' : '0'}
      className={`relative flex min-h-0 flex-col justify-between rounded-lg border-2 px-2 ${compact ? 'py-1' : 'py-1.5'} ${FRAME[st]} ${
        lit ? 'outline outline-[3px] outline-offset-1 outline-accent' : ''
      }`}
      style={drained ? HATCH : undefined}
      title={s ? `${sv}: ${s.status}, p95 ${ms(s.p95_ms)}, ${s.peer_ratio.toFixed(2)}× peers, CPU ${s.cpu_pct.toFixed(0)} %` : `${sv}: no data`}
    >
      <div className="flex items-baseline justify-between gap-1">
        <span className="text-lg font-black leading-none text-ink-100">{sv}</span>
        <span className={`text-xs font-bold uppercase ${TEXT[st]}`}>{s?.status ?? '–'}</span>
      </div>
      {note && <div className="text-sm font-semibold text-accent">{note}</div>}
      {s && (
        <>
          <div className={`font-bold leading-tight ${compact ? 'text-base' : 'text-xl'} ${TEXT[st]}`}>
            {s.peer_ratio.toFixed(1)}
            <span className="unit">× peers</span>
          </div>
          <div className="flex items-center gap-1.5">
            <div className="h-1.5 flex-1 rounded bg-ink-800">
              <div className={`h-full rounded ${BAR[st]}`} style={{ width: `${Math.min(100, s.cpu_pct)}%` }} />
            </div>
            <span className="whitespace-nowrap text-right text-xs text-ink-300">{s.cpu_pct.toFixed(0)} % CPU</span>
          </div>
        </>
      )}
    </div>
  )
}

/** 3×3 fleet grid (w0..w8). Colour = server status; accent outline = highlighted barcode; hatched = drained. */
export function ServerGrid({ servers, highlight = [], highlightLabel, compact = false, title = 'Servers · 3×3 fleet', annotate }: Props) {
  const by = new Map(servers.map((s) => [s.server, s]))
  const lit = new Set(highlight)
  const extra = servers.filter((s) => !FLEET.includes(s.server))
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-baseline justify-between gap-2">
        <div className="panel-title">{title}</div>
        {highlightLabel && <span className="truncate text-sm font-semibold text-accent">{highlightLabel}</span>}
      </div>
      <div className="grid min-h-0 flex-1 grid-cols-3 grid-rows-3 gap-1.5">
        {FLEET.map((sv) => (
          <Cell key={sv} sv={sv} s={by.get(sv)} lit={lit.has(sv)} compact={compact} note={annotate?.[sv]} />
        ))}
      </div>
      {extra.length > 0 && (
        <div className="mt-1 flex gap-3 text-xs text-ink-300">
          {extra.map((s) => (
            <span key={s.server} data-testid={`server-${s.server}`}>
              {s.server}: <b className={TEXT[serverStatus(s)]}>{s.status}</b> · {s.cpu_pct.toFixed(0)} % CPU
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
