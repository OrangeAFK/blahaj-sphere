import { useEffect, useState } from 'react'
import { api } from './api/client'
import { Home } from './Home'
import { JobsTray } from './JobsTray'
import { NodePage } from './NodePage'
import { Onboarding } from './Onboarding'
import { Preflight } from './Preflight'
import { SettingsPage } from './SettingsPage'
import { Fin } from './ui'

type Route = { page: 'home' | 'setup' | 'settings' } | { page: 'node'; id: number }

const parse = (h: string): Route => {
  const m = /^#\/node\/(\d+)$/.exec(h)
  if (m) return { page: 'node', id: Number(m[1]) }
  if (h === '#/setup') return { page: 'setup' }
  if (h === '#/settings') return { page: 'settings' }
  return { page: 'home' }
}

export default function App() {
  const [health, setHealth] = useState('...')
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash))

  useEffect(() => {
    api.health().then((h) => setHealth(h.status)).catch(() => setHealth('down'))
    const on = () => setRoute(parse(window.location.hash))
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  const go = (hash: string) => { window.location.hash = hash }
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
            {tab('setup', '#/setup', 'Setup')}
            {tab('settings', '#/settings', 'Settings')}
          </nav>
          <span className="ml-auto text-xs text-gray-500">Backend: {health}</span>
        </div>
      </header>
      <main className="mx-auto max-w-6xl p-4 pb-48">
        {route.page === 'home' && <Home open={(id) => go(`#/node/${id}`)} />}
        {route.page === 'node' && <NodePage key={route.id} id={route.id} home={() => go('#/')} />}
        {route.page === 'setup' && <><Onboarding /><Preflight /></>}
        {route.page === 'settings' && <SettingsPage />}
      </main>
      <JobsTray />
    </div>
  )
}
