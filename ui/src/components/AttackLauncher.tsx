import { useCallback, useState } from 'react'
import { useBackend, useLive, useNow, useStore } from '../lib/liveContext.ts'
import { DURATION_MAX_S, LAUNCHABLE_ATTACKS, INTENSITY_MAX, INTENSITY_MIN, type AttackName, type AttackReq } from '../types.ts'

const FALLBACK_TENANTS = 'ABCDEFGHIJKL'.split('')
const FALLBACK_SERVERS = ['w0', 'w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'w7', 'w8']

const ABOUT: Partial<Record<AttackName, string>> = {
  flood: '10× request rate of light calls',
  whale: 'few, very heavy searches',
  hog: 'many slow concurrent exports',
  memory: 'memory-hungry requests',
  retry_storm: 'retries instantly on 429/503',
  blackbox_bomb: 'cheap ingest calls, heavy hidden indexer CPU',
  background_bomb: 'cheap writes, heavy background indexing',
  cache_flood: 'reads of never-repeated keys (all cache misses)',
  honest_spike: '3× its own normal traffic (real growth)',
  faulty_server: 'a server turns slow by itself (no tenant)',
}

export function AttackLauncher() {
  const backend = useBackend()
  const store = useStore()
  const { snapshot, lab, labReachable } = useLive()
  const now = useNow()
  const tenants = snapshot?.tenants.map((t) => t.tenant) ?? FALLBACK_TENANTS
  const servers = snapshot?.servers.map((s) => s.server) ?? FALLBACK_SERVERS
  const [attack, setAttack] = useState<AttackName>('whale')
  const [tenant, setTenant] = useState('K')
  const [server, setServer] = useState('w4')
  const onServer = attack === 'faulty_server'
  const target = onServer ? server : tenant
  const [intensity, setIntensity] = useState(2)
  const [duration, setDuration] = useState(60)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  // LiveProvider polls the lab every 2 s; refresh at once after our own launch / stop
  const refresh = useCallback(async () => {
    try {
      store.setLab(await backend.labStatus())
    } catch {
      store.setLab(null)
    }
  }, [backend, store])
  const labError = labReachable === false

  const intensityOk = intensity >= INTENSITY_MIN && intensity <= INTENSITY_MAX
  const durationOk = duration > 0 && duration <= DURATION_MAX_S

  const launch = async () => {
    setBusy(true)
    setMsg(null)
    const req: AttackReq = {
      attack,
      tenant: onServer ? null : tenant,
      server: onServer ? server : null,
      intensity,
      duration_s: duration,
    }
    try {
      const st = await backend.startAttack(req)
      setMsg({ ok: true, text: `${st.attack} on ${st.tenant ?? st.server} started (${st.attack_id})` })
      refresh()
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  const stop = async (id: string) => {
    try {
      await backend.stopAttack(id)
      refresh()
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) })
    }
  }

  const running = lab?.attacks.filter((a) => a.state === 'running') ?? []

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-baseline justify-between">
        <div className="panel-title">Attack launcher</div>
        <span className={`text-sm ${labError ? 'text-bad' : 'text-ink-300'}`}>
          {labError ? 'lab unreachable' : lab ? `calm traffic ${lab.calm_running ? 'on' : 'off'}` : '…'}
        </span>
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)_5.5rem_6.5rem] gap-2">
        <label className="flex flex-col text-sm text-ink-300">
          Attack
          <select className="field mt-0.5 w-full min-w-0" value={attack} onChange={(e) => setAttack(e.target.value as AttackName)} title={ABOUT[attack]}>
            {LAUNCHABLE_ATTACKS.map((a) => (
              <option key={a} value={a}>
                {a} — {ABOUT[a]}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col text-sm text-ink-300">
          {onServer ? 'Server' : 'Tenant'}
          {onServer ? (
            <select className="field mt-0.5" value={server} onChange={(e) => setServer(e.target.value)}>
              {servers.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          ) : (
            <select className="field mt-0.5" value={tenant} onChange={(e) => setTenant(e.target.value)}>
              {tenants.map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          )}
        </label>
        <label className="flex flex-col text-sm text-ink-300">
          Duration (s)
          <input
            className={`field mt-0.5 w-full ${durationOk ? '' : 'border-bad'}`}
            type="number"
            min={1}
            max={DURATION_MAX_S}
            value={duration}
            onChange={(e) => setDuration(Number(e.target.value))}
          />
        </label>
      </div>
      <div className="mt-2 flex items-end gap-3">
        <label className="flex flex-1 flex-col text-sm text-ink-300">
          <span>
            Intensity <b className="text-lg text-ink-100">{intensity.toFixed(1)} ×</b> <span className="text-xs">(0.1–5 × default profile)</span>
          </span>
          <input
            type="range"
            min={INTENSITY_MIN}
            max={INTENSITY_MAX}
            step={0.1}
            value={intensity}
            onChange={(e) => setIntensity(Number(e.target.value))}
            className="accent-sky-400"
          />
        </label>
        <button className="btn-accent whitespace-nowrap" disabled={busy || !intensityOk || !durationOk} onClick={launch}>
          {busy ? 'Launching…' : `Launch on ${target}`}
        </button>
      </div>
      {msg && (
        <div className={`mt-1 text-sm ${msg.ok ? 'text-ink-300' : 'text-bad'}`} role={msg.ok ? 'status' : 'alert'}>
          {msg.text}
        </div>
      )}
      <div className="mt-2 min-h-0 flex-1 space-y-1 overflow-y-auto">
        {running.map((a) => (
          <div key={a.attack_id} className="flex items-center justify-between rounded-lg border border-bad/60 bg-bad/10 px-2 py-1">
            <span className="text-base">
              <b>{a.attack}</b> → {a.tenant ?? a.server} · {a.intensity.toFixed(1)} × ·{' '}
              <span className="text-ink-300">{Math.max(0, Math.round(a.ends_at - now))} s left</span>
            </span>
            <button className="btn-ghost px-2 py-0.5 text-sm" onClick={() => stop(a.attack_id)}>
              Stop
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
