import { useEffect, useRef, useState } from 'react'
import { ReplicaGrid } from '../components/ReplicaGrid.tsx'
import { config } from '../config.ts'
import {
  CHALLENGE_KINDS,
  HUNT_LIMIT_S,
  cleared,
  eventKey,
  evidence,
  findAnswer,
  innocentsPunished,
  kindInfo,
  newSalt,
  openIncidents,
  outcome,
  seal,
  stillHot,
  type Answer,
  type ChallengeKind,
  type Evidence,
  type RoundStart,
} from '../lib/challenge.ts'
import { useBackend, useLive, useNow, useStore } from '../lib/liveContext.ts'
import { scenarioOf } from '../lib/scenario.ts'
import type { TenantView } from '../types.ts'

type Phase = 'pick' | 'hunt' | 'answer' | 'reveal'

interface Round {
  target: string
  kind: ChallengeKind
  salt: string
  seal: string
  attackId: string
  startedAt: number // browser clock, ms
  start: RoundStart
}

/** What the reveal shows, frozen the moment the seal is opened (the round's attack is stopped then). */
interface Frozen {
  heldBack: TenantView[]
  clears: { tenant: string; verdict: string }[]
}

export function Challenge() {
  const backend = useBackend()
  const store = useStore()
  const { snapshot, incidents, lab, labReachable, pendingMode } = useLive()
  const now = useNow()
  const [phase, setPhase] = useState<Phase>('pick')
  const [kind, setKind] = useState<ChallengeKind>('whale')
  const [round, setRound] = useState<Round | null>(null)
  const [answer, setAnswer] = useState<Answer | null>(null)
  const [proof, setProof] = useState<Evidence | null>(null)
  const [foundAfter, setFoundAfter] = useState<number | null>(null)
  const [watched, setWatched] = useState(0)
  const [frozen, setFrozen] = useState<Frozen | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [, tick] = useState(0)
  // the attack this page started and has not stopped yet (stopped when the seal opens or the page is left)
  const live = useRef<string | null>(null)

  useEffect(
    () => () => {
      if (live.current) backend.stopAttack(live.current).catch(() => null)
    },
    [backend],
  )

  const tenants = snapshot?.tenants ?? []
  const nameOf = (id: string) => tenants.find((t) => t.tenant === id)?.name ?? id
  const projectOf = (id: string) => tenants.find((t) => t.tenant === id)?.project ?? null
  const info = kindInfo(kind)

  // ---------- is the system calm enough for a fair round? ----------
  const scenario = scenarioOf(lab, labReachable !== false, now)
  const mode = pendingMode ?? snapshot?.mode ?? null
  const hot = stillHot(tenants)
  const open = snapshot ? openIncidents(incidents, snapshot.ts) : []
  const blocker = !snapshot
    ? 'Waiting for live data from the brain…'
    : scenario.kind === 'unknown'
      ? 'The lab is not answering: rounds are started through the lab.'
      : lab?.run
        ? 'A scenario run is in progress (its result goes to the Scorecard). Wait for it to finish.'
        : scenario.kind === 'attack'
          ? `${scenario.label}. Stop it first so only the judge's choice is in play.`
          : scenario.kind !== 'calm'
            ? 'Calm traffic is off, so nobody could be slowed. Start calm traffic on the lab first.'
            : mode !== 'tenantguard'
              ? 'Switch the mode to TenantGuard (top right) first.'
              : hot.length
                ? `Waiting for the system to calm down: ${hot.map((t) => t.tenant).join(', ')} still held back.`
                : open.length
                  ? `Waiting for the brain to close its open incident${open.length > 1 ? 's' : ''} (${open.length}).`
                  : null
  const ready = blocker === null
  const stoppable = scenario.kind === 'attack' && !lab?.run ? scenario.attacks : []

  // re-render 4x a second while hunting (timer on screen + the time limit)
  useEffect(() => {
    if (phase !== 'hunt') return
    const id = setInterval(() => tick((n) => n + 1), 250)
    return () => clearInterval(id)
  }, [phase])

  // the brain's answer: the first event of this round that points at a company; its numbers are frozen then
  useEffect(() => {
    if (phase !== 'hunt' || !round) return
    const a = findAnswer(incidents, round.start, 'name')
    if (a) {
      const t = (Date.now() - round.startedAt) / 1000
      setAnswer(a)
      setProof(evidence(store.getState().snapshot, a.tenant, config.p95WarnMs, config.noisy.warn))
      setFoundAfter(t)
      setWatched(t)
      setPhase('answer')
    }
  }, [incidents, phase, round, store])

  const elapsed = round ? (Date.now() - round.startedAt) / 1000 : 0

  const openSeal = (watchedS?: number) => {
    if (!round) return
    setFrozen({
      heldBack: innocentsPunished(store.getState().snapshot?.tenants ?? [], round.target),
      clears: cleared(store.getState().incidents, round.start),
    })
    if (watchedS !== undefined) setWatched(watchedS)
    setPhase('reveal')
    if (live.current) {
      backend.stopAttack(live.current).catch(() => null)
      live.current = null
    }
  }

  useEffect(() => {
    if (phase === 'hunt' && elapsed > HUNT_LIMIT_S) openSeal(HUNT_LIMIT_S)
  })

  const stopRunning = async () => {
    setBusy(true)
    try {
      await Promise.all(stoppable.map((a) => backend.stopAttack(a.attack_id)))
      store.setLab(await backend.labStatus())
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const start = async (target: string) => {
    if (!ready || !snapshot) return
    setBusy(true)
    setErr(null)
    try {
      const salt = newSalt()
      const roundStart: RoundStart = { before: new Set(store.getState().incidents.map(eventKey)), brainTs: snapshot.ts }
      const st = await backend.startAttack({
        attack: kind,
        tenant: target,
        server: null,
        intensity: info.intensity,
        duration_s: 180,
      })
      live.current = st.attack_id
      setRound({
        target,
        kind,
        salt,
        seal: seal(target, kind, salt),
        attackId: st.attack_id,
        startedAt: Date.now(),
        start: roundStart,
      })
      setAnswer(null)
      setProof(null)
      setFoundAfter(null)
      setFrozen(null)
      setPhase('hunt')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const again = () => {
    if (live.current) {
      backend.stopAttack(live.current).catch(() => null)
      live.current = null
    }
    setRound(null)
    setAnswer(null)
    setProof(null)
    setFrozen(null)
    setPhase('pick')
  }

  // ---------- 1. pick ----------
  if (phase === 'pick' || !round) {
    return (
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
        <Header step={1} title="Judge: secretly pick one company to become the attacker" />
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-lg text-ink-300">How it attacks:</span>
          {CHALLENGE_KINDS.map((k) => (
            <button
              key={k.kind}
              onClick={() => setKind(k.kind)}
              className={`btn ${kind === k.kind ? 'bg-accent text-ink-950' : 'border border-ink-700 text-ink-100 hover:border-accent'}`}
              title={k.about}
            >
              {k.label}
            </button>
          ))}
          <span className="text-base text-ink-300">{info.about}</span>
        </div>
        {blocker && (
          <div className="panel flex items-center justify-between gap-4 border-warn" role="status">
            <span className="text-lg text-warn">{blocker}</span>
            {stoppable.length > 0 && (
              <button className="btn-ghost whitespace-nowrap" disabled={busy} onClick={stopRunning}>
                Stop {stoppable.map((a) => `${a.attack} on ${a.tenant ?? a.server}`).join(', ')}
              </button>
            )}
          </div>
        )}
        <div className="grid grid-cols-4 gap-3" data-testid="challenge-targets">
          {tenants.map((t) => (
            <button
              key={t.tenant}
              disabled={!ready || busy}
              onClick={() => start(t.tenant)}
              className="panel flex flex-col items-start gap-1 text-left transition hover:border-accent disabled:cursor-not-allowed disabled:opacity-40"
            >
              <span className="text-2xl font-bold">{t.name}</span>
              <span className="text-base text-ink-300">
                {t.tenant} · {t.tier} · project {t.project}
              </span>
            </button>
          ))}
        </div>
        {err && (
          <div className="text-lg text-bad" role="alert">
            {err}
          </div>
        )}
        <p className="text-base text-ink-300">
          Presenter, look away. The moment the judge taps, the choice is sealed as a SHA-256 hash shown in full on screen
          (photograph it), and the attack starts. TenantGuard never sees the tap: it only gets the measurements.
        </p>
      </div>
    )
  }

  // ---------- 2-4. hunt, answer, reveal ----------
  const rk = kindInfo(round.kind)
  const culprit = answer?.tenant ?? null
  const result = frozen
    ? outcome(
        round.target,
        answer,
        frozen.heldBack.map((t) => t.tenant),
      )
    : null

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
      <Header
        step={phase === 'hunt' ? 2 : phase === 'answer' ? 3 : 4}
        title={
          phase === 'hunt'
            ? 'Someone is attacking. TenantGuard sees only the measurements.'
            : phase === 'answer'
              ? 'TenantGuard has named a company'
              : 'Open the seal'
        }
      />
      <div className="panel flex flex-wrap items-center gap-x-6 gap-y-1">
        <span className="text-lg text-ink-300">Judge's sealed choice (photograph it)</span>
        <code className="break-all text-lg font-bold text-accent" data-testid="challenge-seal">
          {round.seal}
        </code>
        {phase === 'hunt' && (
          <span className="ml-auto text-3xl font-black tabular-nums" data-testid="challenge-timer">
            {elapsed.toFixed(1)} s
          </span>
        )}
      </div>

      <div className="grid grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)] gap-4">
        <div className="panel h-[340px]">
          <ReplicaGrid
            pool={snapshot?.pool}
            servers={snapshot?.servers ?? []}
            highlight={culprit ? projectOf(culprit) : null}
            highlightLabel={culprit ? `${nameOf(culprit)} · project ${projectOf(culprit) ?? '?'}` : undefined}
            title="The replicas TenantGuard is watching"
          />
        </div>

        <div className="panel flex flex-col gap-2">
          {phase === 'hunt' && (
            <>
              <div className="text-2xl font-bold">Watching…</div>
              <p className="text-lg text-ink-300">
                TenantGuard only sees per-tenant usage and per-replica latency and CPU. It has to find which company is hurting
                the others, and act on that company alone.
              </p>
              <div className="mt-auto text-base text-ink-300">Limit {HUNT_LIMIT_S} s, then the seal is opened.</div>
            </>
          )}
          {answer && phase !== 'hunt' && (
            <>
              <div className="text-lg text-ink-300">TenantGuard names</div>
              <div className="text-4xl font-black" data-testid="challenge-answer">
                {nameOf(answer.tenant)} <span className="text-ink-300">({answer.tenant})</span>
              </div>
              <div className="text-xl">
                after <b>{foundAfter?.toFixed(1)} s</b>
                {answer.also.length > 0 && <> · also named: {answer.also.map(nameOf).join(', ')}</>}
              </div>
              {proof && (
                <ul className="space-y-0.5 text-lg" data-testid="challenge-evidence">
                  <li>
                    Noisy score <b>{proof.score.toFixed(1)}</b> <span className="text-ink-300">(flagged from {proof.warn})</span>
                  </li>
                  <li>
                    Uses <b>{Math.round(proof.cpuShare * 100)} %</b> <span className="text-ink-300">of all companies' CPU</span>
                  </li>
                  <li>
                    Others in project {proof.project} over the {proof.sloMs} ms target: <b>{proof.neighboursOver}</b>{' '}
                    <span className="text-ink-300">of {proof.neighbours}</span>
                  </li>
                  <li>
                    Slow replicas of project {proof.project}: <b>{proof.slow.join(' ') || 'none'}</b>
                  </li>
                </ul>
              )}
              {answer.detail && (
                <div className="line-clamp-3 text-base text-ink-300" title={answer.detail}>
                  Brain's own words: “{answer.detail}”
                </div>
              )}
            </>
          )}
          {!answer && phase === 'reveal' && (
            <div className="text-2xl font-bold text-bad" data-testid="challenge-answer">
              TenantGuard did not name anyone in {watched.toFixed(0)} s.
            </div>
          )}
          {phase === 'answer' && (
            <button className="btn-accent mt-auto text-xl" onClick={() => openSeal()}>
              Open the judge's seal
            </button>
          )}
        </div>
      </div>

      {phase === 'reveal' && frozen && result && (
        <div
          className={`panel flex flex-col gap-3 border-2 ${result.right ? 'border-ok' : 'border-bad'}`}
          data-testid="challenge-reveal"
        >
          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
            <span className={`text-4xl font-black ${result.right ? 'text-ok' : 'text-bad'}`}>{result.title}</span>
            <span className="text-2xl">
              The judge picked <b>{nameOf(round.target)}</b> ({round.target}), {rk.label.toLowerCase()}.
            </span>
          </div>
          <div className="text-lg text-ink-300">
            The sealed text was <code className="text-ink-100">{`${round.target}|${round.kind}|${round.salt}`}</code>. Check it on
            any phone: its SHA-256 must equal the hash you photographed.
          </div>
          {frozen.clears.length > 0 && (
            <div className="text-xl">
              Checked and cleared by TenantGuard:{' '}
              {frozen.clears.map((c) => `${nameOf(c.tenant)} (${c.verdict.replace(/_/g, ' ')}, no penalty)`).join(', ')}
            </div>
          )}
          <div className="text-xl">
            Innocent companies held back when the seal opened:{' '}
            <b className={frozen.heldBack.length ? 'text-bad' : 'text-ok'}>
              {frozen.heldBack.length ? frozen.heldBack.map((t) => `${t.name} (${t.tenant})`).join(', ') : '0'}
            </b>
          </div>
          <button className="btn-accent self-start text-xl" onClick={again}>
            Play again
          </button>
        </div>
      )}
      {phase !== 'reveal' && (
        <button className="btn-ghost self-start" onClick={() => openSeal(phase === 'hunt' ? elapsed : undefined)}>
          Open the seal now
        </button>
      )}
    </div>
  )
}

function Header({ step, title }: { step: number; title: string }) {
  return (
    <div className="flex items-baseline gap-4">
      <span className="rounded-lg bg-accent px-3 py-1 text-xl font-black text-ink-950">{step} / 4</span>
      <h1 className="text-3xl font-black tracking-tight">{title}</h1>
    </div>
  )
}
