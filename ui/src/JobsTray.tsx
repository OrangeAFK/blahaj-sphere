import { useEffect, useState } from 'react'
import { subscribeJobs, type Job } from './api/client'

const RECENT_SHOWN = 4

/** Live job list for every screen (SPEC §6.3): active jobs with progress + last log line, then a few finished ones. */
export function JobsTray() {
  const [jobs, setJobs] = useState<Record<string, Job>>({})
  const [connected, setConnected] = useState(false)

  useEffect(
    () => subscribeJobs((j) => setJobs((prev) => ({ ...prev, [j.id]: j })), setConnected),
    [],
  )

  const all = Object.values(jobs).sort((a, b) => b.created_ns - a.created_ns)
  const active = all.filter((j) => j.status === 'queued' || j.status === 'running')
  const recent = all.filter((j) => j.status === 'done' || j.status === 'failed').slice(0, RECENT_SHOWN)

  return (
    <aside aria-label="Jobs" className="fixed inset-x-0 bottom-0 border-t bg-white/95 px-4 py-2 text-sm shadow-lg">
      <div className="mx-auto max-w-xl">
        <div className="flex items-center justify-between">
          <span className="font-semibold">Jobs{active.length > 0 && ` (${active.length} active)`}</span>
          <span className={connected ? 'text-green-600' : 'text-amber-600'}>{connected ? 'live' : 'reconnecting…'}</span>
        </div>
        {active.length === 0 && recent.length === 0 && <p className="text-gray-500">No jobs yet.</p>}
        <ul className="space-y-1">
          {[...active, ...recent].map((j) => (
            <JobRow key={j.id} job={j} />
          ))}
        </ul>
      </div>
    </aside>
  )
}

function JobRow({ job }: { job: Job }) {
  const running = job.status === 'queued' || job.status === 'running'
  const last = job.log_tail[job.log_tail.length - 1] ?? ''
  const color = job.status === 'failed' ? 'text-red-600' : job.status === 'done' ? 'text-green-600' : 'text-blue-600'
  return (
    <li data-testid={`job-${job.id}`}>
      <div className="flex items-center gap-2">
        <span className={`w-16 shrink-0 ${color}`}>{job.status}</span>
        <span className="truncate font-mono">{job.type}</span>
        {job.node_id && <span className="text-gray-500">{job.node_id}</span>}
        {running && (
          <progress className="ml-auto w-24" max={1} value={job.progress} aria-label={`${job.type} progress`} />
        )}
      </div>
      {job.error ? (
        <p className="truncate text-red-600">{job.error}</p>
      ) : (
        last && <p className="truncate font-mono text-xs text-gray-500">{last}</p>
      )}
    </li>
  )
}
