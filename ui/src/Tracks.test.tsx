import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from './App'
import type { TrackView, TracksView } from './api/client'
import { Tracks } from './Tracks'

const track = (id: string, name: string, over: Partial<TrackView> = {}): TrackView => ({
  id, name, domain: 'rfic', status: 'paused', active: false, created: '2026-11-10T00:00:00-06:00', updated: '2026-11-10T00:00:00-06:00',
  stage: 'done', nodes: 2, current_node: null, tiers: { matching: 3, noise: 2 }, mastery: {}, last_activity: '2026-11-11T08:00:00-06:00', ...over,
})

/** A tiny stateful backend for /api/tracks*. Records every call as "METHOD path body". */
function backend(initial: TracksView) {
  let v: TracksView = structuredClone(initial)
  const calls: string[] = []
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const m = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(init.body as string) : undefined
    const path = url.replace(/^\/api/, '')
    calls.push(`${m} ${path}${body ? ' ' + JSON.stringify(body) : ''}`)
    const reply = (j: unknown, ok = true) => ({ ok, json: async () => j })
    if (path === '/health') return reply({ status: 'ok' })
    if (path === '/nodes' || path === '/scores') return reply([])
    if (path === '/tracks' && m === 'GET') return reply(v)
    if (path === '/tracks' && m === 'POST') {
      const t = track(body.name.toLowerCase().replace(/\s+/g, '-'), body.name, { status: 'active', active: true, stage: 'interview', nodes: 0, tiers: {} })
      v = { ...v, active: t.id, tracks: [...v.tracks.map((x) => ({ ...x, active: false, status: x.active ? 'paused' : x.status })), t] }
      return reply(v)
    }
    if (path === '/tracks/trash' && m === 'DELETE') {
      if (body.confirm !== 'PURGE') return reply({ detail: 'confirm with PURGE' }, false)
      const purged = v.trash.map((x) => x.entry)
      v = { ...v, trash: [] }
      return reply({ purged })
    }
    let mm = /^\/tracks\/([^/]+)\/activate$/.exec(path)
    if (mm && m === 'POST') {
      v = { ...v, active: mm[1], tracks: v.tracks.map((x) => ({ ...x, active: x.id === mm![1], status: x.id === mm![1] ? 'active' : x.active ? 'paused' : x.status })) }
      return reply(v)
    }
    mm = /^\/tracks\/([^/]+)\/(archive|unarchive|reset)$/.exec(path)
    if (mm && m === 'POST') {
      if (mm[2] === 'archive') v = { ...v, tracks: v.tracks.map((x) => (x.id === mm![1] ? { ...x, status: 'archived' } : x)) }
      if (mm[2] === 'unarchive') v = { ...v, tracks: v.tracks.map((x) => (x.id === mm![1] ? { ...x, status: 'paused' } : x)) }
      if (mm[2] === 'reset') v = { ...v, trash: [{ entry: `${mm[1]}-1-reset`, track_id: mm[1], name: 'x', domain: 'rfic', kind: 'reset', trashed_at: '2026-11-11T09:00:00-06:00', events: 3, misconceptions: 1, restorable: false }, ...v.trash] }
      return reply(v)
    }
    mm = /^\/tracks\/([^/]+)$/.exec(path)
    if (mm && m === 'PATCH') {
      v = { ...v, tracks: v.tracks.map((x) => (x.id === mm![1] ? { ...x, name: body.name } : x)) }
      return reply(v)
    }
    if (mm && m === 'DELETE') {
      const gone = v.tracks.find((x) => x.id === mm![1])!
      if (body.confirm_name !== gone.name) return reply({ detail: 'type the track\'s exact name to confirm the deletion' }, false)
      v = { ...v, tracks: v.tracks.filter((x) => x.id !== gone.id), trash: [{ entry: `${gone.id}-1`, track_id: gone.id, name: gone.name, domain: 'rfic', kind: 'deleted', trashed_at: '2026-11-11T09:00:00-06:00', events: 5, misconceptions: 0, restorable: true }, ...v.trash] }
      return reply(v)
    }
    mm = /^\/tracks\/trash\/([^/]+)\/restore$/.exec(path)
    if (mm && m === 'POST') {
      const e = v.trash.find((x) => x.entry === mm![1])!
      v = { ...v, tracks: [...v.tracks, track(e.track_id, e.name)], trash: v.trash.filter((x) => x !== e) }
      return reply(v)
    }
    return reply({})
  })
  return { fetchMock, calls }
}

const two = (): TracksView => ({
  active: 'rfic-default', domains: ['rfic'], trash: [],
  tracks: [track('rfic-default', 'RFIC', { status: 'active', active: true, nodes: 4, current_node: { id: 7, title: 'L-match', status: 'lab' } }), track('rfic-mmwave', 'mmWave')],
})

beforeEach(() => { vi.stubGlobal('EventSource', class { addEventListener() {} close() {} }) })
afterEach(() => { vi.restoreAllMocks(); window.location.hash = '' })

test('lists tracks with domain, status, tiers, nodes and last activity', async () => {
  const { fetchMock } = backend(two())
  vi.stubGlobal('fetch', fetchMock)
  render(<Tracks onChange={() => {}} go={() => {}} />)
  const a = await screen.findByTestId('track-rfic-default')
  expect(within(a).getByText('RFIC')).toBeInTheDocument()
  expect(within(a).getByText('active')).toBeInTheDocument()
  expect(a).toHaveTextContent('matching T3 · noise T2 · 4 nodes · working on #7 L-match (lab)')
  expect(within(a).getByRole('button', { name: 'Enter' })).toBeDisabled()  // already active
  expect(within(screen.getByTestId('track-rfic-mmwave')).getByRole('button', { name: 'Enter' })).toBeEnabled()
})

test('enter switches the active track and returns to Home', async () => {
  const { fetchMock, calls } = backend(two())
  vi.stubGlobal('fetch', fetchMock)
  const go = vi.fn()
  render(<Tracks onChange={() => {}} go={go} />)
  fireEvent.click(within(await screen.findByTestId('track-rfic-mmwave')).getByRole('button', { name: 'Enter' }))
  await waitFor(() => expect(go).toHaveBeenCalledWith('#/'))
  expect(calls).toContain('POST /tracks/rfic-mmwave/activate')
})

test('create posts name and domain, then goes to Setup for onboarding', async () => {
  const { fetchMock, calls } = backend(two())
  vi.stubGlobal('fetch', fetchMock)
  const go = vi.fn()
  render(<Tracks onChange={() => {}} go={go} />)
  await screen.findByTestId('track-rfic-default')
  const create = screen.getByRole('button', { name: 'Create and start onboarding' })
  expect(create).toBeDisabled()  // a name is required
  fireEvent.change(screen.getByLabelText('Track name'), { target: { value: 'RFIC PLL' } })
  fireEvent.click(create)
  await waitFor(() => expect(go).toHaveBeenCalledWith('#/setup'))
  expect(calls).toContain('POST /tracks {"name":"RFIC PLL","domain":"rfic"}')
  expect(await screen.findByTestId('track-rfic-pll')).toHaveTextContent('onboarding: interview')
})

test('rename, archive and unarchive', async () => {
  const { fetchMock, calls } = backend(two())
  vi.stubGlobal('fetch', fetchMock)
  render(<Tracks onChange={() => {}} go={() => {}} />)
  const m = await screen.findByTestId('track-rfic-mmwave')
  fireEvent.click(within(m).getByRole('button', { name: 'Rename' }))
  fireEvent.change(screen.getByLabelText('New name'), { target: { value: 'mmWave 60 GHz' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save name' }))
  expect(await screen.findByText('mmWave 60 GHz')).toBeInTheDocument()
  fireEvent.click(within(screen.getByTestId('track-rfic-mmwave')).getByRole('button', { name: 'Archive' }))
  const archived = await screen.findByText('archived')
  expect(archived).toBeInTheDocument()
  const row = screen.getByTestId('track-rfic-mmwave')
  expect(within(row).getByRole('button', { name: 'Enter' })).toBeDisabled()  // archived tracks cannot be entered
  expect(within(row).getByRole('button', { name: 'Reset' })).toBeDisabled()
  fireEvent.click(within(row).getByRole('button', { name: 'Unarchive' }))
  await waitFor(() => expect(calls).toContain('POST /tracks/rfic-mmwave/unarchive'))
  expect(calls).toContain('PATCH /tracks/rfic-mmwave {"name":"mmWave 60 GHz"}')
})

test('delete needs the typed name; restore brings the track back; purge needs PURGE', async () => {
  const { fetchMock, calls } = backend(two())
  vi.stubGlobal('fetch', fetchMock)
  render(<Tracks onChange={() => {}} go={() => {}} />)
  fireEvent.click(within(await screen.findByTestId('track-rfic-mmwave')).getByRole('button', { name: 'Delete' }))
  const del = screen.getByRole('button', { name: 'Delete mmWave' })
  expect(del).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Type the track name'), { target: { value: 'mmwave' } })  // wrong case
  expect(del).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Type the track name'), { target: { value: 'mmWave' } })
  expect(del).toBeEnabled()
  fireEvent.click(del)
  await waitFor(() => expect(screen.queryByTestId('track-rfic-mmwave')).not.toBeInTheDocument())
  expect(calls).toContain('DELETE /tracks/rfic-mmwave {"confirm_name":"mmWave"}')
  const trash = within(screen.getByRole('list', { name: 'Trash list' }))
  expect(trash.getByText('deleted')).toBeInTheDocument()
  // purge is a separate, typed confirmation
  fireEvent.click(screen.getByRole('button', { name: 'Purge trash…' }))
  const purge = screen.getByRole('button', { name: 'Purge trash' })
  expect(purge).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Type PURGE'), { target: { value: 'purge' } })
  expect(purge).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(calls.filter((c) => c.startsWith('DELETE /tracks/trash'))).toEqual([])  // cancelling never purges
  // restore
  fireEvent.click(trash.getByRole('button', { name: 'Restore' }))
  expect(await screen.findByTestId('track-rfic-mmwave')).toBeInTheDocument()
  expect(calls).toContain('POST /tracks/trash/rfic-mmwave-1/restore')
})

test('purge empties the trash once PURGE is typed', async () => {
  const start = two()
  start.trash = [{ entry: 'old-1', track_id: 'old', name: 'Old', domain: 'rfic', kind: 'deleted', trashed_at: '2026-11-01T00:00:00-06:00', events: 1, misconceptions: 0, restorable: true }]
  const { fetchMock, calls } = backend(start)
  vi.stubGlobal('fetch', fetchMock)
  render(<Tracks onChange={() => {}} go={() => {}} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Purge trash…' }))
  fireEvent.change(screen.getByLabelText('Type PURGE'), { target: { value: 'PURGE' } })
  fireEvent.click(screen.getByRole('button', { name: 'Purge trash' }))
  expect(await screen.findByText('Nothing in the trash.')).toBeInTheDocument()
  expect(calls).toContain('DELETE /tracks/trash {"confirm":"PURGE"}')
})

test('reset keeps goals by default, goes to Setup, and a reset entry is not restorable', async () => {
  const { fetchMock, calls } = backend(two())
  vi.stubGlobal('fetch', fetchMock)
  const go = vi.fn()
  render(<Tracks onChange={() => {}} go={go} />)
  fireEvent.click(within(await screen.findByTestId('track-rfic-default')).getByRole('button', { name: 'Reset' }))
  expect(screen.getByLabelText(/Keep my goals/)).toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: 'Reset RFIC' }))
  await waitFor(() => expect(go).toHaveBeenCalledWith('#/setup'))
  expect(calls).toContain('POST /tracks/rfic-default/reset {"keep_goals":true}')
  expect(await screen.findByText('kept until purged')).toBeInTheDocument()
})

test('a server error is shown and nothing changes', async () => {
  const { fetchMock } = backend(two())
  vi.stubGlobal('fetch', fetchMock)
  render(<Tracks onChange={() => {}} go={() => {}} />)
  fireEvent.click(within(await screen.findByTestId('track-rfic-mmwave')).getByRole('button', { name: 'Delete' }))
  fireEvent.change(screen.getByLabelText('Type the track name'), { target: { value: 'mmWave' } })
  fetchMock.mockImplementationOnce(async () => ({ ok: false, json: async () => ({ detail: 'cannot delete while jobs are running' }) }))
  fireEvent.click(screen.getByRole('button', { name: 'Delete mmWave' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('cannot delete while jobs are running')
  expect(screen.getByTestId('track-rfic-mmwave')).toBeInTheDocument()
})

test('header switcher on every screen: switching activates, goes Home and remounts the page', async () => {
  const { fetchMock, calls } = backend(two())
  const nodes = { 'rfic-default': [{ id: 7, dir: '0007-l', title: 'L-match', kind: 'challenge', domain: 'matching', tier: 2, status: 'lab', provisional: false, next_action: 'submit_proof', outcome: null }], 'rfic-mmwave': [] }
  let active = 'rfic-default'
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/nodes') return { ok: true, json: async () => nodes[active as keyof typeof nodes] }
    if (url === '/api/scores') return { ok: true, json: async () => [] }
    const r = await fetchMock(url, init)
    const mm = /\/activate$/.exec(url)
    if (mm) active = url.split('/')[3]
    return r
  }))
  window.location.hash = '#/'
  render(<App />)
  const sel = await screen.findByLabelText('Active track')
  expect(sel).toHaveValue('rfic-default')
  expect((await screen.findAllByText(/L-match/)).length).toBeGreaterThan(0)  // Home shows the first track's node
  expect(within(sel).getAllByRole('option').map((o) => o.textContent)).toEqual(['RFIC', 'mmWave'])
  expect(screen.getByRole('link', { name: 'Manage tracks' })).toHaveAttribute('href', '#/tracks')
  fireEvent.change(sel, { target: { value: 'rfic-mmwave' } })
  await waitFor(() => expect(calls).toContain('POST /tracks/rfic-mmwave/activate'))
  await waitFor(() => expect(window.location.hash).toBe('#/'))
  expect(await screen.findByLabelText('Active track')).toHaveValue('rfic-mmwave')
  await waitFor(() => expect(screen.queryByText(/L-match/)).not.toBeInTheDocument())  // the page remounted on the other track's data
  expect(await screen.findByText('Plan the next node')).toBeInTheDocument()
})

test('archived tracks are hidden from the switcher', async () => {
  const v = two()
  v.tracks[1].status = 'archived'
  const { fetchMock } = backend(v)
  vi.stubGlobal('fetch', fetchMock)
  render(<App />)
  const sel = await screen.findByLabelText('Active track')
  expect(within(sel).getAllByRole('option').map((o) => o.textContent)).toEqual(['RFIC'])
})
