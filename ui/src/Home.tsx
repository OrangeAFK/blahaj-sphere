import { useCallback, useEffect, useState } from 'react'
import { api, type BranchOptionT, type MasteryDelta, type NodeSummary, type PlannerOptions } from './api/client'
import { Badge, Btn, Card, Err, errMsg } from './ui'

const DONE = ['done', 'rescoped', 'abandoned']
const ACTION: Record<string, string> = {
  submit_prelab: 'Answer the prelab',
  submit_proof: 'Open the lab',
  start_oral: 'Start the oral (text)',
  finish_debrief: 'Open the debrief',
  plan_next: 'Plan the next node',
}

/** The node the learner is working on: the newest one that is not finished. */
export const currentNode = (nodes: NodeSummary[]) => [...nodes].filter((n) => !DONE.includes(n.status)).sort((a, b) => b.id - a.id)[0]

/** Mastery per domain = `mastery_after` of the latest debrief that touched it (no scores endpoint exists; DECISIONS M6b). */
async function loadMastery(nodes: NodeSummary[]): Promise<Record<string, MasteryDelta>> {
  const out: Record<string, MasteryDelta> = {}
  for (const n of [...nodes].filter((n) => n.status === 'done' || n.status === 'debrief').sort((a, b) => a.id - b.id)) {
    try {
      for (const m of (await api.debrief(n.id)).mastery) out[m.domain] = m
    } catch {
      /* a node without a debrief yet */
    }
  }
  return out
}

export function Home({ open }: { open: (id: number) => void }) {
  const [nodes, setNodes] = useState<NodeSummary[] | null>(null)
  const [mastery, setMastery] = useState<Record<string, MasteryDelta>>({})
  const [opts, setOpts] = useState<PlannerOptions | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const refresh = useCallback(async () => {
    try {
      const ns = await api.nodes()
      setNodes(ns)
      setMastery(await loadMastery(ns))
    } catch (e) {
      setErr(errMsg(e))
    }
  }, [])
  useEffect(() => {
    void refresh()
    const t = setInterval(refresh, 5000) // a finished build shows up without a reload; Home never blocks on it
    return () => clearInterval(t)
  }, [refresh])

  const cur = nodes ? currentNode(nodes) : undefined
  const plan = async () => {
    setBusy(true)
    setErr('')
    try {
      setOpts(await api.generateOptions())
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy(false)
    }
  }
  const build = async (i: number) => {
    setErr('')
    try {
      const b = await api.startBuild(i)
      setOpts(null)
      setErr(`Building node ${b.node_id} (${b.node_dir}). Progress is in the job tray; you can keep using the app.`)
    } catch (e) {
      setErr(errMsg(e))
    }
  }

  return (
    <div className="grid gap-3 md:grid-cols-[2fr_1fr]">
      <Card title="Current node">
        {nodes === null ? (
          <p className="text-sm text-gray-500">Loading…</p>
        ) : cur ? (
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-sm text-gray-500">#{String(cur.id).padStart(4, '0')}</span>
              <span className="font-semibold">{cur.title}</span>
              <Badge>{cur.status}</Badge>
              {cur.domain && <Badge tone="gray">{cur.domain} T{cur.tier}</Badge>}
              {cur.provisional && <Badge tone="amber">provisional</Badge>}
            </div>
            <Btn primary className="mt-3" onClick={() => open(cur.id)}>{ACTION[cur.next_action] ?? 'Open node'}</Btn>
          </div>
        ) : (
          <div>
            <p className="text-sm">No active node. Ask the Planner for the next options.</p>
            <Btn primary className="mt-3" disabled={busy} onClick={plan}>{busy ? 'Planning…' : ACTION.plan_next}</Btn>
          </div>
        )}
        {opts && (
          <ul className="mt-3 space-y-2">
            {opts.options.map((o: BranchOptionT, i: number) => (
              <li key={i} className="rounded border border-sky-100 p-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold">{o.title}</span>
                  <Badge tone="gray">{o.kind}</Badge>
                  <Badge tone="gray">{o.domain} T{o.tier}</Badge>
                  <Badge tone="gray">~{o.size_est_min} min</Badge>
                </div>
                <p className="text-gray-600">{o.why_now}</p>
                <Btn className="mt-1" onClick={() => build(i)}>Build this</Btn>
              </li>
            ))}
          </ul>
        )}
        <Err e={err} />
        {nodes && nodes.length > 0 && (
          <details className="mt-3 text-sm">
            <summary className="cursor-pointer text-sky-800">All nodes ({nodes.length})</summary>
            <ul className="mt-1 divide-y">
              {nodes.map((n) => (
                <li key={n.id} className="flex items-center gap-2 py-1">
                  <button className="truncate text-left text-sky-800 underline" onClick={() => open(n.id)}>#{n.id} {n.title}</button>
                  <Badge tone={n.status === 'done' ? 'green' : 'gray'}>{n.status}</Badge>
                </li>
              ))}
            </ul>
          </details>
        )}
      </Card>
      <Card title="Mastery">
        {Object.keys(mastery).length === 0 && <p className="text-sm text-gray-500">No evidence yet. Finish a node to fill these.</p>}
        <ul className="space-y-2">
          {Object.values(mastery).map((m) => (
            <li key={m.domain} data-testid={`mastery-${m.domain}`}>
              <div className="flex justify-between text-sm">
                <span>{m.domain} <span className="text-gray-500">T{m.tier}</span></span>
                <span className="font-mono">{m.mastery_after}%</span>
              </div>
              <progress className="h-2 w-full accent-sky-600" max={100} value={m.mastery_after} aria-label={`${m.domain} mastery`} />
            </li>
          ))}
        </ul>
      </Card>
    </div>
  )
}
