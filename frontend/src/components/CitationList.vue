<script setup lang="ts">
import { ref } from 'vue'
import { Collection } from '@element-plus/icons-vue'
import type { Citation } from '@/api/chat'

defineProps<{ citations: Citation[] }>()

const dialogVisible = ref(false)
const active = ref<Citation | null>(null)

function open(c: Citation) {
  active.value = c
  dialogVisible.value = true
}
</script>

<template>
  <div class="citation-list">
    <span class="citation-title">
      <el-icon :size="12"><Collection /></el-icon>
      引用来源
    </span>
    <button
      v-for="c in citations"
      :key="c.number"
      type="button"
      class="cite-chip"
      :title="c.filename"
      @click="open(c)"
    >
      <span class="cite-no">{{ c.number }}</span>
      <span class="cite-name">{{ c.filename }}</span>
      <span v-if="c.page_no != null" class="cite-page">p.{{ c.page_no }}</span>
    </button>

    <el-dialog v-model="dialogVisible" title="引用详情" width="560px">
      <div v-if="active" class="citation-detail">
        <div class="detail-row">
          <span class="detail-label">文档</span>
          <span>{{ active.filename }}</span>
        </div>
        <div class="detail-row">
          <span class="detail-label">页码</span>
          <span>{{ active.page_no ?? '—' }}</span>
        </div>
        <div class="detail-row">
          <span class="detail-label">分块</span>
          <span>#{{ active.chunk_id }}</span>
        </div>
        <div class="detail-row excerpt-row">
          <span class="detail-label">摘录</span>
          <div class="excerpt">{{ active.excerpt }}</div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.citation-list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--el-border-color-lighter);
}
.citation-title {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.cite-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 240px;
  padding: 2px 10px 2px 3px;
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: 999px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 12px;
  line-height: 1.4;
  cursor: pointer;
  transition:
    box-shadow 0.18s ease,
    transform 0.18s ease,
    background-color 0.18s ease;
}
.cite-chip:hover {
  background: var(--el-color-primary-light-8);
  transform: translateY(-1px);
  box-shadow: var(--app-shadow-card);
}
.cite-no {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 17px;
  height: 17px;
  border-radius: 50%;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
}
.cite-name {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cite-page {
  flex-shrink: 0;
  opacity: 0.75;
}
.citation-detail {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.detail-row {
  display: flex;
  gap: 12px;
  align-items: baseline;
}
.detail-label {
  flex-shrink: 0;
  width: 36px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.excerpt-row {
  align-items: flex-start;
}
.excerpt {
  max-height: 260px;
  overflow-y: auto;
  padding: 8px 10px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
