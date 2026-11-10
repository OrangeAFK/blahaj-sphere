import { useEffect, useState } from 'react'
import { api, type Settings } from './api/client'

export default function App() {
  const [health, setHealth] = useState('...')
  const [s, setS] = useState<Settings | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    api.health().then((h) => setHealth(h.status)).catch(() => setHealth('down'))
    api.getSettings().then(setS).catch(() => {})
  }, [])

  const save = async () => {
    if (!s) return
    setS(await api.putSettings(s))
    setSaved(true)
  }

  return (
    <main className="mx-auto max-w-xl p-8 font-sans">
      <h1 className="text-2xl font-bold">BlahajSphere</h1>
      <p className="text-sm text-gray-500">Backend: {health}</p>
      <h2 className="mt-6 text-lg font-semibold">Settings</h2>
      {s && (
        <div className="mt-2 space-y-3">
          <label className="block text-sm">
            OpenRouter API key
            <input
              className="mt-1 block w-full rounded border px-2 py-1"
              type="password"
              value={s.openrouter_api_key}
              onChange={(e) => { setSaved(false); setS({ ...s, openrouter_api_key: e.target.value }) }}
            />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={s.allow_library_to_free_models}
              onChange={(e) => { setSaved(false); setS({ ...s, allow_library_to_free_models: e.target.checked }) }}
            />
            Allow library text to free models
          </label>
          <button className="rounded bg-blue-600 px-3 py-1 text-white" onClick={save}>Save</button>
          {saved && <span className="ml-2 text-sm text-green-600">Saved</span>}
        </div>
      )}
    </main>
  )
}
