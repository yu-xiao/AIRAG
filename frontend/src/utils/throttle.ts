/** 首次立即执行 + 窗口内合并 + 尾随补发的节流(流式 markdown 渲染用)。 */
export function throttle<F extends (...args: never[]) => void>(fn: F, ms: number) {
  let last = 0
  let timer: number | undefined
  const wrapped = (...args: Parameters<F>) => {
    const now = Date.now()
    if (now - last >= ms) {
      last = now
      fn(...args)
    } else if (timer === undefined) {
      timer = window.setTimeout(() => {
        timer = undefined
        last = Date.now()
        fn(...args)
      }, ms - (now - last))
    }
  }
  return wrapped
}
