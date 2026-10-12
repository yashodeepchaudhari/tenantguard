import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { LevelBadge } from '../components/LevelBadge.tsx'
import { caseIndex } from '../lib/case.ts'
import { clock } from '../lib/format.ts'
import { useBackend, useLive } from '../lib/liveContext.ts'
import type { IncidentEvent } from '../types.ts'

export function Cases() {
  const backend = useBackend()
  const { incidents } = useLive()
  const [history, setHistory] = useState<IncidentEvent[]>([])

  useEffect(() => {
    let alive = true
    backend.incidents().then((l) => alive && setHistory(l))
    return () => {
      alive = false
    }
  }, [backend])

  const cases = caseIndex([...history, ...incidents])

  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-3xl font-bold">Case files</h1>
      {cases.length === 0 ? (
        <div className="panel text-lg text-ink-300">No incidents yet. A case opens when a tenant reaches warn.</div>
      ) : (
        <div className="panel overflow-x-auto p-0">
          <table className="w-full text-base">
            <thead>
              <tr className="border-b border-ink-700 text-left text-sm uppercase tracking-wider text-ink-300">
                <th className="px-3 py-2">Incident</th>
                <th className="px-2 py-2">Tenant</th>
                <th className="px-2 py-2">Opened</th>
                <th className="px-2 py-2">Status</th>
                <th className="px-2 py-2">Level</th>
                <th className="px-2 py-2">Verdict</th>
                <th className="px-2 py-2">Latest</th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr key={c.id} className="border-b border-ink-800 hover:bg-ink-800/40" data-testid={`case-${c.id}`}>
                  <td className="px-3 py-2">
                    <Link to={`/case/${encodeURIComponent(c.id)}`} className="font-mono text-accent hover:underline">
                      {c.id}
                    </Link>
                  </td>
                  <td className="px-2 py-2 text-xl font-black">{c.tenant ?? '–'}</td>
                  <td className="px-2 py-2 font-mono text-sm">{c.openedAt ? clock(c.openedAt) : '–'}</td>
                  <td className={`px-2 py-2 font-bold uppercase ${c.closed ? 'text-ok' : 'text-bad'}`}>{c.closed ? 'resolved' : 'open'}</td>
                  <td className="px-2 py-2">{c.level ? <LevelBadge level={c.level} /> : '–'}</td>
                  <td className="px-2 py-2">{c.verdict ?? '–'}</td>
                  <td className="max-w-xl truncate px-2 py-2 text-ink-300" title={c.detail}>
                    {c.detail || '–'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
