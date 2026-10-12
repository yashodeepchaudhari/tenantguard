import type { Status } from '../lib/status.ts'

const DOT: Record<Status, string> = {
  ok: 'bg-ok',
  warn: 'bg-warn',
  bad: 'bg-bad animate-pulse',
  idle: 'bg-ink-500',
}

export function StatusDot({ status, className = '' }: { status: Status; className?: string }) {
  return <span className={`inline-block h-3 w-3 shrink-0 rounded-full ${DOT[status]} ${className}`} aria-label={status} />
}
