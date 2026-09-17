import { fetchEventSource } from '@microsoft/fetch-event-source'
import { watch, type Ref } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { parseSSEChunk, type SSEEvent } from '@/utils/sse'
import type { Citation } from '@/api/chat'

/**
 * /api/chat/ask SSE 契约(与后端 app/api/ask.py 一致,T5 评审约定):
 * - 事件序:token* → citations → done;error 为终态(收到即停)
 * - 渲染单一源:token 仅做流式增量显示;done.answer 为权威终稿,
 *   到达后应整体替换消息内容
 * - citations 事件单独携带引用数组
 *
 * 本组合式只做流封装与回调,不做 UI 状态管理(状态在页面)。
 */

/** done 事件负载 */
export interface DonePayload {
  conversation_id: number
  /** 权威终稿:到达后整体替换流式累积的内容 */
  answer: string
  /** M7:拒答标记(refused=true 时前端隐藏引用) */
  refused?: boolean
}

export interface AskPayload {
  kbIds: number[]
  question: string
  conversationId?: number
  /** M4:请求级精排开关;provider 未开时后端直通 */
  rerank?: boolean
}

export interface AskHandlers {
  onToken: (t: string) => void
  onCitations: (c: Citation[]) => void
  onDone: (d: DonePayload) => void
  onError: (msg: string) => void
}

export function useChatStream() {
  let currentCtrl: AbortController | null = null

  async function ask(
    payload: AskPayload,
    handlers: AskHandlers,
    isAborted?: Ref<boolean>,
  ): Promise<void> {
    const ctrl = new AbortController()
    currentCtrl = ctrl
    const stopWatch = isAborted
      ? watch(isAborted, (v) => {
          if (v) ctrl.abort()
        })
      : null

    const dispatch = (ev: SSEEvent): void => {
      if (ev.type === 'token') handlers.onToken(String(ev.data))
      else if (ev.type === 'citations') handlers.onCitations(ev.data as Citation[])
      else if (ev.type === 'done') handlers.onDone(ev.data as DonePayload)
      else if (ev.type === 'error') {
        handlers.onError(String(ev.data))
        ctrl.abort() // error 是终态:收到即停,不再消费后续帧
      }
    }

    try {
      await fetchEventSource('/api/chat/ask', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${useAuthStore().token}`,
        },
        body: JSON.stringify({
          kb_ids: payload.kbIds,
          question: payload.question,
          conversation_id: payload.conversationId ?? null,
          rerank: payload.rerank ?? false,
        }),
        signal: ctrl.signal,
        onmessage(msg) {
          // msg.data 已是单帧负载:JSON.parse 为主路径
          try {
            dispatch(JSON.parse(msg.data) as SSEEvent)
            return
          } catch {
            // JSON.parse 抛错静默跳过,交给 parseSSEChunk 双保险兜底
          }
          for (const ev of parseSSEChunk(`data: ${msg.data}\n\n`)) {
            dispatch(ev)
          }
        },
        onerror(err) {
          if (!ctrl.signal.aborted) {
            handlers.onError(err instanceof Error ? err.message : String(err))
          }
          throw err // 阻止 fetch-event-source 自动重试
        },
      })
    } catch {
      // 错误已通过 onError 回调上报(或为主动中止);ask() 正常返回,
      // 调用方只需处理回调,无需额外 catch。
    } finally {
      stopWatch?.stop()
      if (currentCtrl === ctrl) currentCtrl = null
    }
  }

  /** 中止当前流(页面卸载时调用,防 SSE 泄漏) */
  function abort(): void {
    currentCtrl?.abort()
    currentCtrl = null
  }

  return { ask, abort }
}
