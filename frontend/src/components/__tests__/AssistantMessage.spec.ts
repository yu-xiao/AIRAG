import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AssistantMessage from '@/components/AssistantMessage.vue'
import CitationList from '@/components/CitationList.vue'
import type { Citation } from '@/api/chat'

const citation: Citation = {
  number: 1,
  chunk_id: 1,
  document_id: 1,
  filename: 'a.pdf',
  page_no: 1,
  excerpt: '摘录',
}

describe('AssistantMessage', () => {
  it('renders refusal style and hides citations when refused', () => {
    const w = mount(AssistantMessage, {
      props: {
        content: '知识库中未找到相关内容',
        refused: true,
        citations: [citation],
      },
    })
    expect(w.find('.refusal').exists()).toBe(true)
    expect(w.findComponent(CitationList).exists()).toBe(false)
  })

  it('renders markdown and citations when not refused', () => {
    const w = mount(AssistantMessage, {
      props: { html: '<p>答案</p>', content: '答案', citations: [citation] },
    })
    expect(w.find('.refusal').exists()).toBe(false)
    expect(w.find('.markdown-body').exists()).toBe(true)
    expect(w.findComponent(CitationList).exists()).toBe(true)
  })
})
