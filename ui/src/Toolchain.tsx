import { useState } from 'react'
import { api } from './api/client'

/** RFIC toolchain launchers: GUI tools open in noVNC, JupyterLab opens at the node workspace (system browser). */
export function Toolchain() {
  const [node, setNode] = useState('')
  const [msg, setMsg] = useState('')

  const act = async (label: string, fn: () => Promise<{ url?: string } | unknown>) => {
    setMsg(`${label}…`)
    try {
      await fn()
      setMsg(`${label}: opened in your browser`)
    } catch (e) {
      setMsg(`${label} failed: ${String(e)}`)
    }
  }

  return (
    <section className="mt-6">
      <h2 className="text-lg font-semibold">RFIC toolchain</h2>
      <label className="mt-2 block text-sm">
        Node directory
        <input
          className="mt-1 block w-full rounded border px-2 py-1"
          value={node}
          placeholder="e.g. ems_sample"
          onChange={(e) => setNode(e.target.value)}
        />
      </label>
      <div className="mt-2 flex flex-wrap gap-2">
        <button className="rounded border px-3 py-1" onClick={() => act('Xschem', () => api.launchGui('xschem', node || undefined))}>
          Open Xschem
        </button>
        <button className="rounded border px-3 py-1" onClick={() => act('KLayout', () => api.launchGui('klayout', node || undefined))}>
          Open KLayout
        </button>
        <button
          className="rounded border px-3 py-1 disabled:opacity-50"
          disabled={!node}
          onClick={() => act('JupyterLab', () => api.launchJupyter(node))}
        >
          Open JupyterLab
        </button>
      </div>
      {msg && <p className="mt-2 text-sm text-gray-600">{msg}</p>}
    </section>
  )
}
