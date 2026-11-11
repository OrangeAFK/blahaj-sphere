import { useEffect, useRef, useState } from 'react'
import { api, type NodeView, type TutorMessage } from './api/client'
import { Badge, Btn, Card, Err, errMsg } from './ui'
import { Markdown } from './Markdown'

/** Side panel: Socratic tutor (light tier) plus "Show me" (heavy, sees the reference; reduces credit, SPEC §7.4/§8.2). */
export function Tutor({ node }: { node: NodeView }) {
  const [msgs, setMsgs] = useState<TutorMessage[]>([])
  const [used, setUsed] = useState<string[]>([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [confirmShow, setConfirmShow] = useState(false)
  const end = useRef<HTMLDivElement>(null)

  useEffect(() => {
    api.tutorHistory(node.id).then((h) => { setMsgs(h.messages); setUsed(h.show_me_used) }).catch(() => {})
  }, [node.id])
  useEffect(() => { end.current?.scrollIntoView?.({ block: "nearest" }) }, [msgs])

  const send = async () => {
    const message = text.trim()
    if (!message) return
    setBusy(true)
    setErr('')
    setText('')
    setMsgs((m) => [...m, { role: 'user', content: message }])
    try {
      const r = await api.tutorChat(node.id, message)
      setMsgs((m) => [...m, { role: 'assistant', content: r.reply, hint: r.hint, redacted: r.redacted }])
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy(false)
    }
  }
  const showMe = async () => {
    const question = text.trim() || 'Show me how to get unstuck on the current step.'
    setConfirmShow(false)
    setBusy(true)
    setErr('')
    setText('')
    setMsgs((m) => [...m, { role: 'user', content: `[Show me] ${question}` }])
    try {
      const r = await api.showMe(node.id, question)
      setMsgs((m) => [...m, { role: 'assistant', kind: 'show_me', content: r.explanation }])
      setUsed((u) => [...new Set([...u, ...r.subskills])])
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Tutor" className="flex h-fit max-h-[80vh] flex-col" right={used.length > 0 && <Badge tone="amber">show-me used: {used.join(', ')}</Badge>}>
      <div className="min-h-32 flex-1 space-y-2 overflow-auto text-sm" aria-live="polite">
        {msgs.length === 0 && <p className="text-gray-500">Ask a question. The tutor answers with questions and hints, never the answer.</p>}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'text-right' : ''}>
            <div className={`inline-block max-w-full rounded-lg px-2 py-1 text-left ${m.role === 'user' ? 'bg-sky-700 text-white' : m.kind === 'show_me' ? 'bg-amber-50' : 'bg-sky-50'}`}>
              {m.role === 'user' ? <span className="whitespace-pre-wrap">{m.content}</span> : <Markdown>{m.content}</Markdown>}
            </div>
          </div>
        ))}
        <div ref={end} />
      </div>
      <textarea className="mt-2 w-full rounded border px-2 py-1 text-sm" rows={2} placeholder="Ask the tutor…" aria-label="Tutor message" value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }} />
      <div className="mt-1 flex items-center gap-2">
        <Btn primary disabled={busy || !text.trim()} onClick={send}>{busy ? '…' : 'Ask'}</Btn>
        <Btn disabled={busy} onClick={() => setConfirmShow(true)}>Show me</Btn>
      </div>
      {confirmShow && (
        <div role="alertdialog" className="mt-2 rounded bg-amber-50 p-2 text-sm text-amber-900">
          “Show me” gives a direct explanation from the reference solution. It <b>reduces credit</b> for the affected subskills on this node.
          <div className="mt-1 flex gap-2">
            <Btn danger onClick={showMe}>Show me anyway</Btn>
            <Btn onClick={() => setConfirmShow(false)}>Cancel</Btn>
          </div>
        </div>
      )}
      <Err e={err} />
    </Card>
  )
}
