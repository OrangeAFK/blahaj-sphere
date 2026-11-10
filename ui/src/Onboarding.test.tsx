import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { Onboarding } from './Onboarding'

afterEach(() => vi.unstubAllGlobals())

type Handler = (body: unknown) => unknown
/** fetch mock keyed by "METHOD /path"; records calls. */
function mockApi(routes: Record<string, Handler>) {
  const calls: string[] = []
  const f = vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${url.replace('/api', '')}`
    calls.push(key)
    const h = routes[key]
    if (!h) return { ok: false, status: 404, json: async () => ({ detail: `no mock for ${key}` }) }
    const out = h(init?.body ? JSON.parse(init.body as string) : undefined)
    if (out instanceof Error) return { ok: false, status: 502, json: async () => ({ detail: out.message }) }
    return { ok: true, json: async () => out }
  })
  vi.stubGlobal('fetch', f)
  return calls
}

const state = (stage: string) => ({ stage, goals: stage !== 'interview', framework_frozen: ['diagnostic', 'done'].includes(stage), diagnostic_done: stage === 'done' })
const iv = (over = {}) => ({ done: false, questions_asked: 0, max_questions: 10, last_message: null, goals: null, ...over })
const tiers = { 1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five' }
const fwDomain = (id: string) => ({ id, name: `Name ${id}`, scope: 's', subskills: [], tiers })
const framework = { domain_pack: 'rfic', version: 1, domains: ['a', 'b', 'c', 'd', 'e'].map(fwDomain) }

test('interview: start, answer, and finish refreshes the stage', async () => {
  let stage = 'interview'
  let n = 0
  mockApi({
    'GET /onboarding/state': () => state(stage),
    'GET /onboarding/interview': () => iv(),
    'POST /onboarding/interview/turn': (b) => {
      n += 1
      if ((b as { answer?: string }).answer) { stage = 'framework'; return iv({ done: true, questions_asked: 2, goals: '# Goals' }) }
      return iv({ questions_asked: 1, last_message: 'What do you want to build?' })
    },
    'GET /onboarding/framework': () => ({ frozen: false, framework: null }),
  })
  render(<Onboarding />)
  fireEvent.click(await screen.findByRole('button', { name: 'Start interview' }))
  expect(await screen.findByText('What do you want to build?')).toBeInTheDocument()
  const send = screen.getByRole('button', { name: 'Send' })
  expect(send).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Your answer'), { target: { value: 'an LNA' } })
  fireEvent.click(send)
  expect(await screen.findByRole('button', { name: 'Draft framework' })).toBeInTheDocument()
  expect(n).toBe(2)
})

test('framework: draft, edit a tier descriptor, approve sends the edited framework', async () => {
  let approved: unknown
  mockApi({
    'GET /onboarding/state': () => state('framework'),
    'GET /onboarding/interview': () => iv({ done: true, goals: '# Goals' }),
    'GET /onboarding/framework': () => ({ frozen: false, framework: null }),
    'POST /onboarding/framework/draft': () => ({ frozen: false, framework: framework }),
    'POST /onboarding/framework/approve': (b) => { approved = b; return { frozen: true, framework } },
  })
  render(<Onboarding />)
  fireEvent.click(await screen.findByRole('button', { name: 'Draft framework' }))
  const t1 = await screen.findByLabelText('a T1')
  fireEvent.change(t1, { target: { value: 'My edit.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Approve and freeze' }))
  await waitFor(() => expect(approved).toBeTruthy())
  const sent = (approved as { framework: { domains: { tiers: Record<string, string> }[] } }).framework
  expect(sent.domains[0].tiers['1']).toBe('My edit.')
  expect(sent.domains).toHaveLength(5)
})

test('diagnostic: question, answer, shows the final tiers; grading errors are surfaced', async () => {
  let calls = 0
  const dom = (done: boolean, probes: { tier: number; subskill: string; score: number }[]) => ({ id: 'a', name: 'Name a', done, probes })
  mockApi({
    'GET /onboarding/state': () => state('diagnostic'),
    'GET /onboarding/interview': () => iv({ done: true, goals: '# Goals' }),
    'GET /onboarding/diagnostic': () => ({ done: false, pending: null, tiers: null, domains: [dom(false, [])] }),
    'POST /onboarding/diagnostic/next': () => ({
      done: false, tiers: null, domains: [dom(false, [])],
      pending: { domain: 'a', tier: 1, subskill: 'a.x', question: 'Why does Q matter?' },
    }),
    'POST /onboarding/diagnostic/answer': () => {
      calls += 1
      if (calls === 1) return new Error('grading unavailable, try again later')
      return { score: 3, view: { done: true, pending: null, tiers: { a: 2 }, domains: [dom(true, [{ tier: 1, subskill: 'a.x', score: 3 }])] } }
    },
  })
  render(<Onboarding />)
  fireEvent.click(await screen.findByRole('button', { name: 'Start diagnostic' }))
  expect(await screen.findByText('Why does Q matter?')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Your answer'), { target: { value: 'because bandwidth' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit answer' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('grading unavailable')
  expect(screen.getByText('Why does Q matter?')).toBeInTheDocument() // question stays open for a retry
  fireEvent.click(screen.getByRole('button', { name: 'Submit answer' }))
  expect(await screen.findByText(/starts at/)).toHaveTextContent('T2')
})
