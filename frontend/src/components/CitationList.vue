<script setup lang="ts">
import { ref } from 'vue'
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
    <span class="citation-title">引用来源</span>
    <el-tag
      v-for="c in citations"
      :key="c.number"
      class="citation-tag"
      type="info"
      effect="plain"
      @click="open(c)"
    >
      [{{ c.number }}] {{ c.filename }}{{ c.page_no != null ? ` p.${c.page_no}` : '' }}
    </el-tag>

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
  margin-top: 8px;
}
.citation-title {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.citation-tag {
  cursor: pointer;
}
.citation-tag:hover {
  color: var(--el-color-primary);
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
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
