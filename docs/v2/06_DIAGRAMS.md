# 06 — v2 diagrams (Mermaid)

**Index**

| Group | Diagrams |
|---|---|
| Structure | 1 Context · 2 Hierarchy · 3 Runtime topology · 4 Components and ownership |
| Flows | 5 End-to-end architecture flow · 6 Gateway request flow · 7 Control tick |
| Lifecycles | 8 Project scaling state machine · 9 Replica lifecycle |
| Sequences | 10 Onboarding · 11 Request lifecycle · 12 Cold wake from zero · 13 Honest spike → scale up · 14 Owner returns (reclaim) · 15 Noisy tenant inside a project · 16 Hash split for hidden work · 17 Brain crash |
| Models and charts | 18 Class diagram · 19 ER diagram · 20 Allocation chart (03 §3 numbers) · 21 Build dependency graph |

## 1. Context
```mermaid
flowchart LR
  ADM(["Admin<br/>sets pool, projects, tenants"])
  TEN(["Tenant apps<br/>X-API-Key"])
  OPS(["Operator / judge"])
  LAB(["Lab: personas + attacks"])
  subgraph TG["TenantGuard v2"]
    GW["Gateway"]
    BR["Brain: control loop"]
    REP["Project replicas<br/>(created on demand)"]
    UI["Dashboard"]
    BILL["Billing"]
  end
  DOCK[("Docker Engine<br/>pool cgroup tg-pool.slice")]
  ADM -->|"Admin API"| BR
  TEN -->|HTTP| GW
  LAB -->|HTTP| GW
  OPS --> UI --> BR
  GW --> REP
  BR -->|"create / start / stop / update"| DOCK
  DOCK --- REP
  BR -->|snapshot| GW
  GW -->|"telemetry 250 ms, wake"| BR
  BILL -->|"/stats"| REP
```

## 2. Hierarchy
```mermaid
flowchart TB
  POOL["Pool<br/>cpu_total 5.75 · mem 2688 MB · headroom 10%"]
  POOL --> H["health · enterprise · w4<br/>floor 1.25 · cap 3.0"]
  POOL --> C["commerce · enterprise · w4<br/>floor 1.25 · cap 3.0"]
  POOL --> M["mobility · pro · w2<br/>floor 0.75 · cap 2.0"]
  POOL --> ME["media · pro · w2<br/>floor 0.75 · cap 2.0"]
  POOL --> F["free · free · w1<br/>floor 0.5 · cap 1.0 · may sleep"]
  POOL --> HR["headroom 0.575 CPU / 269 MB<br/>never promised"]
  H --> A["A"] & D["D"]
  C --> J["J"] & B["B"]
  M --> G["G"] & K["K"] & E["E"]
  ME --> HH["H"] & L["L"]
  F --> CC["C"] & FF["F"] & I["I"]
```

## 3. Runtime topology (example: §3.1 allocation)
```mermaid
flowchart TB
  GW["gateway :8000<br/>snapshot v=n"]
  subgraph SLICE["tg-pool.slice  cpu.max = 5.75 cores"]
    direction TB
    subgraph PH["health (shares ∝ 1.25)"]
      h1["tg-health-1<br/>0.65 cpu · 160 MB"]
      h2["tg-health-2"]
      h3["tg-health-3"]
    end
    subgraph PC["commerce (shares ∝ 1.25)"]
      c1["tg-commerce-1"]
      c2["tg-commerce-2"]
      c3["tg-commerce-3"]
    end
    subgraph PM["mobility (shares ∝ 0.75)"]
      m1["tg-mobility-1<br/>0.55 cpu · 192 MB"]
      m2["tg-mobility-2"]
    end
    subgraph PME["media (idle, warm)"]
      me1["tg-media-1<br/>0.1 cpu cap · 160 MB"]
    end
    subgraph PF["free (idle, warm)"]
      f1["tg-free-1<br/>0.1 cpu cap · 192 MB"]
    end
  end
  BR["brain :8200<br/>docker.sock"]
  GW --> PH & PC & PM & PME & PF
  BR -.manages.-> SLICE
```

## 4. Components and ownership (who may write what)
```mermaid
flowchart LR
  subgraph Mgmt["Management"]
    API["Admin API"] --> REG["registry.py<br/>(pure)"] --> RST["registry_store<br/>(file I/O)"]
  end
  subgraph Ctrl["Control: single writer"]
    TICK["ControlLoop.tick"]
    COL["collect"]
    ALL["allocator (pure)"]
    SCA["scaler (pure)"]
    DET["detector (pure)"]
    LAD["ladder (v1)"]
    POL["policy"]
    PUB["publisher"]
    DA["docker_adapter<br/>(only Docker I/O)"]
  end
  subgraph Data["Data plane"]
    GWY["gateway: identify, admit,<br/>queues, route, window"]
    WRK["replicas (v1 worker)"]
  end
  REG --> TICK
  COL --> TICK
  TICK --> ALL & SCA & DET & LAD & POL & PUB & DA
  PUB -->|Snapshot| GWY
  GWY --> WRK
  GWY -->|telemetry| COL
  WRK -->|"/stats"| COL
  DA -->|"cgroup CPU"| COL
```

## 5. End-to-end architecture flow
```mermaid
flowchart TD
  S1["1. Admin sets pool"] --> S2["2. Admin creates projects<br/>check_capacity: Σ floors ≤ usable"]
  S2 --> S3["3. Admin adds tenants<br/>key shown once, hash stored"]
  S3 --> S4["4. tick: warm replicas started<br/>cpu.weight ∝ floor, memory fixed"]
  S4 --> S5["5. Snapshot pushed to gateway"]
  S5 --> S6["6. Tenants send requests<br/>identify → admit → project queue → DRR → rendezvous → replica"]
  S6 --> S7["7. Telemetry + worker meters + cgroup CPU"]
  S7 --> S8["8. Demand EWMA incl. queued + refused work"]
  S8 --> S9["9. Allocate: floors, then surplus by tier weight"]
  S9 --> S10["10. Scale: drain → stop → lower → start → raise"]
  S7 --> S11["11. Detect inside projects:<br/>attribute → score → ladder;<br/>split test if CPU unattributed"]
  S10 & S11 --> S12["12. New snapshot (replicas, overrides, pins)"]
  S12 --> S6
  S7 --> S13["13. Billing: per-tenant CPU × tier price,<br/>calm reference, SLA credits"]
```

## 6. Gateway request flow
```mermaid
flowchart TD
  IN["request + X-API-Key"] --> ID{"identify<br/>key hash in snapshot?"}
  ID -- no --> E401["401"]
  ID -- yes --> CL["classify<br/>method|path|query keys|body bucket|log2(n)"]
  CL --> RB{"retry budget ok?"}
  RB -- no --> E429R["429 retry_budget"]
  RB -- yes --> AD{"admit<br/>block · tokens (tier × factor) · concurrency"}
  AD -- no --> E429["429 + Retry-After<br/>(refused cost counts toward project demand)"]
  AD -- yes --> COST["cost = costs.reference_cost(tenant, class)<br/>seeded from calibration"]
  COST --> LANE{"lane"}
  LANE --> Q["project queue set<br/>express / interactive / batch<br/>DRR among tenants (equal × weight_factor)"]
  Q --> RDY{"project has ready replicas?"}
  RDY -- no --> WAKE["request_wake(project), debounced 2 s<br/>wait up to cold_wait_ms"]
  WAKE --> RDY
  RDY -- yes --> PIN{"split pin for tenant?"}
  PIN -- yes --> PR["pinned replica"]
  PIN -- no --> HR["rendezvous: max hash64(tenant, replica)<br/>spill to next if window full"]
  PR & HR --> WIN{"Gradient2 window free<br/>for this lane?"}
  WIN -- no --> Q
  WIN -- yes --> FWD["forward"]
  FWD --> OBS["observe: Cpu-Ms → costs.update<br/>window.observe(rtt − cpu)<br/>telemetry"]
  Q -- "waited too long" --> E503["503 queue_timeout"]
```

## 7. Control tick
```mermaid
flowchart TD
  T["tick (1 s) or wake"] --> V["collect.views"]
  V --> ST{"telemetry stale over 2 s?"}
  ST -- yes --> HOLD["keep allocs + replicas<br/>re-publish same policy"] --> PUSH
  ST -- no --> IN2["poll_workers + docker cpu_ms"]
  IN2 --> DEM["estimate_demand per project"]
  DEM --> ALC["allocate → water_fill"]
  ALC --> PLN["scaler.plan<br/>replicas_needed · cpu_settings · replica_memory_mb"]
  PLN --> APP["docker_adapter.apply<br/>(failures returned, retried next tick)"]
  IN2 --> ATT["detector.attribute"]
  ATT --> SCO["detector.score → Ladder.step"]
  ATT --> SPL["detector.split_step (if triggered)"]
  V --> EXT["detector.external_contention"]
  SCO & SPL & EXT --> POLF["policy.apply_finding<br/>(event written first)"]
  APP & POLF --> BLD["publisher.build snapshot v+1"]
  BLD --> PUSH["publisher.push → gateway"]
```

## 8. Project scaling state machine
```mermaid
stateDiagram-v2
  [*] --> warm: create (warm_min 1)
  [*] --> asleep: create (warm_min 0)
  asleep --> waking: wake (first request)
  waking --> warm: replica ready
  warm --> busy: demand over 0.5 core (est.)
  busy --> busy: need changes → add now (2 ticks) / remove one (30 ticks)
  busy --> warm: demand low 30 ticks
  warm --> asleep: idle 300 ticks and warm_min 0
  note right of busy
    min 2 replicas while busy
    replicas = ceil(demand / (1.0 × 0.8))
    capped by cap_cpu and free memory
  end note
```

## 9. Replica lifecycle
```mermaid
stateDiagram-v2
  [*] --> starting: docker create + start (labels tg.project, tg.replica, tg.borrowed)
  starting --> ready: /healthz ok → added to snapshot
  starting --> [*]: start failed → retried next tick
  ready --> ready: update NanoCpus / CpuShares (live)
  ready --> draining: scale down / reclaim borrowed / replace
  draining --> [*]: in-flight = 0 or 2 s → docker stop + rm
  ready --> [*]: crashed → gateway marks down, scaler replaces
```

## 10. Sequence: onboarding
```mermaid
sequenceDiagram
  autonumber
  actor AD as Admin
  participant API as Brain Admin API
  participant REG as registry
  participant ST as registry_store
  participant CL as ControlLoop
  participant DA as docker_adapter
  participant GW as Gateway
  AD->>API: PUT /admin/pool {cpu 5.75, mem 2688}
  API->>REG: set_pool → check_capacity
  API->>ST: save
  AD->>API: POST /admin/projects {health, enterprise, floor 1.25, cap 3.0}
  API->>REG: create_project → check_capacity (Σ floors ≤ 5.175)
  API->>ST: save
  AD->>API: POST /admin/projects/health/tenants {A}
  API->>REG: add_tenant → sha256(key)
  API-->>AD: {tenant_id A, api_key tk_...} (shown once)
  API->>CL: wake(health)
  CL->>DA: apply [start tg-health-1 160 MB, shares, NanoCpus]
  DA-->>CL: ready
  CL->>GW: push snapshot (keys, replicas)
```

## 11. Sequence: request lifecycle
```mermaid
sequenceDiagram
  autonumber
  participant T as Tenant A
  participant GW as Gateway
  participant R as tg-health-2
  participant BR as Brain
  T->>GW: GET /api/search?n=50000 (X-API-Key)
  GW->>GW: identify → (A, health); classify (size bucket 2^16)
  GW->>GW: retry ok · tokens ok · concurrency ok
  GW->>GW: cost = reference_cost(A, class) (calibration seed 3.83 ms → EWMA)
  GW->>GW: enqueue health/interactive; DRR picks A
  GW->>GW: route: rendezvous(A) = tg-health-2, window free
  GW->>R: forward + X-TenantGuard-Tenant A
  R-->>GW: 200 + X-TenantGuard-Cpu-Ms
  GW->>GW: costs.update · window.observe · telemetry
  GW-->>T: 200
  GW-->>BR: telemetry (250 ms batch)
```

## 12. Sequence: cold wake from zero (free project)
```mermaid
sequenceDiagram
  autonumber
  participant T as Tenant C
  participant GW as Gateway
  participant BR as ControlLoop
  participant DA as docker_adapter
  participant B as Borrowed replica (mobility)
  T->>GW: request
  GW->>GW: project free has 0 ready replicas → queue (≤ 5000 ms)
  GW->>BR: POST /control/wake {free} (debounced)
  BR->>BR: tick now: demand(free) > 0
  alt free memory available
    BR->>DA: start tg-free-1 (192 MB)
  else memory held by a borrowed replica
    BR->>GW: snapshot: B draining
    BR->>DA: stop B after in-flight = 0 or 2 s
    BR->>DA: start tg-free-1
  end
  DA-->>BR: ready
  BR->>GW: snapshot (free: tg-free-1 ready)
  GW->>GW: dispatch queued requests
  GW-->>T: 200 (latency includes cold start, est. 1-3 s, measure)
```

## 13. Sequence: honest spike → scale up (lending)
```mermaid
sequenceDiagram
  autonumber
  participant K as Tenant K (mobility)
  participant GW as Gateway
  participant BR as ControlLoop
  participant DA as docker_adapter
  participant K1 as Kernel (pool cgroup)
  K->>GW: traffic 3× normal
  K1->>K1: idle CPU of media and free flows to mobility at once (work-conserving)
  GW-->>BR: queued_cost(mobility) rising
  BR->>BR: demand ↑ → allocate: mobility 0.75 + loan → 1.095
  BR->>BR: replicas_needed = ceil(1.095 / 0.8) = 2 (held 2 ticks)
  BR->>DA: start tg-mobility-2 (192 MB) · NanoCpus 0.55 each
  BR->>GW: snapshot (2 replicas) → rendezvous moves about half of mobility's tenants
  Note over GW,K: K served, not refused; no other project dropped below its floor
```

## 14. Sequence: owner returns (reclaim)
```mermaid
sequenceDiagram
  autonumber
  participant M as Media tenants
  participant K1 as Kernel
  participant BR as ControlLoop
  participant DA as docker_adapter
  participant GW as Gateway
  M->>GW: traffic resumes
  GW->>K1: media replica becomes runnable
  K1->>K1: media gets ≥ floor share at once (weight ∝ 0.75)
  Note over K1: no software step needed for the floor
  BR->>BR: next tick: allocate (03 §3.2) → others' caps lowered
  BR->>DA: plan order: lower NanoCpus (health, commerce, mobility) → raise media
  BR->>GW: snapshot
  Note over BR: if media needs another replica and memory is tight, a borrowed replica is drained first
```

## 15. Sequence: noisy tenant inside a project
```mermaid
sequenceDiagram
  autonumber
  participant G as Tenant G (noisy)
  participant E as Tenant E (same project)
  participant GW as Gateway
  participant BR as Brain
  G->>GW: whale searches
  GW->>GW: DRR charges G by cost, E still gets its turns
  GW-->>BR: E p95 ↑ vs baseline
  BR->>BR: attribute → G holds 80% of mobility CPU, fair 1/3
  BR->>BR: score(G) = 0.8 / 0.33 × (1 + impact) ≥ 2.0 held → throttle
  BR->>GW: snapshot override G factor 0.5 (TTL 60 s)
  GW-->>G: 429 above the reduced rate / concurrency
  Note over BR: other projects unaffected throughout (own cgroups)
  BR->>BR: calm 10 s → AIMD +0.1/s → clear
```

## 16. Sequence: hash split for hidden work
```mermaid
sequenceDiagram
  autonumber
  participant BR as Detector
  participant SC as Scaler
  participant GW as Gateway
  participant R0 as tg-free-1
  participant R1 as tg-free-2 (temporary, borrowed)
  BR->>BR: free: unattributed CPU 40% for 10 s, tenant I hurt → trigger
  BR->>SC: need 2 replicas for split
  SC->>R1: start (if memory) else abort "no capacity"
  loop round r = 0 .. ceil(log2 3) - 1
    BR->>GW: pins: bit(hash(t, r)) = 0 → R0, 1 → R1
    Note over R0,R1: settle 2 s, sample 6 s
    BR->>BR: heavier unattributed CPU → keep that half
  end
  BR->>BR: Finding(unattributed_work, tenant F, evidence per round)
  BR->>GW: clear pins
  SC->>R1: drain + stop
  BR->>BR: policy.apply_finding → ladder evidence for F
```

## 17. Sequence: brain crash
```mermaid
sequenceDiagram
  autonumber
  participant BR as Brain
  participant GW as Gateway
  participant K1 as Kernel
  participant T as Tenants
  BR->>GW: snapshot v=n (overrides TTL 60 s)
  Note over BR: crash
  T->>GW: traffic continues
  GW->>GW: last snapshot: same replicas, rendezvous, DRR, windows
  K1->>K1: floors still enforced by cpu.weight
  GW->>GW: overrides expire after TTL → tier limits
  T->>GW: request for a sleeping project → 503 Retry-After (no one to wake it)
  BR->>BR: restart → docker_adapter.list_replicas (labels) → GET state → tick
  BR->>GW: snapshot v=n+1
```

## 18. Class diagram
```mermaid
classDiagram
  class Registry {
    pool Pool
    projects map
    tenants map
    version int
  }
  class Pool {
    cpu_total float
    mem_total_mb int
    headroom_ratio float
  }
  class ProjectSpec {
    id str
    tier Tier
    floor_cpu float
    cap_cpu float
    warm_min int
    sla_p95_ms int
  }
  class Tenant {
    id str
    project_id str
    key_sha256 str
  }
  class Replica {
    id str
    project_id str
    state ReplicaState
    borrowed bool
    nano_cpus int
    cpu_shares int
    mem_mb int
  }
  class Alloc {
    base float
    loan float
    cap float
  }
  class Snapshot {
    version int
    keys map
    replicas map
    overrides list
    split_pins map
  }
  class Finding {
    kind str
    project_id str
    tenant str
    evidence dict
  }
  class ControlLoop {
    tick()
    wake(project_id)
  }
  Registry "1" o-- "1" Pool
  Registry "1" o-- "*" ProjectSpec
  ProjectSpec "1" o-- "*" Tenant
  ProjectSpec "1" o-- "*" Replica
  ControlLoop ..> Alloc : allocate
  ControlLoop ..> Replica : plan and apply
  ControlLoop ..> Finding : detect
  ControlLoop ..> Snapshot : publish
```

## 19. ER diagram (persistent data)
```mermaid
erDiagram
  POOL ||--o{ PROJECT : contains
  PROJECT ||--o{ TENANT : has
  PROJECT ||--o{ REPLICA : "runs (runtime, from labels)"
  TENANT ||--o{ EVENT : "findings, ladder steps"
  PROJECT ||--o{ EVENT : "scaling actions"
  POOL {
    float cpu_total
    int mem_total_mb
    float headroom_ratio
  }
  PROJECT {
    string id
    string tier
    float floor_cpu
    float cap_cpu
    int warm_min
  }
  TENANT {
    string id
    string project_id
    string key_sha256
  }
  EVENT {
    float ts
    string kind
    string subject
    string evidence
  }
```

## 20. Allocation chart (03 §3.1 → 3.2, computed)
```mermaid
xychart-beta
  title "CPU per project: media idle, then media wakes"
  x-axis ["media idle", "media wakes"]
  y-axis "cores" 0 --> 2.2
  bar [1.94, 1.608]
  bar [1.94, 1.608]
  bar [1.095, 0.929]
  bar [0.1, 0.929]
  bar [0.1, 0.1]
```
The bars, in order, are health, commerce, mobility, media and free.

## 21. Build dependency graph (07)
```mermaid
flowchart LR
  V1["V1 contracts + config"] --> V2["V2 registry + Admin API"]
  V1 --> V3["V3 allocator + scaler (pure)"]
  V1 --> V4["V4 docker_adapter"]
  V1 --> V5["V5 gateway v2"]
  V1 --> V7["V7 worker counter + tenant list from env"]
  V2 & V3 & V4 --> V6["V6 control loop + detector + publisher"]
  V5 & V6 --> V8["V8 lab v2 + scorecard"]
  V6 --> V9["V9 UI"]
  V6 --> V10["V10 billing inputs"]
  V8 --> V11["V11 docs + acceptance"]
  V9 & V10 --> V11
```
