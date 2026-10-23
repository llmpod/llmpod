// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { CubeOutline } from '@vicons/ionicons5'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('vue-router', () => ({ useRouter: () => routerMock }))
vi.mock('@/api/model', () => ({ listTenantModels: vi.fn() }))
vi.mock('@/api/proxy-endpoint', () => ({ getProxyEndpoint: vi.fn() }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    message: { error: vi.fn(), success: vi.fn() },
  }
})

const routerMock = { push: vi.fn() }

import { listTenantModels, type RouteData } from '@/api/model'
import { getProxyEndpoint } from '@/api/proxy-endpoint'
import { useStore, brandIconOf, modelTypeMap, formatContextLength, type RouteData as StoreRouteData } from '@/views/model-mark/useStore'
import { useController } from '@/views/model-mark/useController'

/** 取组件名用于断言命中了哪个品牌 */
const nameOf = (component: unknown) => (component as { name?: string } | null)?.name

function makeRoute(overrides: Partial<RouteData> = {}): RouteData {
  return {
    id: 1,
    name: 'deepseek-v3',
    status: 'published',
    origin: 'manual',
    model_type: 'llm',
    description: 'MoE 大语言模型',
    context_length: 131072,
    tags: ['思考'],
    input_price_per_mtok: '2.000000',
    output_price_per_mtok: '8.000000',
    cached_input_price_per_mtok: null,
    failover_enabled: true,
    failover_max_retries: 2,
    created_at: '2026-01-15T00:00:00Z',
    updated_at: '2026-01-15T00:00:00Z',
    ...overrides,
  }
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listTenantModels).mockResolvedValue([
    makeRoute({ id: 1, name: 'deepseek-v3' }),
    makeRoute({ id: 2, name: 'qwen3.5-max', input_price_per_mtok: null }),
    makeRoute({ id: 3, name: 'bge-m3', failover_enabled: false, failover_max_retries: 0 }),
  ])
})

describe('model-mark useStore', () => {
  it('fetchModels 拉取全量并填充 records,完成后 loading 回落', async () => {
    expect(store.records.value).toHaveLength(0)
    const promise = store.fetchModels()
    expect(store.loading.value).toBe(true)
    await promise
    expect(listTenantModels).toHaveBeenCalledTimes(1)
    expect(store.records.value.map((m: StoreRouteData) => m.name)).toEqual([
      'deepseek-v3',
      'qwen3.5-max',
      'bge-m3',
    ])
    expect(store.loading.value).toBe(false)
  })

  it('fetchModels 失败时 loading 也回落(错误向上抛)', async () => {
    vi.mocked(listTenantModels).mockRejectedValue(new Error('boom'))
    await expect(store.fetchModels()).rejects.toThrow('boom')
    expect(store.loading.value).toBe(false)
  })

  it('fetchModels 完成后通知订阅者', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    await store.fetchModels()
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('fetchProxyEndpoint 失败静默;成功后填充 base_url(去尾斜杠)且不再重复请求', async () => {
    // 先失败:不抛错,值保持 null
    vi.mocked(getProxyEndpoint).mockRejectedValueOnce(new Error('boom'))
    await expect(store.fetchProxyEndpoint()).resolves.toBeUndefined()
    expect(store.proxyBaseUrl.value).toBeNull()

    // 再成功:填充并去尾斜杠
    vi.mocked(getProxyEndpoint).mockResolvedValue({
      base_url: 'https://api.example.com/',
      proxy_host: '0.0.0.0',
      proxy_port: 8090,
      advertise_url_configured: true,
    })
    await store.fetchProxyEndpoint()
    expect(store.proxyBaseUrl.value).toBe('https://api.example.com')

    // 成功后 loaded,重复调用不再发请求(值不被覆盖)
    vi.mocked(getProxyEndpoint).mockClear()
    await store.fetchProxyEndpoint()
    expect(getProxyEndpoint).not.toHaveBeenCalled()
  })

  it('modelTypeMap: 七类模型类别文案齐全', () => {
    expect(Object.keys(modelTypeMap)).toHaveLength(7)
    expect(modelTypeMap.llm).toBe('语言模型')
    expect(modelTypeMap.image).toBe('图像生成')
  })

  it('formatContextLength: 千分位 + tokens,空值显示 —', () => {
    expect(formatContextLength(131072)).toBe('131,072 tokens')
    expect(formatContextLength(1000000)).toBe('1,000,000 tokens')
    expect(formatContextLength(null)).toBe('—')
  })

  it('brandIconOf 按名称识别品牌家族,未命中回退通用立方体图标', () => {
    expect(nameOf(brandIconOf('deepseek-v3'))).toBe('BrandDeepSeek')
    expect(nameOf(brandIconOf('qwen3.5-max'))).toBe('BrandQwen')
    expect(nameOf(brandIconOf('GLM-4-Plus'))).toBe('BrandGlm')
    expect(brandIconOf('bge-m3')).toBe(CubeOutline)
    expect(brandIconOf('')).toBe(CubeOutline)
  })
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

describe('model-mark useController', () => {
  it('挂载后自动拉取,filtered 即全量', async () => {
    const { controller, wrapper } = mountController()
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(controller.filtered.value.length).toBe(3)
    wrapper.unmount()
  })

  it('关键词按模型名本地过滤(大小写不敏感),无结果为空', async () => {
    const { controller, wrapper } = mountController()
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))

    controller.keyword.value = 'DEEPSEEK'
    expect(controller.filtered.value.map((m) => m.name)).toEqual(['deepseek-v3'])

    controller.keyword.value = '不存在的模型xyz'
    expect(controller.filtered.value).toHaveLength(0)

    controller.keyword.value = ''
    expect(controller.filtered.value.length).toBe(3)
    wrapper.unmount()
  })

  it('openDetail 锁定当前模型并打开抽屉', async () => {
    const { controller, wrapper } = mountController()
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    const model = controller.filtered.value[0] as RouteData
    controller.openDetail(model)
    expect(controller.activeModel.value).toBe(model)
    expect(controller.detailDrawerShow.value).toBe(true)
    wrapper.unmount()
  })

  it('goPlayground 跳转带模型名', async () => {
    const { controller, wrapper } = mountController()
    await nextTick()
    controller.goPlayground(makeRoute({ name: 'deepseek-v3' }))
    expect(routerMock.push).toHaveBeenCalledWith({
      name: 'playground',
      query: { model: 'deepseek-v3' },
    })
    wrapper.unmount()
  })
})
