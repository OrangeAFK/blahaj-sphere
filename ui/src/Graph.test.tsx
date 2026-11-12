import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { GraphNode, GraphView } from './api/client'
import { Graph } from './Graph'

const node = (id: number, over: Partial<GraphNode> = {}): GraphNode => ({
  id, dir: `000${id}`, title: `Node ${id}`, kind: 'challenge', domain: 'rfic', tier: 3, status: 'done', provisional: false,
  outcome: 'success', size_est_min: 30, subskills: ['em.layout'], parents: [], ...over,
})

const TRACK_A: GraphView = {
  track: 'rfic-default',
  nodes: [
    node(1, { status: 'done' }),
    node(2, { status: 'oral', parents: [1], outcome: 'in_progress', provisional: true }),
    node(3, { status: 'abandoned', parents: [1], outcome: 'abandoned' }),
    node(4, { kind: 'remediation', status: 'prelab', parents: [2], domain: null, tier: null, title: 'Fix the match' }),
  ],
  edges: [{ source: 1, target: 2 }, { source: 1, target: 3 }, { source: 2, target: 4 }],
}
const TRACK_B: GraphView = { track: 'other', nodes: [node(7, { title: 'Other track node', parents: [] })], edges: [] }

let graphs: Record<string, GraphView>
let current: string
let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  graphs = { 'rfic-default': TRACK_A, other: TRACK_B }
  current = 'rfic-default'
  // React Flow measures the canvas with ResizeObserver and offsetWidth/offsetHeight, and culls edges outside that viewport.
  // jsdom has neither layout nor ResizeObserver, so give it a fixed 1000x800 canvas.
  vi.stubGlobal('ResizeObserver', class {
    cb: (entries: unknown[]) => void
    constructor(cb: (entries: unknown[]) => void) { this.cb = cb }
    observe(el: Element) {
      // report each element once, as a real observer does on attach (react-flow needs this to measure handles and the canvas)
      const size = { inlineSize: 1000, blockSize: 800 }
      this.cb([{ target: el, contentRect: { width: 1000, height: 800 }, borderBoxSize: [size], contentBoxSize: [size] }])
    }
    unobserve() {}
    disconnect() {}
  })
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => 1000 })
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => 800 })
  fetchMock = vi.fn(async (url: string) => {
    if (url === '/api/graph') return { ok: true, json: async () => graphs[current] }
    return { ok: false, json: async () => ({ detail: `unexpected ${url}` }) }
  })
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => {
  cleanup() // unmount react-flow while its ResizeObserver stub still exists
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  Reflect.deleteProperty(HTMLElement.prototype, 'offsetWidth')
  Reflect.deleteProperty(HTMLElement.prototype, 'offsetHeight')
})

const cardOf = (title: string) => screen.getByText(title).closest('.react-flow__node') as HTMLElement

test('draws one card per node of the track, with status and domain/tier badge', async () => {
  const { container } = render(<Graph track="rfic-default" />)
  expect(await screen.findByText('Node 1')).toBeInTheDocument()
  expect(container.querySelectorAll('.react-flow__node')).toHaveLength(4)
  expect(within(cardOf('Node 1')).getByText('rfic · T3')).toBeInTheDocument()
  expect(within(cardOf('Node 1')).getByText('done')).toBeInTheDocument()
  // a node with no domain/tier has no badge text
  expect(within(cardOf('Fix the match')).queryByText(/T\d/)).toBeNull()
  expect(fetchMock).toHaveBeenCalledWith('/api/graph', expect.anything())
})

test('colour follows status: done green, in progress sky, abandoned gray', async () => {
  render(<Graph track="rfic-default" />)
  await screen.findByText('Node 1')
  expect(cardOf('Node 1').firstElementChild!.className).toContain('border-emerald-500')
  expect(cardOf('Node 2').firstElementChild!.className).toContain('border-sky-500')
  expect(cardOf('Node 3').firstElementChild!.className).toContain('border-slate-400')
})

test('clicking a node opens a read-only detail panel for it; clicking the pane clears it', async () => {
  const { container } = render(<Graph track="rfic-default" />)
  await screen.findByText('Node 1')
  expect(screen.getByText('Click a node to see its details.')).toBeInTheDocument()

  fireEvent.click(cardOf('Node 2'))
  const panel = screen.getByText('Status').closest('dl')!
  expect(within(panel).getByText('oral')).toBeInTheDocument()
  expect(within(panel).getByText('in progress')).toBeInTheDocument()
  expect(within(panel).getByText('#1')).toBeInTheDocument()
  expect(within(panel).getByText('~30 min')).toBeInTheDocument()
  expect(screen.getByText('provisional')).toBeInTheDocument()
  // read-only: the panel offers no action
  expect(panel.closest('section')!.querySelector('button')).toBeNull()

  fireEvent.click(container.querySelector('.react-flow__pane')!)
  await waitFor(() => expect(screen.getByText('Click a node to see its details.')).toBeInTheDocument())
})

test('re-loads the graph when the active track changes', async () => {
  const { rerender } = render(<Graph track="rfic-default" />)
  await screen.findByText('Node 1')
  current = 'other'
  rerender(<Graph track="other" />)
  expect(await screen.findByText('Other track node')).toBeInTheDocument()
  expect(screen.queryByText('Node 1')).toBeNull()
  expect(fetchMock.mock.calls.filter(([u]) => u === '/api/graph')).toHaveLength(2)
})

test('an empty track says so instead of drawing nothing', async () => {
  graphs['rfic-default'] = { track: 'rfic-default', nodes: [], edges: [] }
  await act(async () => { render(<Graph track="rfic-default" />) })
  expect(await screen.findByText(/No nodes in this track yet/)).toBeInTheDocument()
})
