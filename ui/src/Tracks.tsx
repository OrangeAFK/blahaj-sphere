import { useCallback, useEffect, useState } from 'react'
import { api, type TrackView, type TracksView, type TrashView } from './api/client'
import { Badge, Btn, Card, Err, errMsg } from './ui'

type Panel = { kind: 'rename' | 'reset' | 'delete'; id: string } | { kind: 'purge' } | null

const tierSummary = (t: TrackView) => {
  const e = Object.entries(t.tiers)
  return e.length ? e.map(([d, tier]) => `${d} T${tier}`).join(' · ') : 'no tiers yet'
}
const when = (ts: string | null | undefined) => (ts ? new Date(ts).toLocaleString() : '—')
const STAGE: Record<string, string> = { interview: 'onboarding: interview', framework: 'onboarding: framework', diagnostic: 'onboarding: diagnostic' }

/** Tracks screen (SPEC §8.6): create, enter, rename, archive, reset, delete, restore and purge. Nothing here changes data until the
 *  button of an open confirmation panel is pressed; delete and purge need typed confirmation. */
export function Tracks({ onChange, go }: { onChange: (v: TracksView) => void; go: (hash: string) => void }) {
  const [view, setView] = useState<TracksView | null>(null)
  const [panel, setPanel] = useState<Panel>(null)
  const [text, setText] = useState('')
  const [keepGoals, setKeepGoals] = useState(true)
  const [name, setName] = useState('')
  const [domain, setDomain] = useState('rfic')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const apply = useCallback((v: TracksView) => {
    setView(v)
    onChange(v)
  }, [onChange])

  useEffect(() => {
    api.tracks().then(apply).catch((e) => setErr(errMsg(e)))
  }, [apply])

  const act = async (fn: () => Promise<TracksView | { purged: string[] }>, after?: () => void) => {
    setBusy(true)
    setErr('')
    try {
      const r = await fn()
      if ('tracks' in r) apply(r)
      else apply(await api.tracks())
      setPanel(null)
      setText('')
      after?.()
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  const open = (p: Panel, initial = '') => {
    setPanel(p)
    setText(initial)
    setErr('')
  }
  const target = panel && 'id' in panel ? view?.tracks.find((t) => t.id === panel.id) : undefined

  const row = (t: TrackView) => (
    <li key={t.id} className="rounded border border-sky-100 p-2" data-testid={`track-${t.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <strong className="text-sky-900">{t.name}</strong>
        <Badge tone={t.active ? 'green' : t.status === 'archived' ? 'gray' : 'sky'}>{t.active ? 'active' : t.status}</Badge>
        <span className="text-xs text-gray-500">{t.domain}</span>
        {t.stage !== 'done' && <Badge tone="amber">{STAGE[t.stage] ?? t.stage}</Badge>}
        <span className="ml-auto flex flex-wrap gap-1">
          <Btn primary disabled={busy || t.active || t.status === 'archived'} onClick={() => act(() => api.activateTrack(t.id), () => go('#/'))}>Enter</Btn>
          <Btn disabled={busy} onClick={() => open({ kind: 'rename', id: t.id }, t.name)}>Rename</Btn>
          {t.status === 'archived' ? (
            <Btn disabled={busy} onClick={() => act(() => api.unarchiveTrack(t.id))}>Unarchive</Btn>
          ) : (
            <Btn disabled={busy} onClick={() => act(() => api.archiveTrack(t.id))}>Archive</Btn>
          )}
          <Btn danger disabled={busy || t.status === 'archived'} onClick={() => { setKeepGoals(true); open({ kind: 'reset', id: t.id }) }}>Reset</Btn>
          <Btn danger disabled={busy} onClick={() => open({ kind: 'delete', id: t.id })}>Delete</Btn>
        </span>
      </div>
      <p className="mt-1 text-xs text-gray-600">
        {tierSummary(t)} · {t.nodes} node{t.nodes === 1 ? '' : 's'}
        {t.current_node ? ` · working on #${t.current_node.id} ${t.current_node.title ?? ''} (${t.current_node.status})` : ''} · last activity {when(t.last_activity)}
      </p>
    </li>
  )

  const trashRow = (e: TrashView) => (
    <li key={e.entry} className="flex flex-wrap items-center gap-2 rounded border border-gray-200 p-2 text-sm">
      <strong>{e.name}</strong>
      <Badge tone={e.kind === 'deleted' ? 'amber' : 'gray'}>{e.kind}</Badge>
      <span className="text-xs text-gray-500">{when(e.trashed_at)} · {e.events} events · {e.misconceptions} misconceptions</span>
      <span className="ml-auto">
        {e.restorable ? (
          <Btn disabled={busy} onClick={() => act(() => api.restoreTrack(e.entry))}>Restore</Btn>
        ) : (
          <span className="text-xs text-gray-500">kept until purged</span>
        )}
      </span>
    </li>
  )

  return (
    <div className="space-y-4">
      <Card title="Tracks">
        <p className="mb-2 text-sm text-gray-600">
          A track is an independent learning path with its own goals, framework, nodes and scores. One track is active at a time; the others keep their progress.
        </p>
        {!view && !err && <p className="text-sm text-gray-500">Loading…</p>}
        <ul className="space-y-2" aria-label="Track list">{view?.tracks.map(row)}</ul>

        {panel?.kind === 'rename' && target && (
          <form className="mt-3 flex flex-wrap items-center gap-2 rounded bg-sky-50 p-2" onSubmit={(e) => { e.preventDefault(); void act(() => api.renameTrack(target.id, text)) }}>
            <label className="text-sm">New name for {target.name}
              <input aria-label="New name" className="ml-2 rounded border px-2 py-1" value={text} onChange={(e) => setText(e.target.value)} />
            </label>
            <Btn primary type="submit" disabled={busy || !text.trim()}>Save name</Btn>
            <Btn type="button" onClick={() => setPanel(null)}>Cancel</Btn>
          </form>
        )}
        {panel?.kind === 'reset' && target && (
          <div className="mt-3 space-y-2 rounded border border-red-200 bg-red-50 p-2 text-sm" role="group" aria-label="Reset track">
            <p>
              Reset <strong>{target.name}</strong>: its nodes, framework, events and misconceptions move to the trash (the shared voice models and library are not touched).
              You then redo the framework and the diagnostic.
            </p>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={keepGoals} onChange={(e) => setKeepGoals(e.target.checked)} /> Keep my goals (re-edit them in Setup)
            </label>
            <div className="flex gap-2">
              <Btn danger disabled={busy} onClick={() => act(() => api.resetTrack(target.id, keepGoals), () => go('#/setup'))}>Reset {target.name}</Btn>
              <Btn onClick={() => setPanel(null)}>Cancel</Btn>
            </div>
          </div>
        )}
        {panel?.kind === 'delete' && target && (
          <div className="mt-3 space-y-2 rounded border border-red-200 bg-red-50 p-2 text-sm" role="group" aria-label="Delete track">
            <p>
              Delete <strong>{target.name}</strong>? It moves to the trash and can be restored until the trash is purged. Type the track&apos;s name to confirm.
            </p>
            <input aria-label="Type the track name" className="rounded border px-2 py-1" value={text} onChange={(e) => setText(e.target.value)} />
            <div className="flex gap-2">
              <Btn danger disabled={busy || text !== target.name} onClick={() => act(() => api.deleteTrack(target.id, text))}>Delete {target.name}</Btn>
              <Btn onClick={() => setPanel(null)}>Cancel</Btn>
            </div>
          </div>
        )}
        <Err e={err} />
      </Card>

      <Card title="New track">
        <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); void act(() => api.createTrack(name, domain), () => { setName(''); go('#/setup') }) }}>
          <label className="text-sm">Name
            <input aria-label="Track name" className="ml-2 rounded border px-2 py-1" placeholder="RFIC mmWave" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="text-sm">Domain
            <select aria-label="Domain plugin" className="ml-2 rounded border px-2 py-1" value={domain} onChange={(e) => setDomain(e.target.value)}>
              {(view?.domains?.length ? view.domains : ['rfic']).map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </label>
          <Btn primary type="submit" disabled={busy || !name.trim()}>Create and start onboarding</Btn>
        </form>
      </Card>

      <Card
        title="Trash"
        right={view?.trash.length ? <Btn danger disabled={busy} onClick={() => open({ kind: 'purge' })}>Purge trash…</Btn> : undefined}
      >
        {!view?.trash.length ? (
          <p className="text-sm text-gray-500">Nothing in the trash.</p>
        ) : (
          <ul className="space-y-2" aria-label="Trash list">{view.trash.map(trashRow)}</ul>
        )}
        {panel?.kind === 'purge' && (
          <div className="mt-3 space-y-2 rounded border border-red-200 bg-red-50 p-2 text-sm" role="group" aria-label="Purge trash">
            <p>Purging permanently deletes everything in the trash. This cannot be undone. Type PURGE to confirm.</p>
            <input aria-label="Type PURGE" className="rounded border px-2 py-1" value={text} onChange={(e) => setText(e.target.value)} />
            <div className="flex gap-2">
              <Btn danger disabled={busy || text !== 'PURGE'} onClick={() => act(() => api.purgeTrash(text))}>Purge trash</Btn>
              <Btn onClick={() => setPanel(null)}>Cancel</Btn>
            </div>
          </div>
        )}
      </Card>
    </div>
  )
}
