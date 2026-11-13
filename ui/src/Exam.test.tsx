import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { Home } from './Home'
import { NodePage } from './NodePage'

type Handler = (body: unknown) => unknown
let routes: Record<string, Handler>
const calls: string[] = []

const node = (over: Record<string, unknown> = {}) => ({
  id: 12, dir: '0012-x', title: 'Tier 3 exam: Matching', kind: 'tier_exam', domain: 'matching', tier: 3, status: 'oral', provisional: false,
  next_action: 'start_oral', subskills: ['m.a'], size_est_min: 20, sim_runtime_s: null, long_run_warning: false, brief: 'Six oral questions.',
  prelab: { markdown: '', items: [], locked: false, submitted: null, answers: null },
  proof_artifacts: [], workspace_unlocked: true, workspace: null, rescopable: false, ...over,
})
const debrief = (tierUp: boolean, status = 'debrief') => ({
  node_id: 12, title: 'Tier 3 exam: Matching', kind: 'tier_exam', status, provisional: false, proof: { passed: false, submissions: 0, failed_submissions: 0 },
  calibration: { numeric: [], within: 0, total: 0 }, grades: { status: 'none', items: [], pending_items: [], model: null }, checker: null,
  oral: {
    status: 'graded', note: '', passed: tierUp, mean: tierUp ? 2.5 : 1.8, items: [], remediation: null,
    exam: { domain: 'matching', tier_before: 3, tier_after: tierUp ? 4 : 3, tier_up: tierUp, pass_mean: 2.3, mastery_after: 0 },
  },
  misconceptions: { logged: [], resolved: [] }, mastery: [], next_action: 'finish_debrief',
})

beforeEach(() => {
  calls.length = 0
  routes = {}
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${url.replace('/api', '')}`
    calls.push(key)
    const h = routes[key]
    if (!h) return { ok: false, status: 404, json: async () => ({ detail: `no route ${key}` }) }
    return { ok: true, json: async () => h(undefined) }
  }))
})
afterEach(() => vi.unstubAllGlobals())

test('tier exam: the stepper has only oral/debrief/done, no rescope, exam wording on the oral start', async () => {
  routes['GET /nodes/12'] = () => node()
  routes['GET /nodes/12/oral'] = () => ({
    implemented: true, stage: 'oral', status: 'none', mode: 'text', tts: false, voice: { stt: false, tts: false, microphone: false }, recording: false,
    plan: null, progress: { asked: 0, of: 0 }, can_finish: false, turns: [], latency: [],
  })
  render(<NodePage id={12} home={() => {}} />)
  expect(await screen.findByText(/Tier exam: six oral questions/)).toBeInTheDocument()
  const stepper = screen.getByLabelText('Stage')
  expect(Array.from(stepper.querySelectorAll('li')).map((l) => l.textContent)).toEqual(['oral', 'debrief', 'done'])
  expect(screen.queryByText('Rescope…')).toBeNull()
})

test.each([
  [true, /Tier up! matching moves from T3 to T4/],
  [false, /Not this time: matching stays at T3/],
])('tier exam debrief shows the result (tier up: %s)', async (up, text) => {
  routes['GET /nodes/12'] = () => node({ status: 'debrief' })
  routes['GET /nodes/12/debrief'] = () => debrief(up)
  render(<NodePage id={12} home={() => {}} />)
  expect(await screen.findByTestId('exam-result')).toHaveTextContent(text)
  expect(screen.queryByText(/Prediction vs actual/)).toBeNull()
  expect(screen.getByText(/pass: mean ≥ 2.3 and no 0/)).toBeInTheDocument()
})

test('read-only view: a done node shows its debrief without actions; an in-progress node only its stage and fetches nothing else', async () => {
  routes['GET /nodes/12'] = () => node({ status: 'done' })
  routes['GET /nodes/12/debrief'] = () => debrief(true, 'done')
  const { unmount } = render(<NodePage id={12} home={() => {}} readOnly />)
  expect(await screen.findByTestId('exam-result')).toBeInTheDocument()
  expect(screen.getByText('read-only view')).toBeInTheDocument()
  expect(screen.queryByText('Plan the next node')).toBeNull()
  unmount()
  calls.length = 0
  routes['GET /nodes/12'] = () => node({ status: 'oral' })
  render(<NodePage id={12} home={() => {}} readOnly />)
  expect(await screen.findByText(/This node is at the oral stage/)).toBeInTheDocument()
  expect(calls.filter((c) => c !== 'GET /nodes/12')).toEqual([])
})

test('Home: a tier-exam option starts the exam and opens its node instead of building', async () => {
  routes['GET /nodes'] = () => []
  routes['GET /scores'] = () => []
  routes['POST /planner/options'] = () => ({
    model: 'm', ts: 't',
    options: [
      { title: 'Cascade noise', why_now: 'w', kind: 'challenge', domain: 'noise', subskills: ['noise.sources'], tier: 2, size_est_min: 30 },
      { title: 'Tier 2 exam: Noise', why_now: 'unlocked', kind: 'tier_exam', domain: 'noise', subskills: ['noise.sources'], tier: 2, size_est_min: 20 },
    ],
  })
  routes['POST /competency/noise/exam'] = () => ({ node_id: 15, dir: '0015-x', reused: false })
  const open = vi.fn()
  render(<Home open={open} />)
  fireEvent.click(await screen.findByText('Plan the next node'))
  fireEvent.click(await screen.findByText('Start the exam'))
  await waitFor(() => expect(open).toHaveBeenCalledWith(15))
  expect(calls).toContain('POST /competency/noise/exam')
  expect(calls).not.toContain('POST /build')
  expect(screen.getByText('Build this')).toBeInTheDocument()
})
