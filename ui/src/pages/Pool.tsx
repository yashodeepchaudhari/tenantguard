import { useState, type ReactNode } from 'react'
import { buildPoolSpec, buildProjectPatch, buildProjectSpec, buildTenantCreate, budget, TIER_LIST, type ProjectDraft } from '../lib/admin.ts'
import { useBackend, useLive } from '../lib/liveContext.ts'
import type { PoolView, ProjectView, ReplicaView, TenantCreated, Tier } from '../types.ts'

const PROJECT_COLOURS = ['#38bdf8', '#a78bfa', '#f472b6', '#facc15', '#34d399', '#fb923c', '#60a5fa', '#c084fc']
const colourOf = (i: number) => PROJECT_COLOURS[i % PROJECT_COLOURS.length]
const cores = (v: number) => `${v.toFixed(2)} core`

type Msg = { ok: boolean; text: string } | null

function useAction() {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<Msg>(null)
  const run = async (fn: () => Promise<string>) => {
    setBusy(true)
    setMsg(null)
    try {
      setMsg({ ok: true, text: await fn() })
      return true
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) })
      return false
    } finally {
      setBusy(false)
    }
  }
  return { busy, msg, setMsg, run }
}

function Note({ msg }: { msg: Msg }) {
  if (!msg) return null
  return (
    <div className={`text-sm ${msg.ok ? 'text-ok' : 'text-bad'}`} role={msg.ok ? 'status' : 'alert'}>
      {msg.text}
    </div>
  )
}

function Bar({ label, testid, children, used, total }: { label: string; testid: string; children: ReactNode; used?: number; total: number }) {
  return (
    <div className="flex items-center gap-3">
      <span className="w-40 shrink-0 text-right text-sm text-ink-300">{label}</span>
      <div className="relative flex h-8 flex-1 overflow-hidden rounded-lg border border-ink-700 bg-ink-900" data-testid={testid}>
        {children}
        {used !== undefined && <div className="absolute inset-y-0 w-0.5 bg-ink-100" style={{ left: `${(used / total) * 100}%` }} title={`measured use ${cores(used)}`} />}
      </div>
    </div>
  )
}

/** The pool in two rows: what is guaranteed (each project's floor, then the headroom) and what each project is
 *  allocated right now (its floor-backed base solid, the cores lent to it striped). */
function PoolBar({ pool }: { pool: PoolView }) {
  const total = pool.pool.cpu_total
  const b = budget(pool)
  const pct = (v: number) => `${Math.max(0, (v / total) * 100)}%`
  const used = pool.projects.reduce((a, p) => a + p.used_cpu, 0)
  const label = (p: ProjectView, v: number) => (v / total > 0.07 ? p.id : '')
  return (
    <div className="space-y-1.5" data-testid="pool-bar">
      <Bar label="guaranteed floors" testid="floors-bar" total={total}>
        {pool.projects.map((p, i) => (
          <div
            key={p.id}
            className="flex h-full items-center justify-center overflow-hidden border-r border-ink-950 text-xs font-bold text-ink-950"
            style={{ width: pct(p.floor_cpu), background: colourOf(i) }}
            title={`${p.name}: floor ${p.floor_cpu} cores (always available to it)`}
          >
            {label(p, p.floor_cpu)}
          </div>
        ))}
        <div className="flex h-full items-center justify-center text-xs text-ink-300" style={{ width: pct(b.free) }} title="usable, not promised to any project">
          {b.free / total > 0.07 ? 'free' : ''}
        </div>
        <div className="flex h-full items-center justify-center border-l-2 border-dashed border-ink-500 text-xs text-ink-500" style={{ width: pct(b.headroom) }} title="headroom: never promised">
          {b.headroom / total > 0.07 ? 'headroom' : ''}
        </div>
      </Bar>
      <Bar label="allocated now" testid="alloc-bar" total={total} used={used}>
        {pool.projects.map((p, i) => (
          <div key={p.id} className="flex h-full" title={`${p.name}: ${p.base_cpu.toFixed(2)} base + ${p.loan_cpu.toFixed(2)} lent = ${p.alloc_cpu.toFixed(2)} cores`}>
            <div className="flex h-full items-center justify-center overflow-hidden text-xs font-bold text-ink-950" style={{ width: pct(p.base_cpu), background: colourOf(i) }}>
              {label(p, p.alloc_cpu)}
            </div>
            {p.loan_cpu > 0.005 && (
              <div className="h-full" style={{ width: pct(p.loan_cpu), background: `repeating-linear-gradient(45deg, ${colourOf(i)} 0 6px, transparent 6px 10px)` }} data-testid={`loan-${p.id}`} />
            )}
          </div>
        ))}
      </Bar>
      <div className="flex flex-wrap gap-x-5 pl-[10.75rem] text-sm text-ink-300">
        <span>
          pool <b className="text-ink-100">{cores(total)}</b> · {pool.pool.mem_total_mb} MB
        </span>
        <span>
          floors <b className="text-ink-100">{cores(b.floors)}</b> of {cores(b.usable)} usable
        </span>
        <span>
          allocated <b className="text-ink-100">{cores(b.allocated)}</b>
          {b.lent > 0.005 && (
            <>
              {' '}
              · <b className="text-accent">{cores(b.lent)} lent</b>
            </>
          )}
        </span>
        <span>
          in use <b className="text-ink-100">{cores(used)}</b> (white line)
        </span>
        <span>
          memory <b className="text-ink-100">{pool.reserved_mem_mb} MB</b> of {pool.usable_mem_mb} MB usable
        </span>
      </div>
    </div>
  )
}

function ReplicaChip({ r }: { r: ReplicaView }) {
  const tone = r.state === 'ready' ? 'border-ok/60' : 'border-ink-700 text-ink-300'
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-xs ${tone}`} data-testid={`chip-${r.id}`}>
      {r.id.replace(/^tg-/, '')}
      <span className="text-ink-300">{r.state !== 'ready' ? r.state : `${(r.nano_cpus / 1e9).toFixed(2)}c · ${r.mem_mb}MB`}</span>
      {r.borrowed && <b className="text-accent">loan</b>}
    </span>
  )
}

const draftOf = (p: ProjectView): Omit<ProjectDraft, 'id'> => ({
  name: p.name,
  tier: p.tier,
  floor_cpu: String(p.floor_cpu),
  cap_cpu: String(p.cap_cpu),
  warm_min: String(p.warm_min),
})

function ProjectRow({ p, i }: { p: ProjectView; i: number }) {
  const backend = useBackend()
  const [edit, setEdit] = useState<Omit<ProjectDraft, 'id'> | null>(null)
  const { busy, msg, run } = useAction()

  const save = () =>
    run(async () => {
      const r = buildProjectPatch(p, edit!)
      if ('error' in r) throw new Error(r.error)
      if (!Object.keys(r.patch).length) {
        setEdit(null)
        return 'nothing changed'
      }
      await backend.patchProject(p.id, r.patch)
      setEdit(null)
      return `${p.id} updated`
    })
  const remove = () => run(async () => (await backend.deleteProject(p.id), `${p.id} deleted`))
  const dropTenant = (t: string) => run(async () => (await backend.removeTenant(t), `tenant ${t} removed`))

  const cell = 'px-2 py-2 align-top'
  return (
    <tr className="border-b border-ink-800" data-testid={`pool-row-${p.id}`}>
      <td className={cell}>
        <div className="flex items-center gap-2">
          <span className="h-3 w-3 rounded-sm" style={{ background: colourOf(i) }} />
          <span className="text-lg font-black">{p.name}</span>
          <span className="font-mono text-sm text-ink-300">{p.id}</span>
        </div>
        <div className="text-sm text-ink-300">
          {p.state}
          {p.warm_min === 0 && ' · may sleep'}
        </div>
      </td>
      {edit ? (
        <>
          <td className={cell}>
            <input className="field w-28" aria-label="name" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
            <select className="field mt-1 w-28" aria-label="tier" value={edit.tier} onChange={(e) => setEdit({ ...edit, tier: e.target.value as Tier })}>
              {TIER_LIST.map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          </td>
          <td className={cell}>
            <input className="field w-20 text-right" aria-label="floor" value={edit.floor_cpu} onChange={(e) => setEdit({ ...edit, floor_cpu: e.target.value })} />
          </td>
          <td className={cell}>
            <input className="field w-20 text-right" aria-label="cap" value={edit.cap_cpu} onChange={(e) => setEdit({ ...edit, cap_cpu: e.target.value })} />
          </td>
          <td className={cell}>
            <input className="field w-16 text-right" aria-label="warm" value={edit.warm_min} onChange={(e) => setEdit({ ...edit, warm_min: e.target.value })} />
          </td>
        </>
      ) : (
        <>
          <td className={cell}>
            <div className="uppercase">{p.tier}</div>
            <div className="text-sm text-ink-300">weight {p.weight}</div>
          </td>
          <td className={`${cell} text-right font-semibold`}>{p.floor_cpu}</td>
          <td className={`${cell} text-right`}>{p.cap_cpu}</td>
          <td className={`${cell} text-right`}>{p.warm_min}</td>
        </>
      )}
      <td className={`${cell} text-right`}>{p.demand_cpu.toFixed(2)}</td>
      <td className={`${cell} text-right`}>{p.used_cpu.toFixed(2)}</td>
      <td className={`${cell} text-right`}>
        <b>{p.alloc_cpu.toFixed(2)}</b>
        <div className="text-sm text-ink-300">
          {p.base_cpu.toFixed(2)} base
          {p.loan_cpu > 0.005 && <span className="text-accent"> + {p.loan_cpu.toFixed(2)} lent</span>}
        </div>
      </td>
      <td className={cell}>
        <div className="flex flex-wrap gap-1">{p.replicas.length ? p.replicas.map((r) => <ReplicaChip key={r.id} r={r} />) : <span className="text-ink-500">none</span>}</div>
      </td>
      <td className={cell}>
        <div className="flex flex-wrap gap-1">
          {p.tenants.map((t) => (
            <span key={t} className="inline-flex items-center gap-1 rounded bg-ink-800 px-1.5 text-sm font-bold">
              {t}
              <button className="text-ink-500 hover:text-bad" title={`remove tenant ${t}`} aria-label={`remove tenant ${t}`} disabled={busy} onClick={() => dropTenant(t)}>
                ×
              </button>
            </span>
          ))}
        </div>
      </td>
      <td className={cell}>
        <div className="flex gap-1">
          {edit ? (
            <>
              <button className="btn-accent px-2 py-0.5" disabled={busy} onClick={save}>
                Save
              </button>
              <button className="btn-ghost px-2 py-0.5" disabled={busy} onClick={() => setEdit(null)}>
                Cancel
              </button>
            </>
          ) : (
            <>
              <button className="btn-ghost px-2 py-0.5" onClick={() => setEdit(draftOf(p))} data-testid={`edit-${p.id}`}>
                Edit
              </button>
              <button className="btn-ghost px-2 py-0.5" disabled={busy || p.tenants.length > 0} title={p.tenants.length ? 'remove its tenants first' : 'delete project'} onClick={remove}>
                Delete
              </button>
            </>
          )}
        </div>
        <Note msg={msg} />
      </td>
    </tr>
  )
}

function PoolForm({ pool }: { pool: PoolView }) {
  const backend = useBackend()
  const [d, setD] = useState({ cpu_total: String(pool.pool.cpu_total), mem_total_mb: String(pool.pool.mem_total_mb), headroom_ratio: String(pool.pool.headroom_ratio) })
  const { busy, msg, run } = useAction()
  const save = () =>
    run(async () => {
      const r = buildPoolSpec(d)
      if ('error' in r) throw new Error(r.error)
      await backend.setPool(r.spec)
      return `pool set to ${r.spec.cpu_total} cores, ${r.spec.mem_total_mb} MB, headroom ${r.spec.headroom_ratio}`
    })
  return (
    <div className="flex flex-wrap items-end gap-2" data-testid="pool-form">
      {(
        [
          ['cpu_total', 'Cores'],
          ['mem_total_mb', 'Memory MB'],
          ['headroom_ratio', 'Headroom 0..1'],
        ] as const
      ).map(([k, label]) => (
        <label key={k} className="flex flex-col text-sm text-ink-300">
          {label}
          <input className="field w-28 text-right" value={d[k]} onChange={(e) => setD({ ...d, [k]: e.target.value })} />
        </label>
      ))}
      <button className="btn-accent px-3 py-1" disabled={busy} onClick={save}>
        Set pool
      </button>
      <Note msg={msg} />
    </div>
  )
}

function NewProject({ freeCores }: { freeCores: number }) {
  const backend = useBackend()
  const [d, setD] = useState<ProjectDraft>({ id: '', name: '', tier: 'pro', floor_cpu: '0.25', cap_cpu: '1', warm_min: '1' })
  const { busy, msg, run } = useAction()
  const create = () =>
    run(async () => {
      const r = buildProjectSpec(d, freeCores)
      if ('error' in r) throw new Error(r.error)
      await backend.createProject(r.spec)
      setD({ ...d, id: '', name: '' })
      return `project ${r.spec.id} created: floor ${r.spec.floor_cpu} cores guaranteed`
    })
  return (
    <div className="flex flex-wrap items-end gap-2" data-testid="new-project">
      <label className="flex flex-col text-sm text-ink-300">
        Id
        <input className="field w-28" value={d.id} onChange={(e) => setD({ ...d, id: e.target.value })} placeholder="games" />
      </label>
      <label className="flex flex-col text-sm text-ink-300">
        Name
        <input className="field w-32" value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} placeholder="Games" />
      </label>
      <label className="flex flex-col text-sm text-ink-300">
        Tier
        <select className="field w-28" value={d.tier} onChange={(e) => setD({ ...d, tier: e.target.value as Tier })}>
          {TIER_LIST.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
      </label>
      {(
        [
          ['floor_cpu', 'Floor cores'],
          ['cap_cpu', 'Cap cores'],
          ['warm_min', 'Warm replicas'],
        ] as const
      ).map(([k, label]) => (
        <label key={k} className="flex flex-col text-sm text-ink-300">
          {label}
          <input className="field w-24 text-right" value={d[k]} onChange={(e) => setD({ ...d, [k]: e.target.value })} />
        </label>
      ))}
      <button className="btn-accent px-3 py-1" disabled={busy} onClick={create}>
        Create project
      </button>
      <Note msg={msg} />
    </div>
  )
}

function NewTenant({ projects }: { projects: ProjectView[] }) {
  const backend = useBackend()
  const [d, setD] = useState({ project: projects[0]?.id ?? '', id: '', name: '', revenue: '' })
  const [created, setCreated] = useState<TenantCreated | null>(null)
  const { busy, msg, run } = useAction()
  const add = () =>
    run(async () => {
      const r = buildTenantCreate(d)
      if ('error' in r) throw new Error(r.error)
      const c = await backend.addTenant(d.project, r.req)
      setCreated(c)
      setD({ ...d, id: '', name: '', revenue: '' })
      return `tenant ${c.tenant} added to ${c.project}`
    })
  return (
    <div className="space-y-2" data-testid="new-tenant">
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col text-sm text-ink-300">
          Project
          <select className="field w-32" value={d.project} onChange={(e) => setD({ ...d, project: e.target.value })}>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.id}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col text-sm text-ink-300">
          Tenant id
          <input className="field w-24" value={d.id} onChange={(e) => setD({ ...d, id: e.target.value })} placeholder="M" />
        </label>
        <label className="flex flex-col text-sm text-ink-300">
          Name
          <input className="field w-40" value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} placeholder="Mitra Games" />
        </label>
        <label className="flex flex-col text-sm text-ink-300">
          ₹ / month
          <input className="field w-28 text-right" value={d.revenue} onChange={(e) => setD({ ...d, revenue: e.target.value })} placeholder="0" />
        </label>
        <button className="btn-accent px-3 py-1" disabled={busy || !d.project} onClick={add}>
          Add tenant
        </button>
        <Note msg={msg} />
      </div>
      {created && (
        <div className="rounded-lg border-2 border-accent bg-accent/10 px-3 py-2" data-testid="api-key">
          <div className="text-sm text-ink-300">API key for {created.tenant} (shown once; only its SHA-256 is stored):</div>
          <code className="select-all break-all text-lg text-accent">{created.api_key}</code>
          <button className="btn-ghost ml-3 px-2 py-0.5 text-sm" onClick={() => setCreated(null)}>
            Done
          </button>
        </div>
      )}
    </div>
  )
}

export function Pool() {
  const { snapshot } = useLive()
  const pool = snapshot?.pool ?? null
  if (!pool) {
    return <div className="flex flex-1 items-center justify-center text-2xl text-ink-300">Waiting for the pool view from the brain…</div>
  }
  return (
    <div className="flex flex-col gap-3" data-testid="pool-page">
      <div className="flex flex-wrap items-baseline gap-4">
        <h1 className="text-3xl font-bold">Pool</h1>
        <span className="text-base text-ink-300">
          Floors are always held · idle CPU is lent by tier weight (striped) and comes back to its owner first · replicas start and stop with demand
        </span>
      </div>
      <section className="panel">
        <PoolBar pool={pool} />
      </section>
      <section className="panel overflow-x-auto p-0">
        <table className="w-full min-w-[1280px] border-collapse text-base">
          <thead>
            <tr className="border-b border-ink-700 text-left text-sm uppercase tracking-wider text-ink-300">
              <th className="px-2 py-2">Project</th>
              <th className="px-2 py-2">Tier</th>
              <th className="px-2 py-2 text-right">Floor</th>
              <th className="px-2 py-2 text-right">Cap</th>
              <th className="px-2 py-2 text-right">Warm</th>
              <th className="px-2 py-2 text-right">Demand</th>
              <th className="px-2 py-2 text-right">Used</th>
              <th className="px-2 py-2 text-right">Allocated</th>
              <th className="px-2 py-2">Replicas</th>
              <th className="px-2 py-2">Tenants</th>
              <th className="px-2 py-2" />
            </tr>
          </thead>
          <tbody>
            {pool.projects.map((p, i) => (
              <ProjectRow key={p.id} p={p} i={i} />
            ))}
          </tbody>
        </table>
        <div className="px-3 py-1 text-xs text-ink-300">All CPU figures in cores. Demand = used + queued + refused work; allocated = floor-backed base + lent surplus.</div>
      </section>
      <div className="grid grid-cols-12 gap-3">
        <section className="panel col-span-12 xl:col-span-5">
          <div className="panel-title">Pool capacity (admin)</div>
          <PoolForm key={`${pool.pool.cpu_total}-${pool.pool.mem_total_mb}-${pool.pool.headroom_ratio}`} pool={pool} />
        </section>
        <section className="panel col-span-12 xl:col-span-7">
          <div className="panel-title">New project</div>
          <NewProject freeCores={budget(pool).free} />
        </section>
        <section className="panel col-span-12">
          <div className="panel-title">New tenant</div>
          <NewTenant projects={pool.projects} />
        </section>
      </div>
    </div>
  )
}
