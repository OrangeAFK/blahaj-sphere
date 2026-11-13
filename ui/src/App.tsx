import { useCallback, useEffect, useState } from 'react'
import { api, type TracksView } from './api/client'
import { Competency } from './Competency'
import { Graph } from './Graph'
import { Home } from './Home'
import { JobsTray } from './JobsTray'
import { Library } from './Library'
import { NodePage } from './NodePage'
import { Onboarding } from './Onboarding'
import { Preflight } from './Preflight'
import { SettingsPage } from './SettingsPage'
import { Tracks } from './Tracks'
import { Fin } from './ui'

type Route = { page: 'home' | 'setup' | 'settings' | 'tracks' | 'graph' | 'library' | 'competency' } | { page: 'node'; id: number; readOnly?: boolean }

const parse = (h: string): Route => {
  const m = /^#\/node\/(\d+)(\/view)?$/.exec(h)
  if (m) return { page: 'node', id: Number(m[1]), readOnly: !!m[2] }
  if (h === '#/competency') return { page: 'competency' }
  if (h === '#/setup') return { page: 'setup' }
  if (h === '#/settings') return { page: 'settings' }
  if (h === '#/tracks') return { page: 'tracks' }
  if (h === '#/graph') return { page: 'graph' }
  if (h === '#/library') return { page: 'library' }
  return { page: 'home' }
}

export default function App() {
  const [health, setHealth] = useState('...')
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash))
  const [tracks, setTracks] = useState<TracksView | null>(null)
  const [switchErr, setSwitchErr] = useState('')
  const onTracks = useCallback((v: TracksView) => setTracks(v), [])

  useEffect(() => {
    api.health().then((h) => setHealth(h.status)).catch(() => setHealth('down'))
    api.tracks().then((v) => { if (v && Array.isArray(v.tracks)) setTracks(v) }).catch(() => {})
    const on = () => setRoute(parse(window.location.hash))
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  const go = (hash: string) => { window.location.hash = hash }
  const list = tracks?.tracks ?? []
  const active = list.find((t) => t.active)
  const switchTo = async (id: string) => {
    setSwitchErr('')
    try {
      const v = await api.activateTrack(id)
      setTracks(v)
      go('#/')  // every screen is scoped to the active track: back to its Home (the keyed <main> below remounts)
    } catch (e) {
      setSwitchErr(e instanceof Error ? e.message : String(e))
    }
  }
  const tab = (page: string, hash: string, label: string) => (
    <a href={hash} className={`rounded px-2 py-1 text-sm ${route.page === page ? 'bg-sky-700 text-white' : 'text-sky-900 hover:bg-sky-100'}`}>{label}</a>
  )

  return (
    <div className="min-h-screen bg-sky-50/50 font-sans text-slate-900">
      <header className="sticky top-0 z-[5] border-b border-sky-200 bg-white/95">
        <div className="mx-auto flex max-w-6xl items-center gap-3 px-4 py-2">
          <Fin className="h-6 w-6 text-sky-600" />
          <span className="text-lg font-bold text-sky-900">BlahajSphere</span>
          <nav className="ml-4 flex gap-1" aria-label="Main">
            {tab('home', '#/', 'Home')}
            {tab('graph', '#/graph', 'Graph')}
            {tab('competency', '#/competency', 'Competency')}
            {tab('library', '#/library', 'Library')}
            {tab('setup', '#/setup', 'Setup')}
            {tab('tracks', '#/tracks', 'Tracks')}
            {tab('settings', '#/settings', 'Settings')}
          </nav>
          {active && (
            <span className="ml-auto flex items-center gap-2 text-sm">
              <label className="flex items-center gap-1 text-sky-900">
                Track
                <select
                  aria-label="Active track"
                  className="rounded border border-sky-300 bg-white px-1 py-0.5"
                  value={active.id}
                  onChange={(e) => void switchTo(e.target.value)}
                >
                  {list.filter((t) => t.status !== 'archived').map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                </select>
              </label>
              <a href="#/tracks" className="text-xs text-sky-700 underline">Manage tracks</a>
            </span>
          )}
          <span className={`${active ? '' : 'ml-auto '}text-xs text-gray-500`}>Backend: {health}</span>
        </div>
      </header>
      {switchErr && <p role="alert" className="mx-auto max-w-6xl px-4 pt-2 text-sm text-red-600">{switchErr}</p>}
      <main key={active?.id ?? 'none'} className="mx-auto max-w-6xl p-4 pb-48">
        {route.page === 'home' && <Home open={(id) => go(`#/node/${id}`)} />}
        {route.page === 'node' && <NodePage key={`${route.id}${route.readOnly ? 'v' : ''}`} id={route.id} readOnly={route.readOnly} home={() => go('#/')} />}
        {route.page === 'graph' && <Graph track={active?.id ?? ''} />}
        {route.page === 'competency' && <Competency open={(id) => go(`#/node/${id}`)} />}
        {route.page === 'setup' && <><Onboarding track={active?.name} /><Preflight /></>}
        {route.page === 'tracks' && <Tracks onChange={onTracks} go={go} />}
        {route.page === 'library' && <Library />}
        {route.page === 'settings' && <SettingsPage />}
      </main>
      <JobsTray />
    </div>
  )
}
