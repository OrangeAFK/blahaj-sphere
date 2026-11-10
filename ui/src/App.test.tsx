import { render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import App from './App'

afterEach(() => vi.restoreAllMocks())

test('shows health and settings', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
    ok: true,
    json: async () => (url.endsWith('/health')
      ? { status: 'ok' }
      : { openrouter_api_key: '', allow_library_to_free_models: false, claude_model: '' }),
  })))
  render(<App />)
  expect(await screen.findByText('Backend: ok')).toBeInTheDocument()
  expect(await screen.findByText('OpenRouter API key')).toBeInTheDocument()
})
