import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { CompetencyView, DomainCompetency } from './api/client'
import { Competency } from './Competency'

const tiers = (cur: number) => [1, 2, 3, 4, 5].map((t) => ({ tier: t, descriptor: `Descriptor ${t}`, state: t < cur ? 'passed' : t === cur ? 'current' : 'locked' }))
const dom = (over: Partial<DomainCompetency> = {}): DomainCompetency => ({
  domain: 'matching', name: 'Impedance matching', scope: 'Smith chart', tier: 3, mastery: 40, n_window: 8, tiers: tiers(3),
  unlock: { mastery_needed: 65, subskills_needed: 3, subskills_evidenced: 2, mastery_ok: false, subskills_ok: false },
  subskills: [{ name: 'matching.l_network', evidenced: true }, { name: 'smith_chart.navigation', evidenced: true }, { name: 'matching.bandwidth', evidenced: false }],
  exam: { state: 'locked', missing: ['mastery 40% of 65% needed', '2 of 3 subskills evidenced at T3'], node_id: null, pass_mean: 2.3, questions: 6, attempts: [], tier_ups: [] },
  ...over,
})
let view: CompetencyView
let calls: string[]

beforeEach(() => {
  calls = []
  view = {
    track: 'rfic-default',
    domains: [
      dom(),
      dom({
        domain: 'noise', name: 'Noise', tier: 2, tiers: tiers(2), mastery: 70,
        unlock: { mastery_needed: 65, subskills_needed: 3, subskills_evidenced: 3, mastery_ok: true, subskills_ok: true },
        exam: { state: 'available', missing: [], node_id: null, pass_mean: 2.3, questions: 6, tier_ups: [],
                attempts: [{ node_id: 9, dir: '0009-x', tier: 2, status: 'done', passed: false, mean: 1.8, tier_up: false }] },
      }),
    ],
  }
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push(`${init?.method ?? 'GET'} ${url}`)
    if (url === '/api/competency') return { ok: true, json: async () => view }
    if (url === '/api/competency/noise/exam') return { ok: true, json: async () => ({ node_id: 12, dir: '0012-x', reused: false }) }
    return { ok: false, status: 404, json: async () => ({ detail: `unexpected ${url}` }) }
  }))
})
afterEach(() => vi.unstubAllGlobals())

test('shows tier descriptors, mastery, subskills and why a locked exam is locked', async () => {
  render(<Competency open={() => {}} />)
  expect(await screen.findByText('Impedance matching')).toBeInTheDocument()
  expect(screen.getByTestId('tier-matching-3').dataset.state).toBe('current')
  expect(screen.getByTestId('tier-matching-2').dataset.state).toBe('passed')
  expect(screen.getByTestId('tier-matching-4').dataset.state).toBe('locked')
  expect(screen.getAllByText(/Descriptor 3/).length).toBeGreaterThan(0)
  expect(screen.getByLabelText('Impedance matching mastery')).toHaveValue(40)
  expect(within(screen.getByTestId('domain-matching')).getByText('matching.l_network ✓')).toBeInTheDocument()
  expect(within(screen.getByTestId('domain-matching')).getByText('matching.bandwidth')).toBeInTheDocument()
  expect(screen.getByTestId('exam-missing-matching')).toHaveTextContent('mastery 40% of 65% needed; 2 of 3 subskills evidenced at T3')
})

test('an unlocked exam can be started and opens its node; past attempts are listed', async () => {
  const open = vi.fn()
  render(<Competency open={open} />)
  const btn = await screen.findByText('Take the T2 → T3 exam')
  expect(screen.getByText(/not passed, mean 1.80/)).toBeInTheDocument()
  fireEvent.click(btn)
  await waitFor(() => expect(open).toHaveBeenCalledWith(12))
  expect(calls).toContain('POST /api/competency/noise/exam')
})
