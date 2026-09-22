<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import { evalApi } from '@/api/eval'
import { useTheme } from '@/composables/useTheme'
import { buildTrendSeries, TREND_METRICS } from '@/utils/evalTrend'

use([CanvasRenderer, LineChart, TooltipComponent, LegendComponent,
     GridComponent])

const props = defineProps<{ kbId: number | undefined }>()

const { isDark } = useTheme()
const el = ref<HTMLDivElement>()
const expanded = ref(false)
const empty = ref(false)
const rootEl = ref<HTMLDivElement>()
const mode = ref<'retrieval' | 'generation'>('retrieval')
const metricKeys = ref<string[]>(
  TREND_METRICS.retrieval.map((m) => m.key))

let chart: ReturnType<typeof import('echarts/core').init> | null = null
// 实例化于 onMounted:观察卡片根元素,容器尺寸变化同步 chart(M16 救活)
let ro: ResizeObserver | null = null as ResizeObserver | null

const PALETTE = { light: ['#5b6ee1', '#3aa376', '#c98a2d'],
                  dark: ['#8b9cff', '#5ec89a', '#e0a75a'] }

async function render() {
  // 注意:此处不判 !el.value——空态时画布 v-if 未挂载,带着守卫会在
  // 「空态→切库/切模式有数据」时永远早退;判空移至 nextTick 之后
  if (!expanded.value || !props.kbId) return
  const { init } = await import('echarts/core')
  const resp = await evalApi.listRuns({
    kb_id: props.kbId, mode: mode.value, page: 1, page_size: 100,
  })
  const { times, series } = buildTrendSeries(resp.items, metricKeys.value)
  empty.value = times.length === 0
  if (empty.value) {
    if (chart) { chart.dispose(); chart = null }
    return
  }
  await nextTick()  // empty=false 后画布随 v-if 挂载,el.value 就绪
  if (!el.value) return
  chart ??= init(el.value)
  chart.setOption({
    backgroundColor: 'transparent',
    color: PALETTE[isDark.value ? 'dark' : 'light'],
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { left: 40, right: 16, top: 32, bottom: 24 },
    xAxis: { type: 'category', data: times },
    yAxis: { type: 'value', min: 0, max: 1 },
    series: series.map((s) => ({
      name: labelOf(s.name), type: 'line', data: s.data,
      connectNulls: true, symbolSize: 6,
    })),
  }, { notMerge: true })
}

function labelOf(key: string) {
  return TREND_METRICS[mode.value].find((m) => m.key === key)?.label ?? key
}

// 切模式即恢复该模式默认全选:否则旧键在新模式 summary 里缺席→全 null 空线,
// labelOf 也找不到映射→legend 显示原始键名(如 faithfulness_avg)
watch(mode, () => {
  metricKeys.value = TREND_METRICS[mode.value].map((m) => m.key)
})

watch([expanded, mode, metricKeys, isDark, () => props.kbId],
  async () => {
    await nextTick()
    if (chart && !expanded.value) { chart.dispose(); chart = null; return }
    render()
  }, { deep: true })

onMounted(() => {
  ro = new ResizeObserver(() => chart?.resize())
  ro.observe(rootEl.value!)
})

onBeforeUnmount(() => {
  ro?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <div class="trend-card" ref="rootEl">
    <div class="trend-head" @click="expanded = !expanded">
      <span class="trend-title">指标趋势</span>
      <el-tag size="small" type="info">{{ expanded ? '收起' : '展开' }}</el-tag>
    </div>
    <template v-if="expanded">
      <div class="trend-filters">
        <el-radio-group v-model="mode" size="small">
          <el-radio-button value="retrieval">检索</el-radio-button>
          <el-radio-button value="generation">生成</el-radio-button>
        </el-radio-group>
        <el-checkbox-group v-model="metricKeys" size="small">
          <el-checkbox v-for="m in TREND_METRICS[mode]" :key="m.key" :value="m.key">
            {{ m.label }}
          </el-checkbox>
        </el-checkbox-group>
      </div>
      <!-- 三态互斥:有数据画布 / 选库无完成运行 / 未选库 -->
      <div v-if="kbId && !empty" ref="el" class="trend-canvas" />
      <el-empty v-else-if="kbId" description="暂无已完成的运行" :image-size="48" />
      <el-empty v-else description="先选择知识库" :image-size="48" />
    </template>
  </div>
</template>

<style scoped>
.trend-card {
  margin-bottom: var(--app-spacing-md);
  padding: var(--app-spacing-sm) var(--app-spacing-lg);
  border: 1px solid var(--app-card-border);
  border-radius: var(--app-radius);
  background: var(--app-card-bg);
}
.trend-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
}
.trend-title {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.trend-filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--app-spacing-lg);
  margin: var(--app-spacing-sm) 0;
}
.trend-canvas {
  height: 260px;
}
</style>
