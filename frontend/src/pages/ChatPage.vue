<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ChatDotRound, Delete, Download, Plus, Promotion, VideoPause } from '@element-plus/icons-vue'
import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'
import python from 'highlight.js/lib/languages/python'
import typescript from 'highlight.js/lib/languages/typescript'
import 'highlight.js/styles/github.css'
import { conversationsApi, type Citation, type ConversationItem, type MessageItem } from '@/api/chat'
import { kbApi, type KbItem } from '@/api/kb'
import { disambiguateKbNames } from '@/utils/kbLabel'
import { useChatStream } from '@/composables/useChatStream'
import { useAuthStore } from '@/stores/auth'
import { throttle } from '@/utils/throttle'
import AssistantMessage from '@/components/AssistantMessage.vue'

// ---- markdown 渲染:页面级单例;语言子集 python/ts/json/bash ----
// 注:M4 起 v-html 前统一过 DOMPurify(markdown html:false 之外的第二道防线)。
hljs.registerLanguage('python', python)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('json', json)
hljs.registerLanguage('bash', bash)

const md = new MarkdownIt({
  breaks: true,
  html: false, // 原始 HTML 一律转义,作为无 DOMPurify 时的兜底防线
  highlight(str: string, lang: string): string {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang }).value}</code></pre>`
      } catch {
        /* 高亮失败回退到默认转义 */
      }
    }
    return '' // 空串 → markdown-it 走自身转义路径
  },
})

function render(src: string): string {
  return DOMPurify.sanitize(md.render(src))
}

// ---- 状态 ----
const route = useRoute()
const auth = useAuthStore()

interface ChatMessage {
  id?: number
  role: 'user' | 'assistant'
  content: string
  /** 渲染缓存:消息创建/节流器写入,模板只 v-html m.html(避免每帧全量重渲) */
  html?: string
  citations?: Citation[] | null
  /** 该条是否仍在流式生成中(仅本地追问消息使用) */
  pending?: boolean
  /** M7:拒答标记(refused=true 时隐藏 markdown 渲染与引用) */
  refused?: boolean
}

const conversations = ref<ConversationItem[]>([])
const currentId = ref<number | null>(null)
const messages = ref<ChatMessage[]>([])
const kbs = ref<KbItem[]>([])
const kbLabels = computed(() => disambiguateKbNames(kbs.value))
const selectedKbIds = ref<number[]>([])
const question = ref('')
const streaming = ref(false)
const bottomAnchor = ref<HTMLElement>()
const inputRef = ref<{ focus: () => void }>()
/** M4:请求级精排开关;M7 起默认开(阈值门控生效);localStorage 记忆用户选择 */
const rerankEnabled = ref(
  localStorage.getItem('airag_rerank') === null
    ? true
    : localStorage.getItem('airag_rerank') === '1',
)

// ---- 空态 Hero:问候 + 示例问题(点击仅填入输入框,不代发) ----
const greeting = computed(() => {
  const h = new Date().getHours()
  if (h < 6) return '夜深了'
  if (h < 12) return '早上好'
  if (h < 18) return '下午好'
  return '晚上好'
})

const EXAMPLE_QUESTIONS = [
  '帮我总结知识库的核心内容',
  '文档里提到了哪些关键数字?',
  '根据资料,主要的流程或结论是什么?',
]

function useExample(q: string) {
  question.value = q
  inputRef.value?.focus()
}

function onRerankChange(v: boolean) {
  localStorage.setItem('airag_rerank', v ? '1' : '0')
}

const { ask, abort } = useChatStream()

// ---- 加载 ----
async function loadKbs() {
  try {
    kbs.value = await kbApi.list()
  } catch {
    ElMessage.error('加载知识库列表失败')
  }
}

async function loadConversations() {
  try {
    conversations.value = await conversationsApi.list()
  } catch {
    ElMessage.error('加载会话列表失败')
  }
}

function toChatMessage(m: MessageItem): ChatMessage {
  return {
    id: m.id,
    role: m.role === 'user' ? 'user' : 'assistant',
    content: m.content,
    html: render(m.content),
    citations: m.citations,
    refused: m.refused ?? false,
  }
}

function guardStreaming(): boolean {
  if (streaming.value) {
    ElMessage.warning('回答生成中,请稍候')
    return true
  }
  return false
}

// ---- 会话切换 / 新对话 ----
async function openConversation(c: ConversationItem) {
  if (guardStreaming() || c.id === currentId.value) return
  currentId.value = c.id
  selectedKbIds.value = [...c.kb_ids]
  messages.value = []
  try {
    messages.value = (await conversationsApi.messages(c.id)).map(toChatMessage)
  } catch {
    ElMessage.error('加载历史消息失败')
  }
  scrollToBottom()
}

function newConversation() {
  if (guardStreaming()) return
  currentId.value = null
  messages.value = []
  // selectedKbIds 保留(组件级缓存,便于连续提问)
}

async function removeConversation(c: ConversationItem) {
  if (guardStreaming()) return
  try {
    await ElMessageBox.confirm(`删除会话「${c.title}」?历史消息将一并删除。`, '删除会话', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await conversationsApi.remove(c.id)
    if (currentId.value === c.id) {
      currentId.value = null
      messages.value = []
    }
    await loadConversations()
  } catch {
    ElMessage.error('删除会话失败')
  }
}

async function exportConversation(c: ConversationItem) {
  try {
    const blob = await conversationsApi.export(c.id)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `conv-${c.id}.md`
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    ElMessage.error('导出失败')
  }
}

/** 会话列表相对时间:刚刚/N 分钟前/N 小时前/昨天/日期(同年省年份) */
function fmtRelative(iso: string): string {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const m = Math.floor((Date.now() - t) / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  if (h < 48) return '昨天'
  const d = new Date(t)
  const pad = (n: number) => String(n).padStart(2, '0')
  return d.getFullYear() === new Date().getFullYear()
    ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// ---- 发送与 SSE 渲染契约 ----
// token → 增量追加;done → 用 done.answer 整体替换(权威对账);
// citations → 存入当前助手消息;error → 终止流 + ElMessage。
async function onSend() {
  const q = question.value.trim()
  if (streaming.value || !q) return
  if (selectedKbIds.value.length === 0) {
    ElMessage.warning('请至少选择一个知识库')
    return
  }

  messages.value.push({ role: 'user', content: q, html: render(q) })
  // reactive 保证 token 回调里的高频属性变更能触发视图更新
  const assistant: ChatMessage = reactive({
    role: 'assistant',
    content: '',
    html: '',
    citations: null,
    pending: true,
  })
  messages.value.push(assistant)
  question.value = ''
  streaming.value = true
  scrollToBottom()

  // 节流渲染:token 高频追加只在 120ms 窗口内合并渲染一次
  const flushRender = throttle(() => {
    assistant.html = render(assistant.content)
  }, 120)

  await ask(
    {
      kbIds: [...selectedKbIds.value],
      question: q,
      conversationId: currentId.value ?? undefined,
      rerank: rerankEnabled.value,
    },
    {
      onToken(t) {
        assistant.content += t
        flushRender()
        scrollToBottom()
      },
      onCitations(c) {
        assistant.citations = c
      },
      onDone(d) {
        assistant.content = d.answer // 权威终稿整体替换流式累积内容
        assistant.html = render(d.answer)
        assistant.pending = false
        assistant.refused = d.refused ?? false
        if (currentId.value === null) {
          currentId.value = d.conversation_id
          loadConversations() // 首问建会话,侧栏同步
        }
      },
      onError(msg) {
        assistant.pending = false
        ElMessage.error(`回答失败:${msg}`)
      },
    },
  )
  streaming.value = false
  assistant.pending = false
  scrollToBottom()
}

// ---- 输入交互:Enter/Alt+Enter 发送 / Shift+Enter 换行;IME 组态确认不触发 ----
function onEnterKey(e: KeyboardEvent) {
  if (e.isComposing || e.shiftKey) return
  if (e.altKey || e.metaKey || e.key === 'Enter') {
    e.preventDefault()
    onSend()
  }
}

const canSend = () => !streaming.value && question.value.trim().length > 0 && selectedKbIds.value.length > 0

function scrollToBottom() {
  nextTick(() => {
    bottomAnchor.value?.scrollIntoView({ block: 'end' })
  })
}

onMounted(async () => {
  loadKbs()
  await loadConversations()
  const cv = Number(route.query.conv)
  if (cv) {
    const target = conversations.value.find((c) => c.id === cv)
    if (target) openConversation(target)
  }
})

onUnmounted(() => {
  // 离开页面时中止进行中的 SSE 流,防后台悬挂
  abort()
})
</script>

<template>
  <div class="chat-page">
    <aside class="chat-side">
      <el-button type="primary" class="new-chat" :disabled="streaming" @click="newConversation">
        <el-icon style="margin-right: 6px"><Plus /></el-icon>新对话
      </el-button>
      <div class="conv-list">
        <div
          v-for="c in conversations"
          :key="c.id"
          class="conv-item"
          :class="{ active: c.id === currentId }"
          @click="openConversation(c)"
        >
          <div class="conv-row">
            <el-icon class="conv-icon" :size="14"><ChatDotRound /></el-icon>
            <span class="conv-title" :title="c.title">{{ c.title }}</span>
            <el-icon class="conv-export" :size="13" @click.stop="exportConversation(c)">
              <Download />
            </el-icon>
            <el-icon class="conv-delete" :size="13" @click.stop="removeConversation(c)">
              <Delete />
            </el-icon>
          </div>
          <div class="conv-time">{{ fmtRelative(c.created_at) }}</div>
        </div>
        <div v-if="conversations.length === 0" class="conv-empty">
          <el-icon :size="20"><ChatDotRound /></el-icon>
          <span>暂无会话,发起第一问吧</span>
        </div>
      </div>
    </aside>

    <div class="chat-main">
      <div class="chat-toolbar">
        <span class="kb-label">知识库</span>
        <el-select
          v-model="selectedKbIds"
          multiple
          filterable
          collapse-tags
          collapse-tags-tooltip
          placeholder="至少选择 1 个知识库"
          class="kb-select"
          :disabled="streaming"
        >
          <el-option v-for="k in kbs" :key="k.id" :label="kbLabels.get(k.id) ?? k.name" :value="k.id" />
        </el-select>
        <el-tooltip content="重排序提升检索相关性,每次提问多一次轻量调用" placement="bottom">
          <div class="rerank-group">
            <span class="rerank-label">精排</span>
            <el-switch
              size="small"
              :model-value="rerankEnabled"
              @change="onRerankChange"
              @update:model-value="rerankEnabled = $event"
            />
          </div>
        </el-tooltip>
      </div>

      <div class="chat-messages">
        <div v-if="messages.length === 0" class="chat-hero">
          <div class="hero-mark">AI</div>
          <h3>{{ greeting }},{{ auth.user?.username ?? '' }}</h3>
          <p>回答基于所选知识库的文档,并附引用溯源;库里没有的内容会明确告知。</p>
          <div class="hero-chips">
            <el-button v-for="q in EXAMPLE_QUESTIONS" :key="q" round size="small" @click="useExample(q)">
              {{ q }}
            </el-button>
          </div>
        </div>
        <div v-for="(m, i) in messages" :key="m.id ?? `local-${i}`" class="msg-row" :class="m.role">
          <span v-if="m.role === 'user'" class="avatar user-avatar">
            {{ auth.user?.username?.slice(0, 1).toUpperCase() ?? '?' }}
          </span>
          <AssistantMessage
            v-if="m.role === 'assistant'"
            :html="m.html"
            :content="m.content"
            :pending="m.pending"
            :refused="m.refused"
            :citations="m.citations"
          />
          <div v-else class="bubble">{{ m.content }}</div>
        </div>
        <div ref="bottomAnchor"></div>
      </div>

      <div class="composer-wrap">
        <div class="composer">
          <el-input
            ref="inputRef"
            v-model="question"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 4 }"
            resize="none"
            :disabled="streaming"
            placeholder="输入你的问题…"
            @keydown.enter="onEnterKey"
          />
          <el-button
            v-if="streaming"
            type="danger"
            plain
            circle
            :icon="VideoPause"
            class="send-fab"
            aria-label="停止生成"
            @click="abort()"
          />
          <el-button
            v-else
            type="primary"
            circle
            :icon="Promotion"
            class="send-fab"
            aria-label="发送"
            :disabled="!canSend()"
            @click="onSend"
          />
        </div>
        <div class="composer-hint">Enter 发送 · Shift+Enter 换行 · 回答基于所选知识库并附引用</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  gap: var(--app-spacing-md);
  height: 100%;
  min-height: 0;
}

/* 左栏:会话列表 */
.chat-side {
  display: flex;
  flex-direction: column;
  width: 216px;
  flex-shrink: 0;
  gap: var(--app-spacing-sm);
}
.new-chat {
  width: 100%;
}
.conv-list {
  flex: 1;
  overflow-y: auto;
  background: var(--app-card-bg);
  border: 1px solid var(--app-card-border);
  border-radius: var(--app-radius);
  padding: var(--app-spacing-sm);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.conv-item {
  padding: 8px 10px;
  border-radius: var(--app-radius-sm);
  cursor: pointer;
  color: var(--el-text-color-primary);
}
.conv-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.conv-icon {
  flex-shrink: 0;
  color: var(--el-text-color-secondary);
}
.conv-title {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 13px;
}
.conv-time {
  margin-left: 20px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  opacity: 0.85;
}
.conv-delete,
.conv-export {
  flex-shrink: 0;
  color: var(--el-text-color-secondary);
  visibility: hidden;
}
.conv-item:hover .conv-export,
.conv-item:hover .conv-delete {
  visibility: visible;
}
.conv-export:hover {
  color: var(--el-color-primary);
}
.conv-delete:hover {
  color: var(--el-color-danger);
}
.conv-item:hover {
  background: var(--el-fill-color-light);
}
.conv-item.active {
  background: var(--el-color-primary-light-9);
}
.conv-item.active .conv-title,
.conv-item.active .conv-icon {
  color: var(--el-color-primary);
}
.conv-empty {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 24px 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

/* 主区:KB 选择 + 消息 + 输入 */
.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--app-spacing-sm);
}
.chat-toolbar {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-sm);
  background: var(--app-card-bg);
  border: 1px solid var(--app-card-border);
  border-radius: var(--app-radius);
  padding: 8px 12px;
  box-shadow: var(--app-shadow-card);
}
.kb-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
}
.kb-select {
  flex: 1;
  min-width: 280px;
}
.rerank-group {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  cursor: default;
}
.rerank-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.chat-messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: var(--app-card-bg);
  border: 1px solid var(--app-card-border);
  border-radius: var(--app-radius);
  padding: var(--app-spacing-lg);
  display: flex;
  flex-direction: column;
  gap: var(--app-spacing-md);
}

/* 空态 Hero:品牌标 + 问候 + 示例问题 */
.chat-hero {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--app-spacing-sm);
  text-align: center;
  padding: var(--app-spacing-xl);
  max-width: 520px;
}
.hero-mark {
  width: 56px;
  height: 56px;
  border-radius: 16px;
  background: linear-gradient(135deg, var(--el-color-primary), var(--el-color-primary-light-3));
  color: #fff;
  font-size: 20px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 16px var(--el-color-primary-light-7);
}
.chat-hero h3 {
  margin: 4px 0 0;
}
.chat-hero p {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.hero-chips {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: var(--app-spacing-sm);
  margin-top: var(--app-spacing-sm);
}
.hero-chips :deep(.el-button) {
  transition: transform 0.15s ease;
}
.hero-chips :deep(.el-button:hover) {
  transform: translateY(-1px);
}

/* 气泡:用户右侧(主色渐变) / 助手左侧(卡片,样式在 AssistantMessage 组件) */
.msg-row {
  display: flex;
  gap: var(--app-spacing-sm);
  align-items: flex-start;
  animation: msg-in 0.25s ease-out;
}
.msg-row.user {
  justify-content: flex-end;
}
.msg-row.user .bubble {
  background: linear-gradient(135deg, var(--el-color-primary), var(--el-color-primary-light-5));
  color: #fff;
  max-width: 78%;
  padding: var(--app-spacing-sm) var(--app-spacing-md);
  border-radius: 12px 12px 4px 12px;
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
  /* 保留 Shift+Enter 多行输入的换行(旧 .plain-text 语义,brief 遗漏系笔误) */
  white-space: pre-wrap;
}
.avatar {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  font-size: 12px;
  font-weight: 600;
}
.user-avatar {
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary);
  order: 2; /* 用户消息头像在气泡右侧 */
}
@keyframes msg-in {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

/* 输入区:composer 卡片 + 圆形发送钮 + 快捷键提示 */
.composer-wrap {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.composer {
  display: flex;
  align-items: flex-end;
  gap: var(--app-spacing-sm);
  background: var(--app-card-bg);
  border: 1px solid var(--app-card-border);
  border-radius: var(--app-radius);
  padding: 8px 8px 8px 12px;
  transition:
    border-color 0.2s,
    box-shadow 0.2s;
}
.composer:focus-within {
  border-color: var(--el-color-primary);
  box-shadow: 0 0 0 2px var(--el-color-primary-light-8);
}
.composer :deep(.el-textarea) {
  flex: 1;
}
.composer :deep(.el-textarea__inner) {
  border: none;
  background: transparent;
  box-shadow: none !important; /* 描边交给 composer:focus-within */
  padding: 6px 0;
}
.send-fab {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
}
.composer-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: center;
}

@media (prefers-reduced-motion: reduce) {
  .msg-row {
    animation: none;
  }
  .hero-chips :deep(.el-button) {
    transition: none;
  }
}
</style>
