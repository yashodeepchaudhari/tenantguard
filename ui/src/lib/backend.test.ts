import { describe, expect, it } from 'vitest'
import { errorText, parseBrainMessage } from './backend.ts'

describe('backend helpers', () => {
  it('parses only state / incident frames', () => {
    expect(parseBrainMessage('{"type":"state","ts":1}')?.type).toBe('state')
    expect(parseBrainMessage('{"type":"incident","ts":1}')?.type).toBe('incident')
    expect(parseBrainMessage('{"type":"other"}')).toBeNull()
    expect(parseBrainMessage('not json')).toBeNull()
  })

  it('reads FastAPI error bodies', () => {
    expect(errorText({ detail: 'unknown tenant' }, 'x')).toBe('unknown tenant')
    expect(errorText({ detail: [{ loc: ['body', 'intensity'], msg: 'Input should be less than or equal to 5' }] }, 'x')).toBe(
      'intensity: Input should be less than or equal to 5',
    )
    expect(errorText(undefined, 'fallback')).toBe('fallback')
  })
})
