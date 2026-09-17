import { describe, expect, it } from 'vitest'
import { disambiguateKbNames } from '@/utils/kbLabel'

describe('disambiguateKbNames', () => {
  it('uniquely named kbs keep plain name', () => {
    const m = disambiguateKbNames([
      { id: 1, name: '甲库' },
      { id: 2, name: '乙库' },
    ])
    expect(m.get(1)).toBe('甲库')
    expect(m.get(2)).toBe('乙库')
  })

  it('duplicate names get id suffix on every collision member', () => {
    const m = disambiguateKbNames([
      { id: 3, name: '同名库' },
      { id: 7, name: '乙库' },
      { id: 5, name: '同名库' },
    ])
    expect(m.get(3)).toBe('同名库 ·#3')
    expect(m.get(5)).toBe('同名库 ·#5')
    expect(m.get(7)).toBe('乙库')
  })

  it('empty list yields empty map', () => {
    expect(disambiguateKbNames([]).size).toBe(0)
  })
})
