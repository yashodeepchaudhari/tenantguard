// Every number on screen carries a unit. Formatting lives here.
export function ms(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '– ms'
  if (v >= 10000) return `${(v / 1000).toFixed(1)} s`
  if (v >= 1000) return `${(v / 1000).toFixed(2)} s`
  if (v >= 100) return `${Math.round(v)} ms`
  return `${v.toFixed(v < 10 ? 1 : 0)} ms`
}

export function rps(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '– rps'
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} rps`
}

export function pct(frac: number | null | undefined, digits = 0): string {
  if (frac === null || frac === undefined || !Number.isFinite(frac)) return '– %'
  return `${(frac * 100).toFixed(digits)} %`
}

export function times(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '– ×'
  return `${v.toFixed(digits)} ×`
}

export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('en-GB', { hour12: false })
}

export function ago(ts: number, now = Date.now() / 1000): string {
  const d = Math.max(0, now - ts)
  if (d < 60) return `${Math.round(d)} s ago`
  if (d < 3600) return `${Math.round(d / 60)} min ago`
  return `${Math.round(d / 3600)} h ago`
}

export function rupees(v: number): string {
  return `₹${v.toLocaleString('en-IN')}`
}
