import { useCallback, useEffect, useState } from 'react'
import { api, type CompetencyView, type DomainCompetency } from './api/client'
import { Badge, Btn, Card, Err, errMsg } from './ui'

const TIER_TONE: Record<string, string> = {
  passed: 'border-green-300 bg-green-50 text-green-900',
  current: 'border-sky-500 bg-sky-50 font-medium text-sky-950',
  locked: 'border-gray-200 bg-gray-50 text-gray-500',
}

function ExamBox({ d, start, busy }: { d: DomainCompetency; start: () => void; busy: boolean }) {
  const e = d.exam
  if (e.state === 'max_tier') return <p className="text-sm text-gray-600">Top tier reached: there is no exam above T{d.tier}.</p>
  if (e.state === 'in_progress') return <p className="text-sm text-amber-800">A tier exam is in progress (node #{e.node_id}).</p>
  if (e.state === 'available') {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="green">tier exam unlocked</Badge>
        <Btn primary disabled={busy} onClick={start}>{busy ? 'Writing the exam…' : `Take the T${d.tier} → T${d.tier + 1} exam`}</Btn>
        <span className="text-xs text-gray-500">{e.questions} oral questions, pass: mean ≥ {e.pass_mean} and no 0</span>
      </div>
    )
  }
  return (
    <p className="text-sm text-gray-600" data-testid={`exam-missing-${d.domain}`}>
      Exam locked: {e.missing.join('; ')}.
    </p>
  )
}

/** Competency screen (SPEC §11): domains x tiers with descriptors, mastery, tier-exam availability and subskills of the ACTIVE track. */
export function Competency({ open }: { open: (id: number) => void }) {
  const [view, setView] = useState<CompetencyView | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(() => api.competency().then((v) => { setView(v); setErr('') }).catch((e) => setErr(errMsg(e))), [])
  useEffect(() => { void load() }, [load])

  const start = async (domain: string) => {
    setBusy(domain)
    setErr('')
    try {
      const x = await api.startTierExam(domain)
      open(x.node_id)
    } catch (e) {
      setErr(errMsg(e))
      setBusy('')
    }
  }

  if (!view) return <div><Err e={err} />{!err && <p className="text-sm text-gray-500">Loading…</p>}</div>
  return (
    <div className="space-y-3">
      <Err e={err} />
      {view.domains.map((d) => (
        <Card key={d.domain} title={d.name} right={<Badge tone="sky">T{d.tier}</Badge>}>
          <div data-testid={`domain-${d.domain}`}>
            {d.scope && <p className="text-xs text-gray-500">{d.scope}</p>}
            <div className="mt-2 flex items-center gap-2 text-sm">
              <span className="w-24 shrink-0">Mastery at T{d.tier}</span>
              <progress className="h-2 flex-1 accent-sky-600" max={100} value={d.mastery} aria-label={`${d.name} mastery`} />
              <span className="w-12 text-right font-mono">{d.mastery}%</span>
            </div>
            <p className="text-xs text-gray-500">
              Unlock at {d.unlock.mastery_needed}% with {d.unlock.subskills_needed} subskills evidenced: now {d.mastery}% and {d.unlock.subskills_evidenced}.
            </p>
            <ol aria-label={`${d.name} tiers`} className="mt-2 grid gap-1 md:grid-cols-5">
              {d.tiers.map((t) => (
                <li key={t.tier} data-testid={`tier-${d.domain}-${t.tier}`} data-state={t.state} className={`rounded border p-2 text-xs ${TIER_TONE[t.state]}`}>
                  <b>T{t.tier}</b> {t.state === 'passed' && '✓'} {t.descriptor}
                </li>
              ))}
            </ol>
            <ul className="mt-2 flex flex-wrap gap-1" aria-label={`${d.name} subskills`}>
              {d.subskills.map((s) => (
                <li key={s.name}><Badge tone={s.evidenced ? 'green' : 'gray'}>{s.name}{s.evidenced ? ' ✓' : ''}</Badge></li>
              ))}
            </ul>
            <div className="mt-2"><ExamBox d={d} busy={busy === d.domain} start={() => void start(d.domain)} /></div>
            {d.exam.attempts.length > 0 && (
              <ul className="mt-1 text-xs text-gray-600">
                {d.exam.attempts.map((a) => (
                  <li key={a.node_id}>
                    <button className="text-sky-800 underline" onClick={() => open(a.node_id)}>Exam #{a.node_id} (T{a.tier})</button>{' '}
                    {a.passed == null ? a.status : a.passed ? `passed, mean ${a.mean?.toFixed(2)}${a.tier_up ? ', tier up' : ''}` : `not passed, mean ${a.mean?.toFixed(2)}`}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      ))}
    </div>
  )
}
