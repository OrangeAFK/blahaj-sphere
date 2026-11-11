import { useCallback, useEffect, useState } from 'react'
import { api, REASON_CLASSES, waitJob, type CheckerView, type CheckTest, type DebriefView, type NodeView, type ProofView, type RunRecord } from './api/client'
import { Badge, Btn, Card, Err, errMsg } from './ui'
import { Tutor } from './Tutor'

const STAGES = ['prelab', 'lab', 'proof', 'oral', 'debrief', 'done'] as const
const WORKING = ['lab', 'proof']

export function Stepper({ status }: { status: string }) {
  const at = STAGES.indexOf(status as (typeof STAGES)[number])
  return (
    <ol aria-label="Stage" className="flex flex-wrap gap-1 text-xs">
      {STAGES.map((s, i) => (
        <li key={s} aria-current={s === status ? 'step' : undefined}
            className={`rounded-full px-2.5 py-0.5 ${s === status ? 'bg-sky-700 font-semibold text-white' : i < at ? 'bg-sky-100 text-sky-800' : 'bg-gray-100 text-gray-500'}`}>
          {i < at ? '✓ ' : ''}{s}
        </li>
      ))}
      {at < 0 && <li className="rounded-full bg-amber-100 px-2.5 py-0.5 text-amber-800">{status}</li>}
    </ol>
  )
}

export function NodePage({ id, home }: { id: number; home: () => void }) {
  const [node, setNode] = useState<NodeView | null>(null)
  const [err, setErr] = useState('')
  const [rescoping, setRescoping] = useState(false)
  const [busyMsg, setBusyMsg] = useState('')

  const load = useCallback(async () => {
    try {
      setNode(await api.node(id))
      setErr('')
    } catch (e) {
      setErr(errMsg(e))
    }
  }, [id])
  useEffect(() => { void load() }, [load])

  if (!node) return <div><Err e={err} />{!err && <p className="text-sm text-gray-500">Loading…</p>}</div>
  const working = WORKING.includes(node.status)
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Btn onClick={home}>← Home</Btn>
        <h1 className="text-lg font-bold">#{String(node.id).padStart(4, '0')} {node.title}</h1>
        {node.provisional && <Badge tone="amber">provisional: grades pending</Badge>}
        <Badge tone="gray">{node.domain} T{node.tier}</Badge>
        {node.size_est_min && <Badge tone="gray">~{node.size_est_min} min</Badge>}
        <span className="ml-auto" />
        {node.rescopable && <Btn danger onClick={() => setRescoping(true)}>Rescope…</Btn>}
      </div>
      <Stepper status={node.status} />
      {busyMsg && <p className="rounded bg-amber-50 p-2 text-sm text-amber-800">{busyMsg}</p>}
      <div className={working ? 'grid gap-3 lg:grid-cols-[3fr_2fr]' : ''}>
        <div className="space-y-3">
          <Card title="Brief"><details open={node.status === 'prelab'}><summary className="cursor-pointer text-sm text-sky-800">Show / hide brief</summary><pre className="mt-2 whitespace-pre-wrap font-sans text-sm">{node.brief}</pre></details></Card>
          {node.status === 'prelab' && <Prelab node={node} onDone={load} />}
          {working && <Lab node={node} reload={load} />}
          {node.status === 'oral' && <Oral node={node} onDone={load} />}
          {(node.status === 'debrief' || node.status === 'done') && <Debrief node={node} onDone={load} home={home} />}
          {!STAGES.includes(node.status as (typeof STAGES)[number]) && <Card><p className="text-sm">This node is {node.status}.</p></Card>}
          <Err e={err} />
        </div>
        {working && <Tutor node={node} />}
      </div>
      {rescoping && <Rescope node={node} close={() => setRescoping(false)} reload={load} setBusyMsg={setBusyMsg} />}
    </div>
  )
}

function Prelab({ node, onDone }: { node: NodeView; onDone: () => void }) {
  const [vals, setVals] = useState<Record<string, string>>({})
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const p = node.prelab
  const ready = p.items.every((it) => (vals[it.id] ?? '').trim() !== '')
  const submit = async () => {
    setBusy(true)
    try {
      const answers: Record<string, string | number> = {}
      for (const it of p.items) answers[it.id] = it.kind === 'numeric' ? Number(vals[it.id]) : vals[it.id]
      await api.submitPrelab(node.id, answers)
      onDone()
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card title="Prelab" right={p.locked ? <Badge tone="green">locked</Badge> : <Badge tone="amber">locks on submit</Badge>}>
      <p className="mb-2 text-xs text-gray-600">Answer before the workspace unlocks. Units are fixed. Answers are immutable once submitted and graded only in the debrief.</p>
      <div className="space-y-3">
        {p.items.map((it) => (
          <label key={it.id} className="block text-sm">
            <span className="font-mono text-xs text-sky-700">{it.id}</span> {it.prompt}
            {it.kind === 'numeric' ? (
              <span className="mt-1 flex items-center gap-2">
                <input className="w-40 rounded border px-2 py-1 font-mono" inputMode="decimal" aria-label={it.id} value={vals[it.id] ?? ''}
                       disabled={p.locked} onChange={(e) => setVals({ ...vals, [it.id]: e.target.value })} />
                <span className="rounded bg-sky-50 px-2 py-1 font-mono text-sky-900" title="fixed unit">{it.unit}</span>
              </span>
            ) : (
              <textarea className="mt-1 block w-full rounded border px-2 py-1" rows={3} aria-label={it.id} value={vals[it.id] ?? ''}
                        disabled={p.locked} onChange={(e) => setVals({ ...vals, [it.id]: e.target.value })} />
            )}
          </label>
        ))}
      </div>
      <Btn primary className="mt-3" disabled={!ready || busy} onClick={submit}>Lock answers and unlock the lab</Btn>
      <Err e={err} />
    </Card>
  )
}

function TestTable({ tests }: { tests: CheckTest[] }) {
  if (tests.length === 0) return <p className="text-sm text-gray-500">No tests reported.</p>
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-xs uppercase text-gray-500"><tr><th>Test</th><th>Result</th><th>Measured</th><th>Spec</th><th>Unit</th></tr></thead>
      <tbody>
        {tests.map((t, i) => (
          <tr key={i} className="border-t">
            <td>{t.name}</td>
            <td className={t.pass ? 'text-green-700' : 'text-red-700'}>{t.pass ? 'pass' : 'fail'}</td>
            <td className="font-mono">{String(t.measured ?? '')}</td>
            <td className="font-mono">{String(t.spec ?? '')}</td>
            <td>{t.unit}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function CheckResult({ c }: { c: CheckerView }) {
  return (
    <div data-testid="checker-result" className="mt-2">
      <div className="mb-1 flex items-center gap-2 text-sm">
        <Badge tone={c.verdict === 'pass' ? 'green' : c.verdict === 'error' ? 'amber' : 'red'}>{c.verdict}</Badge>
        <span className="text-xs text-gray-500">{c.kind} run {c.seconds != null && `· ${c.seconds}s`}</span>
      </div>
      {c.detail && <pre className="max-h-32 overflow-auto rounded bg-gray-50 p-1 text-xs">{c.detail}</pre>}
      <TestTable tests={c.tests as CheckTest[]} />
    </div>
  )
}

function Lab({ node, reload }: { node: NodeView; reload: () => void }) {
  const [check, setCheck] = useState<CheckerView | null>(null)
  const [proof, setProof] = useState<ProofView | null>(null)
  const [run, setRun] = useState<RunRecord | null>(null)
  const [runLog, setRunLog] = useState<string[]>([])
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [running, setRunning] = useState('')
  const id = node.id

  const refreshProof = useCallback(async () => setProof(await api.proof(id)), [id])
  useEffect(() => {
    api.checker(id).then(setCheck).catch(() => {})
    api.latestRun(id).then(setRun).catch(() => {})
    refreshProof().catch(() => {})
  }, [id, refreshProof])

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setErr('')
    setMsg(`${label}…`)
    try {
      await fn()
      setMsg(`${label}: done`)
    } catch (e) {
      setMsg('')
      setErr(`${label} failed: ${errMsg(e)}`)
    }
  }
  const runJob = async (label: string, start: () => Promise<{ job_id: string }>, after: () => Promise<void>, tick?: (log: string[]) => void) => {
    setRunning(label)
    setErr('')
    try {
      const j = await waitJob((await start()).job_id, 1000, tick ? (x) => tick(x.log_tail) : undefined)
      if (j.status === 'failed') setErr(`${label} failed: ${j.error ?? 'job failed'}`)
      await after()
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setRunning('')
    }
  }
  const dir = node.dir

  return (
    <>
      <Card title="Lab" right={<code className="truncate text-xs text-gray-500" title={node.workspace ?? ''}>{node.workspace}</code>}>
        {node.long_run_warning && (
          <p className="mb-2 rounded bg-amber-50 p-2 text-sm text-amber-800">Long run: the reference simulation took {node.sim_runtime_s}s. The checker can take several minutes.</p>
        )}
        <div className="flex flex-wrap gap-2">
          <Btn onClick={() => act('Open folder', async () => { await api.openFolder(id) })}>Open folder</Btn>
          <Btn onClick={() => act('JupyterLab', () => api.launchJupyter(dir))}>Jupyter</Btn>
          <Btn onClick={() => act('Xschem', () => api.launchGui('xschem', dir))}>Xschem</Btn>
          <Btn onClick={() => act('KLayout', () => api.launchGui('klayout', dir))}>KLayout</Btn>
          <Btn onClick={() => act('Qucs-S', () => api.launchGui('qucs-s', dir))}>Qucs-S</Btn>
          <Btn disabled={!!running} onClick={() => { setRunLog([]); void runJob('Simulation', () => api.runSimulation(id), async () => setRun(await api.latestRun(id)), setRunLog) }}>
            {running === 'Simulation' ? 'Running simulation…' : 'Run simulation'}
          </Btn>
          <Btn primary disabled={!!running} onClick={() => runJob('Checker', () => api.runChecker(id), async () => setCheck(await api.checker(id)))}>
            {running === 'Checker' ? 'Running checker…' : 'Run checker'}
          </Btn>
        </div>
        {msg && <p className="mt-1 text-xs text-gray-600">{msg}</p>}
        {(running === 'Simulation' || runLog.length > 0) && (
          <pre data-testid="run-log" className="mt-2 max-h-40 overflow-auto rounded bg-slate-900 p-2 text-xs text-green-100">{runLog.join('\n') || 'starting…'}</pre>
        )}
        {run && (
          <div data-testid="run-result" className="mt-2 text-sm">
            <div className="flex items-center gap-2">
              <Badge tone={run.ok ? 'green' : 'red'}>{run.ok ? 'simulation ok' : 'simulation failed'}</Badge>
              <span className="text-xs text-gray-500">{run.seconds}s · {run.steps.map((s) => `${s.cmd} → ${s.exit_code}`).join(' ; ')}</span>
            </div>
            {run.steps.filter((st) => st.stderr_tail).map((st, i) => <pre key={i} className="mt-1 max-h-24 overflow-auto bg-gray-50 p-1 text-xs">{st.stderr_tail}</pre>)}
            <p className="mt-1 text-xs uppercase text-gray-500">Files written</p>
            <ul className="font-mono text-xs">
              {run.files_changed.map((f) => <li key={f.path}>{f.path} <span className="text-gray-500">{f.size} B</span></li>)}
              {run.files_changed.length === 0 && <li className="text-gray-500">none</li>}
            </ul>
          </div>
        )}
        {check && <CheckResult c={check} />}
        <Err e={err} />
      </Card>
      <Card title="Proof" right={proof && proof.submissions.length > 0 && <Badge tone="gray">{proof.submissions.length} submission(s)</Badge>}>
        <p className="text-sm">Submits <span className="font-mono">{node.proof_artifacts.join(', ')}</span>. No penalty for retries.</p>
        <Btn primary className="mt-2" disabled={!!running}
             onClick={() => runJob('Proof', () => api.submitProof(id), async () => { await refreshProof(); reload() })}>
          {running === 'Proof' ? 'Checking proof…' : 'Submit proof'}
        </Btn>
        {proof?.submissions.slice().reverse().map((s) => (
          <div key={s.n} data-testid={`proof-${s.n}`} className="mt-2 border-t pt-1">
            <div className="flex items-center gap-2 text-sm">
              <span>Submission {s.n}</span>
              <Badge tone={s.verdict === 'pass' ? 'green' : s.verdict === 'fail' ? 'red' : 'amber'}>{s.verdict ?? s.status}</Badge>
              {s.seconds != null && <span className="text-xs text-gray-500">{s.seconds}s</span>}
            </div>
            {s.detail && <pre className="max-h-24 overflow-auto bg-gray-50 p-1 text-xs">{s.detail}</pre>}
            <TestTable tests={s.tests} />
          </div>
        ))}
        {proof?.remediation_offer && proof.remediation_decision === null && (
          <div className="mt-2 rounded bg-amber-50 p-2 text-sm">
            Three failed submissions. Want a remediation node on the underlying skills?
            <span className="ml-2 inline-flex gap-2">
              <Btn onClick={() => api.remediation(id, true).then(setProof).catch((e) => setErr(errMsg(e)))}>Yes</Btn>
              <Btn onClick={() => api.remediation(id, false).then(setProof).catch((e) => setErr(errMsg(e)))}>No, keep going</Btn>
            </span>
          </div>
        )}
        {proof?.remediation_decision && <p className="mt-2 text-sm text-gray-600">Remediation: {proof.remediation_decision}.</p>}
      </Card>
    </>
  )
}

function Oral({ node, onDone }: { node: NodeView; onDone: () => void }) {
  const [info, setInfo] = useState<{ note: string } | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { api.oral(node.id).then(setInfo).catch((e) => setErr(errMsg(e))) }, [node.id])
  const go = async () => {
    setBusy(true)
    try {
      await api.advanceOral(node.id)
      onDone()
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card title="Oral" right={<Badge tone="amber">text stub</Badge>}>
      <p className="text-sm">Proof passed. Voice oral arrives in M7; for now the text stub moves you straight to the debrief.</p>
      {info && <p className="mt-1 text-xs text-gray-500">{info.note}</p>}
      <Btn primary className="mt-2" disabled={busy} onClick={go}>{busy ? 'Grading explanations…' : 'Start oral (text) and go to debrief'}</Btn>
      <Err e={err} />
    </Card>
  )
}

function Debrief({ node, onDone, home }: { node: NodeView; onDone: () => void; home: () => void }) {
  const [d, setD] = useState<DebriefView | null>(null)
  const [err, setErr] = useState('')
  const load = useCallback(() => api.debrief(node.id).then(setD).catch((e) => setErr(errMsg(e))), [node.id])
  useEffect(() => {
    void load()
    const t = setInterval(load, 5000) // pending grades arrive from the retry queue while this is open
    return () => clearInterval(t)
  }, [load])
  if (!d) return <Card title="Debrief"><Err e={err} /></Card>
  const finish = async () => {
    try {
      await api.completeDebrief(node.id)
      onDone()
    } catch (e) {
      setErr(errMsg(e))
    }
  }
  return (
    <Card title="Debrief" right={d.grades.status === 'pending' ? <Badge tone="amber">grades pending</Badge> : <Badge tone="green">graded</Badge>}>
      <p className="text-sm">Proof: {d.proof.passed ? 'passed' : 'not passed'} after {d.proof.submissions} submission(s).</p>
      {d.checker && (
        <div data-testid="debrief-checker">
          <h3 className="mt-3 text-sm font-semibold">Final checker result</h3>
          <CheckResult c={d.checker} />
        </div>
      )}
      <h3 className="mt-3 text-sm font-semibold">Prediction vs actual ({d.calibration.within}/{d.calibration.total} within tolerance)</h3>
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-gray-500"><tr><th>Item</th><th>Predicted</th><th>Actual</th><th>Error</th><th /></tr></thead>
        <tbody>
          {d.calibration.numeric.map((c) => (
            <tr key={c.id} className="border-t">
              <td>{c.id} <span className="text-xs text-gray-500">{c.subskill}</span></td>
              <td className="font-mono">{c.predicted} {c.unit}</td>
              <td className="font-mono">{Number(c.actual.toPrecision(4))} {c.unit}</td>
              <td className="font-mono">{c.rel_error != null ? `${(c.rel_error * 100).toFixed(1)}%` : '–'}</td>
              <td className={c.within ? 'text-green-700' : 'text-red-700'}>{c.within ? 'within' : 'outside'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3 className="mt-3 text-sm font-semibold">Written explanations</h3>
      {d.grades.status === 'pending' && (
        <ul className="text-sm">
          {d.grades.pending_items.map((i) => <li key={i.id}>{i.id}: <Badge tone="amber">pending</Badge> <span className="text-gray-500">{i.subskill}</span></li>)}
          <li className="mt-1"><Btn onClick={() => api.retryGrading().then(load).catch((e) => setErr(errMsg(e)))}>Retry grading now</Btn></li>
        </ul>
      )}
      {d.grades.items.map((g) => (
        <div key={g.id} className="mt-1 text-sm" data-testid={`grade-${g.id}`}>
          <span className="font-mono text-xs text-sky-700">{g.id}</span> <span className="font-semibold">{g.score}/3</span>{' '}
          <span className="text-xs text-gray-500">{g.subskill}</span>
          {g.quote && <blockquote className="border-l-2 border-sky-300 pl-2 text-gray-700">“{g.quote}”</blockquote>}
          <p className="text-gray-600">{g.rationale}</p>
        </div>
      ))}
      <h3 className="mt-3 text-sm font-semibold">Misconceptions</h3>
      <ul className="text-sm">
        {d.misconceptions.logged.map((m) => <li key={m.id}>logged: {m.text} <span className="text-xs text-gray-500">{m.subskill}</span></li>)}
        {d.misconceptions.resolved.map((m) => <li key={m.id} className="text-green-700">resolved: {m.text}</li>)}
        {d.misconceptions.logged.length + d.misconceptions.resolved.length === 0 && <li className="text-gray-500">none</li>}
      </ul>
      <h3 className="mt-3 text-sm font-semibold">Mastery</h3>
      <ul className="text-sm">
        {d.mastery.map((m) => (
          <li key={m.domain}>
            {m.domain} T{m.tier}: {m.mastery_before}% → {m.mastery_after}%{' '}
            <span className={m.delta >= 0 ? 'text-green-700' : 'text-red-700'}>({m.delta >= 0 ? '+' : ''}{m.delta})</span>
            {m.exam_unlocked && <Badge tone="green">tier exam unlocked</Badge>}
          </li>
        ))}
        {d.grades.status === 'pending' && <li className="text-xs text-amber-700">Provisional: deltas will change when grades land.</li>}
      </ul>
      <p className="mt-2 text-xs text-gray-500">Oral: {d.oral.note}</p>
      {d.status === 'debrief' ? (
        <Btn primary className="mt-3" onClick={finish}>Finish debrief</Btn>
      ) : (
        <Btn primary className="mt-3" onClick={home}>Plan the next node</Btn>
      )}
      <Err e={err} />
    </Card>
  )
}

const REASON_LABEL: Record<string, string> = {
  too_hard: 'Too hard (counts as a failure for difficulty control)',
  wrong_direction: 'Wrong direction',
  objection: 'I object to the task',
  broken_env: 'Broken environment',
}

function Rescope({ node, close, reload, setBusyMsg }: { node: NodeView; close: () => void; reload: () => void; setBusyMsg: (s: string) => void }) {
  const [cls, setCls] = useState<string>(REASON_CLASSES[0])
  const [reason, setReason] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [res, setRes] = useState<{ action: string; changes: string; backup?: string | null } | null>(null)

  const go = async () => {
    setBusy(true)
    setErr('')
    try {
      const r = await api.rescope(node.id, cls, reason)
      setRes({ action: r.action, changes: r.changes, backup: r.backup })
      setBusyMsg(`Rescope (${r.action}) running: ${r.changes}`)
      const j = await waitJob(r.job_id)
      setBusyMsg(j.status === 'failed' ? `Rescope failed: ${j.error}` : r.action === 'replace' ? `Replacement node ${r.node_id} built; this node is rescoped.` : '')
      reload()
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div role="dialog" aria-label="Rescope" className="fixed inset-0 z-10 flex items-center justify-center bg-black/40 p-4">
      <div className="max-h-full w-full max-w-lg overflow-auto rounded-lg bg-white p-4 shadow-xl">
        <h2 className="text-lg font-semibold">Rescope this node</h2>
        <fieldset className="mt-2 space-y-1 text-sm">
          <legend className="text-xs uppercase text-gray-500">Reason</legend>
          {REASON_CLASSES.map((c) => (
            <label key={c} className="flex items-center gap-2">
              <input type="radio" name="reason" checked={cls === c} onChange={() => setCls(c)} /> {REASON_LABEL[c]}
            </label>
          ))}
        </fieldset>
        <textarea className="mt-2 block w-full rounded border px-2 py-1 text-sm" rows={3} placeholder="Say why, in your own words" aria-label="Reason" value={reason}
                  onChange={(e) => setReason(e.target.value)} />
        <p className="mt-2 rounded bg-amber-50 p-2 text-xs text-amber-900">
          The agent either <b>edits</b> this node or <b>replaces</b> it. An edit <b>resets the workspace</b> to a fresh starter; your current files are saved first in{' '}
          <code>data/builds/{node.dir}/rescope-&lt;timestamp&gt;/node/workspace</code> (the exact folder is shown once you confirm). Prelab answers stay locked.
        </p>
        {res && (
          <div className="mt-2 text-sm" data-testid="rescope-result">
            <p>Agent chose <b>{res.action}</b>: {res.changes}</p>
            {res.backup && (
              <p>Backup of your workspace: <code className="break-all">data/{res.backup}</code>{' '}
                <Btn onClick={() => api.openFolder(node.id, 'backup').catch((e) => setErr(errMsg(e)))}>Open backup folder</Btn></p>
            )}
          </div>
        )}
        <Err e={err} />
        <div className="mt-3 flex justify-end gap-2">
          <Btn onClick={close}>{res ? 'Close' : 'Cancel'}</Btn>
          <Btn danger disabled={busy || !reason.trim() || !!res} onClick={go}>{busy ? 'Rescoping…' : 'Rescope'}</Btn>
        </div>
      </div>
    </div>
  )
}
