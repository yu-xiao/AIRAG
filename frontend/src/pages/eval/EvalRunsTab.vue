<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, type TableInstance } from 'element-plus'
import { evalApi, type EvalItem, type EvalRun, type EvalRunDetail, type MyKb } from '@/api/eval'
import { kbApi, type KbItem } from '@/api/kb'
import TrendCard from '@/pages/eval/TrendCard.vue'
import CompareDrawer from '@/pages/eval/CompareDrawer.vue'

const loading = ref(false)
const runs = ref<EvalRun[]>([])
const total = ref(0)
const forbidden = ref(false)
const query = reactive({ kbId: 0 as number | 0, mode: '' as '' | 'retrieval' | 'generation' | undefined, page: 1, pageSize: 20 })
const kbOptions = ref<Pick<KbItem, 'id' | 'name'>[]>([])

// 汇总指标列随 mode 过滤切换;「全部」给两条头部指标,缺席值显示 —
const METRIC_COLS: Record<'' | 'retrieval' | 'generation', { key: string; label: string }[]> = {
  '': [
    { key: 'hit', label: '命中率' },
    { key: 'faithfulness_avg', label: '忠实度' },
  ],
  retrieval: [
    { key: 'hit', label: '命中率' },
    { key: 'mrr', label: 'MRR' },
    { key: 'keyword_recall', label: '关键词召回' },
  ],
  generation: [
    { key: 'faithfulness_avg', label: '忠实度' },
    { key: 'relevancy_avg', label: '相关性' },
    { key: 'reference_avg', label: '参考一致' },
    { key: 'refused_count', label: '拒答数' },
  ],
}
// el-select 清空时 Element Plus 会把 v-model 置为 undefined,兜底回 '' 走默认列
const metricCols = computed(() => METRIC_COLS[query.mode || ''])

const ITEM_COLS: Record<'retrieval' | 'generation', { key: keyof EvalItem; label: string }[]> = {
  retrieval: [
    { key: 'hit_at_k', label: 'hit@k' },
    { key: 'mrr', label: 'MRR' },
    { key: 'keyword_recall', label: '关键词召回' },
  ],
  generation: [
    { key: 'faithfulness', label: '忠实度' },
    { key: 'relevancy', label: '相关性' },
    { key: 'reference_score', label: '参考一致' },
  ],
}

// summary 自 M15 起可空(运行中/失败尚未汇总),null 与缺席同示 —
function fmtMetric(summary: Record<string, number | null> | null | undefined, key: string) {
  const v = summary?.[key]
  return v == null ? '—' : Number(v).toFixed(2)
}

function fmtScore(v: number | null | undefined) {
  return v == null ? '—' : Number(v).toFixed(2)
}

/** 明细抽屉「期望」列:期望文档/关键词收缩展示(规范 B),两者皆空显示 — */
function expectSummary(item: EvalItem) {
  const docs = item.expect_doc_ids?.length ? `文档${item.expect_doc_ids.join(',')}` : ''
  const kws = item.expect_keywords?.length ? `关键词${item.expect_keywords.join('、')}` : ''
  return [docs, kws].filter(Boolean).join(' / ') || '—'
}

function isLow(v: number | null | undefined) {
  return v != null && v < 0.5
}

function fmtTime(iso: string) {
  return iso.replace('T', ' ').slice(0, 19)
}

// silent:轮询路径静默刷新,不闪 loading 遮罩、不弹错误(DocsPage 模式)
async function load(silent = false) {
  if (disposed) return
  if (!silent) loading.value = true
  forbidden.value = false
  try {
    const resp = await evalApi.listRuns({
      kb_id: query.kbId || undefined,
      mode: query.mode || undefined,
      page: query.page,
      page_size: query.pageSize,
    })
    runs.value = resp.items
    total.value = resp.total
  } catch (e) {
    const status = (e as { response?: { status?: number } })?.response?.status
    if (status === 403) {
      forbidden.value = true
      runs.value = []
      total.value = 0
    } else if (!silent) {
      ElMessage.error('加载评估记录失败')
    }
  } finally {
    if (!silent) loading.value = false
    syncPolling()
  }
}

function search() {
  query.page = 1
  load()
}

// 模式切换即重查:用 watch 而非 @change——ElSelect 的 change 仅在组件内部交互时发出,
// 直接改 v-model(如测试)不会触发;watch 对两种路径都生效。
watch(
  () => query.mode,
  () => search(),
)

async function loadKbOptions() {
  try {
    kbOptions.value = (await kbApi.list()).map((k) => ({ id: k.id, name: k.name }))
  } catch {
    /* 下拉加载失败不阻塞页面,主列表另行报错 */
  }
}

// ---- 运行评估 ----
const myKbs = ref<MyKb[]>([])

async function loadMyKbs() {
  try { myKbs.value = await evalApi.myKbs() } catch { /* 不阻塞 */ }
}

const runDialogVisible = ref(false)
const runForm = reactive<{
  kb_id: number; mode: 'retrieval' | 'generation'; rerank: boolean; top_k: number
}>({ kb_id: 0, mode: 'retrieval', rerank: false, top_k: 8 })

function openRunDialog() {
  const eligible = myKbs.value.filter((k) => k.question_count > 0)
  runForm.kb_id = eligible[0]?.kb_id ?? 0
  runDialogVisible.value = true
}

async function submitRun() {
  if (!runForm.kb_id) return
  try {
    await evalApi.triggerRun({
      kb_id: runForm.kb_id, mode: runForm.mode,
      rerank: runForm.rerank,
      ...(runForm.mode === 'retrieval' ? { top_k: runForm.top_k } : {}),
    })
    ElMessage.success('评估已发起')
    runDialogVisible.value = false
    query.page = 1
    load()
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })
      ?.response?.data?.detail
    ElMessage.error(detail ?? '发起评估失败')
  }
}

// ---- running 3s 轮询(DocsPage 模式):hasRunning 开,全终态/卸载即停 ----
// disposed:卸载后在途 load 的 finally→syncPolling 不再重建孤儿 interval
let timer: number | undefined
let disposed = false

function hasRunning() {
  return runs.value.some((r) => r.status === 'running')
}

function syncPolling() {
  if (disposed) return
  if (hasRunning()) {
    if (timer === undefined) timer = window.setInterval(() => load(true), 3000)
  } else if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

onBeforeUnmount(() => {
  disposed = true
  if (timer !== undefined) window.clearInterval(timer)
})

// ---- 明细抽屉 ----
const drawerVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<EvalRunDetail | null>(null)

async function openDetail(row: { id: number }) {
  drawerVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await evalApi.getRun(row.id)
  } catch {
    ElMessage.error('加载评估明细失败')
    drawerVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

// selection 列(checkbox)点击与行点击冲突:目标落在该列 cell 内则不开明细
// (EP 的 row-click 对 checkbox 点击也无条件发出,需在此裁决——保留行点击入口)
function onRowClick(row: EvalRun, _column: unknown, event: Event) {
  if ((event.target as HTMLElement | null)?.closest('.el-table-column--selection')) return
  openDetail(row)
}

// ---- 双 run 对比 ----
const tableRef = ref<TableInstance>()
const sel = ref<EvalRun[]>([])
const cmpVisible = ref(false)

/** 已选 2 条后仅已选行可勾(可反选),阻断第三选 */
function canSelect(row: EvalRun) {
  return sel.value.length < 2 || sel.value.some((s) => s.id === row.id)
}

/** 同 mode 校验:第二选不同 mode → 警告并回退该勾选(回退再触发 selection-change 收敛 sel) */
function onSelectionChange(rows: EvalRun[]) {
  if (rows.length === 2 && rows[0]!.mode !== rows[1]!.mode) {
    const added = rows.find((r) => !sel.value.some((s) => s.id === r.id)) ?? rows[1]!
    ElMessage.warning('只能对比相同模式的两个运行')
    tableRef.value?.toggleRowSelection(added, false)
    return
  }
  sel.value = rows
}

function openCompare() {
  if (sel.value.length !== 2) return
  cmpVisible.value = true
}

onMounted(() => {
  load()
  loadKbOptions()
  loadMyKbs()
})
</script>

<template>
  <div class="eval-page">
    <div class="filters">
      <el-select
        v-model="query.kbId"
        placeholder="知识库"
        clearable
        filterable
        class="filter-select"
      >
        <el-option v-for="kb in kbOptions" :key="kb.id" :label="kb.name" :value="kb.id" />
      </el-select>
      <el-select
        v-model="query.mode"
        placeholder="模式"
        clearable
        class="filter-select narrow"
      >
        <el-option label="检索评估" value="retrieval" />
        <el-option label="生成评估" value="generation" />
      </el-select>
      <el-button type="primary" @click="search">查询</el-button>
      <el-button type="primary" class="run-btn" @click="openRunDialog">运行评估</el-button>
      <el-button class="cmp-btn" :disabled="sel.length !== 2" @click="openCompare">对比</el-button>
    </div>

    <el-alert
      v-if="forbidden"
      type="warning"
      :closable="false"
      show-icon
      title="仅库主/管理员可查看该库的评估记录"
      class="forbidden-alert"
    />

    <TrendCard :kb-id="query.kbId || undefined" class="trend-block" />

    <el-table
      ref="tableRef"
      v-loading="loading"
      :data="runs"
      class="eval-table"
      row-class-name="clickable"
      row-key="id"
      @row-click="onRowClick"
      @selection-change="onSelectionChange"
    >
      <template #empty>
        <el-empty description="暂无评估记录——点「运行评估」发起第一次评估" />
      </template>
      <!-- reserve-selection + row-key:轮询刷新替换 runs 数组引用后勾选保留
           (对比场景选好两条,刷新不清空) -->
      <el-table-column type="selection" width="44" :selectable="canSelect" reserve-selection />
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column label="知识库" min-width="160">
        <template #default="{ row }">{{ row.kb_name ?? '(已删除)' }}</template>
      </el-table-column>
      <el-table-column label="模式" width="100">
        <template #default="{ row }">
          <el-tag :type="row.mode === 'generation' ? 'success' : 'info'" size="small">
            {{ row.mode === 'generation' ? '生成' : '检索' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.status === 'running'" type="warning" size="small">
            运行中 {{ row.done_count }}/{{ row.item_count }}
          </el-tag>
          <el-tag v-else-if="row.status === 'failed'" type="danger" size="small">失败</el-tag>
          <el-tag v-else type="success" size="small">完成</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="item_count" label="题数" width="70" />
      <el-table-column
        v-for="col in metricCols"
        :key="col.key"
        :label="col.label"
        width="110"
      >
        <template #default="{ row }">{{ fmtMetric(row.summary, col.key) }}</template>
      </el-table-column>
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="query.page"
      v-model:page-size="query.pageSize"
      class="eval-pagination"
      layout="total, prev, pager, next, sizes"
      :page-sizes="[20, 50, 100]"
      :total="total"
      @current-change="() => load()"
      @size-change="search"
    />

    <el-drawer v-model="drawerVisible" :title="`评估明细 #${detail?.id ?? ''}`" size="62%">
      <div v-loading="detailLoading" class="detail-body">
        <template v-if="detail">
          <el-alert
            v-if="detail.status === 'failed'"
            type="error"
            :closable="false"
            show-icon
            :title="detail.error ?? '评估失败'"
            class="error-alert"
          />
          <div class="detail-summary">
            <el-tag :type="detail.mode === 'generation' ? 'success' : 'info'" size="small">
              {{ detail.mode === 'generation' ? '生成' : '检索' }}
            </el-tag>
            <span>{{ detail.kb_name ?? '(已删除)' }} · {{ detail.item_count }} 题</span>
            <span v-if="detail.created_by">发起人 {{ detail.created_by }}</span>
            <span v-if="detail.items_truncated" class="truncated-note">
              (仅显示前 {{ detail.items.length }} 条)
            </span>
          </div>
          <el-table :data="detail.items" size="small" class="items-table">
            <el-table-column prop="question" label="问题" min-width="180" show-overflow-tooltip />
            <el-table-column label="答案" min-width="240" show-overflow-tooltip>
              <template #default="{ row }">{{ row.answer ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="拒答" width="70">
              <template #default="{ row }">
                <el-tag v-if="row.refused" type="danger" size="small">拒答</el-tag>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column label="期望" width="150" show-overflow-tooltip>
              <template #default="{ row }">
                {{ expectSummary(row) }}
              </template>
            </el-table-column>
            <el-table-column
              v-for="col in ITEM_COLS[detail.mode]"
              :key="col.key"
              :label="col.label"
              width="100"
            >
              <template #default="{ row }">
                <span :class="{ 'score-low': isLow(row[col.key] as number | null) }">
                  {{ fmtScore(row[col.key] as number | null) }}
                </span>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </div>
    </el-drawer>

    <el-dialog v-model="runDialogVisible" title="运行评估" width="420px">
      <el-form label-width="90px">
        <el-form-item label="知识库">
          <el-select v-model="runForm.kb_id">
            <el-option v-for="k in myKbs.filter((x) => x.question_count > 0)"
              :key="k.kb_id" :label="`${k.kb_name}(${k.question_count}题)`" :value="k.kb_id" />
          </el-select>
        </el-form-item>
        <el-form-item label="模式">
          <el-radio-group v-model="runForm.mode">
            <el-radio value="retrieval">检索评估</el-radio>
            <el-radio value="generation">生成评估</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="runForm.mode === 'retrieval'" label="top_k">
          <el-input-number v-model="runForm.top_k" :min="1" :max="50" />
        </el-form-item>
        <el-form-item label="重排序">
          <el-switch v-model="runForm.rerank" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="runDialogVisible = false">取消</el-button>
        <el-button type="primary" class="run-confirm" :disabled="!runForm.kb_id" @click="submitRun">发起</el-button>
      </template>
    </el-dialog>

    <CompareDrawer
      v-model:visible="cmpVisible"
      :run-a="sel[0]?.id ?? null"
      :run-b="sel[1]?.id ?? null"
    />
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  gap: 8px;
  margin-bottom: var(--app-spacing-md);
}
.filter-select {
  width: 200px;
}
.filter-select.narrow {
  width: 140px;
}
.forbidden-alert {
  margin-bottom: 12px;
}
.eval-table {
  width: 100%;
  border-radius: var(--app-radius);
}
.eval-table :deep(.clickable) {
  cursor: pointer;
}
.eval-pagination {
  margin-top: 12px;
  justify-content: flex-end;
}
.detail-body {
  min-height: 200px;
}
.error-alert {
  margin-bottom: 12px;
}
.detail-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.truncated-note {
  color: var(--el-color-warning);
}
.score-low {
  color: var(--el-color-danger);
}
</style>
