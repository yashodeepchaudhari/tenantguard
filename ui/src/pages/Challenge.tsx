import { useEffect, useState } from 'react'
import { ServerGrid } from '../components/ServerGrid.tsx'
import { config } from '../config.ts'
import {
  CHALLENGE_KINDS,
  HUNT_LIMIT_S,
  busiest,
  cleared,
  eventKey,
  evidence,
  findAnswer,
  innocentsPunished,
  kindInfo,
  newSalt,
  outcome,
  seal,
  stillHot,
  type Answer,
  type ChallengeKind,
  type Evidence,
} from '../lib/challenge.ts'
import { FLEET } from '../lib/status.ts'
import { useBarcodes } from '../lib/useBarcodes.ts'
import { useBackend, useLive, useStore } from '../lib/liveContext.ts'

type Phase = 'pick' | 'hunt' | 'answer' | 'reveal'

interface Round {
  target: string // tenant id, or server for faulty_server
  kind: ChallengeKind
  salt: string
  seal: string
  attackId: string
  startedAt: number // browser clock, ms
  before: Set<string>
}

const short = (h: string) => `${h.slice(0, 8)}…${h.slice(-4)}`
const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x))

export function Challenge() {
  const backend = useBackend()
  const store = useStore()
  const { snapshot, incidents, lab } = useLive()
  const barcodes = useBarcodes()
  const [phase, setPhase] = useState<Phase>('pick')
  const [kind, setKind] = useState<ChallengeKind>('whale')
  const [round, setRound] = useState<Round | null>(null)
  const [answer, setAnswer] = useState<Answer | null>(null)
  const [proof, setProof] = useState<Evidence | null>(null)
  const [foundAfter, setFoundAfter] = useState<number | null>(null)
  const [watched, setWatched] = useState<number>(0)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [, tick] = useState(0)

  const tenants = snapshot?.tenants ?? []
  const warn = snapshot?.noisy_thresholds?.warn ?? config.noisy.warn
  const nameOf = (id: string) => tenants.find((t) => t.tenant === id)?.name ?? id
  const running = lab?.attacks.filter((a) => a.state === 'running') ?? []
  const hot = stillHot(tenants)
  const ready = running.length === 0 && hot.length === 0 && tenants.length > 0
  const info = kindInfo(kind)

  // re-render 4x a second while hunting (timer on screen + the time limit)
  useEffect(() => {
    if (phase !== 'hunt') return
    const id = setInterval(() => tick((n) => n + 1), 250)
    return () => clearInterval(id)
  }, [phase])

  // the brain's answer: the first new event that blames a tenant; the numbers behind it are frozen then
  useEffect(() => {
    if (phase !== 'hunt' || !round) return
    const a = findAnswer(incidents, round.before, kindInfo(round.kind).blame ? 'name' : 'blame')
    if (a) {
      const t = (Date.now() - round.startedAt) / 1000
      setAnswer(a)
      setProof(evidence(store.getState().snapshot, barcodes, a.tenant, config.p95WarnMs, config.noisy.warn))
      setFoundAfter(t)
      setWatched(t)
      setPhase('answer')
    }
  }, [incidents, phase, round, barcodes, store])

  const elapsed = round ? (Date.now() - round.startedAt) / 1000 : 0
  const openSeal = () => {
    if (phase === 'hunt') setWatched(elapsed)
    setPhase('reveal')
  }
  useEffect(() => {
    if (phase === 'hunt' && elapsed > HUNT_LIMIT_S) {
      setWatched(HUNT_LIMIT_S)
      setPhase('reveal')
    }
  }, [phase, elapsed])

  const stopAll = async () => {
    setBusy(true)
    try {
      await Promise.all(running.map((a) => backend.stopAttack(a.attack_id).catch(() => null)))
      store.setLab(await backend.labStatus())
    } catch {
      /* the lab poll catches up */
    } finally {
      setBusy(false)
    }
  }

  const start = async (target: string) => {
    setBusy(true)
    setErr(null)
    try {
      if ((snapshot?.mode ?? null) !== 'tenantguard') {
        store.setPendingMode('tenantguard')
        await backend.setMode('tenantguard')
      }
      const salt = newSalt()
      const before = new Set(store.getState().incidents.map(eventKey))
      const onServer = info.target === 'server'
      const st = await backend.startAttack({
        attack: kind,
        tenant: onServer ? null : target,
        server: onServer ? target : null,
        intensity: info.intensity,
        duration_s: 180,
      })
      setRound({
        target,
        kind,
        salt,
        seal: seal(target, kind, salt),
        attackId: st.attack_id,
        startedAt: Date.now(),
        before,
      })
      setAnswer(null)
      setProof(null)
      setFoundAfter(null)
      setPhase('hunt')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const again = async () => {
    if (round) await backend.stopAttack(round.attackId).catch(() => null)
    setRound(null)
    setAnswer(null)
    setProof(null)
    setPhase('pick')
  }

  // ---------- 1. pick ----------
  if (phase === 'pick' || !round) {
    const kindBtn = (k: (typeof CHALLENGE_KINDS)[number]) => (
      <button
        key={k.kind}
        onClick={() => setKind(k.kind)}
        className={`btn ${kind === k.kind ? 'bg-accent text-ink-950' : 'border border-ink-700 text-ink-100 hover:border-accent'}`}
        title={k.about}
      >
        {k.label}
      </button>
    )
    return (
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
        <Header step={1} title="Judge: secretly choose what happens" />
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          <div className="flex items-center gap-2">
            <span className="text-lg text-ink-300">A company attacks:</span>
            {CHALLENGE_KINDS.filter((k) => k.blame).map(kindBtn)}
          </div>
          <div className="flex items-center gap-2">
            <span className="text-lg text-ink-300">Or nobody is to blame:</span>
            {CHALLENGE_KINDS.filter((k) => !k.blame).map(kindBtn)}
          </div>
        </div>
        <div className="text-lg text-ink-300">{info.about}</div>
        {!ready && (
          <div className="panel flex items-center justify-between gap-4 border-warn">
            <span className="text-lg text-warn">
              {tenants.length === 0
                ? 'Waiting for live data from the brain…'
                : running.length > 0
                  ? `${running.length} attack${running.length > 1 ? 's are' : ' is'} still running: stop ${running.length > 1 ? 'them' : 'it'} first so only the judge's choice is in play.`
                  : `Waiting for the fleet to calm down: ${hot.map((t) => t.tenant).join(', ')} still held back.`}
            </span>
            {running.length > 0 && (
              <button className="btn-ghost whitespace-nowrap" disabled={busy} onClick={stopAll}>
                Stop running attacks
              </button>
            )}
          </div>
        )}
        {info.target === 'tenant' ? (
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
                  {t.tenant} · {t.tier}
                </span>
              </button>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-3" data-testid="challenge-targets">
            {FLEET.map((sv) => (
              <button
                key={sv}
                disabled={!ready || busy}
                onClick={() => start(sv)}
                className="panel text-left text-3xl font-black transition hover:border-accent disabled:cursor-not-allowed disabled:opacity-40"
              >
                {sv}
              </button>
            ))}
          </div>
        )}
        {err && (
          <div className="text-lg text-bad" role="alert">
            {err}
          </div>
        )}
        <p className="text-base text-ink-300">
          Presenter, look away. The moment the judge taps, the choice is sealed with a hash shown on screen and it starts.
          TenantGuard never sees the tap: it only gets the servers' measurements. If nobody is to blame, the right answer is to
          blame nobody.
        </p>
      </div>
    )
  }

  // ---------- 2-4. hunt, answer, reveal ----------
  const rk = kindInfo(round.kind)
  const servers = snapshot?.servers ?? []
  const slow = servers.filter((s) => s.status === 'slow').map((s) => s.server)
  const culprit = answer?.tenant ?? null
  const result = outcome(round.kind, round.target, answer)
  const innocents = innocentsPunished(tenants, rk.blame ? round.target : '')
  const top = busiest(tenants)
  const checked = seal(round.target, round.kind, round.salt)
  const clears = cleared(incidents, round.before)

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
      <Header
        step={phase === 'hunt' ? 2 : phase === 'answer' ? 3 : 4}
        title={
          phase === 'hunt'
            ? 'Something is happening. TenantGuard sees only the servers.'
            : phase === 'answer'
              ? rk.blame
                ? 'TenantGuard has named a company'
                : 'TenantGuard has punished a company'
              : 'Open the seal'
        }
      />
      <div className="panel flex flex-wrap items-baseline gap-x-6 gap-y-1">
        <span className="text-lg text-ink-300">Judge's sealed choice</span>
        <code className="text-2xl font-bold text-accent" data-testid="challenge-seal">
          {short(round.seal)}
        </code>
        {phase === 'hunt' && (
          <span className="ml-auto text-3xl font-black tabular-nums" data-testid="challenge-timer">
            {elapsed.toFixed(1)} s
          </span>
        )}
      </div>

      <div className="grid grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)] gap-4">
        <div className="panel h-[340px]">
          <ServerGrid
            servers={servers}
            highlight={culprit ? (barcodes[culprit] ?? []) : []}
            highlightLabel={culprit ? `${nameOf(culprit)}'s servers` : undefined}
            title="The servers TenantGuard is watching"
          />
        </div>

        <div className="panel flex flex-col gap-2">
          {phase === 'hunt' && (
            <>
              <div className="text-2xl font-bold">Watching…</div>
              <p className="text-lg text-ink-300">
                Every company runs on 3 of the 9 servers, and no two companies share more than one. If a company is causing this,
                its 3 slow servers point to it. If nobody is, TenantGuard must blame nobody.
              </p>
              <div className="mt-auto text-base text-ink-300">Limit {HUNT_LIMIT_S} s, then the seal is opened.</div>
            </>
          )}
          {answer && phase !== 'hunt' && (
            <>
              <div className="text-lg text-ink-300">{rk.blame ? 'TenantGuard names' : 'TenantGuard held back'}</div>
              <div className="text-4xl font-black" data-testid="challenge-answer">
                {nameOf(answer.tenant)} <span className="text-ink-300">({answer.tenant})</span>
              </div>
              <div className="text-xl">
                after <b>{foundAfter?.toFixed(1)} s</b>
                {answer.stage === 'named' && answer.confidence !== null && (
                  <> · decoder {Math.round(answer.confidence * 100)} % sure</>
                )}
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
                    Neighbours over the {proof.sloMs} ms target: <b>{proof.neighboursOver}</b>{' '}
                    <span className="text-ink-300">of {proof.neighbours}</span>
                  </li>
                  {barcodes[answer.tenant] && (
                    <li>
                      Its servers <b className="text-accent">{barcodes[answer.tenant].join(' ')}</b>
                      {sameSet(barcodes[answer.tenant], slow)
                        ? ' are exactly the slow ones'
                        : `; slow now: ${slow.join(' ') || 'none'}`}
                    </li>
                  )}
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
            <div className="flex flex-col gap-2" data-testid="challenge-answer">
              <div className="text-2xl font-bold">TenantGuard blamed no company in {watched.toFixed(0)} s.</div>
              {top && (
                <div className="text-lg text-ink-300">
                  Busiest company: <b className="text-ink-100">{top.name}</b> ({top.tenant}), noisy score{' '}
                  <b className="text-ink-100">{top.noisy_score.toFixed(1)}</b> (flagged from {warn}).
                </div>
              )}
              <div className="text-lg text-ink-300">Slow servers now: {slow.join(' ') || 'none'}</div>
            </div>
          )}
          {phase === 'answer' && (
            <button className="btn-accent mt-auto text-xl" onClick={openSeal}>
              Open the judge's seal
            </button>
          )}
        </div>
      </div>

      {phase === 'reveal' && (
        <div
          className={`panel flex flex-col gap-3 border-2 ${result.right ? 'border-ok' : 'border-bad'}`}
          data-testid="challenge-reveal"
        >
          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
            <span className={`text-4xl font-black ${result.right ? 'text-ok' : 'text-bad'}`}>{result.title}</span>
            <span className="text-2xl">
              {rk.target === 'server' ? (
                <>
                  The judge broke server <b>{round.target}</b>. No company was to blame.
                </>
              ) : rk.blame ? (
                <>
                  The judge picked <b>{nameOf(round.target)}</b> ({round.target}
                  ), {rk.label.toLowerCase()}.
                </>
              ) : (
                <>
                  The judge gave <b>{nameOf(round.target)}</b> ({round.target}) honest growth. No company was to blame.
                </>
              )}
            </span>
          </div>
          <div className="text-lg text-ink-300">
            Seal check: sha256("{round.target}|{round.kind}|{round.salt}") = <code className="text-ok">{short(checked)}</code>{' '}
            {checked === round.seal ? '✓ same as the seal shown before it started' : '✕ does not match'}
          </div>
          {clears.length > 0 && (
            <div className="text-xl">
              Checked and cleared by TenantGuard:{' '}
              {clears.map((c) => `${nameOf(c.tenant)} (${c.verdict.replace(/_/g, ' ')}, no penalty)`).join(', ')}
            </div>
          )}
          <div className="text-xl">
            Innocent companies held back right now:{' '}
            <b className={innocents.length ? 'text-bad' : 'text-ok'}>
              {innocents.length ? innocents.map((t) => `${t.name} (${t.tenant})`).join(', ') : '0'}
            </b>
          </div>
          <button className="btn-accent self-start text-xl" onClick={again}>
            Stop it · play again
          </button>
        </div>
      )}
      {phase !== 'reveal' && (
        <button className="btn-ghost self-start" onClick={openSeal}>
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
