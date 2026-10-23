// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@repo/naive-ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/naive-ui')>()
  return { ...actual, createChatSender: vi.fn(() => ({ send: vi.fn() })) }
})
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return { ...actual, useDialog: vi.fn(), message: { error: vi.fn() } }
})
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))
vi.mock('@/api/api-key', () => ({ listTenantKeys: vi.fn(), createTenantKey: vi.fn() }))
vi.mock('@/api/proxy-endpoint', () => ({ getProxyEndpoint: vi.fn() }))
vi.mock('@/api/model', () => ({ listTenantModels: vi.fn() }))
vi.mock('@/views/api-key/ApiKeyForm.vue', () => ({ default: {} }))

import { createChatSender, LmPlayground } from '@repo/naive-ui'
import { useDialog, message } from '@repo/hooks'
import { listTenantKeys, createTenantKey } from '@/api/api-key'
import { getProxyEndpoint } from '@/api/proxy-endpoint'
import { listTenantModels } from '@/api/model'
import ApiKeyForm from '@/views/api-key/ApiKeyForm.vue'
import { useStore } from '@/views/api-key/useStore'
import { useController } from '@/views/playground/useController'

const keyFixtures = [
  { id: 10, name: 'prod', plaintext: 'sk-cccc3333', status: 'active' },
  { id: 11, name: 'disabled', plaintext: 'sk-dddd4444', status: 'disabled' },
]

const modelFixtures = [
  { name: 'deepseek-v3', model_type: 'llm', context_length: 65536, tags: ['思考'] },
  { name: 'qwen-vl', model_type: 'multimodal', context_length: null, tags: [] },
  { name: 'bge-m3', model_type: 'embedding', tags: [] },
]

const endpointFixture = {
  base_url: 'http://192.0.2.100:3000',
  proxy_host: '192.0.2.100',
  proxy_port: 3000,
  advertise_url_configured: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listTenantKeys).mockResolvedValue(keyFixtures as never)
  vi.mocked(getProxyEndpoint).mockResolvedValue(endpointFixture as never)
  vi.mocked(listTenantModels).mockResolvedValue(modelFixtures as never)
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

/** 从 createChatSender 调用参数中取出动态鉴权选项。 */
function senderOptions() {
  return vi.mocked(createChatSender).mock.calls[0][0] as {
    baseURL: () => string
    apiKey: () => string | undefined
  }
}

/** 挂载 controller 并装配真实 LmPlayground(含空态创建入口插槽),模拟页面接线。
 *  slot key 用 kebab-case:与 index.vue 模板 #api-key-empty 的编译产物一致
 *  (Vue 不做 slot 名大小写转换,camelCase 会静默失配)。 */
function mountPage() {
  let controller!: ReturnType<typeof useController>
  const wrapper = mount({
    setup() {
      controller = useController()
      return () =>
        // NMessageProvider 为 naive 原生 message 的 provider 壳;playground 现用 @repo/hooks
        // 的 message(挂 MessageHost),此包装仅为与真实 App 装配结构保持一致
        h(NMessageProvider, null, {
          default: () =>
            h(
              LmPlayground,
              {
                models: controller.models.value,
                apiKeys: controller.apiKeys.value,
                apiKey: controller.apiKey.value,
                requireApiKey: true,
                sender: controller.sender,
                'onUpdate:apiKey': (value: string | null) => {
                  controller.apiKey.value = value
                },
              },
              {
                'api-key-empty': () =>
                  h('button', { type: 'button', class: 'create-key-entry' }, '创建 API Key'),
              },
            ),
        })
    },
  })
  return { controller, wrapper }
}

describe('playground useController', () => {
  it('onMounted 拉取 Key 列表/数据面端点/模型列表', async () => {
    const { controller, wrapper } = mountController()
    await flushPromises()

    // 仅启用 Key 进入选项,label 带明文尾号区分
    expect(controller.apiKeys.value).toHaveLength(1)
    expect(controller.apiKeys.value[0]).toMatchObject({ value: '10' })
    expect(controller.apiKeys.value[0].label).toContain('prod')
    expect(controller.apiKeys.value[0].label).toContain('3333')

    // 模型列表:已发布−封禁全集再筛对话类(llm/multimodal);元数据透传供切换模型自动填充
    expect(controller.models.value.map((item) => item.value)).toEqual(['deepseek-v3', 'qwen-vl'])
    expect(controller.models.value[0]).toMatchObject({
      reasoning: true,
    })
    expect(controller.models.value[1]).toMatchObject({ reasoning: false })

    expect(message.error).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('sender 以函数式 baseURL/apiKey 装配:端点补 /v1,Key 按选中项解析明文', async () => {
    const { controller, wrapper } = mountController()
    await flushPromises()

    expect(createChatSender).toHaveBeenCalledTimes(1)
    const options = senderOptions()
    expect(options.baseURL()).toBe('http://192.0.2.100:3000/v1')

    expect(controller.apiKey.value).toBeNull()
    expect(options.apiKey()).toBeUndefined()
    controller.apiKey.value = '10'
    expect(options.apiKey()).toBe('sk-cccc3333')
    wrapper.unmount()
  })

  it('空 Key 状态:展示创建入口,创建完成刷新列表并默认选中第一个', async () => {
    // 首次加载无可用 Key;创建完成后刷新返回 fixtures(仅 active 的 id=10 入选)
    vi.mocked(listTenantKeys)
      .mockReset()
      .mockResolvedValueOnce([] as never)
      .mockResolvedValue(keyFixtures as never)
    vi.mocked(createTenantKey).mockResolvedValue({
      api_key: { name: 'new' },
      plaintext: 'sk-new',
    } as never)

    const { controller, wrapper } = mountPage()
    await flushPromises()

    // 空态:API Key 字段仍展示,插槽创建入口可见
    expect(controller.apiKeys.value).toEqual([])
    expect(controller.apiKey.value).toBeNull()
    const entry = wrapper.find('.create-key-entry')
    expect(entry.exists()).toBe(true)
    expect(entry.text()).toBe('创建 API Key')

    // 空 Key 列表:即使已输入内容,发送仍被禁用(requireApiKey 接线);
    // 组件用类禁用而非原生 disabled(点击需派发事件触发必填提示),断言按类判
    await wrapper.find('.lm-playground__input textarea').setValue('你好')
    expect(wrapper.find('.lm-playground__send-btn').classes()).toContain(
      'lm-playground__send-btn--disabled',
    )

    // 打开创建弹窗:复用 ApiKeyForm,透传全量模型别名(不按可对话类别过滤)
    controller.openCreateApiKey()
    expect(useDialog).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '创建 API Key',
        component: ApiKeyForm,
        props: { modelAliases: ['deepseek-v3', 'qwen-vl', 'bge-m3'] },
      }),
    )

    // 签发完成 → api-key store notify → 刷新 Key 列表,组件 watch 默认选中第一个
    await useStore().create({
      name: 'new',
      allowed_models: [],
      rpm_limit: null,
      tpm_limit: null,
      total_token_quota: null,
      budget_limit: '',
      expires_at: null,
    })
    await flushPromises()
    await nextTick()

    expect(listTenantKeys).toHaveBeenCalledTimes(2)
    expect(controller.apiKeys.value).toHaveLength(1)
    expect(controller.apiKeys.value[0]).toMatchObject({ value: '10' })
    expect(controller.apiKey.value).toBe('10')
    // 空态入口消失,恢复下拉选择;Key 已选中,发送按钮解锁(类禁用移除)
    expect(wrapper.find('.create-key-entry').exists()).toBe(false)
    expect(wrapper.find('.lm-playground__send-btn').classes()).not.toContain(
      'lm-playground__send-btn--disabled',
    )
    wrapper.unmount()
  })

  it('各数据源失败时分别提示错误,互不影响', async () => {
    vi.mocked(listTenantKeys).mockRejectedValue(new Error('network'))
    vi.mocked(getProxyEndpoint).mockRejectedValue(new Error('network'))
    vi.mocked(listTenantModels).mockRejectedValue(new Error('network'))

    const { controller, wrapper } = mountController()
    await flushPromises()

    expect(controller.apiKeys.value).toEqual([])
    expect(controller.models.value).toEqual([])
    const errors = vi.mocked(message.error).mock.calls.map(([msg]) => msg)
    expect(errors).toEqual(
      expect.arrayContaining([
        expect.stringContaining('API Key 列表加载失败'),
        expect.stringContaining('数据面接入地址加载失败'),
        expect.stringContaining('模型列表加载失败'),
      ]),
    )
    wrapper.unmount()
  })
})
