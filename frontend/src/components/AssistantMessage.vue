<script setup lang="ts">
import { InfoFilled } from '@element-plus/icons-vue'
import type { Citation } from '@/api/chat'
import CitationList from '@/components/CitationList.vue'

defineProps<{
  html?: string
  content: string
  pending?: boolean
  refused?: boolean
  citations?: Citation[] | null
}>()
</script>

<template>
  <div class="assistant-row">
    <span class="avatar bot-avatar">AI</span>
    <div class="bubble" :class="{ refused }">
      <div v-if="refused" class="refusal">
        <el-icon><InfoFilled /></el-icon>
        <span>{{ content }}</span>
      </div>
      <div v-else class="markdown-body" v-html="html" />
      <div v-if="pending && !content" class="typing">
        <span class="dot" /><span class="dot" /><span class="dot" />
      </div>
      <CitationList
        v-if="!pending && !refused && citations && citations.length > 0"
        :citations="citations"
      />
    </div>
  </div>
</template>

<style scoped>
.assistant-row {
  display: flex;
  gap: var(--app-spacing-sm);
  align-items: flex-start;
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
.bot-avatar {
  background: var(--el-color-primary);
  color: #fff;
}
.bubble {
  max-width: 78%;
  padding: var(--app-spacing-sm) var(--app-spacing-md);
  border-radius: var(--app-radius);
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
  background: var(--el-fill-color-light);
}
.bubble.refused {
  background: var(--el-fill-color-lighter);
  border: 1px dashed var(--el-border-color);
}
.refusal {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-sm);
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.typing {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  padding: 4px 0;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--el-text-color-secondary);
  animation: typing-blink 1.2s infinite ease-in-out;
}
.dot:nth-child(2) {
  animation-delay: 0.2s;
}
.dot:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes typing-blink {
  0%,
  80%,
  100% {
    opacity: 0.3;
  }
  40% {
    opacity: 1;
  }
}

/* markdown 渲染区(自 ChatPage 原样迁入) */
.markdown-body :deep(p) {
  margin: 0 0 8px;
}
.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}
.markdown-body :deep(pre.hljs) {
  margin: 8px 0;
  padding: 10px 12px;
  border-radius: var(--app-radius-sm);
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
</style>
