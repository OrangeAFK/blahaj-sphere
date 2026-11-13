import { Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, type GraphNode, type GraphView } from './api/client'
import { badgeOf, edgeLook, layout, NODE_H, NODE_W, REMEDIATION_COLOUR, TONE_CLASS, toneOf } from './graphModel'
import { Badge, Card, Err, errMsg } from './ui'

type CardData = { node: GraphNode; selected: boolean }

/** One node of the DAG: title, status, and the domain/tier badge. Handles are hidden: edges are drawn, nobody drags them. */
function GraphCard({ data }: NodeProps<Node<CardData>>) {
  const { node: n, selected } = data
  const badge = badgeOf(n)
  return (
    <div
      style={{ width: NODE_W, height: NODE_H }}
      className={`flex flex-col justify-center rounded-lg border-2 px-3 py-1 shadow-sm ${TONE_CLASS[toneOf(n.status)]} ${selected ? 'ring-2 ring-sky-600' : ''}`}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} className="!opacity-0" />
      <div className="truncate text-sm font-semibold">{n.title ?? n.dir}</div>
      <div className="mt-1 flex items-center gap-1 text-xs">
        <span>{n.status || 'unknown'}</span>
        {n.kind === 'remediation' && <span className="font-semibold text-amber-700">remediation</span>}
        {badge && <Badge tone="gray">{badge}</Badge>}
      </div>
      <Handle type="source" position={Position.Bottom} isConnectable={false} className="!opacity-0" />
    </div>
  )
}

// Stable reference: react-flow re-mounts every node when this object identity changes.
const nodeTypes = { card: GraphCard }

const LEGEND: { tone: 'green' | 'sky' | 'gray'; label: string }[] = [
  { tone: 'green', label: 'done' },
  { tone: 'sky', label: 'in progress' },
  { tone: 'gray', label: 'rescoped / abandoned' },
]

/** Read-only details of the clicked node. Everything shown comes from the graph payload; nothing here changes data. */
function NodeDetail({ node }: { node: GraphNode | undefined }) {
  if (!node) return <Card title="Node"><p className="text-sm text-gray-500">Click a node to see its details.</p></Card>
  const row = (label: string, value: ReactNode) => (
    <div className="flex gap-2 text-sm"><dt className="w-24 shrink-0 text-gray-500">{label}</dt><dd className="min-w-0 break-words">{value}</dd></div>
  )
  return (
    <Card title={node.title ?? node.dir} right={node.provisional ? <Badge tone="amber">provisional</Badge> : undefined}>
      <dl className="space-y-1">
        {row('Status', node.status || 'unknown')}
        {row('Outcome', node.outcome ? node.outcome.replace(/_/g, ' ') : '—')}
        {row('Kind', node.kind ?? '—')}
        {row('Domain · tier', badgeOf(node) ?? '—')}
        {row('Estimate', node.size_est_min != null ? `~${node.size_est_min} min` : '—')}
        {row('Subskills', node.subskills.length ? node.subskills.join(', ') : '—')}
        {row('Parents', node.parents.length ? node.parents.map((p) => `#${p}`).join(', ') : 'none')}
        {row('Folder', node.dir)}
      </dl>
      <a href={`#/node/${node.id}/view`} className="mt-2 inline-block text-sm text-sky-800 underline">Open read-only node view</a>
    </Card>
  )
}

/** Graph screen (SPEC §11): the active track's node DAG. The data is the track's, so the screen reloads whenever `track` changes. */
export function Graph({ track }: { track: string }) {
  // Results and the selection are keyed by track: after a switch the old track's data is ignored until the new one arrives.
  const [loaded, setLoaded] = useState<{ track: string; view: GraphView | null; err: string }>({ track: '', view: null, err: '' })
  const [picked, setPicked] = useState<{ track: string; id: number } | null>(null)
  const view = loaded.track === track ? loaded.view : null
  const err = loaded.track === track ? loaded.err : ''
  const selectedId = picked?.track === track ? picked.id : null

  useEffect(() => {
    let live = true
    api.graph()
      .then((v) => { if (live) setLoaded({ track, view: v, err: '' }) })
      .catch((e) => { if (live) setLoaded({ track, view: null, err: errMsg(e) }) })
    return () => { live = false }
  }, [track])

  const nodes = useMemo<Node<CardData>[]>(() => {
    if (!view) return []
    const pos = layout(view.nodes)
    return view.nodes.map((n) => ({
      id: String(n.id), type: 'card', position: pos.get(n.id) ?? { x: 0, y: 0 }, width: NODE_W, height: NODE_H,
      data: { node: n, selected: n.id === selectedId }, selected: n.id === selectedId, draggable: false,
    }))
  }, [view, selectedId])

  const edges = useMemo<Edge[]>(() => {
    if (!view) return []
    const byId = new Map(view.nodes.map((n) => [n.id, n]))
    return view.edges.map((e) => {
      const look = edgeLook(e, byId)
      return {
        id: look.id, source: String(e.source), target: String(e.target), type: 'smoothstep',
        label: look.label, labelStyle: { fill: REMEDIATION_COLOUR, fontSize: 11, fontWeight: 600 },
        style: { stroke: look.colour, strokeWidth: look.strokeWidth, strokeDasharray: look.dash },
        markerEnd: { type: MarkerType.ArrowClosed, color: look.colour },
      }
    })
  }, [view])

  if (err) return <Err e={err} />
  if (!view) return <p className="text-sm text-gray-500">Loading graph…</p>
  if (!view.nodes.length) {
    return <Card title="Graph"><p className="text-sm text-gray-600">No nodes in this track yet. The Planner's first build will appear here.</p></Card>
  }

  const selected = view.nodes.find((n) => n.id === selectedId)
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-4 text-xs text-gray-700" aria-label="Legend">
        {LEGEND.map((l) => (
          <span key={l.label} className="flex items-center gap-1">
            <span className={`inline-block h-3 w-3 rounded border-2 ${TONE_CLASS[l.tone]}`} />
            {l.label}
          </span>
        ))}
        <span className="flex items-center gap-1">
          <svg width="28" height="8" aria-hidden="true"><line x1="0" y1="4" x2="28" y2="4" stroke={REMEDIATION_COLOUR} strokeWidth="2" strokeDasharray="6 4" /></svg>
          remediation edge
        </span>
      </div>
      <div className="grid gap-3 lg:grid-cols-[1fr_20rem]">
        <div className="h-[34rem] overflow-hidden rounded-lg border border-sky-200 bg-white">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            minZoom={0.2}
            nodesConnectable={false}
            nodesDraggable={false}
            onNodeClick={(_, n) => setPicked({ track, id: Number(n.id) })}
            onPaneClick={() => setPicked(null)}
          >
            <Background />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
        <NodeDetail node={selected} />
      </div>
    </div>
  )
}
