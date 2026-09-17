<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ChatDotRound, Collection, Document, Plus } from '@element-plus/icons-vue'
import { conversationsApi, type ConversationItem } from '@/api/chat'
import { kbApi, type KbItem } from '@/api/kb'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const kbs = ref<KbItem[]>([])
const conversations = ref<ConversationItem[]>([])
const loading = ref(false)

const greeting = computed(() => {
  const h = new Date().getHours()
  if (h < 6) return '夜深了'
  if (h < 12) return '早上好'
  if (h < 18) return '下午好'
  return '晚上好'
})

const totalDocs = computed(() => kbs.value.reduce((s, k) => s + (k.doc_count ?? 0), 0))
const recentConversations = computed(() => conversations.value.slice(0, 5))

const stats = computed(() => [
  { label: '知识库', value: kbs.value.length, icon: Collection },
  { label: '文档总数', value: totalDocs.value, icon: Document },
  { label: '会话数', value: conversations.value.length, icon: ChatDotRound },
])

onMounted(async () => {
  loading.value = true
  try {
    ;[kbs.value, conversations.value] = await Promise.all([kbApi.list(), conversationsApi.list()])
  } catch {
    /* 首页统计加载失败不阻塞,展示 0 */
  } finally {
    loading.value = false
  }
})

function fmtTime(iso: string) {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <div class="home-page" v-loading="loading">
    <section class="hero">
      <h2>{{ greeting }},{{ auth.user?.username ?? '' }}</h2>
      <p>从知识库或对话开始,构建你的私有知识问答。</p>
      <div class="quick-actions">
        <el-button type="primary" :icon="Plus" @click="router.push('/kb?create=1')">
          新建知识库
        </el-button>
        <el-button :icon="ChatDotRound" @click="router.push('/chat')">开始提问</el-button>
        <el-button :icon="Collection" @click="router.push('/kb')">查看知识库</el-button>
      </div>
    </section>

    <section class="stat-cards">
      <el-card v-for="s in stats" :key="s.label" shadow="never" class="stat-card">
        <div class="stat-body">
          <el-icon :size="22" class="stat-icon"><component :is="s.icon" /></el-icon>
          <div>
            <div class="stat-value">{{ s.value }}</div>
            <div class="stat-label">{{ s.label }}</div>
          </div>
        </div>
      </el-card>
    </section>

    <el-card shadow="never" class="recent-card">
      <template #header>最近会话</template>
      <div v-if="recentConversations.length === 0" class="recent-empty">
        还没有会话,去<a @click.prevent="router.push('/chat')" href="/chat">对话页</a>提第一个问题吧。
      </div>
      <div
        v-for="c in recentConversations"
        :key="c.id"
        class="recent-item"
        @click="router.push(`/chat?conv=${c.id}`)"
      >
        <span class="recent-title">{{ c.title }}</span>
        <span class="recent-time">{{ fmtTime(c.created_at) }}</span>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.hero {
  margin-bottom: var(--app-spacing-xl);
}
.hero h2 {
  margin: 0 0 4px;
}
.hero p {
  margin: 0 0 var(--app-spacing-lg);
  color: var(--el-text-color-secondary);
}
.quick-actions {
  display: flex;
  gap: var(--app-spacing-sm);
}
.stat-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--app-spacing-md);
  margin-bottom: var(--app-spacing-lg);
}
.stat-card {
  border-radius: var(--app-radius);
}
.stat-body {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-md);
}
.stat-icon {
  color: var(--el-color-primary);
}
.stat-value {
  font-size: 24px;
  font-weight: 700;
  line-height: 1.2;
}
.stat-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.recent-card {
  border-radius: var(--app-radius);
}
.recent-empty {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.recent-item {
  display: flex;
  justify-content: space-between;
  padding: var(--app-spacing-sm) var(--app-spacing-xs);
  border-radius: var(--app-radius-sm);
  cursor: pointer;
}
.recent-item:hover {
  background: var(--el-fill-color-light);
}
.recent-title {
  max-width: 70%;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 14px;
}
.recent-time {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
