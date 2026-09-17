<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>

<template>
  <div class="page-header">
    <div class="page-header-text">
      <h2>{{ title }}</h2>
      <p v-if="description" class="page-header-desc">{{ description }}</p>
    </div>
    <div class="page-header-actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--app-spacing-lg);
  margin-bottom: var(--app-spacing-lg);
}
.page-header-text h2 {
  margin: 0;
  font-size: 20px;
  font-weight: 700;
  display: flex;
  align-items: center;
  gap: 10px;
}
.page-header-text h2::before {
  content: '';
  width: 4px;
  height: 18px;
  border-radius: 2px;
  background: var(--app-brand-grad);
  flex-shrink: 0;
}
.page-header-desc {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.page-header-actions {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-sm);
}
</style>
