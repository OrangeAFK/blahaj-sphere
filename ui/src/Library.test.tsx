import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { Book, Job, LibraryView } from './api/client'
import { Library } from './Library'

class FakeEventSource {
  static last: FakeEventSource
  listeners: Record<string, (e: MessageEvent<string>) => void> = {}
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  url: string
  constructor(url: string) {
    this.url = url
    FakeEventSource.last = this
  }
  addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners[name] = fn
  }
  close() {}
  emit(job: Job) {
    this.listeners['job'](new MessageEvent('job', { data: JSON.stringify(job) }))
  }
}

const book = (over: Partial<Book> = {}): Book => ({
  id: 'razavi-rf', title: 'Razavi RF', filename: 'razavi.pdf', size_bytes: 5 * 1024 * 1024, sha256: 'x', status: 'ready', pages: 300, chunks: 612,
  ocr_pages: 0, empty_pages: 0, warnings: [], error: null, created: '2026-11-13T10:00:00-06:00', updated: '2026-11-13T10:01:00-06:00', ...over,
})
const job = (over: Partial<Job>): Job => ({
  id: 'j1', type: 'library_ingest', node_id: 'lib:razavi-rf', status: 'running', progress: 0.5, log_tail: ['612 chunks; embedding'],
  error: null, external: false, created_ns: 1, started_ns: 1, finished_ns: null, updated_ns: 1, ...over,
})

function backend(initial: LibraryView) {
  let v = structuredClone(initial)
  const calls: string[] = []
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const m = init?.method ?? 'GET'
    const path = url.replace(/^\/api/, '')
    calls.push(`${m} ${path}`)
    if (path === '/library') return { ok: true, json: async () => v }
    if (path.startsWith('/library/books?') && m === 'POST') {
      expect((init?.headers as Record<string, string>)['Content-Type']).toBe('application/pdf')
      if (new URLSearchParams(path.split('?')[1]).get('filename') === 'dup.pdf') return { ok: false, json: async () => ({ detail: 'this PDF is already in the library' }) }
      v = { ...v, books: [...v.books, book({ id: 'new-book', title: 'New Book', status: 'queued', pages: 0, chunks: 0 })] }
      return { ok: true, json: async () => ({ book: v.books[v.books.length - 1], job_id: 'j2' }) }
    }
    const mm = /^\/library\/books\/([^/]+)$/.exec(path)
    if (mm && m === 'DELETE') {
      v = { ...v, books: v.books.filter((b) => b.id !== mm[1]) }
      return { ok: true, json: async () => ({}) }
    }
    throw new Error(`unexpected ${m} ${path}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, set: (nv: LibraryView) => { v = nv } }
}

const view = (over: Partial<LibraryView> = {}): LibraryView => ({ books: [], ocr_available: true, allow_library_to_free_models: false, ...over })
const pdf = (name = 'notes.pdf') => new File(['%PDF-1.4'], name, { type: 'application/pdf' })

beforeEach(() => vi.stubGlobal('EventSource', FakeEventSource))
afterEach(() => vi.unstubAllGlobals())

test('lists books with pages, chunks and the shared privacy note', async () => {
  backend(view({ books: [book(), book({ id: 'scan', title: 'Scan', ocr_pages: 12, warnings: ['3 pages have < 50 characters and OCR is not available'] })] }))
  render(<Library />)
  const row = await screen.findByTestId('book-razavi-rf')
  expect(within(row).getByText('Razavi RF')).toBeInTheDocument()
  expect(within(row).getByText(/300 pages · 612 chunks/)).toBeInTheDocument()
  expect(within(row).getByText('ready')).toBeInTheDocument()
  expect(within(screen.getByTestId('book-scan')).getByText(/12 read by OCR/)).toBeInTheDocument()
  expect(screen.getByText(/3 pages have < 50 characters/)).toBeInTheDocument()
  expect(screen.getByTestId('privacy-note')).toHaveTextContent('only sent to Claude Code tasks')
})

test('says when library text may go to free models, and when OCR is missing', async () => {
  backend(view({ ocr_available: false, allow_library_to_free_models: true }))
  render(<Library />)
  expect(await screen.findByText('No books yet.')).toBeInTheDocument()
  expect(screen.getByTestId('privacy-note')).toHaveTextContent('may be sent to free models')
  expect(screen.getByText(/OCR is not available/)).toBeInTheDocument()
})

test('uploads the chosen PDF and shows live ingest progress from the job stream, then the finished book', async () => {
  const b = backend(view())
  render(<Library />)
  await screen.findByText('No books yet.')
  fireEvent.change(screen.getByLabelText('PDF file'), { target: { files: [pdf()] } })
  fireEvent.click(screen.getByRole('button', { name: 'Upload' }))
  const row = await screen.findByTestId('book-new-book')
  expect(b.calls).toContain('POST /library/books?filename=notes.pdf')
  expect(within(row).getByText('queued')).toBeInTheDocument()
  expect(within(row).getByRole('button', { name: 'Delete' })).toBeDisabled()  // still being ingested

  act(() => FakeEventSource.last.emit(job({ node_id: 'lib:new-book', progress: 0.62, log_tail: ['600 chunks; embedding'] })))
  expect(within(row).getByRole('progressbar')).toHaveAttribute('value', '0.62')
  expect(within(row).getByText('600 chunks; embedding')).toBeInTheDocument()

  b.set(view({ books: [book({ id: 'new-book', title: 'New Book', pages: 40, chunks: 80 })] }))
  act(() => FakeEventSource.last.emit(job({ node_id: 'lib:new-book', status: 'done', progress: 1 })))
  await waitFor(() => expect(within(screen.getByTestId('book-new-book')).getByText('ready')).toBeInTheDocument())
  expect(screen.getByTestId('book-new-book')).toHaveTextContent('40 pages · 80 chunks')
})

test('an upload the server refuses shows its message and adds nothing', async () => {
  backend(view())
  render(<Library />)
  await screen.findByText('No books yet.')
  fireEvent.change(screen.getByLabelText('PDF file'), { target: { files: [pdf('dup.pdf')] } })
  fireEvent.click(screen.getByRole('button', { name: 'Upload' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('already in the library')
  expect(screen.getByText('No books yet.')).toBeInTheDocument()
})

test('a failed ingest shows its error; delete needs a second click', async () => {
  const b = backend(view({ books: [book({ status: 'failed', error: 'ValueError: not a readable PDF' })] }))
  render(<Library />)
  const row = await screen.findByTestId('book-razavi-rf')
  expect(within(row).getByRole('alert')).toHaveTextContent('not a readable PDF')
  fireEvent.click(within(row).getByRole('button', { name: 'Delete' }))
  expect(b.calls.some((c) => c.startsWith('DELETE'))).toBe(false)
  fireEvent.click(within(row).getByRole('button', { name: 'Delete for good' }))
  await waitFor(() => expect(screen.queryByTestId('book-razavi-rf')).toBeNull())
  expect(b.calls).toContain('DELETE /library/books/razavi-rf')
})
