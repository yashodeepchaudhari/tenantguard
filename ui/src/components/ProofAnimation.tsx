import { useEffect, useRef, useState } from 'react'

interface Props {
  tenant: string | null
  /** the suspect's barcode servers (the smoke starts here) */
  servers: string[]
  /** relocation target, normally the spare */
  target: string
  /** true while the case is in its proving stage */
  live: boolean
}

interface Puff {
  from: number
  born: number
  life: number
  drift: number
  size: number
}

const REPLAY_S = 8

/** The relocation proof: red "smoke" (the suspect's heavy work) drifts from its servers to the spare. */
export function ProofAnimation({ tenant, servers, target, live }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [replayUntil, setReplayUntil] = useState(0)
  const running = live || replayUntil > Date.now()
  const runningRef = useRef(running)
  runningRef.current = running

  // end the replay without polling: one timer per replay
  useEffect(() => {
    if (!replayUntil) return
    const id = setTimeout(() => setReplayUntil(0), Math.max(0, replayUntil - Date.now()))
    return () => clearTimeout(id)
  }, [replayUntil])

  useEffect(() => {
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    if (!canvas || !wrap) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    let w = 0
    let h = 0
    const resize = () => {
      const dpr = window.devicePixelRatio || 1
      w = wrap.clientWidth
      h = wrap.clientHeight
      canvas.width = Math.round(w * dpr)
      canvas.height = Math.round(h * dpr)
      canvas.style.width = `${w}px`
      canvas.style.height = `${h}px`
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(wrap)
    let puffs: Puff[] = []
    let raf = 0
    let acc = 0
    let last = performance.now() / 1000
    let moved = 0 // 0..1, how much of the smoke has reached the target

    const frame = () => {
      const now = performance.now() / 1000
      const dt = Math.min(0.1, now - last)
      last = now
      const on = runningRef.current
      moved = on ? Math.min(1, moved + dt / 5) : Math.max(0, moved - dt / 2)
      const n = Math.max(1, servers.length)
      const boxW = Math.min(110, w * 0.22)
      const boxH = Math.min(52, (h - 20) / n - 8)
      const srcX = 12
      const tgtX = w - boxW - 12
      const tgtY = h / 2 - 34
      const srcY = (i: number) => 10 + i * ((h - 20) / n) + ((h - 20) / n - boxH) / 2

      if (on) {
        acc += dt * 28
        while (acc >= 1) {
          acc -= 1
          puffs.push({ from: Math.floor(Math.random() * n), born: now, life: 1.6 + Math.random() * 0.8, drift: (Math.random() - 0.5) * 60, size: 6 + Math.random() * 10 })
        }
      }
      puffs = puffs.filter((p) => now - p.born < p.life)

      ctx.clearRect(0, 0, w, h)
      // suspect servers: red smoke inside fades as the proof moves it out
      servers.forEach((sv, i) => {
        const y = srcY(i)
        ctx.fillStyle = `rgba(239,68,68,${0.12 + 0.3 * (1 - moved)})`
        ctx.fillRect(srcX, y, boxW, boxH)
        ctx.strokeStyle = '#ef4444'
        ctx.lineWidth = 2
        ctx.strokeRect(srcX, y, boxW, boxH)
        ctx.fillStyle = '#f1f5f9'
        ctx.font = '800 16px "Segoe UI", system-ui'
        ctx.textAlign = 'left'
        ctx.fillText(sv, srcX + 8, y + 20)
      })
      // target (spare)
      ctx.fillStyle = `rgba(239,68,68,${0.08 + 0.35 * moved})`
      ctx.fillRect(tgtX, tgtY, boxW, 68)
      ctx.strokeStyle = moved > 0.05 ? '#ef4444' : '#475569'
      ctx.setLineDash([6, 4])
      ctx.strokeRect(tgtX, tgtY, boxW, 68)
      ctx.setLineDash([])
      ctx.fillStyle = '#f1f5f9'
      ctx.fillText(target, tgtX + 8, tgtY + 22)
      ctx.font = '500 12px "Segoe UI", system-ui'
      ctx.fillStyle = '#cbd5e1'
      ctx.fillText(tenant ? `${tenant}'s heavy work` : 'suspect work', tgtX + 8, tgtY + 42)
      // smoke puffs travel from a source box to the target along a drifting curve
      for (const p of puffs) {
        const t = (now - p.born) / p.life
        const x0 = srcX + boxW
        const y0 = srcY(p.from) + boxH / 2
        const x1 = tgtX + 10
        const y1 = tgtY + 34
        const x = x0 + (x1 - x0) * t
        const y = y0 + (y1 - y0) * t + Math.sin(t * Math.PI) * p.drift
        const r = p.size * (0.6 + t)
        const g = ctx.createRadialGradient(x, y, 0, x, y, r)
        g.addColorStop(0, `rgba(239,68,68,${0.55 * (1 - t)})`)
        g.addColorStop(1, 'rgba(239,68,68,0)')
        ctx.fillStyle = g
        ctx.beginPath()
        ctx.arc(x, y, r, 0, Math.PI * 2)
        ctx.fill()
      }
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
    }
  }, [servers, target, tenant])

  return (
    <div className="flex h-full flex-col" data-testid="proof" data-running={running ? '1' : '0'}>
      <div className="flex items-baseline justify-between gap-2">
        <div className="panel-title">Proof · relocation test</div>
        <button className="btn-ghost px-2 py-0.5 text-sm" onClick={() => setReplayUntil(Date.now() + REPLAY_S * 1000)} data-testid="proof-replay">
          {live ? 'proving now' : running ? 'replaying…' : 'Replay proof'}
        </button>
      </div>
      <div ref={wrapRef} className="relative min-h-[150px] flex-1">
        <canvas ref={canvasRef} className="absolute inset-0" aria-label="Proof animation: suspect's work moving to the spare server" />
      </div>
      <p className="mt-1 text-sm text-ink-300">
        {tenant
          ? `TenantGuard moves ${tenant}'s heavy work from ${servers.join(' ')} to ${target}. If the neighbours recover while ${target} slows down, ${tenant} is proven to be the cause.`
          : `Moving the suspect work to ${target}: if the slowdown follows it, the cause is proven.`}
      </p>
    </div>
  )
}
