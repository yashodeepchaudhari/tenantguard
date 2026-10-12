import { LEVEL_LABEL, levelStatus } from '../lib/status.ts'
import type { Level } from '../types.ts'

const STYLE = {
  ok: 'border-ok/50 bg-ok/15 text-ok',
  warn: 'border-warn/60 bg-warn/15 text-warn',
  bad: 'border-bad/60 bg-bad/20 text-bad',
  idle: 'border-ink-500 text-ink-300',
} as const

export function LevelBadge({ level, className = '' }: { level: Level; className?: string }) {
  return (
    <span className={`inline-flex items-center rounded-md border px-2 py-0.5 text-sm font-bold uppercase tracking-wide ${STYLE[levelStatus(level)]} ${className}`}>
      {LEVEL_LABEL[level]}
    </span>
  )
}
