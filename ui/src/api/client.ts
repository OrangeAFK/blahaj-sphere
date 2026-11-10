import type { components } from './schema'

export type Settings = components['schemas']['Settings']
export type Job = components['schemas']['Job']
export type Check = components['schemas']['Check']
export type ContainerStatus = components['schemas']['ContainerStatus']
export type OnboardingState = components['schemas']['OnboardingState']
export type InterviewView = components['schemas']['InterviewView']
export type Framework = components['schemas']['Framework']
export type FrameworkView = components['schemas']['FrameworkView']
export type DiagnosticView = components['schemas']['DiagnosticView']
export type AnswerResult = components['schemas']['AnswerResult']

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
  onboardingState: () => req<OnboardingState>('/onboarding/state'),
  interview: () => req<InterviewView>('/onboarding/interview'),
  interviewTurn: (answer?: string) => post<InterviewView>('/onboarding/interview/turn', { answer }),
  putGoals: (text: string) => req<{ ok: boolean }>('/onboarding/goals', { method: 'PUT', body: JSON.stringify({ text }) }),
  getFramework: () => req<FrameworkView>('/onboarding/framework'),
  draftFramework: () => post<FrameworkView>('/onboarding/framework/draft'),
  saveFrameworkDraft: (fw: Framework) =>
    req<FrameworkView>('/onboarding/framework/draft', { method: 'PUT', body: JSON.stringify(fw) }),
  approveFramework: (fw?: Framework) => post<FrameworkView>('/onboarding/framework/approve', { framework: fw ?? null }),
  getDiagnostic: () => req<DiagnosticView>('/onboarding/diagnostic'),
  diagnosticNext: () => post<DiagnosticView>('/onboarding/diagnostic/next'),
  diagnosticAnswer: (answer: string) => post<AnswerResult>('/onboarding/diagnostic/answer', { answer }),
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
