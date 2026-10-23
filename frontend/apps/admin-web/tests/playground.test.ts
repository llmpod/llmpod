// @vitest-environment happy-dom
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@repo/naive-ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/naive-ui')>()
  return { ...actual, createChatSender: vi.fn(() => ({ send: vi.fn() })) }
})
vi.mock('@repo/hooks', () => ({ message: { error: vi.fn() } }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))
vi.mock('@/api/publish', () => ({ listPublishes: vi.fn() }))
vi.mock('@/store/auth', () => {
  const auth = { token: '' }
  return { useAuthStore: () => auth, __auth: auth }
})

import { createChatSender } from '@repo/naive-ui'
import { message } from '@repo/hooks'
import { listPublishes } from '@/api/publish'
import * as authModule from '@/store/auth'
import { useController } from '@/views/playground/useController'

const auth = (authModule as unknown as { __auth: { token: string } }).__auth

const publishFixtures = [
  {
    route: {
      name: 'deepseek-v3',
      status: 'published',
      model_type: 'llm',
      context_length: 65536,
      tags: ['思考'],
    },
    bindings: [],
  },
  {
    route: {
      name: 'qwen-vl',
      status: 'published',
      model_type: 'multimodal',
      context_length: null,
      tags: [],
    },
    bindings: [],
  },
  {
    route: { name: 'bge-m3', status: 'published', model_type: 'embedding', tags: [] },
    bindings: [],
  },
  {
    route: { name: 'offline-model', status: 'unpublished', model_type: 'llm', tags: [] },
    bindings: [],
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  auth.token = 'admin-session-token'
  vi.mocked(listPublishes).mockResolvedValue(publishFixtures as never)
})

function mountController() {
  let controller!: ReturnType<typeof useController>
  const wrapper = mount({
    setup: () => {
      controller = useController()
      return () => h('div')
    },
  })
  return { controller, wrapper }
}

/** 从 createChatSender 调用参数中取出鉴权与地址选项。 */
function senderOptions() {
  return vi.mocked(createChatSender).mock.calls[0][0] as {
    baseURL: () => string
    getToken: () => string
  }
}

describe('playground useController', () => {
  it('onMounted 拉取模型列表:已发布 ∩ 可对话调试类别,取路由名', async () => {
    const { controller, wrapper } = mountController()
    await flushPromises()

    expect(controller.models.value.map((item) => item.value)).toEqual(['deepseek-v3', 'qwen-vl'])
    // 模型元数据透传:切换模型时自动填充思考强度
    expect(controller.models.value[0]).toMatchObject({
      label: 'deepseek-v3',
      reasoning: true,
    })
    expect(controller.models.value[1]).toMatchObject({ reasoning: false })
    expect(message.error).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('sender 直连控制面本地转发:baseURL 挂 admin inference,token 取会话态', async () => {
    const { controller, wrapper } = mountController()
    await flushPromises()

    expect(createChatSender).toHaveBeenCalledTimes(1)
    const options = senderOptions()
    // 与 BFF 同源约定:origin + /api/v1/admin/inference(SDK 需绝对地址)
    expect(options.baseURL()).toBe(`${window.location.origin}/api/v1/admin/inference`)
    // 鉴权走管理员会话 token,且发送时刻动态解析(登录态变化可跟随)
    expect(options.getToken()).toBe('admin-session-token')
    auth.token = 'refreshed-token'
    expect(options.getToken()).toBe('refreshed-token')

    // 管理端不再装配 API Key 选择
    expect('apiKeys' in controller).toBe(false)
    expect('apiKey' in controller).toBe(false)
    wrapper.unmount()
  })

  it('模型列表加载失败时提示错误,models 保持为空', async () => {
    vi.mocked(listPublishes).mockRejectedValue(new Error('network'))

    const { controller, wrapper } = mountController()
    await flushPromises()

    expect(controller.models.value).toEqual([])
    expect(message.error).toHaveBeenCalledWith('模型列表加载失败,请稍后重试')
    wrapper.unmount()
  })
})
