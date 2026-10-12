# 11 · Files: lab

The lab (`lab/`, package `tenantguard_lab`, HTTP API on :8300) generates the traffic TenantGuard is measured against. It keeps calm traffic running for every tenant persona: open-loop Poisson arrivals with a per-tenant request mix, each tenant sending through its own keep-alive HTTP pool and its own API key. It launches and stops attack profiles against one tenant. It also runs scripted scenarios (set the gateway mode, then calm → attack → calm) and writes each measured `RunResult` as JSON to `lab/results/`. `settings` turns `tenantguard.yaml` and `lab/personas.yaml` into a `LabConfig`. `poisson` produces arrival times. `stats` holds the sample recorder and the percentile and recovery math. `engine` owns the HTTP pools and the calm streams. `attacks` defines the attack profiles on top of the engine. `isolation` runs one attack in a separate OS process. `scenario` holds the run timeline and the result math. `core.Lab` ties all of these together, and both the FastAPI app (`app`) and the CLI (`__main__`) are built on it.

Module dependencies (inside the package; `tenantguard_common` is the shared library):
- `settings` → `tenantguard_common.config`, `tenantguard_common.contracts`, PyYAML
- `poisson` → standard library only
- `stats` → standard library only
- `engine` → `poisson`, `settings`, `stats`, httpx
- `attacks` → `engine`, `poisson`, `settings`, `tenantguard_common.contracts`
- `isolation` → `attacks`, `settings`, `stats`, `engine` (imported inside the child process)
- `scenario` → `stats`, `tenantguard_common.contracts`
- `core` → `attacks`, `engine`, `isolation`, `scenario`, `settings`, `stats`, httpx
- `app` → `core`, `settings`, FastAPI
- `__main__` → `core`, `settings`, `app` (lazy import for `serve`), httpx, uvicorn (lazy)

---

### lab/tenantguard_lab/__init__.py
This is the package marker. Its only content is the module docstring: "TenantGuard traffic lab: calm tenant personas, attacks and measured scenario runs."

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| (module docstring only) | n/a | Names the package. Defines no symbols. | Imported implicitly whenever any `tenantguard_lab.*` module is imported |

**Key decisions**
- The package exports nothing at top level. Callers import submodules directly (`tenantguard_lab.core`, `tenantguard_lab.app`, and so on).

---

### lab/tenantguard_lab/__main__.py
This is the command-line interface (`python -m tenantguard_lab ...`). It provides the subcommands `serve`, `calm`, `attack`, `stop`, `status`, `metrics`, `run` and `calibrate`. Commands first look for a running lab server at `LabConfig.lab_url`. If one answers `/healthz`, they drive it over HTTP so the dashboard sees the same attacks. Otherwise, or with `--local`, they build a `core.Lab` in-process.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `_table` | `_table(metrics: Dict[str, Any]) -> str` | Formats a `/lab/metrics` payload as a fixed-width table: tenant + name, rps, ok/s, 429/s, 503/s, err/s, p50/p95/p99. Tenants with zero count are skipped. Adds a line for the lab event-loop lag, with an "(lab overloaded: latencies inflated)" note when lag p99 is above 20 ms, and one line per attack with its counts. | `_watch`, `_calibrate`, `main()` (`metrics` command) |
| `_remote` | `_remote(cfg: LabConfig, local: bool) -> Optional[httpx.Client]` | Returns `None` if `local`. Otherwise opens an `httpx.Client(base_url=cfg.lab_url, timeout=10.0)` and probes `GET /healthz` with a 1.0 s timeout. Returns the client if the probe succeeds, and closes it and returns `None` on any `httpx.HTTPError`. | `main()` (`calm`, `attack`, `run`, `stop`/`status`/`metrics`) |
| `_print_json` | `_print_json(x: Any) -> None` | Prints `json.dumps(x, indent=2)`. | `_local_run`, `main()` |
| `_watch` | `async _watch(lab: Lab, seconds: Optional[float], every: float = 5.0) -> None` | Every `every` seconds, prints a timestamp and `_table(lab.metrics(every))`. With `seconds=None` it loops until interrupted. Otherwise it stops at the deadline, shortening the last sleep. | `_local_calm`, `_local_attack` |
| `_local_calm` | `async _local_calm(cfg: LabConfig, seconds: Optional[float]) -> None` | Builds `Lab(cfg)`, starts calm traffic, watches it, and always calls `lab.aclose()`. | `main()` (`calm` with no server or with `--local`) |
| `_local_attack` | `async _local_attack(cfg: LabConfig, req: AttackReq) -> None` | Builds `Lab(cfg)`, starts calm traffic, calls `lab.launch(req)`, watches for `req.duration_s`, prints `lab.attack_counts(...)`, then closes. | `main()` (`attack` with no server or with `--local`) |
| `_local_run` | `async _local_run(cfg: LabConfig, req: RunReq) -> int` | Calls `lab.start_run(req)`, prints the planned duration, awaits `lab.wait_run()`, and prints the `RunResult` as JSON. Returns 0 on success, or 1 with the error if `info.result is None`. | `main()` (`run` with no server or with `--local`) |
| `_calibrate` | `async _calibrate(cfg: LabConfig, seconds: float) -> int` | Measures real fleet CPU under calm load. Fleet workers are `cfg.workers` minus `spare` and `quarantine`. Starts calm traffic, waits 3 s, snapshots each worker's `GET /stats` → `cpu_total_ms`, waits `seconds`, and snapshots again. Prints the `est_cpu_ms` estimate, measured CPU-ms/s and utilisation over reachable workers, and a suggested `rate_scale = rate_scale × target_fleet_util / util`. Returns 1 if no worker `/stats` was reachable. | `main()` (`calibrate`) |
| `_calibrate.snap` (inner) | `async snap() -> Dict[str, float]` | Reads `cpu_total_ms` from every fleet worker. Workers that fail with `httpx.HTTPError`, `ValueError` or `KeyError` are skipped. | `_calibrate` |
| `main` | `main(argv=None) -> int` | Builds the argparse tree and configures logging (INFO for `serve`, WARNING otherwise). Calls `load_config()` and dispatches. `serve` runs `uvicorn.run(create_app(cfg, autostart=False if --no-calm else None), access_log=False)`. `calm`/`attack`/`run` go remote or local as described above. Remote `run` polls `GET /lab/run/{id}` every 2 s until the state is no longer `running`. `stop`/`status`/`metrics` need a server and return 1 without one. Catches `LabError` (prints `error <status>: <detail>`, returns 1) and `KeyboardInterrupt` (returns 130). | Entry point (`if __name__ == "__main__": sys.exit(main())`); Dockerfile `CMD` |

CLI arguments and defaults as written: `serve --host 0.0.0.0 --port 8300 --no-calm`; `calm --seconds None --scale None --local`; `attack <attack> <tenant> --intensity 1.0 --duration 60.0 --local`; `stop <attack_id|all>`; `status`; `metrics --window 10.0`; `run <scenario> <mode> --tenant None --intensity 1.0 --calm-s 20.0 --attack-s 40.0 --local`; `calibrate --seconds 20.0`.

**Key decisions**
- Remote first, local as fallback. A short 1 s health probe decides, so CLI-launched attacks appear on the dashboard whenever a server is running.
- `stop`, `status` and `metrics` work only against a server. There is no in-process state to inspect.
- `calm --scale` sets `cfg.rate_scale` locally and sends `{"rate_scale": X}` to `POST /lab/calm/start` remotely.
- Calibration diffs the cumulative `cpu_total_ms` counters rather than sampling instantaneous CPU. Utilisation is computed only over workers that answered both snapshots.
- `serve` imports uvicorn and the app lazily, so the other commands don't load FastAPI.

---

### lab/tenantguard_lab/settings.py
This module loads configuration. It merges the platform settings and tenant list from `tenantguard.yaml` (via `tenantguard_common.config`) with `lab/personas.yaml` and environment overrides, producing a `LabConfig`. It also computes the calm-load CPU estimate used in printouts.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `LAB_DIR` | `Path(__file__).resolve().parents[1]` | The `lab/` directory. Base for the default `personas.yaml` and `results/` paths. | `load_config` |
| `Endpoint` (dataclass) | `Endpoint(method: str, path: str, params: Dict[str, Any] = {}, est_cpu_ms: float = 1.0)` | One request kind (`light`/`read`/`search`/`write`). `est_cpu_ms` is used only by the calibration estimate. | `load_config`; `engine.Engine.build`; `attacks.memory`, `attacks.hog` (build ad-hoc endpoints) |
| `Persona` (dataclass) | `Persona(tenant: TenantConfig, rps: float, mix: Dict[str, float], sla_p95_ms: float = 100.0, end_users: int = 0, note: str = "")` | A tenant's calm behaviour: base rate, request mix, SLA and profile fields. | `load_config`; read by `engine.Engine.start_calm`, `core.Lab.metrics`, `app.py GET /lab/personas` |
| `Persona.id` (property) | `id -> str` | Returns `tenant.id`. | `app.py GET /lab/personas` |
| `LabConfig` (dataclass) | fields `gateway_url: str, brain_url: str, lab_url: str, ui_origins: List[str], results_dir: Path, workers: Dict[str, str], worker_cpus: float, personas: Dict[str, Persona], endpoints: Dict[str, Endpoint], attacks: Dict[str, Dict[str, Any]], run: Dict[str, Any], client: Dict[str, Any], rate_scale: float = 1.0, autostart: bool = True, default_attacker: str = "K", target_fleet_util: float = 0.30` | The whole lab configuration. It is mutable: `rate_scale` is changed live by `POST /lab/calm/start`, and tests override fields. | Every module |
| `LabConfig.persona` | `persona(self, tenant: str) -> Persona` | Looks up a persona. Raises `KeyError(f"unknown tenant {tenant!r}")` if it is missing. | `engine.Engine.client`, `attacks.AttackCtx.persona_rate` |
| `_env_bool` | `_env_bool(name: str, default: bool) -> bool` | Reads a boolean environment variable. Unset gives `default`. `"0"`, `"false"`, `"no"`, `"off"` and `""` (case-insensitive, stripped) give `False`, and anything else gives `True`. | `load_config` (`TENANTGUARD_LAB_AUTOSTART`) |
| `load_config` | `load_config(personas_path: Optional[Path] = None) -> LabConfig` | Loads settings and tenants, then reads the personas file (`TENANTGUARD_LAB_PERSONAS` wins over `personas_path`, which wins over `lab/personas.yaml`). Builds endpoints and builds one `Persona` per tenant in `tenantguard.yaml`, using `rps` default 5.0, the default `mix`, `sla_p95_ms` 100.0, `end_users` 0 and `note` "". Raises `ValueError` when a mix names an unknown request kind. Resolves URLs from env or `services.*` (defaults `http://127.0.0.1:8000`/`8200`/`8300`, trailing `/` stripped). CORS origins come from `TENANTGUARD_UI_ORIGINS` (comma list, default `http://127.0.0.1:5173`). Also resolves `results_dir` (`TENANTGUARD_LAB_RESULTS` or `lab/results`), `workers` from `worker_urls(settings)`, `worker_cpus` from `worker.cpus` (default 0.5), `rate_scale` (`TENANTGUARD_LAB_RATE_SCALE` or yaml), `autostart`, `default_attacker` and `target_fleet_util`. | `__main__.main()`, `app.create_app()` (when no cfg is passed); tests |
| `estimated_fleet_cpu_ms_per_s` | `estimated_fleet_cpu_ms_per_s(cfg: LabConfig) -> float` | Sum over personas of `rps × rate_scale × Σ(est_cpu_ms × normalised mix weight)`. | `__main__._calibrate`, `app.py GET /lab/personas` |

**Key decisions**
- The tenant list comes from `tenantguard.yaml`, not from `personas.yaml`. A tenant missing from `personas.yaml` still gets calm traffic with defaults, and persona entries for tenants not in the layout are ignored.
- Mixes are validated against the endpoint table at load time, so a typo fails at startup rather than at request time.
- Environment variables override files for everything a deployment needs to change (URLs, CORS, rate scale, autostart, file locations). This is how `docker-compose.yml` points the lab at `gateway:8000` and `brain:8200`.

---

### lab/tenantguard_lab/poisson.py
This module provides the Poisson arrival process: exponential inter-arrival gaps, a helper that lists arrival offsets, and the async scheduler that fires callbacks on an absolute schedule.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `gaps` | `gaps(rate: float, rng: random.Random) -> Iterator[float]` | Endless generator of `rng.expovariate(rate)` gaps in seconds. Raises `ValueError("rate must be > 0")` for `rate <= 0`. | `arrival_times`; tests (`test_poisson.test_gaps_are_exponential`) |
| `arrival_times` | `arrival_times(rate: float, duration_s: float, rng: random.Random) -> List[float]` | Cumulative arrival offsets in `[0, duration_s)`. | Tests only (`test_poisson.test_arrival_count_within_10pct`) |
| `run_poisson` | `async run_poisson(rate_fn: Callable[[], float], fire: Callable[[], None], stop: asyncio.Event, rng: Optional[random.Random] = None, clock: Callable[[], float] = time.monotonic, max_gap_s: float = 1.0) -> None` | Calls `fire()` at Poisson arrival times until `stop` is set. Uses `next = previous + gap` and sleeps only if `next` is in the future. `rate_fn()` is re-read at every arrival. A rate `<= 0` idles and re-checks every `max_gap_s`. If the schedule falls more than 5.0 s behind, `next` is reset to now. | `engine.Engine.start_calm()` (one per persona), `attacks._open_loop()`; tests |

**Key decisions**
- Poisson arrivals with an absolute schedule. Event-loop jitter or a slow `fire()` does not lower the achieved rate, because late arrivals fire immediately and catch up.
- `rate_fn` is a callable, so changing `cfg.rate_scale` (through `POST /lab/calm/start`) takes effect live without restarting streams.
- The 5 s catch-up cap stops a long stall, such as a paused laptop, from producing an unbounded burst.
- `fire` is synchronous: it schedules work and returns. The open loop never waits for responses.

---

### lab/tenantguard_lab/stats.py
This module holds the client-side measurement code with no I/O: the outcome codes, a numpy-compatible percentile, a thread-safe per-tenant sample recorder, time bucketing, and the time-to-recover algorithm used in `RunResult`.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `TIMEOUT` | `= -1` | Outcome code for a request that timed out. | `engine.Engine.send`; tests |
| `CONN_ERROR` | `= -2` | Outcome code for a connect or transport error. | `engine.Engine.send`; tests |
| `percentile` | `percentile(values: Sequence[float], q: float) -> float` | q-th percentile (0..100) with linear interpolation between closest ranks. Sorts its input. Empty input gives 0.0, a single value gives that value, and `q` outside 0..100 raises `ValueError`. | `summarize`, `time_to_recover`, `engine.Engine.loop_lag_ms`, `scenario._p99`; tests |
| `Sample` (frozen dataclass) | `Sample(ts: float, latency_ms: float, status: int, src: str, kind: str = "")` | One finished request: wall-clock completion time (epoch s), latency, HTTP status or `TIMEOUT`/`CONN_ERROR`, source `"calm"` or `"attack"`, and request kind (endpoint kind or attack name). | `engine.Engine.send`, `isolation.ProcessAttack.drain`; tests |
| `Summary` (dataclass) | `Summary(count: int = 0, ok: int = 0, r429: int = 0, r503: int = 0, other_err: int = 0, p50_ms: float = 0.0, p95_ms: float = 0.0, p99_ms: float = 0.0, max_ms: float = 0.0)` | Aggregate of a sample set. | `summarize` |
| `Summary.as_dict` | `as_dict(self, window_s: Optional[float] = None) -> dict` | Copies the fields. If `window_s` is given, adds `<count|ok|r429|r503|other_err>_per_s` rounded to 3 decimals. Rounds the latency fields to 3 decimals. | `core.Lab.metrics`; tests |
| `summarize` | `summarize(samples: Iterable[Sample]) -> Summary` | Counts outcomes (200–399 → ok, 429, 503, everything else including negative codes → `other_err`). Computes p50/p95/p99 and max latency. | `core.Lab.metrics`; tests |
| `Recorder` | `class Recorder` | Thread-safe store of recent samples per tenant, plus cumulative counters and client-drop counts. | `engine.Engine`, `core.Lab`, `isolation`, `scenario.compute_result` |
| `Recorder.__init__` | `__init__(self, retention_s: float = 900.0, max_per_tenant: int = 400_000)` | Sets up per-tenant `deque(maxlen=max_per_tenant)`, a `threading.Lock`, `totals: Dict[str, Dict[str, int]]` and `dropped: Dict[str, int]`. | `engine.Engine.__init__`, `core.Lab.__init__`, `isolation._BufferRecorder.__init__`; tests |
| `Recorder.add` | `add(self, tenant: str, sample: Sample) -> None` | Appends a sample, prunes samples older than `sample.ts - retention_s`, and increments cumulative `totals` (`count`, `ok`, `r429`, `r503`, `other_err`). | `engine.Engine.send`, `isolation.ProcessAttack.drain`; tests |
| `Recorder.drop` | `drop(self, tenant: str) -> None` | Counts a client-side drop (the arrival happened but the in-flight cap was reached). | `engine.Engine.start_calm` (inner `fire`) |
| `Recorder.tenants` | `tenants(self) -> List[str]` | Sorted tenant ids that have samples. | No caller in `lab/` on this branch |
| `Recorder.window` | `window(self, tenant: str, t0: float, t1: float, src: Optional[str] = None, kinds: Optional[Iterable[str]] = None) -> List[Sample]` | Samples completed in `[t0, t1)`, optionally filtered by source and request kinds. Copies the deque under the lock and filters outside it. | `core.Lab.metrics`, `scenario.compute_result`; tests |
| `Recorder.reset` | `reset(self) -> None` | Clears samples, totals and drops. | No caller in `lab/` on this branch |
| `bucketize` | `bucketize(samples: Iterable[Sample], t0: float, t1: float, bucket_s: float) -> List[List[float]]` | Groups latencies into `ceil((t1 - t0) / bucket_s)` consecutive buckets covering `[t0, t1)` (`t1` exclusive). | `scenario.compute_result`; tests |
| `time_to_recover` | `time_to_recover(buckets: List[List[float]], baseline_p99: float, ratio: float, hold: int, bucket_s: float = 1.0) -> Tuple[Optional[float], bool]` | Marks each non-empty bucket as good (`p99 <= ratio × baseline_p99`) or bad. Finds the first bad bucket. After it, returns the start of the first run of `hold` consecutive good non-empty buckets, as `(start × bucket_s, True)`. Returns `(0.0, False)` if no bucket was ever bad, and `(None, True)` if victims never recovered. Empty buckets neither break nor extend a run. | `scenario.compute_result`; tests |

**Key decisions**
- The percentile matches numpy's default `linear` method, so results can be checked against numpy (tested on `range(1, 101)`).
- Retention is two-fold: time-based (`retention_s`, relative to the newest sample) and count-based (`max_per_tenant`), so a long flood cannot exhaust memory. Cumulative `totals` are never pruned.
- `threading.Lock` is used even though the engine is asyncio, so the recorder is safe if a reader runs on another thread.
- Recovery counts only after the first degraded bucket. An attack that degrades victims late is not reported as instant recovery.
- Negative outcome codes (timeouts, connection errors) count as `other_err`, and 3xx counts as ok.

---

### lab/tenantguard_lab/engine.py
This is the asyncio traffic engine. It owns one keep-alive `httpx.AsyncClient` per (tenant, pool), runs one calm Poisson stream per persona, builds requests from endpoint definitions, sends them and records every outcome into the `Recorder`. It also measures its own event-loop lag so client-side latencies can be trusted or flagged.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `log` | `logging.getLogger("tenantguard_lab.engine")` | Module logger. | This module |
| `InflightGate` | `class InflightGate` | Counts in-flight requests of one stream and refuses new ones past `cap`. This is the open-loop protection. | `Engine.start_calm`, `attacks._open_loop` |
| `InflightGate.__init__` | `__init__(self, cap: int)` | Sets `cap`, `n = 0` and `tasks: Set[asyncio.Task]`. | Same as above |
| `InflightGate.full` | `full(self) -> bool` | `n >= cap`. | `Engine.start_calm` (inner `fire`), `attacks._open_loop` (inner `fire`) |
| `InflightGate.spawn` | `spawn(self, coro) -> asyncio.Task` | Increments `n` and schedules the coroutine. A done-callback decrements `n`, discards the task, and logs a warning if the task raised. | Same as above |
| `InflightGate.spawn._done` (inner) | `_done(t: asyncio.Task) -> None` | Done-callback described above. | asyncio (task completion) |
| `InflightGate.cancel_all` | `async cancel_all(self) -> None` | Cancels all tracked tasks and gathers them with `return_exceptions=True`. | `Engine.stop_calm`, `attacks._open_loop` (finally) |
| `Engine` | `class Engine` | Owns the HTTP pools and calm traffic. Attacks reuse `send` with their own pools. | `core.Lab.__init__`, `isolation._child`; tests |
| `Engine.__init__` | `__init__(self, cfg: LabConfig, recorder: Optional[Recorder] = None, seed: Optional[int] = None, transport: Optional[httpx.AsyncBaseTransport] = None)` | Stores the config, an optional injected transport (tests pass `httpx.MockTransport`), the recorder (a new one by default) and a seeded `random.Random`. Initialises client, calm-task and gate maps, `calm_started_at`, and `loop_lag: Deque[(epoch ts, lag ms)]` with `maxlen=6000`. | `core.Lab.__init__`, `isolation._child.main`; tests |
| `Engine.start_monitor` | `start_monitor(self, every_s: float = 0.1) -> None` | Starts, once, a task that sleeps `every_s` and records how late it woke up, in ms (clamped at 0), into `loop_lag`. | `Engine.start_calm`, `core.Lab.launch` |
| `Engine.start_monitor.run` (inner) | `async run() -> None` | Lag sampling loop. | `Engine.start_monitor` |
| `Engine.loop_lag_ms` | `loop_lag_ms(self, window_s: float) -> Dict[str, float]` | `{"p50", "p99", "max"}` of lag samples within the last `window_s`, rounded to 2 decimals. | `core.Lab.metrics` |
| `Engine.client` | `client(self, tenant: str, pool: str = "calm", max_conns: Optional[int] = None) -> httpx.AsyncClient` | Returns, creating if missing or closed, a keep-alive client keyed by `(tenant, pool)`. It uses `base_url = gateway_url`, the tenant's `X-API-Key` header, `httpx.Timeout(client.timeout_s)`, `max_connections = max_keepalive_connections = max_conns or client.max_inflight_per_tenant`, and the injected transport. | `Engine.start_calm`; `attacks._open_loop`, `attacks.hog` (pool = attack id) |
| `Engine.close_pool` | `async close_pool(self, tenant: str, pool: str) -> None` | Removes and closes one pool. | `core.Lab._supervise` (inline attacks) |
| `Engine.build` | `build(self, ep: Endpoint, extra_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]` | Builds `httpx` request kwargs: method, url and merged params. Paths ending `/read` get a random `key` `k0000`..`k{keyspace-1}` (zero-padded to 4). POSTs get a JSON body `{"key": k####, "value": "v<random 6 dp>"}`. `keyspace` comes from `client.keyspace` (default 1000). | `Engine.start_calm` (inner `fire`); `attacks.flood`, `whale`, `memory`, `hog`, `retry_storm`; tests |
| `Engine.send` | `async send(self, client: httpx.AsyncClient, tenant: str, req: Dict[str, Any], src: str, kind: str = "") -> Tuple[int, Optional[httpx.Headers]]` | Sends one request and times it with `perf_counter`. Maps `httpx.TimeoutException` to `TIMEOUT` and any other `httpx.HTTPError` to `CONN_ERROR`. Records `Sample(time.time(), latency_ms, status, src, kind)` and returns `(status, headers)`. | `Engine.start_calm` (inner `fire`); `attacks._open_loop`, `attacks.hog`, `attacks.retry_storm` |
| `Engine.calm_running` (property) | `calm_running -> bool` | True while the calm stop event exists and is not set. | `Engine.start_calm`, `Engine.stop_calm`, `core.Lab` (`status`, `metrics`, `ensure_calm`, `start_run`), `app.py GET /healthz`; tests |
| `Engine.pick_kind` | `pick_kind(self, mix: Dict[str, float]) -> str` | Weighted random choice of request kind (`rng.choices`). | `Engine.start_calm` (inner `fire`) |
| `Engine.start_calm` | `start_calm(self) -> bool` | Returns False if calm traffic is already running. Otherwise starts the lag monitor and, per persona, creates an `InflightGate(client.max_inflight_per_tenant)`, the `"calm"` pool, and a `run_poisson` task with `rate = persona.rps × cfg.rate_scale` and a per-stream RNG seeded from the engine RNG. Each arrival either records a drop (gate full) or spawns `send(... "calm", kind)`. | `core.Lab.start_calm`, `core.Lab.ensure_calm`; tests |
| `Engine.start_calm.fire` (inner) | `fire(tid=tid, persona=persona, gate=gate, client=client) -> None` | One calm arrival. Uses default-argument binding to capture loop variables. | `poisson.run_poisson` |
| `Engine.start_calm.rate` (inner) | `rate(persona=persona) -> float` | `persona.rps * self.cfg.rate_scale`, read live. | `poisson.run_poisson` |
| `Engine.stop_calm` | `async stop_calm(self) -> bool` | Returns False if not running. Otherwise sets the stop event, cancels and gathers the stream tasks, cancels all in-flight requests through the gates, clears state and resets `calm_started_at`. | `Engine.aclose`, `core.Lab.stop_calm`, `core.Lab.restore_calm`; tests |
| `Engine.calm_inflight` | `calm_inflight(self) -> Dict[str, int]` | In-flight calm requests per tenant. | `core.Lab.metrics` |
| `Engine.aclose` | `async aclose(self) -> None` | Stops calm traffic, cancels the lag task and closes every pool. | `core.Lab.aclose`, `isolation._child.main` (finally); tests |

**Key decisions**
- Each tenant has its own pool, and each attack has a pool keyed by its attack id. An attacker's connections can never queue a victim's request on the client side, so measured victim latency reflects the platform.
- Open loop with an in-flight cap. Arrivals do not wait for responses, which matches real independent users. When the cap (`max_inflight_per_tenant`, 64 in `personas.yaml`) is reached, the arrival is counted as a client drop instead of piling up unbounded tasks.
- Pool size equals the in-flight cap, with all connections kept alive, to avoid reconnect cost during a run.
- Latency is measured client-side around `client.request` with `perf_counter`. The sample timestamp is wall-clock (`time.time()`) so it can be matched against scenario marks and brain incident timestamps.
- The engine measures its own event-loop lag at 10 Hz. `/lab/metrics` exposes it so an overloaded lab is visible rather than silently inflating latencies.
- The transport can be injected so the whole engine runs against `httpx.MockTransport` in tests.

---

### lab/tenantguard_lab/attacks.py
This module defines the attack traffic profiles. Each profile is an async function that runs until `ctx.stop` is set; `core.py` handles duration. `AttackReq.intensity` (0.1..5, enforced by the contract) multiplies the profile's rate (flood, whale, memory, retry_storm) or concurrency (hog). Profile parameters come from `personas.yaml` → `attacks`.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `WAVE2` | `{"background_bomb", "blackbox_bomb", "cache_flood", "faulty_server", "honest_spike"}` | Attack names that exist in the contract but have no runner yet. These are in progress (Wave 2) and the API answers them with 501. | `core.Lab._validate` |
| `AttackCtx` (dataclass) | `AttackCtx(attack_id: str, engine: Engine, req: AttackReq, stop: asyncio.Event, counts: Dict[str, int] = {"sent": 0, "ok": 0, "r429": 0, "r503": 0, "other_err": 0, "retries": 0, "dropped": 0})` | Per-attack state: id, engine to send through, request, stop event and counters. | `core.Lab.launch`, `isolation._child.main` |
| `AttackCtx.tenant` (property) | `tenant -> str` | `req.tenant`, asserted not None. | Runners, `core.Lab._supervise` |
| `AttackCtx.profile` (property) | `profile -> dict` | `engine.cfg.attacks[req.attack]`. | Runners, `_open_loop` |
| `AttackCtx.tally` | `tally(self, status: int) -> None` | Increments `sent` and one of `ok` (200–399), `r429`, `r503` or `other_err`. | `_open_loop.one`, `hog.worker`, `retry_storm.fire` |
| `AttackCtx.persona_rate` | `persona_rate(self) -> float` | The attacker persona's `rps × rate_scale`. | `flood`, `retry_storm` |
| `_open_loop` | `async _open_loop(ctx: AttackCtx, rate: float, make_req: Callable[[], dict], on_fire: Callable[[object], Awaitable[None]] = None) -> None` | Poisson arrivals at a fixed `rate` on the attack's own pool. The cap is the profile's `max_inflight`, or `client.max_inflight_per_attack`. Each arrival sends `make_req()` with `src="attack"` and `kind=req.attack`, or runs `on_fire(client)` instead. Arrivals over the cap increment `counts["dropped"]`. Cancels in-flight requests on exit. | `flood`, `whale`, `memory`, `retry_storm` |
| `_open_loop.one` (inner) | `async one() -> None` | Sends one request and tallies it, or delegates to `on_fire`. | `_open_loop.fire` |
| `_open_loop.fire` (inner) | `fire() -> None` | Gate check, then spawn `one()`. | `poisson.run_poisson` |
| `_endpoint` | `_endpoint(ctx: AttackCtx, kind: str) -> Endpoint` | `cfg.endpoints[kind]`. | `flood`, `whale`, `retry_storm` |
| `flood` | `async flood(ctx: AttackCtx) -> None` | Open loop on the profile's `endpoint` (default `light`) at `multiplier × persona_rate × intensity`. | `RUNNERS["flood"]` |
| `whale` | `async whale(ctx: AttackCtx) -> None` | Open loop on the `search` endpoint with the profile `params` merged over the endpoint params, at `rps × intensity`. | `RUNNERS["whale"]` |
| `memory` | `async memory(ctx: AttackCtx) -> None` | Open loop of `GET` on the profile `path` (default `/api/memory`) with the profile `params`, at `rps × intensity`. | `RUNNERS["memory"]` |
| `hog` | `async hog(ctx: AttackCtx) -> None` | Closed loop. `n = max(1, round(concurrency × intensity))` workers each keep one `GET` on the profile `path` (default `/api/export`) with the profile `params` in flight at all times, on a pool of size `n`. On a negative status (gateway unreachable) a worker sleeps 0.2 s; otherwise it yields with `sleep(0)`. Waits for `ctx.stop`, then cancels the workers. | `RUNNERS["hog"]` |
| `hog.worker` (inner) | `async worker() -> None` | One closed-loop worker. | `hog` |
| `retry_storm` | `async retry_storm(ctx: AttackCtx) -> None` | Open loop on the profile `endpoint` (default `read`) at `multiplier × persona_rate × intensity`. Each arrival retries immediately on 429/503, up to `max_retries` (default 50), ignoring `Retry-After`. | `RUNNERS["retry_storm"]` |
| `retry_storm.fire` (inner) | `async fire(client) -> None` | Sends, tallies, increments `retries` and yields with `sleep(0)` between attempts. Stops on any other status or when `ctx.stop` is set. | `_open_loop` via `on_fire` |
| `RUNNERS` | `Dict[str, Callable[[AttackCtx], Awaitable[None]]]` = flood, whale, hog, memory, retry_storm | Registry of implemented attacks. | `core.Lab._validate`, `core.Lab._supervise` (inline), `isolation._child.main` |

**Key decisions**
- Most attacks are open loop (Poisson, like a real abusive client). `hog` is deliberately closed loop: it holds a fixed number of slow requests in flight to occupy worker capacity.
- Attacks get their own pool and their own in-flight cap (512 by default, 32 for whale and memory per `personas.yaml`), separate from the victims'.
- `flood` and `retry_storm` scale with the attacker's own persona rate. `whale` and `memory` use absolute rates from the profile.
- `retry_storm` has no backoff on purpose (it only yields), to model a misbehaving client that ignores `Retry-After`.
- `hog` sleeps 0.2 s on connection failure so an unreachable gateway does not spin the CPU, and yields on fast answers so it cannot starve the event loop.
- Five contract attack names have no runner (in progress, Wave 2) and are rejected before any traffic starts.

---

### lab/tenantguard_lab/scenario.py
This module handles scripted scenario runs: set the gateway mode, then calm → attack → calm, then compute a `RunResult`. The timeline driver is an abstract interface (`ScenarioDriver`), so tests can run it on a fake clock. The result math (`compute_result`) is a pure function over a `Recorder`.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `Timeline` (frozen dataclass) | `Timeline(warmup_s: float, calm_s: float, attack_s: float, cold_start_s: float = 0.0)` | Planned offsets from run start. `cold_start_s` is extra warmup used when calm traffic was not already running. | `core.Lab.timeline`; tests |
| `Timeline.baseline_start` (property) | `-> float` | `warmup_s`. | `phases`; tests |
| `Timeline.attack_start` (property) | `-> float` | `warmup_s + calm_s`. | `attack_end`, `phases`; tests |
| `Timeline.attack_end` (property) | `-> float` | `attack_start + attack_s`. | `end`, `phases`; tests |
| `Timeline.end` (property) | `-> float` | `attack_end + calm_s`. | `core.Lab.start_run` (planned end); tests |
| `Timeline.phases` | `phases(self) -> List[tuple]` | `[("warmup", 0, baseline_start), ("baseline", ...), ("attack", ...), ("after", ...)]`. | Tests only |
| `Marks` (dataclass) | `Marks(t0: float = 0.0, baseline_start: float = 0.0, attack_start: float = 0.0, attack_end: float = 0.0, end: float = 0.0)` | Actual epoch timestamps of each transition. Measurement windows use these, not the plan. | `run_timeline`, `compute_result`, `core.Lab.time_to_detect`, `core.Lab.write_result`; tests |
| `ScenarioDriver` | `class ScenarioDriver` | The interface the runner needs. `core.Lab` implements it against the real engine. | Subclassed by `core.Lab`; tests (`FakeDriver`) |
| `ScenarioDriver.now` | `now(self) -> float` | `time.time()`. | `run_timeline` |
| `ScenarioDriver.sleep` | `async sleep(self, s: float) -> None` | Abstract (`NotImplementedError`). | `run_timeline` |
| `ScenarioDriver.set_mode` | `async set_mode(self, mode: str) -> None` | Abstract. | `run_timeline` |
| `ScenarioDriver.ensure_calm` | `async ensure_calm(self) -> bool` | Abstract. Starts calm traffic if needed and returns whether it was already running. | `run_timeline` |
| `ScenarioDriver.restore_calm` | `async restore_calm(self, was_running: bool) -> None` | Abstract. | `run_timeline` |
| `ScenarioDriver.start_attack` | `async start_attack(self, req: AttackReq) -> str` | Abstract. Returns the attack id. | `run_timeline` |
| `ScenarioDriver.stop_attack` | `async stop_attack(self, attack_id: str) -> None` | Abstract. | `run_timeline` |
| `ScenarioDriver.on_phase` | `on_phase(self, name: str) -> None` | No-op hook for status reporting. | `run_timeline` |
| `run_timeline` | `async run_timeline(driver: ScenarioDriver, req: RunReq, attacker: Optional[str], tl: Timeline) -> Marks` | Steps: set mode, ensure calm, mark `t0`, warm up (`warmup_s`, plus `cold_start_s` if calm was off), mark `baseline_start`, sleep `calm_s`, start the attack, mark `attack_start` after `start_attack` returns, sleep `attack_s`, stop the attack, mark `attack_end`, sleep `calm_s`, mark `end`. The attack is started with `AttackReq(attack=req.scenario, tenant=attacker, server=req.server, intensity=req.intensity, duration_s=min(600.0, req.attack_s + 30.0))`. In a `finally`, it stops any still-running attack and calls `restore_calm(was_running)`. Phases reported: warmup, baseline, attack, after, done. | `core.Lab._run`; tests |
| `_p99` | `_p99(lat: List[float]) -> float` | `round(percentile(lat, 99), 3)`. | `compute_result` |
| `compute_result` | `compute_result(rec: Recorder, tenants: Iterable[str], req: RunReq, attacker: Optional[str], m: Marks, run_id: str, recover_ratio: float = 1.5, recover_hold_s: float = 5.0, bucket_s: float = 1.0, time_to_detect_s: Optional[float] = None, victim_kinds: Optional[Iterable[str]] = None) -> RunResult` | Victims are all tenants except the attacker. For each victim and phase window (baseline `[baseline_start, attack_start)`, attack `[attack_start, attack_end)`, after `[attack_end, end)`) it takes calm samples of `victim_kinds` (all kinds if None or empty). Computes per-tenant and pooled p99. Buckets the pooled victim samples from `attack_start` to `end` and computes `time_to_recover` with `hold = max(1, round(recover_hold_s / bucket_s))`. Counts the attacker's 429 and 503 responses (all sources) in `[attack_start, end)`. Builds the `RunResult` with `time_to_recover_s` rounded to 3 decimals, or None. | `core.Lab._run`; tests |

**Key decisions**
- `attack_start` is marked after `start_attack` returns, meaning once the attack process is really sending. Spawn delay goes into the baseline window, and the measured attack window covers the full `attack_s` of real attack traffic (tested with a 4 s spawn delay).
- The attack's own `duration_s` is a safety net (`attack_s + 30`, capped at 600, the contract maximum). The runner stops the attack explicitly.
- The `finally` guarantees cleanup on cancellation or failure: the attack is stopped and calm traffic returns to its previous state.
- Only victims' calm traffic counts, filtered to `victim_kinds` (`[light, read]` in `personas.yaml`). A victim's own heavy searches therefore don't set its p99, and attack-source samples recorded under a victim are ignored.
- p99 uses the same numpy-linear `percentile`. Baseline p99 is pooled over all victims.

---

### lab/tenantguard_lab/isolation.py
This module runs one attack in its own OS process. The module docstring gives the reason: httpx costs about 3 ms of CPU per request on the dev laptop, so attack traffic sharing the victims' event loop would inflate victim latency, and the results would measure the lab instead of the platform. The child process runs the attack with its own engine and streams its samples and counters back over a multiprocessing queue. The parent folds them into the shared `Recorder` and `AttackCtx.counts`.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `log` | `logging.getLogger("tenantguard_lab.isolation")` | Module logger. | `ProcessAttack.stop` |
| `FLUSH_S` | `= 0.25` | Interval between child flushes and between parent drain checks, in seconds. | `_child`, `ProcessAttack.pump_until` |
| `_CTX` | `mp.get_context("spawn")` | Spawn-based multiprocessing context. Commented as Windows-safe with the same behaviour on Linux. | `ProcessAttack.__init__` |
| `_BufferRecorder` | `class _BufferRecorder(Recorder)` | Child-side recorder that buffers samples as tuples for shipping instead of storing them. | `_child.main` |
| `_BufferRecorder.__init__` | `__init__(self) -> None` | Calls `Recorder.__init__` and sets `buf: List[Tuple[str, float, float, int, str, str]] = []`. | `_child.main` |
| `_BufferRecorder.add` | `add(self, tenant: str, sample: Sample) -> None` | Appends `(tenant, ts, latency_ms, status, src, kind)` to `buf`. No retention or totals. | `engine.Engine.send` (in the child) |
| `_child` | `_child(cfg: LabConfig, req_json: str, attack_id: str, q, stop_ev, ready_ev) -> None` | Child process entry. Imports `Engine` after spawn and runs `main()` under `asyncio.run`. | `multiprocessing` (target of `ProcessAttack.proc`) |
| `_child.main` (inner) | `async main() -> None` | Builds an `Engine` with a `_BufferRecorder` and an `AttackCtx` (the request is re-validated from JSON). Starts `RUNNERS[attack]` and sets `ready_ev`. Every `FLUSH_S`, puts `("batch", samples, counts)` on the queue until `stop_ev` is set or the runner ends. Then sets `ctx.stop` and waits for the runner up to `client.timeout_s + 2` (cancelling it on timeout). In a `finally`, closes the engine and puts the final `("done", samples, counts)`. | `_child` |
| `_child.main.flush` (inner) | `flush(final: bool = False) -> None` | Swaps out the buffer and enqueues it with a copy of the counts. | `_child.main` |
| `ProcessAttack` | `class ProcessAttack` | Parent-side handle for one attack process. | `core.Lab.launch` |
| `ProcessAttack.__init__` | `__init__(self, cfg: LabConfig, ctx: AttackCtx, recorder: Recorder)` | Creates a spawn `Queue`, `stop_ev`, `ready_ev` and a daemon `Process(name=f"lab-{attack_id}")` whose args are the config, the request as JSON, the attack id and the queue/events. Sets `finished = False`. | `core.Lab.launch` |
| `ProcessAttack.start` | `async start(self, timeout_s: float = 30.0) -> None` | Starts the process and waits (in an executor thread) for `ready_ev`. On timeout it kills the process and raises `RuntimeError`. | `core.Lab.launch` |
| `ProcessAttack.drain` | `drain(self) -> None` | Reads every queued message without blocking. Adds each sample to the parent `Recorder`, replaces `ctx.counts` with the child's counts, and sets `finished` on `"done"`. | `pump_until`, `stop` |
| `ProcessAttack.pump_until` | `async pump_until(self, stop: asyncio.Event, duration_s: float) -> bool` | Drains in a loop until `stop` is set (returns False), the child dies (returns False), or `duration_s` elapses (returns True). Waits on `stop` with a timeout of `min(FLUSH_S, left)`. | `core.Lab._supervise` |
| `ProcessAttack.stop` | `async stop(self) -> None` | Sets `stop_ev`. Drains every 0.05 s until the final batch arrives, the child exits, or `client.timeout_s + 5` s pass. Drains once more, then joins for 5 s in an executor thread, kills the process with a warning if it is still alive, and joins again for 2 s. | `core.Lab._supervise` (finally) |

**Key decisions**
- One process per attack (own event loop, own GIL), so attack CPU cost cannot add latency to victim calm traffic in the parent.
- The `spawn` start method, chosen for Windows compatibility. The config and request are pickled or serialised into the child, and `Engine` is imported only in the child.
- `start()` returns only after the child's runner task exists, which is what makes `run_timeline`'s `attack_start` mark accurate.
- Samples stream back every 250 ms, so dashboards and `/lab/metrics` see attack traffic live, and the final flush ensures no samples are lost at stop.
- Counts from the child are authoritative snapshots (`update`), not increments.
- Shutdown escalates in steps: cooperative stop, then bounded join, then kill. A stuck child cannot hang the lab.

---

### lab/tenantguard_lab/core.py
`Lab` ties the engine, the attack manager and scenario runs together. Both the HTTP API and the CLI use it. It validates and launches attacks (process-isolated or inline), supervises their duration, reports status and metrics, implements `ScenarioDriver` for scripted runs, measures time-to-detect from the brain, and reads and writes `RunResult` files.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `log` | `logging.getLogger("tenantguard_lab.core")` | Module logger. | This module |
| `KEEP_FINISHED_ATTACKS` | `= 20` | How many finished attacks stay in `Lab.attacks`. | `Lab._prune_attacks` |
| `LabError` | `class LabError(Exception)` | Error carrying an HTTP status and detail. | Raised throughout `Lab`; handled by `app.py` exception handler and `__main__.main()` |
| `LabError.__init__` | `__init__(self, status: int, detail: str)` | Stores `status` and `detail`. | `Lab._validate`, `Lab.launch`, `Lab.stop_attack`, `Lab.set_mode`, `Lab.start_run` |
| `AttackEntry` (dataclass) | `AttackEntry(status: AttackStatus, ctx: AttackCtx, task: Optional[asyncio.Task] = None, proc: Optional[ProcessAttack] = None)` | Bookkeeping for one attack. | `Lab.launch` |
| `RunInfo` (dataclass) | `RunInfo(run_id: str, req: RunReq, attacker: Optional[str], state: str = "running", phase: str = "starting", started_at: float = time.time(), planned_end: float = 0.0, result: Optional[RunResult] = None, error: Optional[str] = None)` | State of one scenario run. `state` is one of running, done, failed or cancelled. | `Lab.start_run` |
| `RunInfo.view` | `view(self) -> Dict[str, Any]` | JSON view: run_id, state, phase, scenario, mode, attacker, started_at, planned_end, error, and result (dumped, or None). | `app.py POST /lab/run`, `GET /lab/run/{run_id}`, `DELETE /lab/run` |
| `Lab` | `class Lab(ScenarioDriver)` | The lab. | `app.create_app` lifespan, `__main__` local helpers; tests |
| `Lab.__init__` | `__init__(self, cfg: LabConfig, recorder: Optional[Recorder] = None, transport: Optional[httpx.AsyncBaseTransport] = None)` | Creates the recorder, `Engine(cfg, recorder, transport=transport)`, the attack and run maps, and an admin `httpx.AsyncClient(timeout=5.0, transport=transport)` for gateway `/admin/mode` and brain calls. | Same as above |
| `Lab.start_calm` | `start_calm(self) -> bool` | Delegates to `engine.start_calm()`. | `app.py` lifespan and `POST /lab/calm/start`; `__main__._local_calm`, `_local_attack`, `_calibrate` |
| `Lab.stop_calm` | `async stop_calm(self) -> bool` | Delegates to `engine.stop_calm()`. | `app.py POST /lab/calm/stop` |
| `Lab._validate` | `_validate(self, req: AttackReq) -> None` | Raises 501 for `WAVE2` names ("ships in Wave 2"), 501 for names not in `RUNNERS`, 422 when the tenant is missing, and 404 for an unknown tenant. | `Lab.launch`, `Lab.start_run` |
| `Lab.start_attack` | `async start_attack(self, req: AttackReq) -> str` | `ScenarioDriver` method. Returns `(await launch(req)).attack_id`. | `scenario.run_timeline` |
| `Lab.isolated` (property) | `isolated -> bool` | True when `client.attack_isolation` (default `"process"`) is `"process"` and no transport was injected. | `Lab.launch`; tests |
| `Lab.launch` | `async launch(self, req: AttackReq) -> AttackStatus` | Validates, creates an id `atk-<8 hex>` and an `AttackCtx`. If isolated, creates a `ProcessAttack` and awaits `start()`, mapping `RuntimeError` to `LabError(500)`. Builds an `AttackStatus(state="running", ends_at=now + duration_s)`, ensures the lag monitor is running, registers the entry, starts `_supervise`, logs and prunes. | `app.py POST /lab/attack`, `Lab.start_attack`, `__main__._local_attack`; tests |
| `Lab._supervise` | `async _supervise(self, entry: AttackEntry) -> None` | Process path: `pump_until(ctx.stop, duration_s)`. On timeout the state becomes `done`. In a `finally`, sets stop, calls `proc.stop()` and `_finish`. Inline path: runs `RUNNERS[attack](ctx)` and waits for stop or `duration_s` (timeout → `done`). Then sets stop, waits for the runner up to `client.timeout_s + 2` (cancelling it if needed), logs runner exceptions without crashing, closes the attack pool and calls `_finish`. | Task created in `Lab.launch` |
| `Lab._finish` | `_finish(self, entry: AttackEntry) -> None` | If the state is still `running`, sets it to `done`. Clamps `ends_at` to now and logs the counts. | `Lab._supervise` |
| `Lab.stop_attack` | `async stop_attack(self, attack_id: str) -> AttackStatus` | 404 if unknown. If running, sets the state to `stopped` and signals stop, then awaits the supervisor task. Returns the status. | `app.py DELETE /lab/attack/{attack_id}`, `Lab.stop_all_attacks`, `scenario.run_timeline` (as driver); tests |
| `Lab.stop_all_attacks` | `async stop_all_attacks(self) -> List[AttackStatus]` | Stops every running attack in turn. | `app.py DELETE /lab/attacks`, `Lab.aclose` |
| `Lab._prune_attacks` | `_prune_attacks(self) -> None` | Keeps only the last `KEEP_FINISHED_ATTACKS` finished entries. | `Lab.launch` |
| `Lab.attack_counts` | `attack_counts(self, attack_id: str) -> Dict[str, int]` | Copy of an attack's counters. | `__main__._local_attack`; tests |
| `Lab.status` | `status(self) -> LabStatus` | `LabStatus(attacks=[...], calm_running, run=<id of the running run or None>)`. | `app.py GET /lab/status`, `POST /lab/calm/start`, `POST /lab/calm/stop` |
| `Lab.metrics` | `metrics(self, window_s: float = 10.0) -> Dict[str, Any]` | Per persona over `[now - window_s, now + 1)`: tenant, name, tier, `target_rps`, `sla_p95_ms`, the `summarize(...).as_dict(window_s)` fields for all samples, `calm` and `attack` sub-summaries (`attack` is None when there are none), `inflight`, `client_dropped_total` and `totals`. Top level: ts, window_s, gateway, calm_running, rate_scale, `client_loop_lag_ms`, and attacks (status plus counts). | `app.py GET /lab/metrics`, `__main__._watch`, `__main__._calibrate` |
| `Lab.sleep` | `async sleep(self, s: float) -> None` | `asyncio.sleep(s)`. | `scenario.run_timeline` |
| `Lab.set_mode` | `async set_mode(self, mode: str) -> None` | `POST {gateway_url}/admin/mode {"mode": mode}` with the `H_CONTROL_TOKEN` header from `TENANTGUARD_CONTROL_TOKEN` (default `"dev"`). A transport error or an HTTP status of 300 or above gives `LabError(502)`. | `scenario.run_timeline` |
| `Lab.ensure_calm` | `async ensure_calm(self) -> bool` | Records whether calm traffic was running, starts it and returns the previous state. | `scenario.run_timeline` |
| `Lab.restore_calm` | `async restore_calm(self, was_running: bool) -> None` | Stops calm traffic if it was not running before the run. | `scenario.run_timeline` |
| `Lab.on_phase` | `on_phase(self, name: str) -> None` | Sets `current_run.phase` and logs it. | `scenario.run_timeline` |
| `Lab.timeline` | `timeline(self, req: RunReq) -> Timeline` | `Timeline(warmup_s=run.warmup_s (default 3), calm_s=req.calm_s, attack_s=req.attack_s, cold_start_s=run.cold_start_s (default 0))`. | `Lab.start_run` |
| `Lab.start_run` | `start_run(self, req: RunReq) -> RunInfo` | 409 if a run is in progress. The attacker is `req.tenant`, or `cfg.default_attacker`, and is validated through `_validate`. Creates `RunInfo(run_id="run-<8 hex>")` with `planned_end = started_at + tl.end` (+ `cold_start_s` if calm is off) and schedules `_run`. | `app.py POST /lab/run`, `__main__._local_run`; tests |
| `Lab._run` | `async _run(self, info: RunInfo, tl: Timeline) -> None` | Runs `run_timeline`, sets the phase to `computing`, gets `time_to_detect`, calls `compute_result` with the `run.*` settings (`recover_ratio` 1.5, `recover_hold_s` 5 and `bucket_s` 1.0 as defaults, plus `victim_kinds`), writes the result, and sets the state to `done`. Cancellation sets the state to `cancelled` and re-raises. `LabError` or any other exception sets the state to `failed` with the error. | Task created in `Lab.start_run` |
| `Lab.wait_run` | `async wait_run(self) -> None` | Awaits the run task, swallowing exceptions. | `__main__._local_run`; tests |
| `Lab.cancel_run` | `async cancel_run(self) -> Optional[RunInfo]` | Cancels the run task if it is active and returns the run, or returns None. | `app.py DELETE /lab/run`, `Lab.aclose` |
| `Lab.time_to_detect` | `async time_to_detect(self, m: Marks, attacker: Optional[str]) -> Optional[float]` | `GET {brain_url}/api/incidents?limit=5000[&tenant=attacker]` with a 2.0 s timeout. Accepts a list, or a dict with `incidents` or `events`. Keeps events with a numeric `ts` in `[attack_start, end]` whose `tenant` is None or the attacker. Returns `round(min(ts) - attack_start, 3)`. Returns None when the brain is unreachable, the response is not valid JSON, or nothing matches. | `Lab._run` |
| `Lab.write_result` | `write_result(self, result: RunResult, m: Marks) -> RunResult` | Creates `results_dir` and writes `<YYYYmmdd-HHMMSS of t0>-<scenario>-<mode>.json` (indent 2), with `file` set to `lab/results/<name>`. | `Lab._run` |
| `Lab.list_results` | `list_results(self, scenario: Optional[str] = None, mode: Optional[str] = None) -> List[RunResult]` | Parses every `*.json` in `results_dir`, skipping unreadable files with a warning. Filters by scenario and mode, and sorts by `started_at`, newest first. | `app.py GET /lab/results` |
| `Lab.aclose` | `async aclose(self) -> None` | Cancels the run, stops all attacks, closes the engine and the admin client. | `app.py` lifespan, `__main__` local helpers; tests |

**Key decisions**
- A single `Lab` serves both the API and the CLI, so behaviour is identical whether attacks come from the dashboard or the terminal.
- Errors carry HTTP status codes (`LabError`). The API maps them directly, and the CLI prints them.
- Process isolation is the default and is skipped automatically when a mock transport is injected, so tests can stay in-process.
- Attack state machine: `running` → `stopped` (explicit stop) or `done` (duration elapsed or runner ended). `ends_at` is clamped to the real end time.
- Only one scenario run at a time (409). The run validates the attacker and attack before any traffic starts, so Wave-2 scenarios get 501 at `POST /lab/run`.
- Time-to-detect is optional by design: if the brain is unreachable the result is still written, with `time_to_detect_s = null`.
- Result filenames start with the run's start timestamp, so files sort chronologically on disk. Listing sorts by `started_at` regardless of filename.
- Finished attacks are pruned to 20 to keep `/lab/status` bounded.

---

### lab/tenantguard_lab/app.py
This is the FastAPI app for the traffic lab on :8300. It creates one `Lab` per app lifespan, optionally starts calm traffic on boot, configures CORS for the UI origins, maps `LabError` to JSON error responses, and exposes the status, calm, attack, metrics, run and results endpoints.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `log` | `logging.getLogger("tenantguard_lab.app")` | Module logger. | `lifespan` |
| `CalmBody` (pydantic) | `CalmBody(rate_scale: Optional[float] = Field(None, gt=0, le=20))` | Optional body of `POST /lab/calm/start`. | FastAPI route |
| `create_app` | `create_app(cfg: Optional[LabConfig] = None, autostart: Optional[bool] = None, transport=None) -> FastAPI` | Builds the app. Uses `load_config()` if `cfg` is None. Calm traffic starts on boot when `autostart` is True, or when it is None and `cfg.autostart` is set. `transport` is passed to `Lab`. | `__main__.main()` (`serve`), `app_factory`; tests |
| `lifespan` (inner) | `async lifespan(app: FastAPI)` | Creates `Lab(cfg, transport=transport)` on `app.state.lab`, logs targets, optionally starts calm traffic, and always calls `lab.aclose()` on shutdown. | FastAPI lifespan |
| `_lab_error` (inner) | `async _lab_error(_: Request, e: LabError) -> JSONResponse` | `{"detail": e.detail}` with `status_code = e.status`. | FastAPI exception handler |
| `lab_of` (inner) | `lab_of(request: Request) -> Lab` | `request.app.state.lab`. | All routes |
| `healthz` | `GET /healthz` → `Dict[str, Any]` | `{"ok": True, "gateway": ..., "calm_running": ...}`. | FastAPI route; `__main__._remote` probe |
| `status` | `GET /lab/status` → `LabStatus` | `Lab.status()`. | FastAPI route; `__main__` `status` |
| `personas` | `GET /lab/personas` → `Dict[str, Any]` | `rate_scale`, `estimated_fleet_cpu_ms_per_s` (1 dp), and per persona: tenant, name, tier, rps, effective_rps, mix, sla_p95_ms, end_users, note, `monthly_revenue_rs`, `protected`. | FastAPI route |
| `calm_start` | `POST /lab/calm/start` (body `Optional[CalmBody]`) → `LabStatus` | If `rate_scale` is given, sets `cfg.rate_scale` (read live by every Poisson stream). Starts calm traffic and returns the status. | FastAPI route; `__main__` `calm` |
| `calm_stop` | `POST /lab/calm/stop` → `LabStatus` | Stops calm traffic and returns the status. | FastAPI route |
| `attack` | `POST /lab/attack` (body `AttackReq`) → `AttackStatus` | `Lab.launch(req)`. The contract validates the attack name and `intensity` 0.1..5 (422). `LabError` gives 501, 404, 422 or 500. | FastAPI route; `__main__` `attack` |
| `stop_attack` | `DELETE /lab/attack/{attack_id}` → `AttackStatus` | `Lab.stop_attack`. | FastAPI route; `__main__` `stop <id>` |
| `stop_all` | `DELETE /lab/attacks` → `List[AttackStatus]` | `Lab.stop_all_attacks()`. | FastAPI route; `__main__` `stop all` |
| `metrics` | `GET /lab/metrics?window_s=` (`Query(10.0, gt=0, le=900)`) → `Dict[str, Any]` | `Lab.metrics(window_s)`. | FastAPI route; `__main__` `metrics` |
| `run` | `POST /lab/run` (body `RunReq`), status 202 → `Dict[str, Any]` | `Lab.start_run(req).view()`. | FastAPI route; `__main__` `run` |
| `run_status` | `GET /lab/run/{run_id}` → `Dict[str, Any]` | The run view, or 404 `unknown run`. | FastAPI route; `__main__` `run` polling |
| `cancel_run` | `DELETE /lab/run` → `Dict[str, Any]` | Cancels the active run and returns its view, or 404 `no run in progress`. | FastAPI route |
| `results` | `GET /lab/results?scenario=&mode=` (`Optional[AttackName]`, `Optional[Mode]`) → `List[RunResult]` | `Lab.list_results(scenario, mode)`, newest first. Invalid enum values give 422. | FastAPI route (the UI Scorecard reads it) |
| `app_factory` | `app_factory() -> FastAPI` | Configures INFO logging and returns `create_app()`. Intended for `uvicorn tenantguard_lab.app:app_factory --factory`. | Entry point (uvicorn factory); no in-repo caller |

**Key decisions**
- The factory pattern (`create_app(cfg, autostart, transport)`) lets tests inject a config, disable autostart and use a mock transport.
- The `Lab` lives in the lifespan, so shutdown always stops runs, attacks and pools.
- CORS is restricted to the configured `ui_origins`. Other origins get no `Access-Control-Allow-Origin` header (tested).
- Query and body validation is delegated to the shared contracts (`AttackReq`, `RunReq`, `AttackName`, `Mode`), so the API and the CLI reject the same inputs.
- `POST /lab/run` returns 202 immediately. Progress is polled through `GET /lab/run/{id}`.
- `/lab/metrics` caps `window_s` at 900, matching the recorder's default retention.

---

### lab/tools/stub_gateway.py
This is a stand-in gateway for lab development. Its docstring describes it as "a stand-in until the real gateway is merged". By default, every `/api/*` request sleeps about 2 ms. With `STUB_FORWARD=1` it passes requests through to a random worker of the tenant's barcode. Outside `off` mode, a naive per-tenant token bucket answers 429 so attacker 429 counts get exercised. Run with `uvicorn stub_gateway:app --app-dir lab/tools --port 8090`.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| `TENANTS` | `{t.api_key: t for t in load_tenants()}` | API key → tenant lookup. | `api` |
| `FORWARD` | `os.environ.get("STUB_FORWARD") == "1"` | Pass-through switch. | `api` |
| `WORKERS` | `worker_urls()` | Worker name → URL (from `TENANTGUARD_WORKER_URLS`). | `api` |
| `RPS` | `float(os.environ.get("STUB_RPS", "60"))` | Token-bucket rate and burst per tenant. | `api` |
| `state` | `{"mode": "tenantguard"}` | Current mode. | `api`, `get_mode`, `set_mode` |
| `buckets` | `dict` | Tenant id → `(tokens, last_monotonic)`. | `api` |
| `client` | `httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=400, max_keepalive_connections=400))` | Forwarding client. | `api` |
| `app` | `FastAPI()` | The stub app. | uvicorn |
| `api` | `async api(path: str, request: Request, x_api_key: str = Header(None))` on `GET`/`POST /api/{path}` | Unknown key gives 401. When the mode is not `off`, refills the bucket (`min(RPS, tokens + elapsed × RPS)`) and returns 429 `Retry-After: 1` if fewer than 1 token is left. Without forwarding, sleeps 0.002 s and returns `{"tenant", "path"}`. With forwarding, sends to a random `t.servers` worker with the `H_TENANT` and `H_REQUEST_ID` headers and returns the worker's body, status and content type. | FastAPI route (lab traffic during development) |
| `get_mode` | `async get_mode()` on `GET /admin/mode` | Returns `state`. | FastAPI route |
| `set_mode` | `async set_mode(body: dict)` on `POST /admin/mode` | Sets `state["mode"] = body["mode"]` and returns `state`. Does not check the control token. | FastAPI route (`core.Lab.set_mode` when pointed at the stub) |

**Key decisions**
- It is a development stub only and is not part of the Docker image's entry point. It has no fair scheduling ("off mode with a naive limiter").
- The token bucket starts full (`RPS` tokens) and refills continuously, enough to produce 429s for attack traffic.
- Forwarding adds the tenant and request-id headers the workers require, so the lab can be exercised against real workers without the real gateway.

---

### lab/tests/conftest.py
This is the pytest bootstrap. It inserts `lab/` at the front of `sys.path` so `tenantguard_lab` imports without installing the package.

| Function / class | Signature | What it does | Called by |
|---|---|---|---|
| (module statement) | `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` | Makes `lab/` importable. | pytest (auto-loaded) |

**Key decisions**
- It defines no fixtures. The fixtures live in the individual test modules.

---

### lab/tests/test_api.py
These are HTTP API tests through FastAPI's `TestClient`, with a `httpx.MockTransport` gateway. Because a transport is injected, `Lab.isolated` is False and attacks run inline.

| Test | What it proves | Target (production code exercised) |
|---|---|---|
| `ORIGIN` (constant) | `"http://127.0.0.1:5173"`, the allowed CORS origin. | n/a |
| `client` (fixture) | Builds `create_app(load_config(), autostart=False, transport=MockTransport(handler))`. The handler records `(X-API-Key, path)` and answers 429 to every other `/api/light` call from `tk_K_demo`, and 200 otherwise. | `app.create_app` |
| `test_status_starts_idle` | A fresh lab reports `{"attacks": [], "calm_running": False, "run": None}`. | `app.py GET /lab/status`, `core.Lab.status` |
| `test_wave2_attacks_are_501` | All five Wave-2 attack names give 501 on `POST /lab/attack`, and `blackbox_bomb` gives 501 on `POST /lab/run`. | `core.Lab._validate`, `attacks.WAVE2`, `core.Lab.start_run` |
| `test_attack_validation` | Intensity 6 and 0.05 give 422, an unknown attack name gives 422, unknown tenant `Z` gives 404, a missing tenant gives 422, and deleting an unknown attack gives 404. | `AttackReq` contract, `core.Lab._validate`, `core.Lab.stop_attack` |
| `test_attack_lifecycle_and_metrics` | A flood on K returns `running` with `ends_at > started_at`. After 1 s the status is still running, metrics show attack samples and 429s for K, and the attack's `sent` count is above 0. Stopping returns `stopped`. Only K's API key was used. | `core.Lab.launch`, `_supervise`, `metrics`, `stop_attack`; `attacks.flood`; `engine.Engine.client` |
| `test_attack_ends_by_itself_as_done` | A 0.5 s hog on B ends with state `done` and hit `/api/export`. | `core.Lab._supervise`, `_finish`; `attacks.hog` |
| `test_calm_start_stop_and_cors` | Calm start and stop toggle `calm_running`. A CORS preflight from the allowed origin is echoed, and one from `http://evil.example` gets no allow-origin header. | `app.py POST /lab/calm/start` and `/stop`, `CORSMiddleware` configuration |
| `test_run_conflict_and_personas` | `POST /lab/run` returns 202 with attacker defaulting to K, and the status shows the run id. A second run gives 409, and `DELETE /lab/run` returns `cancelled`. `/lab/personas` lists 12 personas, including "KaamKaro" and "Aarogya Health". | `core.Lab.start_run`, `cancel_run`, `RunInfo.view`; `app.py GET /lab/personas` |
| `test_results_newest_first_and_filters` | Results are listed by `started_at`, newest first. Scenario and mode filters work, an unparseable `junk.json` is skipped, and an invalid `mode` gives 422. | `core.Lab.list_results`, `app.py GET /lab/results` |

**Key decisions**
- A mock transport keeps the tests hermetic: no network and no gateway.
- The handler's alternating 429s check that attack-side status counting reaches metrics.

---

### lab/tests/test_isolation.py
Tests the process-isolated attack path against a real local HTTP server (uvicorn in a thread, on a free port), with no mocks across process boundaries.

| Test | What it proves | Target (production code exercised) |
|---|---|---|
| `server_url` (fixture, module scope) | Starts a FastAPI app answering `GET /api/{path}` on a random free port, waits for it to start, and shuts it down after the module. | n/a (test infrastructure) |
| `test_attack_runs_in_child_process_and_streams_samples` | With the default config, `attack_isolation` is `process` and `lab.isolated` is True. A 2 s flood on K runs in a different PID. Samples arrive in the parent while the attack is still running. The attack ends by itself as `done` with the child process exited. `sent` equals the recorder's total for K (more than mid-run), every request was ok, and all samples are `src="attack"`. | `core.Lab.launch`, `_supervise`; `isolation.ProcessAttack.start`, `pump_until`, `drain`, `stop`; `isolation._child`, `_BufferRecorder` |
| `test_stop_isolated_attack_early` | Stopping a 60 s hog on C after 0.5 s returns `stopped` in under 5 s, the child process is gone, and `sent > 0`. | `core.Lab.stop_attack`; `isolation.ProcessAttack.stop` |

**Key decisions**
- It uses a real server because `MockTransport` cannot cross a process boundary, and an injected transport would disable isolation.

---

### lab/tests/test_poisson.py
Statistical and real-time checks of the arrival process and the calm engine rate.

| Test | What it proves | Target (production code exercised) |
|---|---|---|
| `test_arrival_count_within_10pct` (parametrised `rate` in 2.0, 14.0, 300.0) | Over a duration that should yield about 2000 arrivals, the count is within ±200. | `poisson.arrival_times`, `poisson.gaps` |
| `test_gaps_are_exponential` | At rate 10 over 20000 gaps, mean and population SD are both about 0.1 (±5%), which is exponential. Rate 0 raises `ValueError`. | `poisson.gaps` |
| `test_run_poisson_real_time_rate_within_10pct` | At 200/s for 3 s of real time, about 600 fires (±60), and the task exits when `stop` is set. | `poisson.run_poisson` |
| `test_calm_engine_rate_within_10pct` | All 12 personas against a mock gateway for 4 s send within 10% of `Σ rps × 4`, and every tenant uses its own API key. | `engine.Engine.start_calm`, `stop_calm`, `aclose`, `send`, `client` |

**Key decisions**
- Tolerances are ±10%, and seeds are fixed for repeatability.

---

### lab/tests/test_scenario.py
Tests the scenario timeline on a fake clock, the result math on synthetic recorders, and an end-to-end run against a mock gateway and brain.

| Test | What it proves | Target (production code exercised) |
|---|---|---|
| `FakeDriver` (class) | A `ScenarioDriver` with a virtual clock starting at 1000.0. `sleep` advances the clock, or raises `CancelledError` past `fail_at`. It logs mode, calm and attack calls. | Implements `scenario.ScenarioDriver` |
| `FakeDriver.__init__` | `__init__(self, calm_running=False, fail_at=None)` | n/a |
| `FakeDriver.now` / `sleep` / `set_mode` / `ensure_calm` / `restore_calm` / `start_attack` / `stop_attack` | Fake implementations. `start_attack` returns `"atk-1"`. | n/a |
| `test_timeline_offsets` | `Timeline(3, 20, 40)` gives offsets (3, 23, 63, 83) and phases warmup, baseline, attack, after. | `scenario.Timeline` properties and `phases` |
| `test_run_timeline_order_and_timing` | Call order is mode → calm_on → attack_start → attack_stop → restore. The mode is set before any traffic. The attack starts at +23 s with scenario, tenant and intensity passed through, stops at +63 s, and calm is restored to off at +83 s. All marks match. | `scenario.run_timeline` |
| `test_run_timeline_cancel_stops_attack_and_restores` | A cancellation mid-attack still stops the attack and restores calm (to on). | `scenario.run_timeline` (`finally`) |
| `_rec_with` (helper) | `_rec_with(phases, attacker="K")` builds a `Recorder` with 20 Hz calm samples for A, B and the attacker (attacker latency 1.0). | n/a |
| `test_compute_result_phases_recovery_and_attacker_counts` | Victim p99 is 10, 200 and 10 ms for baseline, attack and after. The attacker is excluded from per-tenant results. `time_to_recover_s` is 10.0. Attacker 429/503 = (30, 1), counting only within `[attack_start, end)`. Attack-source samples on a victim are ignored. | `scenario.compute_result`, `stats.bucketize`, `stats.time_to_recover` |
| `test_compute_result_never_recovers` | Victims degraded through the end give `time_to_recover_s is None`. | `scenario.compute_result` |
| `test_lab_run_end_to_end_writes_runresult` | A real `Lab` run with a mock gateway and brain. The gateway's `/admin/mode` requires control token `dev`. The brain returns an incident 0.5 s after the first whale request. Whale requests from K get 429. Proves: the run ends `done`, the mode is set to `tenantguard`, exactly one `*-whale-tenantguard.json` file is written with `file` = `lab/results/<name>`, `attacker_429 > 0`, baseline > 0, K is excluded from victims, `time_to_detect_s` is in [0, 1.5), and calm returns to off. | `core.Lab.start_run`, `_run`, `set_mode`, `time_to_detect`, `write_result`, `ensure_calm`/`restore_calm`; `scenario.run_timeline`, `compute_result` |
| `test_attack_start_marked_after_attack_is_running` | With a 4 s spawn delay, `attack_start` = 1027 and `attack_end` = 1067. The attack window is the full 40 s, and the baseline absorbs the delay (24 s). | `scenario.run_timeline` |
| `test_cold_start_extends_warmup_only_when_calm_was_off` | `cold_start_s=10` makes warmup 13 s when calm was off and 3 s when it was on. | `scenario.run_timeline` |
| `test_victim_kinds_filter` | With fast `light` (5 ms) and slow `search` (400 ms) calm samples, `victim_kinds=["light", "read"]` gives baseline p99 5.0, and no filter gives 400.0. | `scenario.compute_result`, `stats.Recorder.window` (`kinds`) |
| `test_read_and_write_keys_match_worker_seed` | Read requests carry a 5-character `k####` key, and write bodies are `{key, value}` with a numeric key suffix. | `engine.Engine.build` |

**Key decisions**
- The fake clock makes the timeline tests instant and exact. Only the end-to-end test uses real time (short calm and attack windows, warmup 0.2 s).

---

### lab/tests/test_stats.py
Unit tests for the percentile, summary, recorder, bucketing and recovery math.

| Test | What it proves | Target (production code exercised) |
|---|---|---|
| `test_percentile_matches_numpy_linear` | On 1..100: p50 = 50.5, p95 = 95.05, p99 = 99.01, p0 = 1, p100 = 100 (numpy linear values). | `stats.percentile` |
| `test_percentile_unsorted_small_and_empty` | Unsorted input works, two values interpolate (p25 of [10, 20] = 12.5), a single value is returned as is, empty input gives 0.0, and q = 101 raises `ValueError`. | `stats.percentile` |
| `test_p99_is_dominated_by_tail` | With 1% slow requests p99 is about 1.99 (boundary). With 2% slow, p99 = 100. | `stats.percentile` |
| `test_summarize_counts_outcomes` | 200 and 304 count as ok, 429 and 503 are counted separately, and TIMEOUT and CONN_ERROR are `other_err`. Max is correct, and per-second rates are computed by `as_dict(window_s=2)`. | `stats.summarize`, `Summary.as_dict` |
| `test_recorder_window_source_and_retention` | Retention keeps samples 9..19 of 0..19 with `retention_s=10`. The `src` filter works, totals stay cumulative (20), and `drop` counts. | `stats.Recorder.add`, `window`, `drop` |
| `test_bucketize` | Samples go into the correct 1 s buckets, empty buckets are kept, and `t1` is exclusive. | `stats.bucketize` |
| `test_time_to_recover` | Recovery after 4 bad buckets is at 4.0. Never degraded gives (0.0, False). A late bite gives 5.0, not 0. A relapse resets the streak. Never recovered gives (None, True). Empty buckets don't break a streak. | `stats.time_to_recover` |

**Key decisions**
- The cases document each edge case of the recovery definition used in `RunResult.time_to_recover_s`.

---

### lab/Dockerfile
Container image for the lab service. The build context is the repository root, and `docker-compose.yml` service `lab` uses `dockerfile: lab/Dockerfile` under profile `lab`.

| Line / section | Value | Purpose |
|---|---|---|
| Base | `FROM python:3.12-slim` | Python runtime. |
| Shared library | `COPY common /app/common` + `pip install --no-cache-dir -e /app/common` | Installs `tenantguard_common` (contracts, config). |
| Dependencies | `COPY lab/requirements.txt` + `pip install --no-cache-dir -r` | Lab dependencies, copied before the source for layer caching. |
| Config | `COPY tenantguard.yaml /app/tenantguard.yaml`, `COPY layouts /app/layouts` | Platform settings and tenant layout read by `settings.load_config`. |
| Source | `COPY lab /app/lab`, `WORKDIR /app/lab` | The package plus `personas.yaml`. |
| Env | `TENANTGUARD_ROOT=/app`, `PYTHONUNBUFFERED=1` | Lets `tenantguard_common` find `tenantguard.yaml`, and gives unbuffered logs. |
| Port | `EXPOSE 8300` | Lab API. Compose maps `${LAB_PORT:-8300}:8300`. |
| Command | `python -m tenantguard_lab serve --host 0.0.0.0 --port 8300` | Runs `__main__.main()` → uvicorn. |

**Key decisions**
- `requirements.txt` is installed before the source is copied, so source edits don't reinstall dependencies.
- Compose supplies `TENANTGUARD_GATEWAY_URL=http://gateway:8000`, `TENANTGUARD_BRAIN_URL=http://brain:8200`, the worker URLs, the control token and the UI origins.

---

### lab/requirements.txt
Pip requirements for running and testing the lab. `tenantguard_common` must be installed separately (the Dockerfile does this).

| Package | Constraint | Used for |
|---|---|---|
| fastapi | `>=0.104` | `app.py` |
| uvicorn | `>=0.24` | `serve`, `tests/test_isolation.py` server |
| httpx | `>=0.25` | Traffic engine, admin and brain calls, CLI remote client |
| pydantic | `>=2.5` | `CalmBody`, shared contracts |
| PyYAML | `>=6` | `personas.yaml` |
| pytest | `>=7.4` | Tests |
| pytest-asyncio | `>=0.21` | Async tests (`asyncio_mode = "auto"`) |

---

### lab/pyproject.toml
Package metadata and pytest configuration.

| Section / key | Value | Purpose |
|---|---|---|
| `[build-system]` | `setuptools>=61`, `setuptools.build_meta` | Build backend. |
| `[project] name` / `version` | `tenantguard-lab` / `0.1.0` | Package identity. |
| `requires-python` | `>=3.10` | Minimum Python. |
| `dependencies` | fastapi>=0.104, uvicorn>=0.24, httpx>=0.25, pydantic>=2.5, PyYAML>=6 | Runtime dependencies (no test libraries). |
| `[tool.setuptools] packages` | `["tenantguard_lab"]` | Package to install. |
| `[tool.pytest.ini_options] testpaths` | `["tests"]` | Test discovery root. |
| `[tool.pytest.ini_options] asyncio_mode` | `"auto"` | `async def` tests run without explicit markers. |

---

### lab/personas.yaml
The single file holding every lab tunable: calm personas, request kinds, client limits, attack profiles and run timing. Loaded by `settings.load_config` (path overridable with `TENANTGUARD_LAB_PERSONAS`).

| Key | Value(s) | Meaning / consumer |
|---|---|---|
| `rate_scale` | `1.0` | Multiplier on every persona's rps (env `TENANTGUARD_LAB_RATE_SCALE` overrides it, and `POST /lab/calm/start` changes it live). |
| `autostart` | `true` | The server starts calm traffic on boot (env `TENANTGUARD_LAB_AUTOSTART=0` disables it, as does `serve --no-calm`). |
| `default_attacker` | `K` | Attacker used by `/lab/run` when `RunReq.tenant` is empty. |
| `target_fleet_util` | `0.30` | Target utilisation used by `calibrate` to suggest `rate_scale`. |
| `client.timeout_s` | `10.0` | httpx timeout. Also bounds attack shutdown waits. |
| `client.max_inflight_per_tenant` | `64` | Calm in-flight cap and pool size. Arrivals above it are counted as `client_dropped`. |
| `client.max_inflight_per_attack` | `512` | Default attack in-flight cap. |
| `client.attack_isolation` | `process` | Each attack in its own process. `inline` runs it in the same event loop. |
| `client.keyspace` | `1000` | `/api/read` and `/api/write` keys `k0000`..`k0999` (comment: matches worker `seed_rows`). |
| `endpoints` | `light` GET `/api/light` (est 0.8 ms); `read` GET `/api/read` (1.0); `search` GET `/api/search` `n: 200000` (70.0); `write` POST `/api/write` (7.0) | Request kinds. `est_cpu_ms` is only used for the calibration estimate. |
| `mix` | `light 0.45, read 0.30, search 0.15, write 0.10` | Default request mix (weights, normalised). |
| `personas` | A–L. rps 14/10/5 by tenant. `sla_p95_ms` 40–150. `end_users` and `note` per tenant. B, D, H and L override `mix`. | Calm behaviour per tenant. Names and tiers come from the layout. |
| `attacks.flood` | `multiplier: 10, endpoint: light` | 10 × persona rps of `/api/light`. |
| `attacks.whale` | `rps: 2.0, params: {n: 1500000}, max_inflight: 32` | Few, very heavy searches (comment: about 525 ms CPU each). |
| `attacks.hog` | `concurrency: 24, path: /api/export, params: {rows: 1000, chunk: 50, pause_ms: 50}` | Closed loop of slow exports (comment: about 1 s each). |
| `attacks.memory` | `rps: 4.0, params: {mb: 24, hold_ms: 1000}, max_inflight: 32` | Overlapping holds that exceed the 32 MB tenant budget (per the comment). Path defaults to `/api/memory` in code. |
| `attacks.retry_storm` | `multiplier: 3, endpoint: read, max_retries: 50` | Immediate retries on 429/503, ignoring Retry-After. |
| `run.warmup_s` | `3` | Discarded warmup. |
| `run.cold_start_s` | `10` | Extra warmup if calm traffic was off (comment: fresh connections take about 9 s to settle on Windows). |
| `run.recover_ratio` / `run.recover_hold_s` | `1.5` / `5` | Recovered when victim p99 ≤ 1.5 × baseline for 5 consecutive seconds. |
| `run.bucket_s` | `1.0` | Recovery bucket width. |
| `run.victim_kinds` | `[light, read]` | Interactive request kinds that define victim SLO latency. `[]` means every calm request. |

**Key decisions**
- The header comments state the calibration intent: calm load of about 116 req/s, aimed at about 30% of 4500 CPU-ms/s (9 workers × 0.5 cores). CPU comes mostly from request weight (search `n`), not request count. Recalibrate on the real fleet with `calibrate`.
- Attack profiles are data, so intensities and parameters can be tuned without code changes. Only the five implemented attacks have profiles. The Wave-2 attacks are in progress and have none.

---

### lab/README.md
The operator guide for the lab. It covers how to run it, environment variables, the API table, traffic and measurement semantics, calibration notes and tools.

| Section | Content |
|---|---|
| Intro | 12 tenant personas, Poisson arrivals, attacks, scripted scenarios. Each run writes a `RunResult` to `lab/results/`, and slide numbers come only from these files. |
| Run | CLI commands (`serve`, `calm`, `attack`, `stop`, `status`, `metrics`, `run`, `calibrate`), `pytest`, `docker compose --profile lab up lab`. Remote-first behaviour and `--local`. |
| Env | `TENANTGUARD_GATEWAY_URL`, `TENANTGUARD_BRAIN_URL`, `TENANTGUARD_WORKER_URLS`, `TENANTGUARD_UI_ORIGINS`, `TENANTGUARD_CONTROL_TOKEN`, `TENANTGUARD_LAB_RATE_SCALE`, `TENANTGUARD_LAB_AUTOSTART`, `TENANTGUARD_LAB_PERSONAS`, `TENANTGUARD_LAB_RESULTS`. |
| API | Endpoint table with status codes (Wave-2 attacks 501, unknown tenant 404, intensity outside 0.1..5 → 422, concurrent run 409, `POST /lab/run` 202). |
| Traffic | Personas and effective rate, absolute-schedule Poisson, per-tenant and per-attack pools, in-flight caps, process isolation rationale (about 3 ms httpx CPU per request, samples streamed every 250 ms, lag p99 warning above about 20 ms), and attack profiles. Lists the Wave-2 names as 501. |
| Measurement | Timeline (warmup, +10 s if cold, `calm_s`, `attack_s`, `calm_s`), `attack_start` marked once traffic flows (about 4 s spawn on Windows), victim selection and `victim_kinds`, numpy-linear p99, definitions of `time_to_recover_s`, `time_to_detect_s` and `attacker_429/503`. |
| Calibration | Target about 30% of 4500 CPU-ms/s. One recorded measurement on 9 local workers: 1000 CPU-ms/s against an estimate of 1295, with `calibrate` suggesting `rate_scale` 1.35, described as a CPU-starved laptop. Advises recalibrating on the Docker host and `TENANTGUARD_LAB_RATE_SCALE=0.25` on a single laptop. |
| Tools | `tools/stub_gateway.py` summary. |

---

### lab/results/.gitkeep
An empty file that keeps the `lab/results/` folder in git. Scenario runs write `<YYYYmmdd-HHMMSS>-<scenario>-<mode>.json` files here (`core.Lab.write_result`), and `GET /lab/results` and the UI Scorecard read them (`core.Lab.list_results`). The result data files are not catalogued here.

| Item | Value | Purpose |
|---|---|---|
| `.gitkeep` | empty | Keeps the default `results_dir` (`LAB_DIR / "results"`, overridable with `TENANTGUARD_LAB_RESULTS`) present in a fresh checkout. |