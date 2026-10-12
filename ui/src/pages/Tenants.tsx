import { useCallback, useEffect, useState } from 'react'
import { LevelBadge } from '../components/LevelBadge.tsx'
import { StatusDot } from '../components/StatusDot.tsx'
import { config } from '../config.ts'
import { ago, ms, rupees } from '../lib/format.ts'
import { useBackend, useLive, useNow } from '../lib/liveContext.ts'
import { tenantStatus } from '../lib/status.ts'
import { buildPatch, FIELDS, fmt, type QuotaKey } from '../lib/quota.ts'
import type { OverridesView, Quota, TenantAdminView, TenantView } from '../types.ts'

function QuotaCells({ q, eff, factor }: { q: Quota; eff: Quota; factor: number }) {
  return (
    <>
      {FIELDS.map((f) => {
        const reduced = eff[f.key] < q[f.key] - 1e-9
        return (
          <td key={f.key} className="px-2 py-2 text-right">
            <div className="text-lg font-semibold">
              {fmt(q[f.key], f.int)}
              <span className="unit">{f.unit}</span>
            </div>
            {(reduced || factor < 1) && (
              <div className={`text-sm ${reduced ? 'text-bad' : 'text-ink-300'}`} title="effective quota after the brain's throttle factor">
                eff {fmt(eff[f.key], f.int)}
              </div>
            )}
          </td>
        )
      })}
    </>
  )
}

function EditRow({
  t,
  onCancel,
  onSaved,
}: {
  t: TenantAdminView
  onCancel: () => void
  onSaved: (v: TenantAdminView) => void
}) {
  const backend = useBackend()
  const [draft, setDraft] = useState<Record<QuotaKey, string>>(() => ({
    rate_rps: String(t.quota.rate_rps),
    burst: String(t.quota.burst),
    cost_ms_per_s: String(t.quota.cost_ms_per_s),
    concurrency: String(t.quota.concurrency),
  }))
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const save = async () => {
    const r = buildPatch(t.quota, draft)
    if ('error' in r) return setError(r.error)
    if (Object.keys(r.patch).length === 0) return onCancel()
    setBusy(true)
    setError(null)
    try {
      onSaved(await backend.patchQuota(t.id, r.patch))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {FIELDS.map((f) => (
        <td key={f.key} className="px-2 py-2 text-right">
          <input
            aria-label={`${t.id} ${f.label}`}
            className="field w-24 text-right"
            inputMode="decimal"
            value={draft[f.key]}
            onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === 'Enter') save()
              if (e.key === 'Escape') onCancel()
            }}
          />
          <div className="text-xs text-ink-300">{f.unit}</div>
        </td>
      ))}
      <td className="px-2 py-2" colSpan={2}>
        <div className="flex gap-2">
          <button className="btn-accent px-3 py-1" disabled={busy} onClick={save}>
            {busy ? 'Saving…' : 'Save'}
          </button>
          <button className="btn-ghost px-3 py-1" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
        </div>
        {error && (
          <div className="mt-1 text-sm text-bad" role="alert">
            {error}
          </div>
        )}
      </td>
    </>
  )
}

export function Tenants() {
  const backend = useBackend()
  const { snapshot } = useLive()
  const now = useNow()
  const [rows, setRows] = useState<TenantAdminView[] | null>(null)
  const [ov, setOv] = useState<OverridesView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [t, o] = await Promise.all([backend.tenants(), backend.overrides().catch(() => null)])
      setRows(t)
      setOv(o)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [backend])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 3000)
    return () => clearInterval(id)
  }, [refresh])

  const live = new Map<string, TenantView>(snapshot?.tenants.map((t) => [t.tenant, t]) ?? [])

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-6">
        <h1 className="text-3xl font-bold">Tenants & quotas</h1>
        <span className="text-base text-ink-300">
          Configured plan quota (editable) · <span className="text-bad">eff</span> = after the brain&apos;s throttle factor
        </span>
        {ov && (
          <span className="ml-auto flex items-center gap-2 text-base">
            <StatusDot status={ov.fail_static ? 'bad' : 'ok'} />
            {ov.fail_static ? 'Gateway FAIL-STATIC (brain heartbeat lost)' : 'Brain heartbeat OK'}
            {ov.last_heartbeat_age_s !== null && ov.last_heartbeat_age_s !== undefined && (
              <span className="text-ink-300">· last {ov.last_heartbeat_age_s.toFixed(1)} s ago</span>
            )}
            <span className="text-ink-300">· {Object.keys(ov.limits).length} active limits</span>
          </span>
        )}
      </div>
      {error && (
        <div className="rounded-lg border border-bad bg-bad/10 px-3 py-2 text-base text-bad" role="alert">
          Gateway admin API: {error}
        </div>
      )}
      {saved && <div className="text-base text-ok">{saved}</div>}
      <div className="panel overflow-x-auto p-0">
        <table className="w-full min-w-[1200px] border-collapse text-base">
          <thead>
            <tr className="border-b border-ink-700 text-left text-sm uppercase tracking-wider text-ink-300">
              <th className="px-3 py-2">Tenant</th>
              <th className="px-2 py-2">Tier · weight</th>
              <th className="px-2 py-2">Servers</th>
              <th className="px-2 py-2">Live</th>
              {FIELDS.map((f) => (
                <th key={f.key} className="px-2 py-2 text-right">
                  {f.label}
                </th>
              ))}
              <th className="px-2 py-2">Override</th>
              <th className="px-2 py-2" />
            </tr>
          </thead>
          <tbody>
            {rows === null && !error && (
              <tr>
                <td colSpan={10} className="px-3 py-6 text-center text-ink-300">
                  Loading…
                </td>
              </tr>
            )}
            {rows?.map((t) => {
              const lv = live.get(t.id)
              const isEditing = editing === t.id
              return (
                <tr key={t.id} className="border-b border-ink-800 align-top hover:bg-ink-800/40" data-testid={`row-${t.id}`}>
                  <td className="px-3 py-2">
                    <div className="flex items-baseline gap-2">
                      <span className="text-2xl font-black">{t.id}</span>
                      <span className="font-semibold">{t.name}</span>
                      {t.protected && <span className="rounded border border-accent px-1 text-xs text-accent">protected</span>}
                    </div>
                    <div className="text-sm text-ink-300">{rupees(t.monthly_revenue_rs)} / month</div>
                  </td>
                  <td className="px-2 py-2">
                    <div className="uppercase">{t.tier}</div>
                    <div className="text-sm text-ink-300">
                      weight {t.weight}
                      {t.weight_factor < 1 && <span className="text-bad"> × {t.weight_factor}</span>}
                    </div>
                  </td>
                  <td className="px-2 py-2 font-mono text-sm">
                    {t.servers.join(' ')}
                    {t.placement !== 'normal' && <div className="text-warn">→ {t.placement}</div>}
                  </td>
                  <td className="px-2 py-2">
                    {lv ? (
                      <div className="flex flex-col items-start gap-1">
                        <span className="flex items-center gap-2">
                          <StatusDot status={tenantStatus(lv, config)} />
                          {ms(lv.p95_ms)}
                        </span>
                        <LevelBadge level={lv.level} />
                      </div>
                    ) : (
                      <span className="text-ink-500">–</span>
                    )}
                  </td>
                  {isEditing ? (
                    <EditRow
                      t={t}
                      onCancel={() => setEditing(null)}
                      onSaved={(v) => {
                        setRows((rs) => rs?.map((r) => (r.id === v.id ? v : r)) ?? null)
                        setEditing(null)
                        setSaved(`Quota for ${v.id} saved: ${v.quota.rate_rps} rps, burst ${v.quota.burst}, ${v.quota.cost_ms_per_s} CPU-ms/s, ${v.quota.concurrency} in-flight.`)
                      }}
                    />
                  ) : (
                    <>
                      <QuotaCells q={t.quota} eff={t.effective_quota} factor={t.factor} />
                      <td className="px-2 py-2 text-sm">
                        {t.blocked && <div className="font-bold text-bad">BLOCKED</div>}
                        {t.factor < 1 || t.weight_factor < 1 ? (
                          <div>
                            <span className="text-bad">quota × {t.factor.toFixed(2)}</span>
                            {t.override_expires_at ? (
                              <div className="text-ink-300">expires in {Math.max(0, Math.round(t.override_expires_at - now))} s</div>
                            ) : null}
                          </div>
                        ) : (
                          !t.blocked && <span className="text-ink-500">none</span>
                        )}
                      </td>
                      <td className="px-2 py-2 text-right">
                        <button className="btn-ghost px-3 py-1" onClick={() => setEditing(t.id)} disabled={editing !== null}>
                          Edit quota
                        </button>
                      </td>
                    </>
                  )}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {ov && Object.keys(ov.expires_at).length > 0 && (
        <div className="panel">
          <div className="panel-title">Active control overrides</div>
          <ul className="flex flex-wrap gap-3 text-base">
            {Object.entries(ov.expires_at).map(([k, exp]) => (
              <li key={k} className="rounded bg-ink-800 px-2 py-1 font-mono">
                {k} <span className="text-ink-300">· {exp > now ? `${Math.round(exp - now)} s left` : `expired ${ago(exp, now)}`}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
