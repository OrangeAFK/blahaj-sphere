import { expect, test } from 'vitest'
import type { GraphNode } from './api/client'
import { badgeOf, edgeLook, isRemediation, layout, layers, NODE_H, NODE_W, toneOf } from './graphModel'

const node = (id: number, over: Partial<GraphNode> = {}): GraphNode => ({
  id, dir: `000${id}`, title: `N${id}`, kind: 'challenge', domain: 'rfic', tier: 3, status: 'done', provisional: false,
  outcome: null, size_est_min: 30, subskills: [], parents: [], ...over,
})

test('layers: a node sits one layer below its deepest parent; roots are layer 0', () => {
  const nodes = [node(1), node(2, { parents: [1] }), node(3, { parents: [1, 2] }), node(4, { parents: [1] })]
  expect(layers(nodes)).toEqual(new Map([[1, 0], [2, 1], [3, 2], [4, 1]]))
})

test('layers: parents missing from the set are ignored, and a cycle terminates', () => {
  const nodes = [node(1, { parents: [99] }), node(2, { parents: [3] }), node(3, { parents: [2] })]
  const d = layers(nodes)
  expect(d.get(1)).toBe(0)
  expect([d.get(2), d.get(3)].every((x) => typeof x === 'number')).toBe(true)
})

test('layout: one row per layer, rows centred, nodes in id order, no overlaps', () => {
  const nodes = [node(1), node(2, { parents: [1] }), node(3, { parents: [1] }), node(4, { parents: [2] })]
  const pos = layout(nodes)
  expect(pos.get(1)!.y).toBe(0)
  expect(pos.get(2)!.y).toBe(pos.get(3)!.y)
  expect(pos.get(4)!.y).toBeGreaterThan(pos.get(2)!.y)
  // the single root sits in the middle of the two-wide row below it
  expect(pos.get(1)!.x).toBeCloseTo((pos.get(2)!.x + pos.get(3)!.x) / 2)
  expect(pos.get(3)!.x - pos.get(2)!.x).toBeGreaterThanOrEqual(NODE_W)
  expect(pos.get(4)!.y - pos.get(2)!.y).toBeGreaterThanOrEqual(NODE_H)
})

test('colour follows status; unknown statuses fall back to gray', () => {
  expect(toneOf('done')).toBe('green')
  for (const s of ['prelab', 'lab', 'proof', 'oral', 'debrief']) expect(toneOf(s)).toBe('sky')
  expect(toneOf('rescoped')).toBe('gray')
  expect(toneOf('abandoned')).toBe('gray')
  expect(toneOf('mystery')).toBe('gray')
})

test('badge is domain · tier, with whichever part is missing dropped', () => {
  expect(badgeOf(node(1))).toBe('rfic · T3')
  expect(badgeOf(node(1, { tier: null }))).toBe('rfic')
  expect(badgeOf(node(1, { domain: null }))).toBe('T3')
  expect(badgeOf(node(1, { domain: null, tier: null }))).toBeNull()
})

test('an edge is remediation when its child is a remediation node', () => {
  const byId = new Map([node(1), node(2, { kind: 'remediation', parents: [1] }), node(3, { parents: [1] })].map((n) => [n.id, n]))
  expect(isRemediation({ source: 1, target: 2 }, byId)).toBe(true)
  expect(isRemediation({ source: 1, target: 3 }, byId)).toBe(false)
})

test('remediation edges are amber, dashed and labelled; ordinary edges are plain grey and unlabelled', () => {
  const byId = new Map([node(1), node(2, { kind: 'remediation', parents: [1] }), node(3, { parents: [1] })].map((n) => [n.id, n]))
  const rem = edgeLook({ source: 1, target: 2 }, byId)
  expect(rem).toMatchObject({ id: '1-2', remediation: true, label: 'remediation', colour: '#d97706', dash: '6 4', strokeWidth: 2 })
  const plain = edgeLook({ source: 1, target: 3 }, byId)
  expect(plain).toMatchObject({ id: '1-3', remediation: false, colour: '#64748b', strokeWidth: 1.5 })
  expect(plain.label).toBeUndefined()
  expect(plain.dash).toBeUndefined()
})
