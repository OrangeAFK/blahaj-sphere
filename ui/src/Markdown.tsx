import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import 'katex/dist/katex.min.css'

/**
 * The one place markdown is rendered (briefs, prelab, hints, tutor and show-me replies, debrief text).
 * Raw HTML is not enabled (no rehype-raw), so model or author text cannot inject markup; styles live in index.css under `.md`.
 */
export function Markdown({ children, className = '' }: { children?: string | null; className?: string }) {
  return (
    <div className={`md ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex, rehypeHighlight]}>
        {children ?? ''}
      </ReactMarkdown>
    </div>
  )
}
