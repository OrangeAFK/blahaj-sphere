import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { Preflight } from './Preflight'

afterEach(() => vi.unstubAllGlobals())

const checks = [
  { id: 'docker', label: 'Docker running', status: 'ok', detail: 'Docker 29.6.2', fix: '', fix_action: null },
  { id: 'image', label: 'Toolchain image rfic:1', status: 'fail', detail: 'not built yet', fix: 'Build it.', fix_action: 'build_image' },
  { id: 'microphone', label: 'Microphone available', status: 'warn', detail: 'no capture device found', fix: 'Plug one in.', fix_action: null },
]

test('runs checks, shows fix hints only for non-ok, and the fix button starts a job', async () => {
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => ({
    ok: true,
    json: async () => (init?.method === 'POST' ? { id: 'job1', type: 'image_build' } : checks),
  }))
  vi.stubGlobal('fetch', fetchMock)
  render(<Preflight />)

  fireEvent.click(screen.getByRole('button', { name: 'Run checks' }))
  expect(await screen.findByText('Docker 29.6.2')).toBeInTheDocument()
  expect(screen.getByText('Build it.')).toBeInTheDocument()
  expect(screen.getByText('Plug one in.')).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: 'Fix now' })).toHaveLength(1)

  fireEvent.click(screen.getByRole('button', { name: 'Fix now' }))
  expect(fetchMock).toHaveBeenCalledWith('/api/preflight/fix/build_image', expect.objectContaining({ method: 'POST' }))
})

test('surfaces backend errors', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({ detail: 'boom' }) })))
  render(<Preflight />)
  fireEvent.click(screen.getByRole('button', { name: 'Run checks' }))
  expect(await screen.findByText(/boom/)).toBeInTheDocument()
})
