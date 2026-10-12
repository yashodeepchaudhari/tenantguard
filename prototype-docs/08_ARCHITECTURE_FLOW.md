# 08: Architecture Flow Diagram

This page shows the whole of TenantGuard as one flow: a tenant's request goes in, gets protected, and is measured, and the loop protects everyone else. For details, see `02_ARCHITECTURE.md` (components), `05_SEQUENCE_DIAGRAMS.md` (step-by-step messages) and the `11_FILES_*.md` catalogs (every function).

---

## 1. End-to-end flow (technical)

```mermaid
flowchart TB
  %% ---------- tenants ----------
  subgraph T["Tenants (12, simulated by the lab)"]
    TA["Tenant A ... L<br/>X-API-Key"]
    TK["Tenant K<br/>(attacker)"]
  end

  %% ---------- data plane ----------
  subgraph DP["DATA PLANE (Docker host)"]
    direction TB
    subgraph GW["Gateway :8000"]
      direction LR
      G1["1 Identify<br/>API key to tenant"] --> G2["2 Path guard<br/>+ body cap"]
      G2 --> G3["3 Admit<br/>rate · CPU-ms cost · concurrency · retry budget"]
      G3 --> G4["4 Fair queue<br/>weighted DRR<br/>express / interactive / batch"]
      G4 --> G5["5 Dispatch<br/>adaptive per-worker window"]
      G5 --> G6["6 Route<br/>tenant barcode = 3 servers"]
    end
    subgraph WP["Worker pool (Docker cgroups)"]
      direction LR
      W0["w0"]:::w
      W1["w1"]:::w
      W2["... w8"]:::w
      SP["spare<br/>(proof / burst lane)"]:::s
      QU["quarantine<br/>(heavy lane)"]:::q
    end
    DB[("SQLite per tenant<br/>+ namespaced cache")]
  end

  %% ---------- control plane ----------
  subgraph CP["CONTROL PLANE"]
    direction TB
    BR1["Ingest<br/>telemetry 250 ms + worker stats 1 Hz"] --> BR2["Detect<br/>noisy score + server health"]
    BR2 --> BR3["Rescue<br/>route victims to healthy servers"]
    BR2 --> BR4["Name<br/>barcode decoder: culprit or host fault"]
    BR2 --> BR5["Throttle ladder<br/>warn, throttle, deprioritize, quarantine"]
    BR5 --> BR6["Recover<br/>step back up when calm"]
  end

  %% ---------- management plane ----------
  subgraph MP["MANAGEMENT PLANE"]
    UI["Control Room UI :5173<br/>tiles · grid · flow view · cases · scorecard"]
    ADM["Admin API<br/>tenants · quotas · mode (token)"]
    HO["Handoff bridge :8400"]
    NA["Narrator :8700<br/>plain-English reports"]
    WEB["Partner on-call app<br/>(Web PS3)"]
  end

  TA --> G1
  TK --> G1
  G3 -- "429 + Retry-After" --> TK
  G6 --> W0 & W1 & W2
  G6 -. "relocation" .-> SP & QU
  W0 & W1 & W2 --> DB
  W0 & W1 & W2 -- "X-TenantGuard-Cpu-Ms<br/>(debit cost bucket)" --> G3

  GW -- "TelemetryFrame" --> BR1
  WP -- "/stats" --> BR1
  BR3 -- "RouteWeightsCmd (TTL)" --> G6
  BR5 -- "LimitsCmd (TTL)" --> G3
  BR6 -- "LimitsCmd 1.0 (clear)" --> G3

  BR4 & BR5 -- "IncidentEvent" --> UI & HO & NA
  CP -- "StateSnapshot 2 Hz" --> UI
  UI --> ADM --> GW
  HO -- "case" --> WEB
  WEB -- "approve / extend / release" --> HO
  HO -- "OperatorCmd" --> BR5

  classDef w fill:#e8f5e9,stroke:#2e7d32;
  classDef s fill:#e3f2fd,stroke:#1565c0;
  classDef q fill:#fff3e0,stroke:#e65100;
```

---

## 2. The protection loop (what happens during an attack)

```mermaid
flowchart LR
  A["Tenant K sends heavy work"] --> B["K's 3 servers slow down"]
  B --> C{"Brain: is anyone<br/>actually hurt?"}
  C -- "no (idle capacity)" --> OK["Let K burst<br/>(work-conserving)"]
  C -- "yes" --> D["RESCUE<br/>move victims to their<br/>other healthy servers (~1-2 s)"]
  D --> E["NAME<br/>only K's barcode covers<br/>all slow servers"]
  E --> F{"Harm still<br/>persists?"}
  F -- "no" --> G["Done: victims safe,<br/>K not punished"]
  F -- "yes" --> H["THROTTLE K<br/>warn, then x0.5 limits, then lower priority, then quarantine"]
  H --> I["Every action has a TTL<br/>and auto-undo"]
  I --> J["RECOVER<br/>+10% per second once calm"]
  J --> G
  E -. "one server slow,<br/>no tenant explains it" .-> HF["HOST FAULT<br/>no tenant blamed"]
```

---

## 3. Simple version (for non-technical listeners)

```mermaid
flowchart LR
  C["Customer<br/>request"] --> S["Security guard<br/>who are you?<br/>within your limits?"]
  S --> Q["Fair queue<br/>everyone takes turns"]
  Q --> W["Worker server<br/>hard CPU/memory limit"]
  W --> R["Response"]
  W -. "measures CPU,<br/>memory, latency" .-> M["Watchman<br/>(brain)"]
  M -. "someone is hurting others:<br/>move victims, slow the culprit" .-> S
  M -. "live status" .-> D["Dashboard"]
```

---

## 4. Where each box lives in the code

| Box in the diagram | Code |
|---|---|
| Identify, path guard, admit | `gateway/tenantguard_gateway/tenants.py`, `security.py`, `admission.py`, `buckets.py`, `retry.py` |
| Fair queue, dispatch, route | `gateway/tenantguard_gateway/scheduler.py`, `limiter.py`, `router.py`, `core.py` |
| Telemetry out of the gateway | `gateway/tenantguard_gateway/telemetry.py` |
| Worker app, per-tenant data | `worker/app/main.py`, `tenancy.py`, `store.py`, `cache.py`, `memory.py`, `jobs.py`, `blackbox.py` |
| Ingest, detect, ladder | `brain/tenantguard_brain/runtime.py`, `core.py`, `score.py`, `ladder.py` |
| Server health, rescue, naming | `brain/tenantguard_brain/health.py`, `rescue.py`, `decoder.py`, `stage3.py` |
| Operator decisions | `brain/tenantguard_brain/main.py` (`POST /api/operator`) |
| Dashboard | `ui/src/` |
| Handoff to the partner app | `integrations/handoff/tenantguard_handoff/` |
| Incident reports | `narrator/tenantguard_narrator/` |
| Shared message formats | `common/tenantguard_common/contracts.py` |
| Every tunable | `tenantguard.yaml` |

**Status:** proving the culprit by relocation to the spare, the five verdicts and the calibrated health thresholds are **in progress** (T12). The attacks beyond flood/whale/hog/memory/retry storm and the scorecard runner are **in progress** (T10). Fair billing is **in progress** (T17).
