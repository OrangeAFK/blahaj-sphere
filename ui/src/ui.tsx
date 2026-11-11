import type { ButtonHTMLAttributes, ReactNode } from 'react'

/** Shark-fin glyph: the app's one recurring motif. */
export function Fin({ className = 'h-5 w-5' }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true" fill="currentColor">
      <path d="M3 20c4-1 6-5 7-14 3 3 7 8 8 14-3-1-5-1-7 0-2-1-5-1-8 0z" />
    </svg>
  )
}

export function Card({ title, children, right, className = '' }: { title?: ReactNode; children: ReactNode; right?: ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-sky-200 bg-white p-3 shadow-sm ${className}`}>
      {(title || right) && (
        <header className="mb-2 flex items-center justify-between gap-2">
          <h2 className="flex items-center gap-1.5 text-sm font-semibold uppercase tracking-wide text-sky-900">
            <Fin className="h-4 w-4 text-sky-500" />
            {title}
          </h2>
          {right}
        </header>
      )}
      {children}
    </section>
  )
}

export function Btn({ primary, danger, className = '', ...p }: ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean; danger?: boolean }) {
  const tone = primary
    ? 'bg-sky-700 text-white hover:bg-sky-800'
    : danger
      ? 'border border-red-300 text-red-700 hover:bg-red-50'
      : 'border border-sky-300 text-sky-900 hover:bg-sky-50'
  return <button {...p} className={`rounded px-3 py-1 text-sm disabled:opacity-50 ${tone} ${className}`} />
}

export function Err({ e }: { e: string }) {
  return e ? <p role="alert" className="mt-1 text-sm text-red-600">{e}</p> : null
}

export function Badge({ children, tone = 'sky' }: { children: ReactNode; tone?: 'sky' | 'amber' | 'green' | 'red' | 'gray' }) {
  const c = { sky: 'bg-sky-100 text-sky-800', amber: 'bg-amber-100 text-amber-800', green: 'bg-green-100 text-green-800', red: 'bg-red-100 text-red-800', gray: 'bg-gray-100 text-gray-700' }[tone]
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${c}`}>{children}</span>
}

export const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e))
