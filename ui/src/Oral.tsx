import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type OralLatency, type OralTurn, type OralTurnResult, type OralView } from './api/client'
import { Badge, Btn, Card, Err, errMsg } from './ui'

/** Hold Space (or the button) to talk; the backend records the microphone. Typing is always available as the text fallback. */
export function Oral({ node, onDone }: { node: { id: number }; onDone: () => void }) {
  const [view, setView] = useState<OralView | null>(null)
  const [err, setErr] = useState('')
  const [mode, setMode] = useState<'voice' | 'text'>('text')
  const [tts, setTts] = useState(false)
  const [busy, setBusy] = useState('')
  const [recording, setRecording] = useState(false)
  const [typed, setTyped] = useState('')
  const [heard, setHeard] = useState('')
  const startP = useRef<Promise<unknown> | null>(null)
  const down = useRef(false)
  const player = useRef<HTMLAudioElement | null>(null)
  const logEnd = useRef<HTMLDivElement | null>(null)

  const apply = useCallback((v: OralView) => setView(v), [])
  useEffect(() => {
    api.oral(node.id).then((v) => {
      setView(v)
      if (v.status !== 'none') { setMode(v.mode); setTts(v.tts) } else { setMode(v.voice.stt && v.voice.microphone ? 'voice' : 'text'); setTts(v.voice.tts) }
    }).catch((e) => setErr(errMsg(e)))
  }, [node.id])
  useEffect(() => {
    logEnd.current?.scrollIntoView?.({ block: 'nearest' })
  }, [view?.turns.length])

  const play = (url?: string) => {
    if (!url) return
    player.current?.pause()
    const a = new Audio(url)
    player.current = a
    void Promise.resolve(a.play?.()).catch(() => { /* autoplay refused: the replay link still works */ })
  }
  const took = (r: OralTurnResult) => {
    apply(r.view)
    if (r.examiner?.audio_url) play(r.examiner.audio_url)
  }

  const begin = async () => {
    setBusy('Starting…')
    setErr('')
    try {
      const v = await api.startOral(node.id, mode, tts)
      apply(v)
      const first = v.turns.find((t) => t.role === 'examiner')
      if (v.turns.length === 1 && first?.audio_url) play(first.audio_url)
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy('')
    }
  }

  const sendTyped = async () => {
    const text = typed.trim()
    if (!text || busy) return
    setBusy('Examiner is thinking…')
    setErr('')
    try {
      took(await api.answerOral(node.id, text))
      setTyped('')
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy('')
    }
  }

  const pttDown = useCallback(() => {
    if (down.current || busy) return
    down.current = true
    setRecording(true)
    setErr('')
    player.current?.pause() // do not record the examiner's own voice
    startP.current = api.startRecording(node.id).catch((e) => { down.current = false; setRecording(false); setErr(errMsg(e)) })
  }, [node.id, busy])
  const pttUp = useCallback(async () => {
    if (!down.current) return
    down.current = false
    setRecording(false)
    await startP.current // the stop must not overtake the start
    setBusy('Transcribing…')
    try {
      const r = await api.stopRecording(node.id)
      if (r.empty) { setHeard(''); setErr('I did not hear anything. Hold the button or Space while you speak.'); apply(r.view) } else { setHeard(r.transcript ?? ''); took(r) }
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy('')
    }
  }, [node.id, apply]) // eslint-disable-line react-hooks/exhaustive-deps

  const voiceLive = view?.status === 'active' && view.mode === 'voice'
  useEffect(() => {
    if (!voiceLive) return
    const typing = (t: EventTarget | null) => t instanceof HTMLElement && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT' || t.isContentEditable)
    const kd = (e: KeyboardEvent) => { if (e.code === 'Space' && !e.repeat && !typing(e.target)) { e.preventDefault(); pttDown() } }
    const ku = (e: KeyboardEvent) => { if (e.code === 'Space' && !typing(e.target)) { e.preventDefault(); void pttUp() } }
    window.addEventListener('keydown', kd)
    window.addEventListener('keyup', ku)
    return () => { window.removeEventListener('keydown', kd); window.removeEventListener('keyup', ku) }
  }, [voiceLive, pttDown, pttUp])
  useEffect(() => () => { player.current?.pause(); if (down.current) void api.cancelRecording(node.id).catch(() => {}) }, [node.id])

  const finish = async () => {
    setBusy('Grading the exam…')
    setErr('')
    try {
      await api.finishOral(node.id)
      onDone()
    } catch (e) {
      setErr(errMsg(e))
      setBusy('')
    }
  }

  if (!view) return <Card title="Oral"><Err e={err} /></Card>
  const lat = new Map<number, OralLatency>(view.latency.map((l) => [l.n, l]))

  if (view.status === 'none') {
    const v = view.voice
    const voiceOk = v.stt && v.microphone
    return (
      <Card title="Oral" right={<Badge tone="sky">voice or text</Badge>}>
        <p className="text-sm">Proof passed. A short oral exam checks that you understand why your design works. It is graded the same way in either mode.</p>
        <fieldset className="mt-2 space-y-1 text-sm">
          <legend className="text-xs uppercase text-gray-500">Mode</legend>
          <label className="flex items-center gap-2">
            <input type="radio" name="oral-mode" checked={mode === 'voice'} disabled={!voiceOk} onChange={() => setMode('voice')} /> Voice (push-to-talk)
            {!voiceOk && <span className="text-xs text-amber-700">{!v.stt ? 'speech model not downloaded (uv run python -m app.voice download)' : 'no microphone found'}</span>}
          </label>
          <label className="flex items-center gap-2"><input type="radio" name="oral-mode" checked={mode === 'text'} onChange={() => setMode('text')} /> Text</label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={tts} disabled={!v.tts} onChange={(e) => setTts(e.target.checked)} /> Read the questions aloud
            {!v.tts && <span className="text-xs text-amber-700">voice not downloaded</span>}
          </label>
        </fieldset>
        <Btn primary className="mt-2" disabled={!!busy} onClick={begin}>{busy || 'Start oral exam'}</Btn>
        <Err e={err} />
      </Card>
    )
  }

  const complete = view.status === 'complete'
  return (
    <Card title="Oral" right={<Badge tone={complete ? 'green' : 'sky'}>{view.mode === 'voice' ? 'voice' : 'text'} · question {Math.min(view.progress.asked, view.progress.of)}/{view.progress.of}</Badge>}>
      {view.plan && <p className="text-xs text-gray-500">{view.plan.n_core} core question(s){view.plan.n_interleaved ? ' + 1 interleaved' : ''}: {view.plan.reason}.</p>}
      <div className="mt-2 max-h-80 space-y-2 overflow-auto rounded border border-sky-100 p-2" aria-label="Exam transcript" data-testid="oral-log">
        {view.turns.map((t: OralTurn) => (
          <div key={t.n} className={t.role === 'examiner' ? 'text-sm' : 'text-sm text-sky-900'}>
            <span className="mr-1 text-xs font-semibold uppercase text-gray-500">{t.role === 'examiner' ? 'Examiner' : 'You'}</span>
            {t.text}
            {t.audio_url && <button className="ml-2 text-xs text-sky-700 underline" onClick={() => play(t.audio_url)}>replay</button>}
            {lat.get(t.n) && (
              <span className="ml-2 text-xs text-gray-400" title="record → transcript → question audio">
                {lat.get(t.n)!.stt_s != null ? `stt ${lat.get(t.n)!.stt_s}s · ` : ''}llm {lat.get(t.n)!.llm_s}s · tts {lat.get(t.n)!.tts_s}s · total {lat.get(t.n)!.total_s}s
              </span>
            )}
          </div>
        ))}
        <div ref={logEnd} />
      </div>
      {complete ? (
        <div className="mt-2">
          <p className="text-sm">That was the last question. Finish to grade the exam and see your debrief.</p>
          <Btn primary className="mt-1" disabled={!!busy} onClick={finish}>{busy || 'Finish and grade'}</Btn>
        </div>
      ) : (
        <>
          {view.mode === 'voice' && (
            <div className="mt-2 flex items-center gap-3">
              <button
                type="button"
                aria-label="Push to talk"
                className={`rounded-full px-4 py-2 text-sm font-semibold text-white select-none ${recording ? 'bg-red-600' : 'bg-sky-700 hover:bg-sky-800'} disabled:opacity-50`}
                disabled={!!busy && !recording}
                onPointerDown={(e) => { e.currentTarget.setPointerCapture?.(e.pointerId); pttDown() }}
                onPointerUp={() => void pttUp()}
                onPointerCancel={() => void pttUp()}
              >
                {recording ? '● Recording… release to send' : 'Hold to talk (or hold Space)'}
              </button>
              {heard && <span className="text-xs text-gray-500">heard: “{heard}”</span>}
            </div>
          )}
          <div className="mt-2">
            <textarea className="block w-full rounded border px-2 py-1 text-sm" rows={2} aria-label="Type your answer" disabled={!!busy}
                      placeholder={view.mode === 'voice' ? 'Or type your answer (text fallback)' : 'Type your answer'} value={typed} onChange={(e) => setTyped(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void sendTyped() }} />
            <Btn className="mt-1" disabled={!typed.trim() || !!busy} onClick={sendTyped}>Send (Ctrl+Enter)</Btn>
          </div>
          {busy && <p role="status" className="mt-1 text-xs text-gray-500">{busy}</p>}
        </>
      )}
      <Err e={err} />
    </Card>
  )
}
