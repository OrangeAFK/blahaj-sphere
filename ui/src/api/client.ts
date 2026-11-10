import type { components } from './schema'

export type Settings = components['schemas']['Settings']
export type Job = components['schemas']['Job']
export type Check = components['schemas']['Check']
export type ContainerStatus = components['schemas']['ContainerStatus']

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!r.ok) {
    const detail = await r.json().then((b: { detail?: string }) => b.detail).catch(() => undefined)
    throw new Error(detail ?? `${r.status} ${path}`)
  }
  return r.json() as Promise<T>
}

const post = <T>(path: string, body?: unknown) =>
  req<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const api = {
  health: () => req<components['schemas']['Health']>('/health'),
  getSettings: () => req<Settings>('/settings'),
  putSettings: (s: Settings) => req<Settings>('/settings', { method: 'PUT', body: JSON.stringify(s) }),
  getPreflight: () => req<Check[]>('/preflight'),
  runFix: (action: string) => post<Job>(`/preflight/fix/${encodeURIComponent(action)}`),
  listJobs: () => req<Job[]>('/jobs?active=false&limit=20'),
  rficContainer: () => req<ContainerStatus>('/rfic/container'),
  startRficContainer: () => post<ContainerStatus>('/rfic/container/start'),
  launchGui: (tool: string, node?: string) => post<{ url: string }>('/rfic/gui', { tool, node }),
  launchJupyter: (node: string) => post<{ url: string }>('/rfic/jupyter', { node }),
}

/** Subscribe to /api/jobs/stream (SSE). Returns an unsubscribe function. EventSource reconnects by itself. */
export function subscribeJobs(onJob: (j: Job) => void, onState: (connected: boolean) => void): () => void {
  const es = new EventSource('/api/jobs/stream')
  es.addEventListener('job', (e) => onJob(JSON.parse((e as MessageEvent<string>).data) as Job))
  es.onopen = () => onState(true)
  es.onerror = () => onState(false)
  return () => es.close()
}
