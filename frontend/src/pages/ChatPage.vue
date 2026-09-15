<script setup lang="ts">
import { nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
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
import CitationList from '@/components/CitationList.vue'

// ---- markdown 渲染:页面级单例;语言子集 python/ts/json/bash ----
// 注:v-html 前未做 XSS 消毒(内网 + 模型自答内容),DOMPurify 排期在 M4 加固批。
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
  return md.render(src)
}

// ---- 状态 ----
interface ChatMessage {
  id?: number
  role: 'user' | 'assistant'
  content: string
  citations?: Citation[] | null
  /** 该条是否仍在流式生成中(仅本地追问消息使用) */
  pending?: boolean
}

const conversations = ref<ConversationItem[]>([])
const currentId = ref<number | null>(null)
const messages = ref<ChatMessage[]>([])
const kbs = ref<KbItem[]>([])
const selectedKbIds = ref<number[]>([])
const question = ref('')
const streaming = ref(false)
const bottomAnchor = ref<HTMLElement>()

const { ask } = useChatStream()

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
    citations: m.citations,
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

  messages.value.push({ role: 'user', content: q })
  // reactive 保证 token 回调里的高频属性变更能触发视图更新
  const assistant: ChatMessage = reactive({
    role: 'assistant',
    content: '',
    citations: null,
    pending: true,
  })
  messages.value.push(assistant)
  question.value = ''
  streaming.value = true
  scrollToBottom()

  await ask(
    { kbIds: [...selectedKbIds.value], question: q, conversationId: currentId.value ?? undefined },
    {
      onToken(t) {
        assistant.content += t
        scrollToBottom()
      },
      onCitations(c) {
        assistant.citations = c
      },
      onDone(d) {
        assistant.content = d.answer // 权威终稿整体替换流式累积内容
        assistant.pending = false
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

// ---- 输入交互:Enter 发送 / Shift+Enter 换行;IME 组态确认不触发 ----
function onEnterKey(e: KeyboardEvent) {
  if (e.isComposing || e.shiftKey) return
  e.preventDefault()
  onSend()
}

const canSend = () => !streaming.value && question.value.trim().length > 0 && selectedKbIds.value.length > 0

function scrollToBottom() {
  nextTick(() => {
    bottomAnchor.value?.scrollIntoView({ block: 'end' })
  })
}

onMounted(() => {
  loadKbs()
  loadConversations()
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
          {{ c.title }}
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
      </div>

      <div class="chat-messages">
        <el-empty v-if="messages.length === 0" description="选择知识库后开始提问" />
        <div v-for="(m, i) in messages" :key="m.id ?? `local-${i}`" class="msg-row" :class="m.role">
          <div class="bubble">
            <div v-if="m.role === 'assistant'" class="markdown-body" v-html="render(m.content)" />
            <div v-else class="plain-text">{{ m.content }}</div>
            <div v-if="m.pending && !m.content" class="typing">思考中…</div>
            <CitationList
              v-if="!m.pending && m.citations && m.citations.length > 0"
              :citations="m.citations"
            />
          </div>
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
          placeholder="Enter 发送,Shift+Enter 换行"
          @keydown.enter="onEnterKey"
        />
        <el-button type="primary" class="send-btn" :disabled="!canSend()" @click="onSend">
          {{ streaming ? '回答中' : '发送' }}
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
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--el-text-color-primary);
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

/* 气泡:用户右侧 / 助手左侧 */
.msg-row {
  display: flex;
}
.msg-row.user {
  justify-content: flex-end;
}
.msg-row.assistant {
  justify-content: flex-start;
}
.bubble {
  max-width: 78%;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
}
.msg-row.user .bubble {
  background: var(--el-color-primary);
  color: #fff;
}
.msg-row.assistant .bubble {
  background: var(--el-fill-color-light);
}
.plain-text {
  white-space: pre-wrap;
}
.typing {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

/* markdown 渲染区(assistant) */
.markdown-body :deep(p) {
  margin: 0 0 8px;
}
.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}
.markdown-body :deep(pre.hljs) {
  margin: 8px 0;
  padding: 10px 12px;
  border-radius: 4px;
  overflow-x: auto;
  font-size: 13px;
}
.markdown-body :deep(code) {
  font-family: var(--el-font-family);
}
.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--el-border-color);
  padding: 4px 10px;
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
