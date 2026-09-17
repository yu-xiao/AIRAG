import { beforeEach, describe, expect, it } from 'vitest'
import { useTheme } from '@/composables/useTheme'

describe('useTheme', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.classList.remove('dark')
  })

  it('init defaults to light theme', () => {
    const { init, isDark } = useTheme()
    init()
    expect(isDark.value).toBe(false)
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('init restores dark from storage', () => {
    localStorage.setItem('airag_theme', 'dark')
    const { init, isDark } = useTheme()
    init()
    expect(isDark.value).toBe(true)
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('toggle flips and persists', () => {
    const { init, toggle, isDark } = useTheme()
    init()
    toggle()
    expect(isDark.value).toBe(true)
    expect(localStorage.getItem('airag_theme')).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })
})
