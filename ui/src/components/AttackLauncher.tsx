import { useCallback, useEffect, useState } from 'react'
import { useBackend, useLive, useNow } from '../lib/liveContext.ts'
import { ATTACKS, DURATION_MAX_S, INTENSITY_MAX, INTENSITY_MIN, type AttackName, type AttackReq, type LabStatus } from '../types.ts'

const SERVERS = ['w0', 'w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'w7', 'w8']
const FALLBACK_TENANTS = 'ABCDEFGHIJKL'.split('')

const ABOUT: Record<AttackName, string> = {
  flood: '10× request rate of light calls',
  whale: 'few, very heavy searches',
  hog: 'many slow concurrent exports',
  memory: 'memory-hungry requests',
  retry_storm: 'retries instantly on 429/503',
  background_bomb: 'heavy background jobs (Wave 2)',
  blackbox_bomb: 'unattributed vendor CPU (Wave 2)',
  cache_flood: 'evicts everyone’s cache (Wave 2)',
  faulty_server: 'a slow host, not a tenant (Wave 2)',
  honest_spike: 'legit growth, should NOT be punished (Wave 2)',
}

export function AttackLauncher() {
  const backend = useBackend()
  const { snapshot } = useLive()
  const now = useNow()
  const tenants = snapshot?.tenants.map((t) => t.tenant) ?? FALLBACK_TENANTS
  const [attack, setAttack] = useState<AttackName>('whale')
  const [tenant, setTenant] = useState('K')
  const [server, setServer] = useState('w4')
  const [intensity, setIntensity] = useState(2)
  const [duration, setDuration] = useState(60)
  const [lab, setLab] = useState<LabStatus | null>(null)
  const [labError, setLabError] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setLab(await backend.labStatus())
      setLabError(null)
    } catch (e) {
      setLabError(e instanceof Error ? e.message : String(e))
    }
  }, [backend])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 2000)
    return () => clearInterval(id)
  }, [refresh])

  const byServer = attack === 'faulty_server'
  const intensityOk = intensity >= INTENSITY_MIN && intensity <= INTENSITY_MAX
  const durationOk = duration > 0 && duration <= DURATION_MAX_S

  const launch = async () => {
    setBusy(true)
    setMsg(null)
    const req: AttackReq = {
      attack,
      tenant: byServer ? null : tenant,
      server: byServer ? server : null,
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
            {ATTACKS.map((a) => (
              <option key={a} value={a}>
                {a} — {ABOUT[a]}
              </option>
            ))}
          </select>
        </label>
        {byServer ? (
          <label className="flex flex-col text-sm text-ink-300">
            Server
            <select className="field mt-0.5" value={server} onChange={(e) => setServer(e.target.value)}>
              {SERVERS.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </label>
        ) : (
          <label className="flex flex-col text-sm text-ink-300">
            Tenant
            <select className="field mt-0.5" value={tenant} onChange={(e) => setTenant(e.target.value)}>
              {tenants.map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          </label>
        )}
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
          {busy ? 'Launching…' : `Launch on ${byServer ? server : tenant}`}
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
