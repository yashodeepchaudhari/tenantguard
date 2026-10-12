// Runtime config from ui/.env (see .env.example). Peer services live on another laptop,
// so nothing here may assume localhost except as a dev default.
const env = import.meta.env

const trim = (u: string | undefined, fallback: string) => (u && u.trim() ? u.trim() : fallback).replace(/\/+$/, '')
const num = (v: string | undefined, fallback: number) => {
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

// ?mock=1 / ?mock=0 in the URL overrides VITE_MOCK (handy for screenshots).
const urlMock = typeof window !== 'undefined' ? new URLSearchParams(window.location.search).get('mock') : null

export const config = {
  brainUrl: trim(env.VITE_BRAIN_URL, 'http://127.0.0.1:8200'),
  gatewayUrl: trim(env.VITE_GATEWAY_URL, 'http://127.0.0.1:8000'),
  labUrl: trim(env.VITE_LAB_URL, 'http://127.0.0.1:8300'),
  handoffUrl: trim(env.VITE_HANDOFF_URL, 'http://127.0.0.1:8400'),
  /** Sent as X-TenantGuard-Control-Token on gateway /admin/* (gateway env TENANTGUARD_CONTROL_TOKEN). */
  controlToken: env.VITE_CONTROL_TOKEN?.trim() || 'dev',
  mock: urlMock !== null ? urlMock === '1' : env.VITE_MOCK === '1',
  p95WarnMs: num(env.VITE_P95_WARN_MS, 150),
  p95BadMs: num(env.VITE_P95_BAD_MS, 400),
  historyS: 60,
  // Fallback only: the UI uses StateSnapshot.noisy_thresholds from the brain when present.
  noisy: {
    warn: num(env.VITE_NOISY_WARN, 1.5),
    throttle: num(env.VITE_NOISY_THROTTLE, 2),
    quarantine: num(env.VITE_NOISY_QUARANTINE, 4),
  },
}

export const wsUrl = (httpUrl: string, path: string) => httpUrl.replace(/^http/, 'ws') + path
