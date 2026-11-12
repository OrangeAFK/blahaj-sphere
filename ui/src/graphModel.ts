import type { GraphEdge, GraphNode } from './api/client'

// Pure helpers for the Graph screen (SPEC §11): layout, colour by status, badge text, remediation edges. No react-flow here.

export const NODE_W = 220
export const NODE_H = 76
const GAP_X = 40
const GAP_Y = 90

export type Pos = { x: number; y: number }
export type Tone = 'green' | 'sky' | 'gray'

/** Colour follows the node's status (SPEC §11): finished = green, in progress = sky, rescoped/abandoned = gray. */
const STATUS_TONE: Record<string, Tone> = {
  done: 'green',
  prelab: 'sky', lab: 'sky', proof: 'sky', oral: 'sky', debrief: 'sky',
  rescoped: 'gray', abandoned: 'gray',
}
export const toneOf = (status: string): Tone => STATUS_TONE[status] ?? 'gray'

export const TONE_CLASS: Record<Tone, string> = {
  green: 'border-emerald-500 bg-emerald-50 text-emerald-900',
  sky: 'border-sky-500 bg-sky-50 text-sky-900',
  gray: 'border-slate-400 bg-slate-100 text-slate-600',
}

/** Badge text: "domain · T<tier>", whichever parts the node has. */
export function badgeOf(n: GraphNode): string | null {
  const tier = n.tier != null ? `T${n.tier}` : null
  const parts = [n.domain, tier].filter(Boolean)
  return parts.length ? parts.join(' · ') : null
}

/** An edge is a remediation edge when its child is a remediation node (the child points at the node it was spawned from). */
export const isRemediation = (e: GraphEdge, byId: Map<number, GraphNode>): boolean => byId.get(e.target)?.kind === 'remediation'

export const REMEDIATION_COLOUR = '#d97706'
export const PLAIN_COLOUR = '#64748b'

/** What one edge looks like: remediation edges are amber, dashed and labelled; the rest are plain grey. */
export function edgeLook(e: GraphEdge, byId: Map<number, GraphNode>) {
  const rem = isRemediation(e, byId)
  return {
    id: `${e.source}-${e.target}`,
    remediation: rem,
    label: rem ? 'remediation' : undefined,
    colour: rem ? REMEDIATION_COLOUR : PLAIN_COLOUR,
    strokeWidth: rem ? 2 : 1.5,
    dash: rem ? '6 4' : undefined,
  }
}

/** Column-per-layer placement. A node's layer is the length of its longest parent chain, so roots sit on top. */
export function layers(nodes: GraphNode[]): Map<number, number> {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const depth = new Map<number, number>()
  const visit = (id: number, stack: Set<number>): number => {
    const known = depth.get(id)
    if (known !== undefined) return known
    if (stack.has(id)) return 0 // a cycle cannot come from the Planner; guard so a bad file never hangs the screen
    stack.add(id)
    const d = byId.get(id)!.parents.reduce((m, p) => (byId.has(p) ? Math.max(m, visit(p, stack) + 1) : m), 0)
    stack.delete(id)
    depth.set(id, d)
    return d
  }
  for (const n of nodes) visit(n.id, new Set())
  return depth
}

/** Top-left corner of each node: one row per layer, rows centred on the widest one, nodes ordered by id. */
export function layout(nodes: GraphNode[]): Map<number, Pos> {
  const depth = layers(nodes)
  const rows = new Map<number, number[]>()
  for (const n of [...nodes].sort((a, b) => a.id - b.id)) {
    const d = depth.get(n.id) ?? 0
    rows.set(d, [...(rows.get(d) ?? []), n.id])
  }
  const widest = Math.max(1, ...[...rows.values()].map((r) => r.length))
  const pos = new Map<number, Pos>()
  for (const [d, ids] of rows) {
    const offset = ((widest - ids.length) * (NODE_W + GAP_X)) / 2
    ids.forEach((id, i) => pos.set(id, { x: offset + i * (NODE_W + GAP_X), y: d * (NODE_H + GAP_Y) }))
  }
  return pos
}
