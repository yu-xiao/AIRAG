import { ref } from 'vue'

const KEY = 'airag_theme'
/** 模块级单例:多处 useTheme() 共享同一份 isDark */
const isDark = ref(false)

function apply(dark: boolean) {
  isDark.value = dark
  document.documentElement.classList.toggle('dark', dark)
}

export function useTheme() {
  /** App 挂载时调用:按 localStorage 恢复主题(默认亮色) */
  function init() {
    apply(localStorage.getItem(KEY) === 'dark')
  }

  function toggle() {
    const next = !isDark.value
    localStorage.setItem(KEY, next ? 'dark' : 'light')
    apply(next)
  }

  return { isDark, init, toggle }
}
