import { useState } from 'react'
import { api, type Check } from './api/client'

const BADGE: Record<Check['status'], string> = {
  ok: 'bg-green-100 text-green-800',
  warn: 'bg-amber-100 text-amber-800',
  fail: 'bg-red-100 text-red-800',
}

/** Preflight checks with fix hints (SPEC §6.4). A check's fix_action starts a job that shows up in the tray. */
export function Preflight() {
  const [checks, setChecks] = useState<Check[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const run = async () => {
    setBusy(true)
    setError('')
    try {
      setChecks(await api.getPreflight())
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  const fix = async (action: string) => {
    setError('')
    try {
      await api.runFix(action)
    } catch (e) {
      setError(String(e))
    }
  }

  return (
    <section className="mt-6">
      <h2 className="text-lg font-semibold">Preflight</h2>
      <button className="mt-2 rounded bg-blue-600 px-3 py-1 text-white disabled:opacity-50" onClick={run} disabled={busy}>
        {busy ? 'Checking…' : 'Run checks'}
      </button>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      {checks && (
        <ul className="mt-3 space-y-2">
          {checks.map((c) => (
            <li key={c.id} className="rounded border p-2 text-sm">
              <div className="flex items-center gap-2">
                <span className={`rounded px-1.5 text-xs font-medium ${BADGE[c.status]}`}>{c.status}</span>
                <span className="font-medium">{c.label}</span>
                <span className="text-gray-500">{c.detail}</span>
              </div>
              {c.status !== 'ok' && c.fix && <p className="mt-1 text-gray-700">{c.fix}</p>}
              {c.fix_action && (
                <button className="mt-1 rounded border px-2 py-0.5" onClick={() => fix(c.fix_action!)}>
                  Fix now
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
