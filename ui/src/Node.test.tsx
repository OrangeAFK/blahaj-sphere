import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { Home } from './Home'
import { NodePage } from './NodePage'

type Handler = (body: unknown) => unknown
let routes: Record<string, Handler>
const calls: { key: string; body: unknown }[] = []

const node = (over: Record<string, unknown> = {}) => ({
  id: 4, dir: '0004-x', title: 'De-embed', kind: 'challenge', domain: 'measurement', tier: 3, status: 'prelab', provisional: false,
  next_action: 'submit_prelab', subskills: ['m.a'], size_est_min: 45, sim_runtime_s: 2, long_run_warning: false, brief: 'Do the thing',
  prelab: { markdown: '', items: [{ id: 'p1', kind: 'numeric', unit: 'mS', prompt: 'Estimate Y11' }, { id: 'p3', kind: 'explain', unit: null, prompt: 'Why?' }],
            locked: false, submitted: null, answers: null },
  proof_artifacts: ['a.py'], workspace_unlocked: false, workspace: null, rescopable: true, ...over,
})
const emptyProof = { stage: 'lab', artifacts: ['a.py'], submissions: [], failed: 0, remediation_offer: false, remediation_decision: null }
const labRoutes = () => {
  routes['GET /nodes/4/checker'] = () => null
  routes['GET /nodes/4/proof'] = () => emptyProof
  routes['GET /nodes/4/tutor'] = () => ({ messages: [], hints: {}, show_me_used: [] })
}

beforeEach(() => {
  calls.length = 0
  routes = {}
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${url.replace('/api', '')}`
    const body = init?.body ? JSON.parse(init.body as string) : undefined
    calls.push({ key, body })
    const h = routes[key]
    if (!h) return { ok: false, status: 404, json: async () => ({ detail: `no route ${key}` }) }
    return { ok: true, json: async () => h(body) }
  }))
})
afterEach(() => vi.unstubAllGlobals())

test('prelab: numeric units are fixed, answers are submitted as numbers, then the lab unlocks', async () => {
  routes['GET /nodes/4'] = () => node()
  routes['POST /nodes/4/prelab/submit'] = () => node({ status: 'lab' })
  render(<NodePage id={4} home={() => {}} />)
  expect(await screen.findByText('mS')).toBeInTheDocument()
  const btn = screen.getByText('Lock answers and unlock the lab')
  expect(btn).toBeDisabled()
  fireEvent.change(screen.getByLabelText('p1'), { target: { value: '12.5' } })
  fireEvent.change(screen.getByLabelText('p3'), { target: { value: 'because' } })
  expect(btn).toBeEnabled()
  routes['GET /nodes/4'] = () => node({ status: 'lab', workspace: 'C:/ws', workspace_unlocked: true })
  labRoutes()
  fireEvent.click(btn)
  await waitFor(() => expect(calls.find((c) => c.key === 'POST /nodes/4/prelab/submit')?.body).toEqual({ answers: { p1: 12.5, p3: 'because' } }))
  expect(await screen.findByText('Run checker')).toBeInTheDocument()
  expect(screen.getByText('Open folder')).toBeInTheDocument()
})

test('a locked prelab has no editable inputs', async () => {
  routes['GET /nodes/4'] = () => node({ status: 'oral', prelab: { ...node().prelab, locked: true } })
  routes['GET /nodes/4/oral'] = () => ({ note: 'stub' })
  render(<NodePage id={4} home={() => {}} />)
  await screen.findByText('Start oral (text) and go to debrief')
  expect(screen.queryByLabelText('p1')).toBeNull()
})

test('lab: checker shows name, pass/fail, measured, spec, unit; Show me warns before calling', async () => {
  routes['GET /nodes/4'] = () => node({ status: 'lab', workspace: 'C:/ws' })
  labRoutes()
  routes['GET /nodes/4/checker'] = () => ({ verdict: 'fail', passed: false, kind: 'lab', seconds: 1.5, metrics: {},
    tests: [{ name: 'Y11 at 60GHz', pass: false, measured: 3.2, spec: '<= 1', unit: 'mS' }] })
  routes['POST /nodes/4/show-me'] = () => ({ explanation: 'Because Y', subskills: ['m.a'], assist_reveal: true })
  render(<NodePage id={4} home={() => {}} />)
  const row = (await screen.findByText('Y11 at 60GHz')).closest('tr')!
  expect(row).toHaveTextContent('fail')
  expect(row).toHaveTextContent('3.2')
  expect(row).toHaveTextContent('<= 1')
  expect(row).toHaveTextContent('mS')
  fireEvent.click(screen.getByText('Show me'))
  expect(screen.getByRole('alertdialog')).toHaveTextContent('reduces credit')
  expect(calls.some((c) => c.key === 'POST /nodes/4/show-me')).toBe(false)
  fireEvent.click(screen.getByText('Show me anyway'))
  expect(await screen.findByText('Because Y')).toBeInTheDocument()
  expect(screen.getByText(/show-me used: m\.a/)).toBeInTheDocument()
})

test('rescope dialog warns that edit resets the workspace and links the backup', async () => {
  routes['GET /nodes/4'] = () => node({ status: 'lab', workspace: 'C:/ws' })
  labRoutes()
  routes['POST /nodes/4/rescope'] = () => ({ action: 'edit', changes: 'simplify', job_id: 'j1', node_dir: '0004-x', node_id: 4 })
  routes['GET /jobs/j1'] = () => ({ id: 'j1', status: 'done' })
  routes['GET /build/0004-x'] = () => ({ learner_workspace_backup: 'builds/0004-x/rescope-1/node/workspace' })
  render(<NodePage id={4} home={() => {}} />)
  fireEvent.click(await screen.findByText('Rescope…'))
  const dlg = screen.getByRole('dialog', { name: 'Rescope' })
  expect(dlg).toHaveTextContent('resets the workspace')
  const go = screen.getAllByText('Rescope').find((e) => e.tagName === 'BUTTON')!
  expect(go).toBeDisabled()
  fireEvent.click(screen.getByLabelText(/Too hard/))
  fireEvent.change(screen.getByPlaceholderText(/Say why/), { target: { value: 'stuck' } })
  fireEvent.click(go)
  const res = await screen.findByTestId('rescope-result')
  await waitFor(() => expect(res).toHaveTextContent('data/builds/0004-x/rescope-1/node/workspace'))
  expect(calls.find((c) => c.key === 'POST /nodes/4/rescope')?.body).toEqual({ reason_class: 'too_hard', reason: 'stuck' })
})

test('debrief shows calibration, pending badge and mastery deltas', async () => {
  routes['GET /nodes/4'] = () => node({ status: 'debrief', provisional: true })
  routes['GET /nodes/4/debrief'] = () => ({
    node_id: 4, title: 'x', status: 'debrief', provisional: true, proof: { passed: true, submissions: 2, failed_submissions: 1 },
    calibration: { within: 1, total: 2, numeric: [{ id: 'p1', subskill: 'm.a', unit: 'mS', predicted: 12, actual: 12.5, error: -0.5, rel_error: -0.04, tolerance: 1, within: true }] },
    grades: { status: 'pending', items: [], pending_items: [{ id: 'p3', subskill: 'm.a', prompt: 'Why?' }], model: null },
    oral: { status: 'not_graded', note: 'M7' }, misconceptions: { logged: [], resolved: [] },
    mastery: [{ domain: 'measurement', tier: 3, mastery_before: 10, mastery_after: 14.5, delta: 4.5, exam_unlocked: false }], next_action: 'finish_debrief',
  })
  render(<NodePage id={4} home={() => {}} />)
  expect((await screen.findAllByText('grades pending')).length).toBeGreaterThan(0)
  expect(screen.getByText('pending')).toBeInTheDocument()
  expect(screen.getByText('-4.0%')).toBeInTheDocument()
  expect(screen.getByText('(+4.5)')).toBeInTheDocument()
  expect(screen.getByText('Finish debrief')).toBeInTheDocument()
})

test('home: current node with one next-action button and mastery bars', async () => {
  routes['GET /nodes'] = () => [
    { id: 3, dir: 'd3', title: 'Old', status: 'done', provisional: false, next_action: 'plan_next', domain: 'measurement', tier: 3 },
    { id: 4, dir: 'd4', title: 'Current', status: 'lab', provisional: false, next_action: 'submit_proof', domain: 'measurement', tier: 3 },
  ]
  routes['GET /nodes/3/debrief'] = () => ({ mastery: [{ domain: 'measurement', tier: 3, mastery_before: 0, mastery_after: 42, delta: 42, exam_unlocked: false }] })
  const open = vi.fn()
  render(<Home open={open} />)
  fireEvent.click(await screen.findByText('Open the lab'))
  expect(open).toHaveBeenCalledWith(4)
  expect(await screen.findByLabelText('measurement mastery')).toHaveAttribute('value', '42')
})
