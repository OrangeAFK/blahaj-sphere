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
  routes['POST /nodes/4/rescope'] = () => ({ action: 'edit', changes: 'simplify', job_id: 'j1', node_dir: '0004-x', node_id: 4, backup: 'builds/0004-x/rescope-1/node/workspace' })
  routes['POST /nodes/4/open-folder?target=backup'] = () => ({ path: 'x' })
  routes['GET /jobs/j1'] = () => ({ id: 'j1', status: 'done' })
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
  fireEvent.click(screen.getByText('Open backup folder'))
  await waitFor(() => expect(calls.some((c) => c.key === 'POST /nodes/4/open-folder?target=backup')).toBe(true))
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
  routes['GET /scores'] = () => [{ domain: 'measurement', name: 'Measurement', tier: 3, mastery: 42, n_window: 3, unlocked: false, subskills_evidenced: [] }]
  const open = vi.fn()
  render(<Home open={open} />)
  fireEvent.click(await screen.findByText('Open the lab'))
  expect(open).toHaveBeenCalledWith(4)
  expect(await screen.findByLabelText('Measurement mastery')).toHaveAttribute('value', '42')
})

test('lab: Run simulation streams the job log and lists the files the run wrote', async () => {
  routes['GET /nodes/4'] = () => node({ status: 'lab', workspace: 'C:/ws' })
  labRoutes()
  routes['GET /nodes/4/run'] = () => null
  routes['POST /nodes/4/run'] = () => ({ job_id: 'r1' })
  routes['GET /jobs/r1'] = () => ({ id: 'r1', status: 'done', log_tail: ['$ ngspice -b bench.sp', 'done'] })
  render(<NodePage id={4} home={() => {}} />)
  await screen.findByText('Run simulation')
  routes['GET /nodes/4/run'] = () => ({ ok: true, ts: 't', seconds: 2.5, steps: [{ cmd: 'ngspice -b bench.sp', exit_code: 0, seconds: 2, timed_out: false, stderr_tail: '' }],
                                        files_changed: [{ path: 'raw_p1.txt', size: 1234 }] })
  fireEvent.click(screen.getByText('Run simulation'))
  expect(await screen.findByText('raw_p1.txt')).toBeInTheDocument()
  expect(screen.getByTestId('run-log')).toHaveTextContent('$ ngspice -b bench.sp')
  expect(screen.getByTestId('run-result')).toHaveTextContent('simulation ok')
})

test('debrief shows the persisted final checker result', async () => {
  routes['GET /nodes/4'] = () => node({ status: 'debrief' })
  routes['GET /nodes/4/debrief'] = () => ({
    node_id: 4, title: 'x', status: 'debrief', provisional: false, proof: { passed: true, submissions: 1, failed_submissions: 0 },
    checker: { verdict: 'pass', kind: 'proof', seconds: 1, metrics: {}, tests: [{ name: 'S21 error', pass: true, measured: 0.17, spec: '<= 0.5', unit: 'dB' }] },
    calibration: { within: 0, total: 0, numeric: [] }, grades: { status: 'graded', items: [], pending_items: [], model: 'm' },
    oral: { status: 'not_graded', note: 'M7' }, misconceptions: { logged: [], resolved: [] }, mastery: [], next_action: 'finish_debrief',
  })
  render(<NodePage id={4} home={() => {}} />)
  const row = (await screen.findByText('S21 error')).closest('tr')!
  expect(row).toHaveTextContent('0.17')
  expect(screen.getByTestId('debrief-checker')).toHaveTextContent('proof run')
})
