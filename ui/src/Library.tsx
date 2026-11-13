import { useCallback, useEffect, useRef, useState } from 'react'
import { api, subscribeJobs, type Book, type Job, type LibraryView } from './api/client'
import { Badge, Btn, Card, Err, errMsg } from './ui'

const TONE = { queued: 'gray', ingesting: 'amber', ready: 'green', failed: 'red' } as const
const mb = (n: number) => `${(n / (1024 * 1024)).toFixed(1)} MB`
const LIB_JOB = /^lib:(.+)$/

/** Library screen (SPEC §10): upload a PDF, watch its ingest job, list and delete books. The library is shared by every track. */
export function Library() {
  const [view, setView] = useState<LibraryView | null>(null)
  const [jobs, setJobs] = useState<Record<string, Job>>({})
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [confirm, setConfirm] = useState<string | null>(null)
  const file = useRef<HTMLInputElement>(null)

  const load = useCallback(() => api.library().then(setView).catch((e) => setErr(errMsg(e))), [])
  useEffect(() => { void load() }, [load])
  useEffect(
    () =>
      subscribeJobs((j) => {
        const id = LIB_JOB.exec(j.node_id ?? '')?.[1]
        if (!id || j.type !== 'library_ingest') return
        setJobs((prev) => ({ ...prev, [id]: j }))
        if (j.status === 'done' || j.status === 'failed') void load()
      }, () => {}),
    [load],
  )

  const upload = async () => {
    const f = file.current?.files?.[0]
    if (!f) return
    setBusy(true)
    setErr('')
    try {
      await api.uploadBook(f, title.trim() || undefined)
      setTitle('')
      if (file.current) file.current.value = ''
      await load()
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy(false)
    }
  }
  const remove = async (b: Book) => {
    setErr('')
    try {
      await api.deleteBook(b.id)
      setConfirm(null)
      await load()
    } catch (e) {
      setErr(errMsg(e))
    }
  }

  const row = (b: Book) => {
    const job = jobs[b.id]
    const running = b.status === 'queued' || b.status === 'ingesting'
    const last = job?.log_tail[job.log_tail.length - 1]
    return (
      <li key={b.id} className="rounded border border-sky-100 p-2" data-testid={`book-${b.id}`}>
        <div className="flex flex-wrap items-center gap-2">
          <strong className="text-sky-900">{b.title}</strong>
          <Badge tone={TONE[b.status]}>{b.status}</Badge>
          <code className="text-xs text-gray-500">{b.id}</code>
          <span className="ml-auto flex gap-1">
            {confirm === b.id ? (
              <>
                <Btn danger onClick={() => void remove(b)}>Delete for good</Btn>
                <Btn onClick={() => setConfirm(null)}>Keep</Btn>
              </>
            ) : (
              <Btn danger disabled={running} onClick={() => setConfirm(b.id)}>Delete</Btn>
            )}
          </span>
        </div>
        <p className="mt-1 text-xs text-gray-600">
          {b.filename} · {mb(b.size_bytes)}
          {b.status === 'ready' && ` · ${b.pages} pages · ${b.chunks} chunks${b.ocr_pages ? ` · ${b.ocr_pages} read by OCR` : ''}`}
        </p>
        {running && (
          <div className="mt-1 flex items-center gap-2 text-xs text-gray-600">
            <progress className="w-40" max={1} value={job?.progress ?? 0} aria-label={`Ingest progress of ${b.title}`} />
            <span className="truncate font-mono">{last ?? 'waiting for the ingest job…'}</span>
          </div>
        )}
        {b.error && <p role="alert" className="mt-1 text-sm text-red-600">{b.error}</p>}
        {b.warnings.map((w) => <p key={w} className="mt-1 text-xs text-amber-700">{w}</p>)}
      </li>
    )
  }

  return (
    <div className="space-y-4">
      <Card title="Library">
        <p className="text-sm text-gray-700">
          Upload textbook or paper PDFs. They are read, split into passages and indexed on this computer; the Builder and the tutor then
          retrieve the five most relevant passages and cite them as <code>(book-id, p. N)</code>. Books are shared by every track.
        </p>
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <label className="text-sm text-sky-900">
            PDF file
            <input ref={file} type="file" accept="application/pdf,.pdf" className="mt-0.5 block text-sm" aria-label="PDF file" />
          </label>
          <label className="text-sm text-sky-900">
            Title (optional)
            <input className="mt-0.5 block rounded border px-2 py-1 text-sm" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Book title" />
          </label>
          <Btn primary disabled={busy} onClick={() => void upload()}>{busy ? 'Uploading…' : 'Upload'}</Btn>
        </div>
        <Err e={err} />
        {view && !view.ocr_available && (
          <p className="mt-2 text-xs text-amber-700">
            OCR is not available (needs Tesseract): scanned pages with almost no text will be skipped. Equations also extract poorly from any PDF.
          </p>
        )}
        <p className="mt-2 text-xs text-gray-600" data-testid="privacy-note">
          {view?.allow_library_to_free_models
            ? 'Privacy: library text may be sent to free models (Settings). Free providers may log prompts.'
            : 'Privacy: library text is only sent to Claude Code tasks. Change this in Settings.'}
        </p>
      </Card>
      <Card title="Books">
        {!view ? <p className="text-sm text-gray-500">Loading…</p> : view.books.length === 0 ? (
          <p className="text-sm text-gray-500">No books yet.</p>
        ) : (
          <ul className="space-y-2">{view.books.map(row)}</ul>
        )}
      </Card>
    </div>
  )
}
