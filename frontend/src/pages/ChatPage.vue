<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Download } from '@element-plus/icons-vue'
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
const selectedKbIds = ref<number[]>([])
const question = ref('')
const streaming = ref(false)
const bottomAnchor = ref<HTMLElement>()
/** M4:请求级精排开关;M7 起默认开(阈值门控生效);localStorage 记忆用户选择 */
const rerankEnabled = ref(
  localStorage.getItem('airag_rerank') === null
    ? true
    : localStorage.getItem('airag_rerank') === '1',
)

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
        新对话
      </el-button>
      <div class="conv-list">
        <div
          v-for="c in conversations"
          :key="c.id"
          class="conv-item"
          :class="{ active: c.id === currentId }"
          @click="openConversation(c)"
        >
          <span class="conv-title" :title="c.title">{{ c.title }}</span>
          <el-icon class="conv-export" :size="14" @click.stop="exportConversation(c)">
            <Download />
          </el-icon>
          <el-icon class="conv-delete" :size="14" @click.stop="removeConversation(c)">
            <Delete />
          </el-icon>
        </div>
        <el-empty v-if="conversations.length === 0" description="暂无会话" :image-size="48" />
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
          placeholder="至少选择 1 个知识库"
          class="kb-select"
          :disabled="streaming"
        >
          <el-option v-for="k in kbs" :key="k.id" :label="k.name" :value="k.id" />
        </el-select>
        <el-switch
          :model-value="rerankEnabled"
          label="精排"
          active-text="精排"
          @change="onRerankChange"
          @update:model-value="rerankEnabled = $event"
        />
      </div>

      <div class="chat-messages">
        <el-empty v-if="messages.length === 0" description="选择知识库后开始提问" />
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

      <div class="chat-input">
        <el-input
          v-model="question"
          type="textarea"
          :rows="3"
          resize="none"
          :disabled="streaming"
          placeholder="Enter 或 Alt+Enter 发送,Shift+Enter 换行"
          @keydown.enter="onEnterKey"
        />
        <el-button
          v-if="streaming"
          type="danger"
          plain
          class="send-btn"
          @click="abort()"
        >
          停止
        </el-button>
        <el-button v-else type="primary" class="send-btn" :disabled="!canSend()" @click="onSend">
          发送
        </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  gap: 12px;
  height: 100%;
  min-height: 0;
}

/* 左栏:会话列表 */
.chat-side {
  display: flex;
  flex-direction: column;
  width: 200px;
  flex-shrink: 0;
  gap: 8px;
}
.new-chat {
  width: 100%;
}
.conv-list {
  flex: 1;
  overflow-y: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  padding: 4px;
}
.conv-item {
  padding: 8px 10px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
  color: var(--el-text-color-primary);
  display: flex;
  align-items: center;
  gap: 4px;
}
.conv-title {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.conv-delete {
  flex-shrink: 0;
  color: var(--el-text-color-secondary);
  visibility: hidden;
}
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
  color: var(--el-color-primary);
}

/* 主区:KB 选择 + 消息 + 输入 */
.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.chat-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.kb-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.kb-select {
  min-width: 280px;
}
.chat-messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.chat-messages :deep(.el-empty) {
  margin: auto;
}

/* 气泡:用户右侧 / 助手左侧(助手气泡与 markdown 样式已迁入 AssistantMessage 组件) */
.msg-row {
  display: flex;
  gap: var(--app-spacing-sm);
  align-items: flex-start;
}
.msg-row.user {
  justify-content: flex-end;
}
.msg-row.user .bubble {
  background: var(--el-color-primary);
  color: #fff;
  max-width: 78%;
  padding: var(--app-spacing-sm) var(--app-spacing-md);
  border-radius: var(--app-radius);
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

/* 输入区 */
.chat-input {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}
.chat-input :deep(.el-textarea) {
  flex: 1;
}
.send-btn {
  height: 32px;
}
</style>
