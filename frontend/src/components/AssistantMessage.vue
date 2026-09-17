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
    <div class="bubble" :class="{ refused, streaming: pending }">
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
  gap: var(--app-spacing-md);
  align-items: flex-start;
  min-width: 0;
  flex: 1;
}
.avatar {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  font-size: 12px;
  font-weight: 600;
}
.bot-avatar {
  background: var(--app-brand-grad);
  color: #fff;
  box-shadow: var(--app-shadow-brand);
}
.bubble {
  max-width: 78%;
  min-width: 0;
  padding: 10px 16px;
  border-radius: 14px 14px 14px 4px;
  font-size: 14px;
  line-height: 1.65;
  word-break: break-word;
  background: var(--app-card-bg);
  border: 1px solid var(--app-card-border);
  box-shadow: var(--app-shadow-card);
}
.bubble.refused {
  background: var(--el-fill-color-lighter);
  border: 1px dashed var(--el-border-color);
  box-shadow: none;
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
  background: var(--el-color-primary);
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

/* 流式生成中:正文末尾跟随主色光标 */
.bubble.streaming .markdown-body :deep(p:last-child)::after {
  content: '';
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 3px;
  vertical-align: text-bottom;
  border-radius: 1px;
  background: var(--el-color-primary);
  animation: caret-blink 0.9s steps(2, start) infinite;
}
@keyframes caret-blink {
  to {
    visibility: hidden;
  }
}

/* ---------- markdown 渲染区排版 ---------- */
.markdown-body :deep(p) {
  margin: 0 0 8px;
}
.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  margin: 14px 0 8px;
  font-weight: 650;
  line-height: 1.4;
  color: var(--el-text-color-primary);
}
.markdown-body :deep(h1) {
  font-size: 18px;
}
.markdown-body :deep(h2) {
  font-size: 16.5px;
}
.markdown-body :deep(h3) {
  font-size: 15px;
}
.markdown-body :deep(h4) {
  font-size: 14px;
}
.markdown-body :deep(h1:first-child),
.markdown-body :deep(h2:first-child),
.markdown-body :deep(h3:first-child) {
  margin-top: 2px;
}
.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 4px 0 10px;
  padding-left: 1.5em;
}
.markdown-body :deep(li) {
  margin: 3px 0;
}
.markdown-body :deep(li::marker) {
  color: var(--el-color-primary);
}
.markdown-body :deep(blockquote) {
  margin: 8px 0;
  padding: 4px 12px;
  border-left: 3px solid var(--el-color-primary-light-5);
  border-radius: 0 6px 6px 0;
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-secondary);
}
.markdown-body :deep(a) {
  color: var(--el-color-primary);
  text-decoration: none;
  border-bottom: 1px dashed var(--el-color-primary-light-5);
}
.markdown-body :deep(a:hover) {
  border-bottom-style: solid;
}
.markdown-body :deep(strong) {
  font-weight: 650;
  color: var(--el-text-color-primary);
}
.markdown-body :deep(hr) {
  margin: 12px 0;
  border: none;
  border-top: 1px solid var(--el-border-color-lighter);
}
.markdown-body :deep(img) {
  max-width: 100%;
  border-radius: var(--app-radius-sm);
}

/* 行内码:等宽字体 + 浅底胶囊;pre 内代码块单独处理 */
.markdown-body :deep(:not(pre) > code) {
  font-family: var(--app-font-mono);
  font-size: 0.9em;
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--el-fill-color);
  color: var(--el-color-primary);
}
.markdown-body :deep(pre.hljs) {
  margin: 8px 0;
  padding: 10px 14px;
  border-radius: var(--app-radius-sm);
  border: 1px solid var(--app-card-border);
  background: var(--app-bg-soft);
  overflow-x: auto;
  font-size: 13px;
  line-height: 1.6;
}
.markdown-body :deep(pre.hljs code) {
  font-family: var(--app-font-mono);
}
.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
  font-size: 13px;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--el-border-color-lighter);
  padding: 5px 12px;
}
.markdown-body :deep(th) {
  background: var(--el-fill-color-lighter);
  font-weight: 600;
}
.markdown-body :deep(tr:nth-child(even) td) {
  background: var(--el-fill-color-lighter);
}

@media (prefers-reduced-motion: reduce) {
  .bubble.streaming .markdown-body :deep(p:last-child)::after,
  .dot {
    animation: none;
  }
}
</style>
