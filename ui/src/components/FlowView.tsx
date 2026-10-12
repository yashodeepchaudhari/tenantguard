import { useEffect, useRef } from 'react'
import { config } from '../config.ts'
import { pickServer, routeTable } from '../lib/flow.ts'
import { FLEET, serverStatus, STATUS_HEX, tenantStatus } from '../lib/status.ts'
import type { OverridesView, StateSnapshot } from '../types.ts'

interface Props {
  snapshot: StateSnapshot
  overrides: OverridesView | null
  barcodes: Record<string, string[]>
}

interface Dot {
  tenant: string
  to: string | null // server, or null = rejected at the gateway
  born: number
  life: number
  colour: string
  lane: number // small vertical jitter
}

const TRAVEL_S = 1.6
const MAX_DOTS_PER_S = 140
const EXTRA = ['spare', 'quarantine']

/** Live request flow: tenant -> gateway -> server, sampled from snapshot rps. */
export function FlowView({ snapshot, overrides, barcodes }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  // the animation loop reads the latest data through refs (no re-subscribe per snapshot)
  const data = useRef({ snapshot, overrides, barcodes })
  data.current = { snapshot, overrides, barcodes }

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

    let dots: Dot[] = []
    const debt: Record<string, number> = {}
    let last = performance.now() / 1000
    let raf = 0
    let seed = 1
    const rand = () => {
      seed = (seed * 16807) % 2147483647
      return seed / 2147483647
    }

    const frame = () => {
      const now = performance.now() / 1000
      const dt = Math.min(0.1, now - last)
      last = now
      const { snapshot: s, overrides: ov, barcodes: bc } = data.current
      const routes = routeTable(s.tenants.map((t) => t.tenant), bc, ov)
      const servers = new Map(s.servers.map((x) => [x.server, x]))

      // ---- layout ----
      const padX = 16
      const leftW = Math.min(340, w * 0.28)
      const gateX = leftW + (w - leftW) * 0.3
      const gridX0 = leftW + (w - leftW) * 0.45
      const gridW = w - gridX0 - padX
      const extraH = 54
      const gridH = h - extraH - 24
      const cellW = gridW / 3
      const cellH = gridH / 3
      const rowH = (h - 16) / Math.max(1, s.tenants.length)
      const tenantY = new Map(s.tenants.map((t, i) => [t.tenant, 8 + rowH * (i + 0.5)]))
      const box = new Map<string, { x: number; y: number; w: number; h: number }>()
      FLEET.forEach((sv, i) => {
        const c = i % 3
        const r = Math.floor(i / 3)
        box.set(sv, { x: gridX0 + c * cellW + 4, y: 8 + r * cellH + 4, w: cellW - 8, h: cellH - 8 })
      })
      EXTRA.forEach((sv, i) => box.set(sv, { x: gridX0 + i * (gridW / 2) + 4, y: 8 + gridH + 14, w: gridW / 2 - 8, h: extraH - 8 }))

      // ---- spawn ----
      const totalRps = s.tenants.reduce((a, t) => a + t.rps + t.rejected_rps, 0)
      const scale = totalRps > 0 ? Math.min(1, MAX_DOTS_PER_S / totalRps) : 0
      for (const t of s.tenants) {
        const st = tenantStatus(t, config)
        const colour = st === 'ok' || st === 'idle' ? '#94a3b8' : STATUS_HEX[st]
        for (const [kind, rate] of [
          ['ok', t.rps],
          ['rej', t.rejected_rps],
        ] as const) {
          if (rate <= 0) continue
          const key = `${t.tenant}:${kind}`
          debt[key] = (debt[key] ?? 0) + Math.max(0.4, rate) * scale * dt
          while (debt[key] >= 1) {
            debt[key] -= 1
            const to = kind === 'rej' ? null : pickServer(routes[t.tenant] ?? [], rand())
            dots.push({ tenant: t.tenant, to, born: now, life: kind === 'rej' ? TRAVEL_S * 0.45 : TRAVEL_S, colour: kind === 'rej' ? STATUS_HEX.bad : colour, lane: (rand() - 0.5) * 10 })
          }
        }
      }
      dots = dots.filter((d) => now - d.born < d.life + 0.25).slice(-1500)

      // ---- draw ----
      ctx.clearRect(0, 0, w, h)

      // route lines (width = weight) for tenants being steered
      for (const t of s.tenants) {
        const steer = ov?.route_weights?.[t.tenant] || ov?.relocations?.some((r) => r.tenant === t.tenant)
        if (!steer) continue
        const y = tenantY.get(t.tenant)!
        for (const r of routes[t.tenant] ?? []) {
          const b = box.get(r.server)
          if (!b || r.weight <= 0) continue
          ctx.strokeStyle = 'rgba(56,189,248,0.35)'
          ctx.lineWidth = 1 + r.weight * 5
          ctx.beginPath()
          ctx.moveTo(leftW, y)
          ctx.quadraticCurveTo(gateX, y, b.x, b.y + b.h / 2)
          ctx.stroke()
        }
      }

      // gateway bar
      ctx.fillStyle = '#273248'
      ctx.fillRect(gateX - 3, 8, 6, h - 16)
      ctx.fillStyle = '#cbd5e1'
      ctx.font = '600 13px "Segoe UI", system-ui'
      ctx.textAlign = 'center'
      ctx.fillText('GATEWAY', gateX, h - 2)

      // servers
      for (const [sv, b] of box) {
        const x = servers.get(sv)
        const st = serverStatus(x)
        const col = st === 'ok' ? STATUS_HEX.ok : st === 'idle' ? '#475569' : STATUS_HEX[st]
        ctx.fillStyle = st === 'bad' ? 'rgba(239,68,68,0.22)' : st === 'warn' ? 'rgba(245,158,11,0.15)' : 'rgba(34,197,94,0.06)'
        ctx.fillRect(b.x, b.y, b.w, b.h)
        ctx.strokeStyle = col
        ctx.lineWidth = st === 'bad' ? 3 : 2
        if (x?.status === 'drained') ctx.setLineDash([6, 4])
        ctx.strokeRect(b.x, b.y, b.w, b.h)
        ctx.setLineDash([])
        ctx.textAlign = 'left'
        ctx.fillStyle = '#f1f5f9'
        ctx.font = '800 18px "Segoe UI", system-ui'
        ctx.fillText(sv, b.x + 8, b.y + 22)
        if (x) {
          ctx.font = '500 13px "Segoe UI", system-ui'
          ctx.fillStyle = st === 'bad' ? STATUS_HEX.bad : '#cbd5e1'
          const label = x.status === 'slow' ? `SLOW ${x.peer_ratio.toFixed(1)}× peers` : x.status === 'drained' ? 'drained' : `${x.cpu_pct.toFixed(0)} % CPU`
          ctx.fillText(label, b.x + 8, b.y + 40)
        }
      }

      // tenants
      for (const t of s.tenants) {
        const y = tenantY.get(t.tenant)!
        const st = tenantStatus(t, config)
        ctx.fillStyle = st === 'idle' ? '#64748b' : STATUS_HEX[st]
        ctx.beginPath()
        ctx.arc(padX + 6, y, 6, 0, Math.PI * 2)
        ctx.fill()
        ctx.textAlign = 'left'
        ctx.fillStyle = '#f1f5f9'
        ctx.font = '800 17px "Segoe UI", system-ui'
        ctx.fillText(t.tenant, padX + 18, y + 6)
        ctx.font = '500 13px "Segoe UI", system-ui'
        ctx.fillStyle = '#cbd5e1'
        const p95 = t.p95_ms > 0 ? `${Math.round(t.p95_ms)} ms` : 'no traffic'
        const steered = !!(ov?.route_weights?.[t.tenant] || ov?.relocations?.some((r) => r.tenant === t.tenant))
        const stats = ` ${t.rps.toFixed(1)} rps · ${p95}`
        const room = leftW - (padX + 38) - (steered ? 22 : 6)
        let name = t.name
        while (name.length > 3 && ctx.measureText(`${name}${stats}`).width > room) name = name.slice(0, -1)
        if (name !== t.name) name = `${name.slice(0, -1)}…`
        ctx.fillText(`${name}${stats}`, padX + 38, y + 5)
        if (steered) {
          ctx.fillStyle = '#38bdf8'
          ctx.font = '800 16px "Segoe UI", system-ui'
          ctx.fillText('↷', leftW - 18, y + 6)
        }
      }

      // dots
      for (const d of dots) {
        const y0 = tenantY.get(d.tenant)
        if (y0 === undefined) continue
        const p = Math.min(1, (now - d.born) / d.life)
        const x0 = leftW
        let x1: number
        let y1: number
        if (d.to === null) {
          x1 = gateX
          y1 = y0 + d.lane
        } else {
          const b = box.get(d.to)
          if (!b) continue
          x1 = b.x + b.w * 0.15 + (d.lane + 5) * (b.w * 0.07)
          y1 = b.y + b.h * 0.6 + d.lane
        }
        // quadratic curve through the gateway
        const cx = gateX
        const cy = y0 + d.lane
        const u = 1 - p
        const x = u * u * x0 + 2 * u * p * cx + p * p * x1
        const yy = u * u * (y0 + d.lane) + 2 * u * p * cy + p * p * y1
        const fade = p >= 1 ? Math.max(0, 1 - (now - d.born - d.life) / 0.25) : 1
        ctx.globalAlpha = fade
        ctx.fillStyle = d.colour
        if (d.to === null && p >= 1) {
          // rejected: a small red cross at the gateway
          ctx.strokeStyle = STATUS_HEX.bad
          ctx.lineWidth = 2
          ctx.beginPath()
          ctx.moveTo(x - 4, yy - 4)
          ctx.lineTo(x + 4, yy + 4)
          ctx.moveTo(x + 4, yy - 4)
          ctx.lineTo(x - 4, yy + 4)
          ctx.stroke()
        } else {
          ctx.beginPath()
          ctx.arc(x, yy, 3, 0, Math.PI * 2)
          ctx.fill()
        }
        ctx.globalAlpha = 1
      }
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
    }
  }, [])

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-baseline justify-between">
        <div className="panel-title">Live request flow · tenant → gateway → server</div>
        <div className="flex gap-4 text-sm text-ink-300">
          <span>dot ≈ requests (sampled from rps)</span>
          <span className="text-bad">✕ refused at the gateway (429 / 503)</span>
          <span className="text-accent">↷ ― rerouted (route weight = line width)</span>
        </div>
      </div>
      <div ref={wrapRef} className="relative min-h-0 flex-1" data-testid="flow-view">
        <canvas ref={canvasRef} className="absolute inset-0" aria-label="Live request flow from tenants to servers" />
      </div>
    </div>
  )
}
