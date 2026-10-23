import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/tenant', () => ({
  listTenants: vi.fn(),
  createTenant: vi.fn(),
  getTenant: vi.fn(),
  updateTenant: vi.fn(),
  deleteTenant: vi.fn(),
  getTenantsOverview: vi.fn(),
  adjustTenantWallet: vi.fn(),
}))

vi.mock('@/api/publish', () => ({
  listPublishes: vi.fn(),
}))

import {
  adjustTenantWallet,
  createTenant,
  deleteTenant,
  getTenant,
  getTenantsOverview,
  listTenants,
  updateTenant,
  type TenantData,
  type TenantWalletData,
} from '@/api/tenant'
import { listPublishes, type PublishData } from '@/api/publish'
import {
  buildBlockedRouteOptions,
  formatDateTime,
  formatDecimal,
  subtractDecimal,
  tenantStatusFilterOptions,
  tenantStatusMap,
  useStore,
  type TenantFormValues,
} from '@/views/tenant-management/useStore'

function makeTenant(overrides: Partial<TenantData> = {}): TenantData {
  return {
    id: 1,
    name: '企业A',
    status: 'active',
    user_count: 5,
    role_count: 2,
    balance: '100.000000',
    frozen_balance: '5.000000',
    available: '95.000000',
    blocked_models: [],
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    ...overrides,
  } as TenantData
}

function makePublish(routeId: number, overrides: Partial<PublishData['route']> = {}): PublishData {
  return {
    route: {
      id: routeId,
      name: `模型${routeId}`,
      status: 'published',
      ...overrides,
    } as PublishData['route'],
    bindings: [],
  }
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listTenants).mockResolvedValue({
    items: [makeTenant({ id: 1, name: '企业A' }), makeTenant({ id: 2, name: '云智科技', status: 'disabled' })],
    total: 2,
    page: 1,
    page_size: 20,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listTenants>>)
})

describe('tenant-management useStore', () => {
  it('request 后端分页+status 过滤,keyword 仅过滤当前页名称', async () => {
    const result = await store.request({ page: 1, pageSize: 20, status: 'active' })
    expect(listTenants).toHaveBeenCalledWith({ page: 1, page_size: 20, status: 'active' }, undefined)

    const filtered = await store.request({ page: 1, pageSize: 20, keyword: '云智' })
    expect(filtered.data).toHaveLength(1)
    expect(filtered.data[0].name).toBe('云智科技')
    void result
  })

  it('request 过滤平台租户(id=0),total 保持服务端口径', async () => {
    vi.mocked(listTenants).mockResolvedValueOnce({
      items: [makeTenant({ id: 0, name: '平台租户' }), makeTenant({ id: 1, name: '企业A' })],
      total: 3,
      page: 1,
      page_size: 20,
      total_pages: 1,
    } as Awaited<ReturnType<typeof listTenants>>)

    const result = await store.request({ page: 1, pageSize: 20 })

    expect(result.data.map((item) => item.id)).toEqual([1])
    expect(result.total).toBe(3)
  })

  it('save 无 id 创建:同步创建租户超管,空邮箱/手机传 undefined,blocked_route_ids 透传', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(createTenant).mockResolvedValue(makeTenant())

    const result = await store.save({
      name: '新租户',
      admin_username: 'admin',
      admin_password: 'secret',
      admin_email: '',
      admin_phone: '',
      blocked_route_ids: [7, 8],
    } as TenantFormValues)

    expect(result).toEqual({ mode: 'create', name: '新租户' })
    expect(createTenant).toHaveBeenCalledWith({
      name: '新租户',
      admin_username: 'admin',
      admin_password: 'secret',
      admin_email: undefined,
      admin_phone: undefined,
      blocked_route_ids: [7, 8],
    })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('save 有 id 更新:改名并全量替换封禁集;toggleStatus 互切并 notify', async () => {
    vi.mocked(updateTenant).mockResolvedValue(makeTenant())
    const result = await store.save({
      id: 1,
      name: '改名租户',
      blocked_route_ids: [9],
    } as TenantFormValues)
    expect(result.mode).toBe('update')
    expect(updateTenant).toHaveBeenCalledWith(1, { name: '改名租户', blocked_route_ids: [9] })

    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const next = await store.toggleStatus(makeTenant({ id: 1, status: 'active' }))
    expect(next).toBe('disabled')
    expect(updateTenant).toHaveBeenLastCalledWith(1, { status: 'disabled' })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('remove/getDetail 透传,写操作 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    vi.mocked(deleteTenant).mockResolvedValue(undefined)
    await store.remove(1)
    expect(deleteTenant).toHaveBeenCalledWith(1)

    vi.mocked(getTenant).mockResolvedValue(makeTenant({ id: 1 }))
    expect(await store.getDetail(1)).toMatchObject({ id: 1 })

    expect(listener).toHaveBeenCalledTimes(1) // remove
    unsubscribe()
  })

  it('formatDateTime 与状态常量', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(tenantStatusMap.disabled).toEqual({ label: '禁用', type: 'error' })
    expect(tenantStatusFilterOptions[0]).toEqual({ label: '全部', value: '' })
  })

  it('formatDecimal 余额字符串去尾零', () => {
    expect(formatDecimal('100.000000')).toBe('100')
    expect(formatDecimal('95.500000')).toBe('95.5')
    expect(formatDecimal('0.000000')).toBe('0')
  })
})

describe('tenant-management 模型封禁', () => {
  it('buildBlockedRouteOptions 仅保留已发布路由', () => {
    const options = buildBlockedRouteOptions(
      [
        makePublish(1),
        makePublish(2),
        makePublish(3, { status: 'unpublished' }),
      ],
    )

    expect(options).toEqual([
      { route_id: 1, name: '模型1' },
      { route_id: 2, name: '模型2' },
    ])
  })

  it('loadBlockedRouteOptions 取已发布路由全集(勾选初值由调用方从 blocked_models 推导)', async () => {
    vi.mocked(listPublishes).mockResolvedValue([makePublish(1), makePublish(2)])

    const options = await store.loadBlockedRouteOptions()

    expect(listPublishes).toHaveBeenCalledWith(undefined)
    expect(options).toEqual([
      { route_id: 1, name: '模型1' },
      { route_id: 2, name: '模型2' },
    ])
  })

  it('loadBlockedRouteOptions 请求失败则向上抛出', async () => {
    vi.mocked(listPublishes).mockRejectedValue(new Error('boom'))

    await expect(store.loadBlockedRouteOptions()).rejects.toThrow('boom')
  })
})

describe('tenant-management 概览统计卡(并入的原「租户概览」)', () => {
  it('fetchOverview 拉 /admin/tenants/overview 映射四张统计卡', async () => {
    vi.mocked(getTenantsOverview).mockResolvedValue({
      total_tenants: 7,
      active_tenants: 5,
      disabled_tenants: 2,
      total_users: 69,
      dates: ['2026-09-04', '2026-09-05'],
      consume_trend: [{ tenant_id: 1, tenant_name: '企业A', data: [2650.0, 2700.0] }],
      balance_top: [{ tenant_id: 3, tenant_name: '企业C', balance: '5200.000000', available: '5000.000000' }],
    })

    await store.fetchOverview()

    expect(getTenantsOverview).toHaveBeenCalledWith(undefined, undefined)
    expect(store.overviewCards.value).toEqual([
      { label: '总租户数', value: '7' },
      { label: '正常', value: '5', tone: 'success' },
      { label: '冻结/预逾期', value: '2', tone: 'error' },
      { label: '总用户数', value: '69' },
    ])
  })
})

describe('tenant-management 调额', () => {
  it('adjustWallet 透传 payload,notify 联动并返回调整后钱包', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const wallet: TenantWalletData = {
      id: 1,
      tenant_id: 1,
      balance: '200.000000',
      frozen_balance: '5.000000',
      created_at: '2026-09-05T00:00:00Z',
      updated_at: '2026-09-05T00:00:00Z',
    }
    vi.mocked(adjustTenantWallet).mockResolvedValue(wallet)

    const result = await store.adjustWallet(1, { amount: 100, mode: 'add', reason: '充值补录' })

    expect(adjustTenantWallet).toHaveBeenCalledWith(1, { amount: 100, mode: 'add', reason: '充值补录' })
    expect(result).toEqual(wallet)
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })
})

describe('subtractDecimal 十进制减法(AC-01:金额禁浮点)', () => {
  it('浮点会出脏尾的组合得到精确结果', () => {
    // JS: 856.65 - 432.55 === 424.09999999999997;849.19 - 667.3 === 181.8900000000001
    expect(subtractDecimal('856.65', '432.55')).toBe('424.10')
    expect(subtractDecimal('849.19', '667.30')).toBe('181.89')
    expect(subtractDecimal('0.30', '0.10')).toBe('0.20') // JS: 0.19999999999999998
  })

  it('Decimal 高精度字符串对齐到最大小数位', () => {
    expect(subtractDecimal('200.000000', '5.000000')).toBe('195.000000')
    expect(subtractDecimal('100.3', '0.1')).toBe('100.2')
    expect(subtractDecimal('100', '0.01')).toBe('99.99')
  })

  it('整数/空串/负结果边界', () => {
    expect(subtractDecimal('100', '30')).toBe('70')
    expect(subtractDecimal('', '')).toBe('0')
    expect(subtractDecimal('5', '10')).toBe('-5')
    expect(subtractDecimal('0.5', '1.5')).toBe('-1.0')
  })

  it('与展示层 formatDecimal 串联去尾零', () => {
    expect(formatDecimal(subtractDecimal('856.65', '432.55'))).toBe('424.1')
    expect(formatDecimal(subtractDecimal('200.000000', '5.000000'))).toBe('195')
  })
})
