<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { evalApi, type EvalItem, type EvalRun, type EvalRunDetail } from '@/api/eval'
import { kbApi, type KbItem } from '@/api/kb'

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

async function load() {
  loading.value = true
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
    } else {
      ElMessage.error('加载评估记录失败')
    }
  } finally {
    loading.value = false
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

onMounted(() => {
  load()
  loadKbOptions()
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
    </div>

    <el-alert
      v-if="forbidden"
      type="warning"
      :closable="false"
      show-icon
      title="仅库主/管理员可查看该库的评估记录"
      class="forbidden-alert"
    />

    <el-table
      v-loading="loading"
      :data="runs"
      class="eval-table"
      row-class-name="clickable"
      @row-click="openDetail"
    >
      <template #empty>
        <el-empty description="暂无评估记录——在服务器用 eval CLI 加 --save 生成" />
      </template>
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
      @current-change="load"
      @size-change="search"
    />

    <el-drawer v-model="drawerVisible" :title="`评估明细 #${detail?.id ?? ''}`" size="62%">
      <div v-loading="detailLoading" class="detail-body">
        <template v-if="detail">
          <div class="detail-summary">
            <el-tag :type="detail.mode === 'generation' ? 'success' : 'info'" size="small">
              {{ detail.mode === 'generation' ? '生成' : '检索' }}
            </el-tag>
            <span>{{ detail.kb_name ?? '(已删除)' }} · {{ detail.item_count }} 题</span>
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
