<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  evalApi,
  type EvalQuestion,
  type MyKb,
  type QuestionInput,
} from '@/api/eval'

const kbs = ref<MyKb[]>([])
const kbId = ref<number>(0)
const loading = ref(false)
const questions = ref<EvalQuestion[]>([])
const total = ref(0)
const query = reactive({ page: 1, pageSize: 50 })

const dialogVisible = ref(false)
const editing = ref<EvalQuestion | null>(null)
const form = reactive({
  question: '', keywords: [] as string[], docIds: [] as number[],
  reference: '',
})
const kwInput = ref('')
const docInput = ref('')
const deleteTarget = ref<EvalQuestion | null>(null)
const deleteVisible = ref(false)
const fileInput = ref<HTMLInputElement>()

const currentKb = () => kbs.value.find((k) => k.kb_id === kbId.value)

async function loadKbs() {
  try {
    kbs.value = await evalApi.myKbs()
    if (kbs.value.length && !kbs.value.some((k) => k.kb_id === kbId.value))
      kbId.value = kbs.value[0]!.kb_id
  } catch {
    /* 下拉失败不阻塞 */
  }
}

async function load() {
  if (!kbId.value) return
  loading.value = true
  try {
    const resp = await evalApi.listQuestions({
      kb_id: kbId.value, page: query.page, page_size: query.pageSize,
    })
    questions.value = resp.items
    total.value = resp.total
  } catch {
    ElMessage.error('加载题集失败')
  } finally {
    loading.value = false
  }
}

function pickKb() { query.page = 1; load() }

function openCreate() {
  editing.value = null
  form.question = ''; form.keywords = []; form.docIds = []; form.reference = ''
  dialogVisible.value = true
}

function openEdit(row: EvalQuestion) {
  editing.value = row
  form.question = row.question
  form.keywords = [...(row.expect_keywords ?? [])]
  form.docIds = [...(row.expect_doc_ids ?? [])]
  form.reference = row.reference_answer ?? ''
  dialogVisible.value = true
}

function addKeyword() {
  const v = kwInput.value.trim()
  if (v && !form.keywords.includes(v)) form.keywords.push(v)
  kwInput.value = ''
}

function addDocId() {
  const v = docInput.value.trim()
  if (/^\d+$/.test(v)) {
    const n = Number(v)
    if (!form.docIds.includes(n)) form.docIds.push(n)
  } // 非正整数不入 tag(spec D)
  docInput.value = ''
}

async function save() {
  if (!form.question.trim()) {
    ElMessage.warning('问题不能为空')
    return
  }
  const payload = {
    question: form.question,
    expect_keywords: form.keywords,
    expect_doc_ids: form.docIds,
    reference_answer: form.reference.trim() || null,
  }
  try {
    if (editing.value) await evalApi.updateQuestion(editing.value.id, payload)
    else await evalApi.createQuestion({ ...payload, kb_id: kbId.value })
    ElMessage.success(editing.value ? '已更新' : '已添加')
    dialogVisible.value = false
    await loadKbs() // question_count 变化
    load()
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })
      ?.response?.data?.detail
    ElMessage.error(detail ?? '保存失败')
  }
}

async function confirmDelete() {
  if (!deleteTarget.value) return
  try {
    await evalApi.deleteQuestion(deleteTarget.value.id)
    ElMessage.success('已删除')
  } catch {
    ElMessage.error('删除失败')
  } finally {
    deleteVisible.value = false
    await loadKbs()
    load()
  }
}

// ---- M19 T6:导入 / 导出 ----

/** 导出题集:blob → objectURL → a[download] 触发下载,用毕 revoke */
async function doExport() {
  if (!questions.value.length) return
  try {
    const blob = await evalApi.exportQuestions(kbId.value)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `eval-questions-kb${kbId.value}.json`
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    ElMessage.error('导出失败')
  }
}

function pickImport() {
  fileInput.value?.click()
}

/** 导入:兼容导出格式({questions:[...]})与裸数组;非 dict / 空题干条
 *  先行剔除(后端对非 dict 项整体 422),确认后 bulkImport 部分成功 */
async function onImportChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许再次选择同一文件
  if (!file) return
  let parsed: unknown
  try {
    parsed = JSON.parse(await file.text())
  } catch {
    ElMessage.error('文件解析失败')
    return
  }
  const arr = Array.isArray(parsed)
    ? parsed
    : Array.isArray((parsed as { questions?: unknown })?.questions)
      ? (parsed as { questions: unknown[] }).questions
      : []
  const items = arr.filter(
    (q): q is QuestionInput =>
      !!q && typeof q === 'object' && !Array.isArray(q)
      && typeof (q as QuestionInput).question === 'string'
      && (q as QuestionInput).question.trim() !== '',
  )
  if (!items.length) {
    ElMessage.error('文件中没有可导入的题目')
    return
  }
  try {
    await ElMessageBox.confirm(`将导入 ${items.length} 题`, '导入题集')
  } catch {
    return // 用户取消
  }
  try {
    const r = await evalApi.bulkImport(kbId.value, items)
    ElMessage.success(`${r.created} 题导入成功`)
    if (r.errors.length) {
      // 最小可用:提示条只给首条,全量错误进 console
      ElMessage.warning(`${r.errors.length} 条失败:首条 ${r.errors[0]!.detail}`)
      console.warn('[题集导入] 失败条目:', r.errors)
    }
    await loadKbs() // question_count 变化
    load()
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })
      ?.response?.data?.detail
    ElMessage.error(detail ?? '导入失败')
  }
}

onMounted(async () => {
  await loadKbs()
  load()
})
</script>

<template>
  <div class="questions-tab">
    <div class="toolbar">
      <el-select v-model="kbId" placeholder="知识库" filterable class="kb-select" @change="pickKb">
        <el-option v-for="k in kbs" :key="k.kb_id" :label="k.kb_name" :value="k.kb_id" />
      </el-select>
      <span v-if="currentKb()" class="count-hint">共 {{ currentKb()!.question_count }} 题</span>
      <el-button type="primary" class="add-btn" :disabled="!kbId" @click="openCreate">添加题目</el-button>
      <el-button class="export-btn" :disabled="!questions.length" @click="doExport">导出</el-button>
      <el-button class="import-btn" :disabled="!kbId" @click="pickImport">导入</el-button>
      <!-- hidden 文件选择器:导入按钮代理点击,change 后清 value 便于重复导入 -->
      <input
        ref="fileInput"
        type="file"
        accept=".json"
        class="import-file"
        hidden
        @change="onImportChange"
      />
    </div>
    <el-table v-loading="loading" :data="questions" class="q-table">
      <template #empty><el-empty description="暂无题目——点「添加题目」开始建题集" /></template>
      <el-table-column prop="question" label="问题" min-width="220" show-overflow-tooltip />
      <el-table-column label="期望文档" width="120">
        <template #default="{ row }">{{ row.expect_doc_ids?.length ? row.expect_doc_ids.join(',') : '—' }}</template>
      </el-table-column>
      <el-table-column label="期望关键词" min-width="160">
        <template #default="{ row }">
          <el-tag v-for="k in row.expect_keywords ?? []" :key="k" size="small" class="kw-tag">{{ k }}</el-tag>
          <span v-if="!row.expect_keywords?.length">—</span>
        </template>
      </el-table-column>
      <el-table-column label="参考答案" width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ row.reference_answer ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="140">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" class="del-btn" @click="deleteTarget = row; deleteVisible = true">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-model:current-page="query.page" :page-size="query.pageSize"
      layout="total, prev, pager, next" :total="total" class="q-pagination"
      @current-change="load" />

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑题目' : '添加题目'" width="560px">
      <el-form label-width="90px">
        <el-form-item label="问题" required>
          <!-- el-input(type=textarea) 的透传 class 落在外层 div 而非 textarea,
               测试钩子 textarea.q-input 要求类在原生元素上——直接用原生 textarea
               复用 EP 的 el-textarea__inner 样式(maxlength 仍生效) -->
          <textarea
            v-model="form.question"
            rows="3"
            maxlength="2000"
            class="q-input el-textarea__inner"
          />
        </el-form-item>
        <el-form-item label="期望关键词">
          <div class="tag-editor">
            <el-tag v-for="k in form.keywords" :key="k" closable @close="form.keywords = form.keywords.filter((x) => x !== k)">{{ k }}</el-tag>
            <el-input v-model="kwInput" class="tag-input" placeholder="回车添加" @keyup.enter="addKeyword" />
          </div>
        </el-form-item>
        <el-form-item label="期望文档id">
          <div class="tag-editor">
            <el-tag v-for="d in form.docIds" :key="d" closable @close="form.docIds = form.docIds.filter((x) => x !== d)">{{ d }}</el-tag>
            <el-input v-model="docInput" class="tag-input" placeholder="正整数,回车添加" @keyup.enter="addDocId" />
          </div>
        </el-form-item>
        <el-form-item label="参考答案">
          <el-input v-model="form.reference" type="textarea" :rows="2" placeholder="可选;供生成评估一致性打分" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" class="confirm-btn" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="deleteVisible" title="删除题目" width="360px">
      <span>确定删除「{{ deleteTarget?.question }}」?</span>
      <template #footer>
        <el-button @click="deleteVisible = false">取消</el-button>
        <el-button type="danger" class="confirm-del-btn" @click="confirmDelete">删除</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-sm);
  margin-bottom: var(--app-spacing-md);
}
.kb-select {
  width: 220px;
}
.count-hint {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.q-table {
  width: 100%;
  border-radius: var(--app-radius);
}
.kw-tag {
  margin-right: 4px;
}
.tag-editor {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-spacing-sm);
  width: 100%;
}
.tag-input {
  flex: 1;
  min-width: 140px;
}
.q-pagination {
  margin-top: var(--app-spacing-md);
  justify-content: flex-end;
}
</style>
