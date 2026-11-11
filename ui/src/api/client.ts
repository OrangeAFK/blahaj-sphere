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

export type NodeSummary = components['schemas']['NodeSummary']
export type NodeView = components['schemas']['NodeView']
export type CheckerView = components['schemas']['CheckerView']
export type PlannerOptions = components['schemas']['PlannerOptions']
export type BranchOptionT = components['schemas']['BranchOption']
export type TutorReply = components['schemas']['TutorReply']
export type ShowMeReply = components['schemas']['ShowMeReply']
export type RescopeStarted = components['schemas']['RescopeStarted']
export type BuildStarted = components['schemas']['BuildStarted']

// Responses the OpenAPI schema leaves untyped (plain dict endpoints); shapes from app/lifecycle/{service,tutor,debrief}.py.
export type CheckTest = { name?: string; pass?: boolean; measured?: unknown; unit?: string; spec?: unknown }
export type TutorMessage = { role: string; content: string; ts?: string; kind?: string; hint?: unknown; redacted?: boolean }
export type TutorHistory = { messages: TutorMessage[]; hints: unknown; show_me_used: string[] }
export type ProofSubmission = { n: number; ts: string; status: string; verdict: string | null; tests: CheckTest[]; seconds: number | null; detail: string | null }
export type ProofView = {
  stage: string; artifacts: string[]; submissions: ProofSubmission[]; failed: number
  remediation_offer: boolean; remediation_decision: string | null
}
export type OralInfo = { implemented: boolean; milestone: string; stage: string; modes: string[]; can_advance: boolean; note: string }
export type MasteryDelta = { domain: string; tier: number; mastery_before: number; mastery_after: number; delta: number; exam_unlocked: boolean }
export type Calibration = {
  id: string; subskill: string; unit: string | null; predicted: number; actual: number
  error: number; rel_error: number | null; tolerance: number; within: boolean
}
export type DebriefView = {
  node_id: number; title: string | null; status: string; provisional: boolean
  proof: { passed: boolean; submissions: number; failed_submissions: number }
  calibration: { numeric: Calibration[]; within: number; total: number }
  grades: {
    status: 'pending' | 'graded' | 'none'
    items: { id: string; subskill: string; prompt: string; score: number; quote: string; rationale: string }[]
    pending_items: { id: string; subskill: string; prompt: string }[]
    model: string | null
  }
  oral: { status: string; note: string }
  misconceptions: { logged: { id: string; text: string; subskill: string }[]; resolved: { id: string; text: string; subskill: string }[] }
  mastery: MasteryDelta[]
  next_action: string
  checker: CheckerView | null
}
export type LlmUsage = {
  usage: { backend: string; model: string; count: number; cap: number | null }[]
  backends: { backend: string; count: number; cap: number | null; remaining: number | null }[]
  pending_grades: number
}
export type DomainScore = components['schemas']['DomainScoreView']
export type RunRecord = {
  ok: boolean; ts: string; seconds: number
  steps: { cmd: string; exit_code: number; seconds: number; timed_out: boolean; stderr_tail: string }[]
  files_changed: { path: string; size: number }[]
}
export const REASON_CLASSES = ['too_hard', 'wrong_direction', 'objection', 'broken_env'] as const

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
  nodes: () => req<NodeSummary[]>('/nodes'),
  node: (id: number) => req<NodeView>(`/nodes/${id}`),
  submitPrelab: (id: number, answers: Record<string, string | number>) => post<NodeView>(`/nodes/${id}/prelab/submit`, { answers }),
  openFolder: (id: number, target: 'workspace' | 'backup' = 'workspace') => post<{ path: string }>(`/nodes/${id}/open-folder?target=${target}`),
  runSimulation: (id: number) => post<{ job_id: string }>(`/nodes/${id}/run`),
  latestRun: (id: number) => req<RunRecord | null>(`/nodes/${id}/run`),
  scores: () => req<DomainScore[]>('/scores'),
  runChecker: (id: number) => post<{ job_id: string }>(`/nodes/${id}/checker/run`),
  checker: (id: number) => req<CheckerView | null>(`/nodes/${id}/checker`),
  tutorHistory: (id: number) => req<TutorHistory>(`/nodes/${id}/tutor`),
  tutorChat: (id: number, message: string) => post<TutorReply>(`/nodes/${id}/tutor`, { message }),
  showMe: (id: number, question: string) => post<ShowMeReply>(`/nodes/${id}/show-me`, { question }),
  submitProof: (id: number) => post<{ job_id: string; submission: number }>(`/nodes/${id}/proof/submit`),
  proof: (id: number) => req<ProofView>(`/nodes/${id}/proof`),
  remediation: (id: number, accept: boolean) => post<ProofView>(`/nodes/${id}/proof/remediation`, { accept }),
  oral: (id: number) => req<OralInfo>(`/nodes/${id}/oral`),
  advanceOral: (id: number) => post<DebriefView>(`/nodes/${id}/oral/advance`, { mode: 'text' }),
  debrief: (id: number) => req<DebriefView>(`/nodes/${id}/debrief`),
  completeDebrief: (id: number) => post<NodeSummary>(`/nodes/${id}/debrief/complete`),
  rescope: (id: number, reason_class: string, reason: string) => post<RescopeStarted>(`/nodes/${id}/rescope`, { reason_class, reason }),
  plannerOptions: () => req<PlannerOptions>('/planner/options'),
  generateOptions: () => post<PlannerOptions>('/planner/options'),
  startBuild: (option_index: number) => post<BuildStarted>('/build', { option_index }),
  llmUsage: () => req<LlmUsage>('/llm/usage'),
  pendingGrades: () => req<{ id: number; node_id: number | null; attempts: number; last_error: string | null }[]>('/grading/pending'),
  retryGrading: () => post<{ applied: number }>('/grading/retry'),
  job: (id: string) => req<Job>(`/jobs/${id}`),
  buildRecord: (dir: string) => req<{ edit?: { changes: string; backup: string }; learner_workspace_backup?: string }>(`/build/${dir}`),
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

/** Poll a job until it finishes (the tray shows live progress; this just tells a panel when to refetch). */
export async function waitJob(id: string, everyMs = 1000, onTick?: (j: Job) => void): Promise<Job> {
  for (;;) {
    const j = await api.job(id)
    onTick?.(j)
    if (j.status === 'done' || j.status === 'failed') return j
    await new Promise((r) => setTimeout(r, everyMs))
  }
}
