/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BRAIN_URL?: string
  readonly VITE_GATEWAY_URL?: string
  readonly VITE_LAB_URL?: string
  readonly VITE_HANDOFF_URL?: string
  readonly VITE_MOCK?: string
  readonly VITE_CONTROL_TOKEN?: string
  readonly VITE_P95_WARN_MS?: string
  readonly VITE_P95_BAD_MS?: string
  readonly VITE_NOISY_WARN?: string
  readonly VITE_NOISY_THROTTLE?: string
  readonly VITE_NOISY_QUARANTINE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
