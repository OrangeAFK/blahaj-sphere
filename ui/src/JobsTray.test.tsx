import { act, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { Job } from './api/client'
import { JobsTray } from './JobsTray'

class FakeEventSource {
  static last: FakeEventSource
  listeners: Record<string, (e: MessageEvent<string>) => void> = {}
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  url: string
  constructor(url: string) {
    this.url = url
    FakeEventSource.last = this
  }
  addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners[name] = fn
  }
  close() {
    this.closed = true
  }
  emit(job: Job) {
    this.listeners['job'](new MessageEvent('job', { data: JSON.stringify(job) }))
  }
}

const job = (over: Partial<Job>): Job => ({
  id: 'j1', type: 'sim.ngspice', node_id: 'n1', status: 'running', progress: 0.25, log_tail: ['step 1'],
  error: null, external: false, created_ns: 1, started_ns: 1, finished_ns: null, updated_ns: 1, ...over,
})

beforeEach(() => vi.stubGlobal('EventSource', FakeEventSource))
afterEach(() => vi.unstubAllGlobals())

test('subscribes to the SSE stream and shows live progress and the last log line', () => {
  render(<JobsTray />)
  expect(FakeEventSource.last.url).toBe('/api/jobs/stream')
  expect(screen.getByText('No jobs yet.')).toBeInTheDocument()

  act(() => FakeEventSource.last.onopen?.())
  expect(screen.getByText('live')).toBeInTheDocument()

  act(() => FakeEventSource.last.emit(job({})))
  const row = screen.getByTestId('job-j1')
  expect(within(row).getByText('running')).toBeInTheDocument()
  expect(within(row).getByText('step 1')).toBeInTheDocument()
  expect(within(row).getByRole('progressbar')).toHaveAttribute('value', '0.25')
  expect(screen.getByText('Jobs (1 active)')).toBeInTheDocument()

  // The same job updates in place instead of duplicating.
  act(() => FakeEventSource.last.emit(job({ progress: 0.75, log_tail: ['step 1', 'step 2'], updated_ns: 2 })))
  expect(screen.getAllByTestId('job-j1')).toHaveLength(1)
  expect(within(screen.getByTestId('job-j1')).getByText('step 2')).toBeInTheDocument()
  expect(within(screen.getByTestId('job-j1')).getByRole('progressbar')).toHaveAttribute('value', '0.75')
})

test('finished jobs leave the active count; failures show the error', () => {
  render(<JobsTray />)
  act(() => FakeEventSource.last.emit(job({})))
  act(() => FakeEventSource.last.emit(job({ status: 'done', progress: 1, finished_ns: 3, updated_ns: 3 })))
  expect(screen.getByText('Jobs')).toBeInTheDocument() // no "(n active)"
  expect(screen.queryByRole('progressbar')).toBeNull()

  act(() => FakeEventSource.last.emit(job({ id: 'j2', status: 'failed', error: 'exit code 1', created_ns: 5, updated_ns: 5 })))
  const row = screen.getByTestId('job-j2')
  expect(within(row).getByText('failed')).toBeInTheDocument()
  expect(within(row).getByText('exit code 1')).toBeInTheDocument()
})

test('shows a reconnecting state on error and closes the stream on unmount', () => {
  const { unmount } = render(<JobsTray />)
  const es = FakeEventSource.last
  act(() => es.onerror?.())
  expect(screen.getByText('reconnecting…')).toBeInTheDocument()
  unmount()
  expect(es.closed).toBe(true)
})
