import { NavLink, Outlet } from 'react-router-dom'
import { config } from '../config.ts'
import { ago } from '../lib/format.ts'
import { useBackend, useLive, useNow } from '../lib/liveContext.ts'
import { ModeToggle } from './ModeToggle.tsx'
import { StatusDot } from './StatusDot.tsx'

function Connection() {
  const backend = useBackend()
  const { conn, receivedAt, snapshot } = useLive()
  const now = useNow()
  const stale = receivedAt !== null && now - receivedAt > 3
  const status = conn !== 'open' ? 'bad' : stale || snapshot?.brain_ok === false ? 'warn' : 'ok'
  const text =
    conn === 'connecting'
      ? 'connecting to brain…'
      : conn === 'closed'
        ? 'brain offline · retrying'
        : snapshot?.brain_ok === false
          ? 'brain degraded'
          : receivedAt === null
            ? 'waiting for state…'
            : stale
              ? `stale · ${ago(receivedAt, now)}`
              : 'live'
  return (
    <div className="flex items-center gap-2 text-base" title={backend.kind === 'mock' ? 'fixture replay' : config.brainUrl}>
      <StatusDot status={status} />
      <span className="text-ink-300">{text}</span>
      {backend.kind === 'mock' && <span className="rounded border border-accent px-1.5 text-xs font-bold uppercase text-accent">mock</span>}
    </div>
  )
}

const tab = ({ isActive }: { isActive: boolean }) =>
  `rounded-lg px-3 py-1 text-lg font-semibold ${isActive ? 'bg-ink-800 text-ink-100' : 'text-ink-300 hover:text-ink-100'}`

export function Layout() {
  return (
    <div className="flex min-h-screen flex-col xl:h-screen">
      <header className="flex items-center gap-6 border-b border-ink-800 px-4 py-2">
        <div className="flex items-center gap-2">
          <img src="/favicon.svg" alt="" className="h-7 w-7" />
          <span className="text-2xl font-black tracking-tight">TenantGuard</span>
        </div>
        <nav className="flex gap-1">
          <NavLink to="/" end className={tab}>
            Control Room
          </NavLink>
          <NavLink to="/tenants" className={tab}>
            Tenants
          </NavLink>
        </nav>
        <Connection />
        <div className="ml-auto">
          <ModeToggle />
        </div>
      </header>
      <main className="flex min-h-0 flex-1 flex-col overflow-auto p-3">
        <Outlet />
      </main>
    </div>
  )
}
