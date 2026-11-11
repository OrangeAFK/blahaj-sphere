import { render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import App from './App'

afterEach(() => { vi.restoreAllMocks(); window.location.hash = '' })

test('shows health and the Home screen', async () => {
  vi.stubGlobal('EventSource', class { addEventListener() {} close() {} })  // JobsTray subscribes on mount
  vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
    ok: true,
    json: async () => (url.endsWith('/health') ? { status: 'ok' } : url.endsWith('/nodes') || url.endsWith('/scores') ? [] : {}),
  })))
  render(<App />)
  expect(await screen.findByText('Backend: ok')).toBeInTheDocument()
  expect(await screen.findByText('Plan the next node')).toBeInTheDocument()
})

test('settings page shows the key field and free-cap usage', async () => {
  window.location.hash = '#/settings'
  vi.stubGlobal('EventSource', class { addEventListener() {} close() {} })
  vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
    ok: true,
    json: async () => (url.endsWith('/settings') ? { openrouter_api_key: '', allow_library_to_free_models: false, claude_model: '' }
      : url.endsWith('/llm/usage') ? { usage: [{ backend: 'openrouter', model: 'x:free', count: 12, cap: 50 }, { backend: 'claude_code', model: '', count: 3, cap: null }], backends: [{ backend: 'openrouter', count: 12, cap: 50, remaining: 38 }, { backend: 'claude_code', count: 3, cap: null, remaining: null }], pending_grades: 0 }
      : { status: 'ok' }),
  })))
  render(<App />)
  expect(await screen.findByText('OpenRouter API key')).toBeInTheDocument()
  expect(await screen.findByTestId('caps')).toHaveTextContent('openrouter: 38 of 50')
  expect(screen.getByText('x:free')).toBeInTheDocument()
})
