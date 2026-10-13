import { ms } from '../lib/format.ts'
import { serverStatus, type Status } from '../lib/status.ts'
import type { PoolView, ProjectView, ReplicaView, ServerView } from '../types.ts'

const FRAME: Record<Status, string> = {
  ok: 'border-ok/50 bg-ok/[0.06]',
  warn: 'border-warn bg-warn/[0.12]',
  bad: 'border-bad bg-bad/[0.18]',
  idle: 'border-ink-700 bg-ink-900',
}
const TEXT: Record<Status, string> = { ok: 'text-ink-100', warn: 'text-warn', bad: 'text-bad', idle: 'text-ink-500' }
const BAR: Record<Status, string> = { ok: 'bg-ok', warn: 'bg-warn', bad: 'bg-bad', idle: 'bg-ink-500' }
const HATCH = { backgroundImage: 'repeating-linear-gradient(45deg, rgba(100,116,139,0.35) 0 6px, transparent 6px 12px)' }

const cores = (nano: number) => (nano ? `${(nano / 1e9).toFixed(2)} core` : 'no cap')

function Replica({ r, s, pinned }: { r: ReplicaView; s: ServerView | undefined; pinned: string[] }) {
  const st = r.state === 'ready' ? serverStatus(s) : 'idle'
  const word = r.state !== 'ready' ? r.state : s?.status === 'slow' ? 'slow' : null
  return (
    <div
      data-testid={`replica-${r.id}`}
      data-state={r.state}
      data-status={s?.status ?? 'unknown'}
      className={`relative flex min-w-[7.5rem] flex-1 flex-col justify-between overflow-hidden rounded-lg border-2 px-2 py-1 ${FRAME[st]}`}
      style={r.state !== 'ready' ? HATCH : undefined}
      title={`${r.id}: ${r.state}${s ? `, p95 ${ms(s.p95_ms)}, ${s.peer_ratio.toFixed(2)}× project peers` : ''}, CPU cap ${cores(r.nano_cpus)}, ${r.mem_mb} MB${r.borrowed ? ', borrowed' : ''}`}
    >
      <div className="flex min-w-0 items-baseline justify-between gap-1">
        <span className="truncate font-mono text-sm font-bold text-ink-100">{r.id.replace(/^tg-/, '')}</span>
        {word && <span className={`shrink-0 text-xs font-bold uppercase ${TEXT[st]}`}>{word}</span>}
        {r.borrowed && !word && <span className="shrink-0 rounded border border-accent px-1 text-[10px] font-bold uppercase text-accent">loan</span>}
      </div>
      <div className="flex items-center gap-1.5">
        <div className="h-1.5 flex-1 rounded bg-ink-800">
          <div className={`h-full rounded ${BAR[st]}`} style={{ width: `${Math.min(100, s?.cpu_pct ?? 0)}%` }} />
        </div>
        <span className="whitespace-nowrap text-xs text-ink-300">{s ? `${s.cpu_pct.toFixed(0)} %` : '–'}</span>
      </div>
      <div className="flex justify-between gap-1 text-[11px] text-ink-300">
        <span>{cores(r.nano_cpus)}</span>
        <span>{r.mem_mb} MB</span>
      </div>
      {pinned.length > 0 && (
        <div className="truncate text-[11px] font-semibold text-accent" title="split test: tenants pinned here">
          pinned: {pinned.join(' ')}
        </div>
      )}
    </div>
  )
}

function ProjectRow({ p, servers, lit, pins }: { p: ProjectView; servers: Map<string, ServerView>; lit: boolean; pins: Record<string, string> }) {
  const floorPct = p.cap_cpu > 0 ? (p.floor_cpu / p.cap_cpu) * 100 : 0
  return (
    <div
      data-testid={`project-${p.id}`}
      data-lit={lit ? '1' : '0'}
      data-state={p.state}
      className={`flex min-w-0 items-stretch gap-2 rounded-lg px-1 py-0.5 ${lit ? 'outline outline-[3px] outline-offset-1 outline-accent' : ''}`}
    >
      <div className="flex w-32 shrink-0 flex-col justify-center">
        <div className="flex items-baseline gap-1.5">
          <span className="truncate text-base font-black text-ink-100">{p.name}</span>
          <span className="text-[10px] uppercase tracking-wider text-ink-300">{p.tier}</span>
        </div>
        <div className="text-[11px] text-ink-300" title="allocated cores = guaranteed base + lent surplus; floor / cap">
          {p.alloc_cpu.toFixed(2)} core
          {p.loan_cpu > 0.005 && <b className="text-accent"> +{p.loan_cpu.toFixed(2)} lent</b>}
        </div>
        <div className="relative mt-0.5 h-1.5 rounded bg-ink-800" title={`floor ${p.floor_cpu} · used ${p.used_cpu.toFixed(2)} · cap ${p.cap_cpu} cores`}>
          <div className="absolute inset-y-0 left-0 rounded bg-ink-500" style={{ width: `${Math.min(100, (p.used_cpu / p.cap_cpu) * 100)}%` }} />
          <div className="absolute inset-y-[-2px] w-0.5 bg-ok" style={{ left: `${floorPct}%` }} />
        </div>
        <div className="text-[10px] text-ink-500">
          {p.tenants.join(' ')} · {p.state}
        </div>
      </div>
      <div className="flex min-w-0 flex-1 gap-1.5 overflow-hidden">
        {p.replicas.length ? (
          p.replicas.map((r) => (
            <Replica key={r.id} r={r} s={servers.get(r.id)} pinned={Object.entries(pins).filter(([, x]) => x === r.id).map(([t]) => t)} />
          ))
        ) : (
          <div className="flex flex-1 items-center rounded-lg border-2 border-dashed border-ink-700 px-2 text-sm text-ink-500">
            {p.state === 'asleep' ? 'asleep · no replica (wakes on the next request)' : 'no replica yet'}
          </div>
        )}
      </div>
    </div>
  )
}

interface Props {
  pool: PoolView | null | undefined
  servers: ServerView[]
  /** project to outline in the accent colour (the picked tenant's project) */
  highlight?: string | null
  highlightLabel?: string
  pins?: Record<string, string>
  title?: string
  /** only these projects (e.g. a case's project); default all */
  only?: string[]
}

/** Projects as rows, each with its replicas: colour = replica status, hatched = starting / draining, LOAN = borrowed. */
export function ReplicaGrid({ pool, servers, highlight, highlightLabel, pins = {}, title = 'Projects · replicas', only }: Props) {
  const by = new Map(servers.map((s) => [s.server, s]))
  const projects = (pool?.projects ?? []).filter((p) => !only || only.includes(p.id))
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-baseline justify-between gap-2">
        <div className="panel-title">{title}</div>
        {highlightLabel && <span className="truncate text-sm font-semibold text-accent">{highlightLabel}</span>}
      </div>
      <div className="flex min-h-0 flex-1 flex-col justify-between gap-1 overflow-hidden" data-testid="replica-grid">
        {projects.length ? (
          projects.map((p) => <ProjectRow key={p.id} p={p} servers={by} lit={highlight === p.id} pins={pins} />)
        ) : (
          <div className="text-base text-ink-500">Waiting for the pool view from the brain…</div>
        )}
      </div>
    </div>
  )
}
