<script setup lang="ts">
import { ref } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import EvalRunsTab from '@/pages/eval/EvalRunsTab.vue'
import QuestionsTab from '@/pages/eval/QuestionsTab.vue'

const tab = ref<'runs' | 'questions'>('runs')
</script>

<template>
  <div class="eval-page">
    <PageHeader title="评估" description="检索/生成评估:运行记录、题集管理、趋势与对比" />
    <el-tabs v-model="tab" class="eval-tabs">
      <el-tab-pane label="运行记录" name="runs">
        <EvalRunsTab />
      </el-tab-pane>
      <el-tab-pane label="题集管理" name="questions">
        <QuestionsTab />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.eval-tabs {
  background: var(--el-bg-color);
  border-radius: var(--app-radius);
  padding: 0 16px 16px;
}
</style>
