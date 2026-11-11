import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { NodePage } from './NodePage'

type Handler = (body: unknown) => unknown
let routes: Record<string, Handler>
const calls: { key: string; body: unknown }[] = []

const node = (over: Record<string, unknown> = {}) => ({
  id: 4, dir: '0004-x', title: 'De-embed', kind: 'challenge', domain: 'measurement', tier: 3, status: 'oral', provisional: false, next_action: 'start_oral',
  subskills: ['m.a'], size_est_min: 45, sim_runtime_s: 2, long_run_warning: false, brief: 'Do the thing',
  prelab: { markdown: '', items: [], locked: true, submitted: null, answers: null },
  proof_artifacts: [], workspace_unlocked: true, workspace: 'C:/ws', rescopable: false, ...over,
})
const turn = (n: number, role: 'examiner' | 'learner', text: string, extra: Record<string, unknown> = {}) => ({ n, role, text, kind: role === 'examiner' ? 'question' : 'answer', source: 'llm', ...extra })
const view = (over: Record<string, unknown> = {}) => ({
  implemented: true, stage: 'oral', status: 'none', mode: 'text', tts: false, voice: { stt: true, tts: true, microphone: true }, recording: false,
  plan: null, progress: { asked: 0, of: 3 }, can_finish: false, turns: [], latency: [], ...over,
})
const plan = { n_core: 2, n_interleaved: 1, reason: 'all numeric prelab answers were within tolerance' }

beforeEach(() => {
  calls.length = 0
  routes = { 'GET /nodes/4': () => node() }
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${url.replace('/api', '')}`
    calls.push({ key, body: init?.body ? JSON.parse(init.body as string) : undefined })
    const h = routes[key]
    if (!h) return { ok: false, status: 404, json: async () => ({ detail: `no route ${key}` }) }
    return { ok: true, json: async () => h(init?.body ? JSON.parse(init.body as string) : undefined) }
  }))
})
afterEach(() => vi.unstubAllGlobals())

test('voice is offered only when the speech model and a microphone exist; text always works', async () => {
  routes['GET /nodes/4/oral'] = () => view({ voice: { stt: false, tts: false, microphone: true } })
  render(<NodePage id={4} home={() => {}} />)
  expect(await screen.findByText('Start oral exam')).toBeInTheDocument()
  expect(screen.getByLabelText(/Voice \(push-to-talk\)/)).toBeDisabled()
  expect(screen.getByText(/speech model not downloaded/)).toBeInTheDocument()
  expect(screen.getByLabelText(/Read the questions aloud/)).toBeDisabled()
  expect(screen.getByLabelText('Text')).toBeChecked()
})

test('text exam: start, answer, last question, finish and grade', async () => {
  routes['GET /nodes/4/oral'] = () => view({ voice: { stt: false, tts: false, microphone: false } })
  routes['POST /nodes/4/oral/start'] = () => view({ status: 'active', plan, progress: { asked: 1, of: 3 }, turns: [turn(1, 'examiner', 'Why does the frame error grow?')] })
  render(<NodePage id={4} home={() => {}} />)
  fireEvent.click(await screen.findByText('Start oral exam'))
  expect(await screen.findByText('Why does the frame error grow?')).toBeInTheDocument()
  expect(calls.find((c) => c.key === 'POST /nodes/4/oral/start')?.body).toEqual({ mode: 'text', tts: false })
  expect(screen.getByText(/2 core question\(s\) \+ 1 interleaved/)).toBeInTheDocument()
  expect(screen.queryByLabelText('Push to talk')).toBeNull() // text mode: no microphone button
  const done = view({ status: 'complete', can_finish: true, plan, progress: { asked: 3, of: 3 },
                      turns: [turn(1, 'examiner', 'Why does the frame error grow?'), turn(2, 'learner', 'the feed inductance'), { ...turn(3, 'examiner', 'Thanks.'), kind: 'done' }] })
  routes['POST /nodes/4/oral/answer'] = () => ({ learner: done.turns[1], examiner: done.turns[2], done: true, view: done })
  fireEvent.change(screen.getByLabelText('Type your answer'), { target: { value: 'the feed inductance' } })
  fireEvent.click(screen.getByText('Send (Ctrl+Enter)'))
  expect(await screen.findByText('Finish and grade')).toBeInTheDocument()
  expect(calls.find((c) => c.key === 'POST /nodes/4/oral/answer')?.body).toEqual({ text: 'the feed inductance' })
  routes['POST /nodes/4/oral/finish'] = () => ({})
  routes['GET /nodes/4'] = () => node({ status: 'debrief' })
  routes['GET /nodes/4/debrief'] = () => ({
    node_id: 4, title: 'x', status: 'debrief', provisional: false, proof: { passed: true, submissions: 1, failed_submissions: 0 }, checker: null,
    calibration: { within: 0, total: 0, numeric: [] }, grades: { status: 'graded', items: [], pending_items: [], model: 'm' },
    oral: { status: 'graded', note: '', passed: false, mean: 1.5, items: [{ id: 'o1', subskill: 'm.a', interleaved: false, question: 'Why?', score: 0, quote: 'no idea', rationale: 'no link' }],
            remediation: { status: 'building', title: 'Probe the frame' } },
    misconceptions: { logged: [], resolved: [] }, mastery: [], next_action: 'finish_debrief',
  })
  fireEvent.click(screen.getByText('Finish and grade'))
  const res = await screen.findByTestId('oral-result')
  expect(res).toHaveTextContent('not passed')
  expect(screen.getByTestId('oral-o1')).toHaveTextContent('“no idea”')
  expect(screen.getByTestId('oral-remediation')).toHaveTextContent('Probe the frame')
})

test('push-to-talk: the button sends start then stop; the transcript, latency and next question appear', async () => {
  routes['GET /nodes/4/oral'] = () => view({ status: 'active', mode: 'voice', tts: true, plan, progress: { asked: 1, of: 3 }, turns: [turn(1, 'examiner', 'First question?', { audio_url: '/api/nodes/4/oral/audio/1' })] })
  const after = view({ status: 'active', mode: 'voice', tts: true, plan, progress: { asked: 2, of: 3 },
                       turns: [turn(1, 'examiner', 'First question?'), turn(2, 'learner', 'the Smith chart', { source: 'voice' }), turn(3, 'examiner', 'Second question?', { audio_url: '/api/nodes/4/oral/audio/3' })],
                       latency: [{ n: 3, record_s: 2.1, stt_s: 1.2, llm_s: 2.7, tts_s: 0.4, total_s: 4.5 }] })
  routes['POST /nodes/4/oral/record/start'] = () => ({ recording: true })
  routes['POST /nodes/4/oral/record/stop'] = () => ({ empty: false, transcript: 'the Smith chart', learner: after.turns[1], examiner: after.turns[2], done: false, view: after })
  render(<NodePage id={4} home={() => {}} />)
  const btn = await screen.findByLabelText('Push to talk')
  fireEvent.pointerDown(btn)
  expect(await screen.findByText(/Recording… release to send/)).toBeInTheDocument()
  fireEvent.pointerUp(btn)
  expect(await screen.findByText('Second question?')).toBeInTheDocument()
  expect(screen.getByText(/heard: “the Smith chart”/)).toBeInTheDocument()
  expect(screen.getByText(/stt 1.2s · llm 2.7s · tts 0.4s · total 4.5s/)).toBeInTheDocument()
  const order = calls.map((c) => c.key).filter((k) => k.includes('/record/'))
  expect(order).toEqual(['POST /nodes/4/oral/record/start', 'POST /nodes/4/oral/record/stop'])
  expect(screen.getByLabelText('Type your answer')).toBeInTheDocument() // the text fallback stays available in voice mode
})

test('holding Space records, a repeat keydown is ignored, and typing in the box never triggers it', async () => {
  routes['GET /nodes/4/oral'] = () => view({ status: 'active', mode: 'voice', plan, progress: { asked: 1, of: 3 }, turns: [turn(1, 'examiner', 'First question?')] })
  routes['POST /nodes/4/oral/record/start'] = () => ({ recording: true })
  routes['POST /nodes/4/oral/record/stop'] = () => ({ empty: true, heard_s: 0.1, view: view({ status: 'active', mode: 'voice', turns: [turn(1, 'examiner', 'First question?')] }) })
  render(<NodePage id={4} home={() => {}} />)
  await screen.findByLabelText('Push to talk')
  const box = screen.getByLabelText('Type your answer')
  fireEvent.keyDown(box, { code: 'Space' })
  fireEvent.keyUp(box, { code: 'Space' })
  expect(calls.some((c) => c.key.includes('/record/'))).toBe(false)
  fireEvent.keyDown(window, { code: 'Space' })
  fireEvent.keyDown(window, { code: 'Space', repeat: true })
  fireEvent.keyUp(window, { code: 'Space' })
  expect(await screen.findByText(/did not hear anything/)).toBeInTheDocument()
  expect(calls.filter((c) => c.key === 'POST /nodes/4/oral/record/start')).toHaveLength(1)
  await waitFor(() => expect(calls.filter((c) => c.key === 'POST /nodes/4/oral/record/stop')).toHaveLength(1))
})
