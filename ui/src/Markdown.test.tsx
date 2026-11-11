import '@testing-library/jest-dom/vitest'
import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { Markdown } from './Markdown'

test('renders headings, GFM tables, task lists, highlighted code and math', () => {
  const src = ['## Head', '', '| a | b |', '|---|---|', '| 1 | 2 |', '', '- [x] done', '', '`x`', '', '```python', 'print(1)', '```', '', '$E=mc^2$'].join('\n')
  const { container } = render(<Markdown>{src}</Markdown>)
  expect(screen.getByRole('heading', { name: 'Head' })).toBeInTheDocument()
  expect(screen.getByRole('table')).toBeInTheDocument()
  expect(screen.getByRole('checkbox')).toBeChecked()
  expect(container.querySelector('code.hljs')).not.toBeNull()
  expect(container.querySelector('.katex')).not.toBeNull()
})

test('raw HTML in content stays text and is not rendered as markup', () => {
  const { container } = render(<Markdown>{'<img src="x" onerror="alert(1)"> hi'}</Markdown>)
  expect(container.querySelector('img')).toBeNull()
  expect(container.textContent).toContain('<img src="x"')
})

test('empty or missing content renders an empty block', () => {
  const { container } = render(<Markdown>{null}</Markdown>)
  expect(container.querySelector('.md')).toBeEmptyDOMElement()
})
