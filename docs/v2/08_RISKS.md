# 08 — v2 risks

| ID | Risk | Likelihood | Impact | Mitigation | Early warning |
|---|---|---|---|---|---|
| R1 | A pool cgroup slice can't be set up on the Docker host (Docker Desktop on Windows) | High | Med | `cgroup_parent` is optional; caps = max(alloc, floor) plus shares still hold floors against other projects; V4-T2 measures it | V4-T2 ratio off |
| R2 | Container start is slow (≫ 3 s) | Med | Med | Paid tiers never sleep (`warm_min` 1); `cold_wait_ms` set from the V4-T3 measurement | V4-T3 p95 |
| R3 | Per-replica data diverges between replicas (demo data, as in v1) | High | Low | Rendezvous keeps each tenant on one replica; demo data is synthetic | — |
| R4 | GIL: one replica uses less than 1 core | High | Low | `per_replica_max_cpu` from measurement; more replicas instead of bigger ones | CPU plateau per replica |
| R5 | Scaling flaps (start/stop churn) | Med | Med | Hysteresis 2 / 30 / 300 ticks; min 2 while busy; borrowed replicas drained first | Starts per minute |
| R6 | `docker.sock` mounted in the brain is a privileged path | Low | High | Only the brain mounts it; the Admin API is token-protected; document it; production: a scoped proxy (e.g. socket proxy allowing only container ops) | — |
| R7 | Host-level contention (disk I/O, memory bandwidth) is not covered by CPU shares | Med | Med | `external_contention` finding (no blame); optional `BlkioWeight` if the host's I/O scheduler supports it | Projects slow while CPU within alloc |
| R8 | Hash split needs a temporary replica and memory | Low | Low | Abort with "no capacity" (no blame); max 1 split at a time | Split aborts |
| R9 | Brain down → sleeping projects can't wake | Low | Med | Paid tiers warm; 503 + Retry-After; brain restart reconciles from labels | Heartbeat |
| R10 | Kernel share ≠ floor when Σ floors > pool (misconfiguration) | Low | High | `check_capacity` rejects it (I2) | — |
| R11 | Lost pitch: per-project containers look like plain Kubernetes | Med | Med | Scorecard shows what static quotas (the `industry` mode) can't do: lending, instant reclaim, split test, proof before punishment | — |
| R12 | Rewrite risk: v1 features regress (handoff, narrator, UI) | Med | Med | Reuse map (01 §4); keep the v1 contracts used by handoff/narrator unchanged; run the v1 e2e suite | Failing e2e |
| R13 | Estimates are wrong (headroom, timers, thresholds) | High | Low | Every estimate is in the yaml; V4/V7/V8 measurements replace them before the docs are final | — |
