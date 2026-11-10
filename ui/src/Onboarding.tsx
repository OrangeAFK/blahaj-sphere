import { useCallback, useEffect, useState } from 'react'
import { api, type DiagnosticView, type Framework, type InterviewView, type OnboardingState } from './api/client'

const TIERS = ['1', '2', '3', '4', '5'] as const
const btn = 'rounded bg-blue-600 px-3 py-1 text-white disabled:opacity-50'

/** Runs an async action with a busy flag and an error message (LLM calls take seconds). */
function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    setError('')
    try {
      await fn()
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, run }
}

function Interview({ onDone }: { onDone: () => void }) {
  const [v, setV] = useState<InterviewView | null>(null)
  const [answer, setAnswer] = useState('')
  const { busy, error, run } = useAction()

  useEffect(() => { api.interview().then(setV).catch(() => {}) }, [])

  const turn = (a?: string) =>
    run(async () => {
      const next = await api.interviewTurn(a)
      setV(next)
      setAnswer('')
      if (next.done) onDone()
    })

  const started = (v?.questions_asked ?? 0) > 0
  return (
    <section aria-label="Interview" className="mt-4">
      <h3 className="font-semibold">1. Interview</h3>
      {!started && (
        <button className={`${btn} mt-2`} disabled={busy} onClick={() => turn()}>
          {busy ? 'Starting…' : 'Start interview'}
        </button>
      )}
      {started && v && !v.done && (
        <div className="mt-2 space-y-2">
          <p className="text-sm text-gray-500">Question {v.questions_asked} of up to {v.max_questions}</p>
          <p className="rounded bg-sky-50 p-2">{v.last_message}</p>
          <textarea
            aria-label="Your answer" className="block w-full rounded border px-2 py-1" rows={3}
            value={answer} onChange={(e) => setAnswer(e.target.value)}
          />
          <button className={btn} disabled={busy || !answer.trim()} onClick={() => turn(answer)}>
            {busy ? 'Thinking…' : 'Send'}
          </button>
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
    </section>
  )
}

function GoalsEditor() {
  const [text, setText] = useState('')
  const [saved, setSaved] = useState(false)
  useEffect(() => { api.interview().then((v) => setText(v.goals ?? '')).catch(() => {}) }, [])
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-sm text-gray-600">Your goals (editable anytime)</summary>
      <textarea
        aria-label="Goals" className="mt-1 block w-full rounded border px-2 py-1 font-mono text-sm" rows={8}
        value={text} onChange={(e) => { setSaved(false); setText(e.target.value) }}
      />
      <button className={`${btn} mt-1`} onClick={async () => { await api.putGoals(text); setSaved(true) }}>Save goals</button>
      {saved && <span className="ml-2 text-sm text-green-600">Saved</span>}
    </details>
  )
}

function FrameworkStep({ onDone }: { onDone: () => void }) {
  const [fw, setFw] = useState<Framework | null>(null)
  const [frozen, setFrozen] = useState(false)
  const [dirty, setDirty] = useState(false)
  const { busy, error, run } = useAction()

  useEffect(() => {
    api.getFramework().then((v) => { setFw(v.framework); setFrozen(v.frozen) }).catch(() => {})
  }, [])

  const edit = (fn: (draft: Framework) => void) => {
    if (!fw) return
    const copy = structuredClone(fw)
    fn(copy)
    setFw(copy)
    setDirty(true)
  }

  return (
    <section aria-label="Framework" className="mt-4">
      <h3 className="font-semibold">2. Competency framework</h3>
      {!fw && (
        <button className={`${btn} mt-2`} disabled={busy} onClick={() => run(async () => setFw((await api.draftFramework()).framework))}>
          {busy ? 'Drafting (this takes a minute)…' : 'Draft framework'}
        </button>
      )}
      {fw && (
        <div className="mt-2 space-y-3">
          <p className="text-sm text-gray-500">
            {frozen ? 'Frozen (v1).' : 'Edit anything, then approve. Approving freezes it: later changes need a version bump.'}
          </p>
          {fw.domains.map((d, i) => (
            <fieldset key={d.id} className="rounded border p-2" disabled={frozen}>
              <legend className="px-1 text-sm font-medium">{d.id}</legend>
              <input
                aria-label={`${d.id} name`} className="block w-full rounded border px-2 py-1 font-semibold" value={d.name}
                onChange={(e) => edit((f) => { f.domains[i].name = e.target.value })}
              />
              <input
                aria-label={`${d.id} scope`} className="mt-1 block w-full rounded border px-2 py-1 text-sm" value={d.scope ?? ''}
                onChange={(e) => edit((f) => { f.domains[i].scope = e.target.value })}
              />
              {TIERS.map((t) => (
                <label key={t} className="mt-1 flex items-start gap-2 text-sm">
                  <span className="w-6 pt-1 font-mono">T{t}</span>
                  <textarea
                    aria-label={`${d.id} T${t}`} className="w-full rounded border px-2 py-1" rows={2} value={d.tiers[Number(t)] ?? ''}
                    onChange={(e) => edit((f) => { f.domains[i].tiers[Number(t)] = e.target.value })}
                  />
                </label>
              ))}
            </fieldset>
          ))}
          {!frozen && (
            <div className="flex gap-2">
              <button
                className="rounded border px-3 py-1 disabled:opacity-50" disabled={busy || !dirty}
                onClick={() => run(async () => { await api.saveFrameworkDraft(fw); setDirty(false) })}
              >
                Save draft
              </button>
              <button
                className={btn} disabled={busy}
                onClick={() => run(async () => { await api.approveFramework(fw); setFrozen(true); onDone() })}
              >
                Approve and freeze
              </button>
            </div>
          )}
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
    </section>
  )
}

function Diagnostic({ onDone }: { onDone: () => void }) {
  const [v, setV] = useState<DiagnosticView | null>(null)
  const [answer, setAnswer] = useState('')
  const [lastScore, setLastScore] = useState<number | null>(null)
  const { busy, error, run } = useAction()

  useEffect(() => { api.getDiagnostic().then(setV).catch(() => {}) }, [])

  const next = () => run(async () => setV(await api.diagnosticNext()))
  const submit = () =>
    run(async () => {
      const r = await api.diagnosticAnswer(answer)
      setAnswer('')
      setLastScore(r.score)
      setV(r.view)
      if (r.view.done) onDone()
    })

  const probes = v?.domains.reduce((n, d) => n + d.probes.length, 0) ?? 0
  return (
    <section aria-label="Diagnostic" className="mt-4">
      <h3 className="font-semibold">3. Diagnostic</h3>
      {v && (
        <p className="text-sm text-gray-500">
          {v.domains.filter((d) => d.done).length} of {v.domains.length} domains placed, {probes} probes answered
        </p>
      )}
      {v?.done && v.tiers && (
        <ul className="mt-2 text-sm">
          {v.domains.map((d) => <li key={d.id}>{d.name}: starts at <b>T{v.tiers?.[d.id]}</b></li>)}
        </ul>
      )}
      {v && !v.done && !v.pending && (
        <button className={`${btn} mt-2`} disabled={busy} onClick={next}>
          {busy ? 'Preparing…' : probes === 0 ? 'Start diagnostic' : 'Next question'}
        </button>
      )}
      {v?.pending && (
        <div className="mt-2 space-y-2">
          <p className="text-sm text-gray-500">{v.pending.domain} · tier {v.pending.tier}</p>
          <p className="rounded bg-sky-50 p-2">{v.pending.question}</p>
          <textarea
            aria-label="Your answer" className="block w-full rounded border px-2 py-1" rows={4}
            value={answer} onChange={(e) => setAnswer(e.target.value)}
          />
          <button className={btn} disabled={busy || !answer.trim()} onClick={submit}>{busy ? 'Grading…' : 'Submit answer'}</button>
        </div>
      )}
      {lastScore !== null && !v?.done && <p className="mt-1 text-sm text-gray-500">Last answer scored {lastScore}/3.</p>}
      {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
    </section>
  )
}

/** The three onboarding steps (SPEC §9), shown for the stage the backend reports. */
export function Onboarding() {
  const [st, setSt] = useState<OnboardingState | null>(null)
  const refresh = useCallback(() => { api.onboardingState().then(setSt).catch(() => {}) }, [])
  useEffect(refresh, [refresh])
  if (!st) return null
  return (
    <section aria-label="Onboarding" className="mt-6">
      <h2 className="text-lg font-semibold">Onboarding</h2>
      {st.stage === 'interview' && <Interview onDone={refresh} />}
      {st.stage !== 'interview' && <GoalsEditor />}
      {st.stage === 'framework' && <FrameworkStep onDone={refresh} />}
      {(st.stage === 'diagnostic' || st.stage === 'done') && <Diagnostic onDone={refresh} />}
    </section>
  )
}
