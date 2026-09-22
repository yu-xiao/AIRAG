<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { evalApi, type EvalItem, type EvalRunDetail } from '@/api/eval'
import { computeSummaryDiff, ITEM_KEY, joinItems, type CompareRow } from '@/utils/evalCompare'
import { TREND_METRICS } from '@/utils/evalTrend'

const props = defineProps<{ visible: boolean; runA: number | null; runB: number | null }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

const loading = ref(false)
const detailA = ref<EvalRunDetail | null>(null)
const detailB = ref<EvalRunDetail | null>(null)

watch(() => [props.visible, props.runA, props.runB] as const,
  async ([visible]) => {
    if (!visible || !props.runA || !props.runB) return
    loading.value = true
    detailA.value = null
    detailB.value = null
    try {
      ;[detailA.value, detailB.value] = await Promise.all([
        evalApi.getRun(props.runA), evalApi.getRun(props.runB),
      ])
    } catch {
      ElMessage.error('加载对比明细失败')
      emit('update:visible', false)
    } finally {
      loading.value = false
    }
  }, { immediate: true })

const diffRows = computed(() =>
  computeSummaryDiff(detailA.value?.summary, detailB.value?.summary))
const itemRows = computed(() =>
  detailA.value && detailB.value
    ? joinItems(detailA.value.items, detailB.value.items) : [])
const itemCols = computed(() =>
  detailA.value ? TREND_METRICS[detailA.value.mode] : [])

const SIDES = ['a', 'b'] as const
type Side = (typeof SIDES)[number]

// 库名:两侧同名合一,异名分 A/B 标注(同名库不同 run 最常见)
const kbLabel = computed(() => {
  if (!detailA.value || !detailB.value) return ''
  const a = detailA.value.kb_name ?? '(已删除)'
  const b = detailB.value.kb_name ?? '(已删除)'
  return a === b ? a : `A:${a} / B:${b}`
})

const onlyCount = computed(() => {
  const c = { both: 0, a: 0, b: 0 }
  for (const r of itemRows.value) c[r.only]++
  return c
})

/** 逐题取数:汇总指标键经 ITEM_KEY 映射到 EvalItem 得分字段;缺席侧 null */
function itemScore(it: EvalItem | null | undefined, metricKey: string): number | null {
  const field = ITEM_KEY[metricKey]
  if (!field || !it) return null
  return it[field] as number | null
}

/** 未测量判据:该题未设对应期望(hit/MRR 看期望文档、关键词召回看期望关键词)——
 *  A1 前空期望按 0 落库的历史行由此识别,与明细抽屉 fmtItemScore 同口径(M16 终审修复) */
function unmeasured(it: EvalItem | null | undefined, metricKey: string): boolean {
  const field = ITEM_KEY[metricKey]
  if (field === 'hit_at_k' || field === 'mrr') return !it?.expect_doc_ids?.length
  if (field === 'keyword_recall') return !it?.expect_keywords?.length
  return false
}

/** 逐题分值:未测量(null 新语义,或历史落库 0+空期望)显「—」,真测量 0 仍显 0.00 */
function fmtItemScore(it: EvalItem | null | undefined, metricKey: string): string {
  const v = itemScore(it, metricKey)
  if (v == null || (v === 0 && unmeasured(it, metricKey))) return '—'
  return Number(v).toFixed(2)
}

function isItemLow(it: EvalItem | null | undefined, metricKey: string): boolean {
  const v = itemScore(it, metricKey)
  return v != null && v < 0.5 && fmtItemScore(it, metricKey) !== '—'
}

function refusedOf(row: CompareRow, side: Side) {
  return row[side]?.refused === true
}

function fmtScore(v: number | null | undefined) {
  return v == null ? '—' : Number(v).toFixed(2)
}

function fmtDelta(d: number | null) {
  if (d == null) return '—'
  return `${d > 0 ? '+' : ''}${d.toFixed(2)}`
}

/** Δ 着色:>0 绿 <0 红;abs<0.01 视为持平不着色,null 不着色 */
function deltaClass(d: number | null) {
  if (d == null || Math.abs(d) < 0.01) return ''
  return d > 0 ? 'delta-up' : 'delta-down'
}
</script>

<template>
  <el-drawer
    :model-value="visible"
    :title="`运行对比 #${runA ?? ''} vs #${runB ?? ''}`"
    size="80%"
    @update:model-value="emit('update:visible', $event)"
  >
    <div v-loading="loading" class="cmp-body">
      <template v-if="detailA && detailB">
        <div class="cmp-summary">
          <el-tag :type="detailA.mode === 'generation' ? 'success' : 'info'" size="small">
            {{ detailA.mode === 'generation' ? '生成' : '检索' }}
          </el-tag>
          <span>{{ kbLabel }}</span>
          <span>
            共 {{ itemRows.length }} 题 · 配对 {{ onlyCount.both }} / 仅A {{ onlyCount.a }} / 仅B {{ onlyCount.b }}
          </span>
        </div>

        <div class="block-title">汇总指标</div>
        <el-table :data="diffRows" size="small" class="diff-table">
          <el-table-column prop="label" label="指标" min-width="120" />
          <el-table-column :label="`Run #${runA}`" align="center">
            <template #default="{ row }">{{ fmtScore(row.a) }}</template>
          </el-table-column>
          <el-table-column :label="`Run #${runB}`" align="center">
            <template #default="{ row }">{{ fmtScore(row.b) }}</template>
          </el-table-column>
          <el-table-column label="Δ" align="center">
            <template #default="{ row }">
              <span :class="deltaClass(row.delta)">{{ fmtDelta(row.delta) }}</span>
            </template>
          </el-table-column>
        </el-table>

        <div class="block-title">逐题对比</div>
        <el-table :data="itemRows" size="small" class="items-table">
          <el-table-column label="组" width="64" align="center">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="row.only === 'a' ? 'success' : row.only === 'b' ? 'warning' : 'info'"
              >
                {{ row.only === 'both' ? '配对' : row.only === 'a' ? '仅A' : '仅B' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="question" label="问题" min-width="180" show-overflow-tooltip />
          <el-table-column
            v-for="side in SIDES"
            :key="side"
            :label="`Run #${side === 'a' ? runA : runB}`"
            align="center"
          >
            <el-table-column
              v-for="col in itemCols"
              :key="`${side}-${col.key}`"
              :label="col.label"
              width="92"
            >
              <template #default="{ row }">
                <span :class="{ 'score-low': isItemLow(row[side], col.key) }">
                  {{ fmtItemScore(row[side], col.key) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="拒答" width="60">
              <template #default="{ row }">
                <el-tag v-if="refusedOf(row, side)" type="danger" size="small">拒答</el-tag>
                <span v-else>—</span>
              </template>
            </el-table-column>
          </el-table-column>
        </el-table>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.cmp-body {
  min-height: 200px;
}
.cmp-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.block-title {
  margin: 16px 0 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.diff-table,
.items-table {
  width: 100%;
}
.delta-up {
  color: var(--el-color-success);
}
.delta-down {
  color: var(--el-color-danger);
}
.score-low {
  color: var(--el-color-danger);
}
</style>
