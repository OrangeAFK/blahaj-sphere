import { useEffect, useState } from 'react'
import { api, type LlmUsage, type Settings } from './api/client'
import { Card, Err, errMsg, Btn } from './ui'
import { Toolchain } from './Toolchain'

export function UsageView() {
  const [u, setU] = useState<LlmUsage | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => { api.llmUsage().then(setU).catch((e) => setErr(errMsg(e))) }, [])
  if (!u) return <Err e={err} />
  return (
    <Card title="LLM usage today" right={u.pending_grades > 0 && <span className="text-xs text-amber-700">{u.pending_grades} grade(s) pending</span>}>
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-gray-500"><tr><th>Backend</th><th>Model</th><th className="text-right">Calls</th><th className="text-right">Cap</th></tr></thead>
        <tbody>
          {u.usage.map((r) => (
            <tr key={`${r.backend}/${r.model}`} className="border-t">
              <td>{r.backend}</td><td className="font-mono text-xs">{r.model || '(default)'}</td><td className="text-right font-mono">{r.count}</td><td className="text-right font-mono">{r.cap ?? '–'}</td>
            </tr>
          ))}
          {u.usage.length === 0 && <tr><td colSpan={4} className="text-gray-500">No calls yet today.</td></tr>}
        </tbody>
      </table>
      <ul className="mt-2 text-sm" data-testid="caps">
        {u.backends.filter((b) => b.cap !== null).map((b) => (
          <li key={b.backend}>{b.backend}: <b>{b.remaining}</b> of {b.cap} calls remaining today ({b.count} used)</li>
        ))}
      </ul>
    </Card>
  )
}

export function SettingsPage() {
  const [s, setS] = useState<Settings | null>(null)
  const [saved, setSaved] = useState(false)
  useEffect(() => { api.getSettings().then(setS).catch(() => {}) }, [])
  const save = async () => {
    if (!s) return
    setS(await api.putSettings(s))
    setSaved(true)
  }
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <div className="space-y-3">
        <Card title="Settings">
          {s && (
            <div className="space-y-3">
              <label className="block text-sm">
                OpenRouter API key
                <input className="mt-1 block w-full rounded border px-2 py-1" type="password" value={s.openrouter_api_key}
                       onChange={(e) => { setSaved(false); setS({ ...s, openrouter_api_key: e.target.value }) }} />
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={s.allow_library_to_free_models}
                       onChange={(e) => { setSaved(false); setS({ ...s, allow_library_to_free_models: e.target.checked }) }} />
                Allow library text to free models
              </label>
              <Btn primary onClick={save}>Save</Btn>
              {saved && <span className="ml-2 text-sm text-green-600">Saved</span>}
            </div>
          )}
        </Card>
        <UsageView />
      </div>
      <Card title="Toolchain"><Toolchain /></Card>
    </div>
  )
}
