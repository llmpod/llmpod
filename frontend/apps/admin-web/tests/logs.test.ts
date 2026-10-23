// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/audit-log', () => ({ listAuditLogs: vi.fn() }))
vi.mock('@/api/tenant', () => ({ listTenants: vi.fn() }))

import { listAuditLogs } from '@/api/audit-log'
import { listTenants } from '@/api/tenant'
import {
  actionFilterOptions,
  actionMap,
  formatDateTime,
  normalizeDateRange,
  resourceTypeLabel,
  resultMap,
  useStore,
  type AuditLogData,
} from '@/views/logs/useStore'
import { useController } from '@/views/logs/useController'

const store = useStore()

function makeLog(overrides: Partial<AuditLogData> = {}): AuditLogData {
  return {
    id: 1,
    actor_type: 'user',
    actor_id: 2,
    actor_name: 'admin',
    tenant_id: null,
    action: 'create',
    result: 'success',
    resource_type: 'api_key',
    resource_id: '9',
    detail: { params: { name: 'k', password: '***' } },
    ip_address: '127.0.0.1',
    created_at: '2026-09-01T08:00:00Z',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listAuditLogs).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 20,
    total_pages: 0,
  })
})

describe('logs useStore(只读流水,真实接口对接)', () => {
  it('request 拼装分页与筛选参数,返回 items/total', async () => {
    vi.mocked(listAuditLogs).mockResolvedValue({
      items: [makeLog()],
      total: 1,
      page: 2,
      page_size: 50,
      total_pages: 1,
    })

    const res = await store.request({
      page: 2,
      pageSize: 50,
      action: 'create',
      tenant_id: 3,
      resource_id: '  9  ',
      dateRange: [1000, 2000],
    })

    expect(listAuditLogs).toHaveBeenCalledTimes(1)
    expect(listAuditLogs).toHaveBeenCalledWith(
      {
        page: 2,
        page_size: 50,
        action: 'create',
        tenant_id: 3,
        resource_id: '9',
        since_ms: 1000,
        until_ms: 2000,
      },
      undefined,
    )
    expect(res.total).toBe(1)
    expect(res.data[0]).toMatchObject({ id: 1, action: 'create' })
  })

  it('空筛选不传对应参数;resource_id trim 后为空不传;signal 经 context 透传', async () => {
    const controller = new AbortController()

    await store.request(
      {
        page: 1,
        pageSize: 20,
        action: '',
        tenant_id: null,
        resource_id: '   ',
        dateRange: null,
      },
      { signal: controller.signal },
    )

    expect(listAuditLogs).toHaveBeenCalledWith(
      {
        page: 1,
        page_size: 20,
        tenant_id: undefined,
        action: undefined,
        resource_id: undefined,
        since_ms: undefined,
        until_ms: undefined,
      },
      controller.signal,
    )
  })

  it('接口异常向上抛出(由表格层统一处理)', async () => {
    vi.mocked(listAuditLogs).mockRejectedValue(new Error('network'))
    await expect(
      store.request({
        page: 1,
        pageSize: 20,
        action: '',
        tenant_id: null,
        resource_id: '',
        dateRange: null,
      }),
    ).rejects.toThrow('network')
  })
})

describe('筛选选项与映射常量', () => {
  it('操作筛选选项首项为「全部」,其余与映射词表一致', () => {
    expect(actionFilterOptions[0]).toEqual({ label: '全部操作', value: '' })
    expect(actionFilterOptions.slice(1).map((item) => item.value).sort()).toEqual(
      Object.keys(actionMap).sort(),
    )
  })

  it('结果映射:success 成功/failed 失败;操作映射含登录登出', () => {
    expect(resultMap.success).toEqual({ label: '成功', type: 'success' })
    expect(resultMap.failed).toEqual({ label: '失败', type: 'error' })
    expect(actionMap.login.label).toBe('登录')
    expect(actionMap.logout.label).toBe('登出')
  })

  it('resourceTypeLabel:已收录取中文,未收录回退原串,空值占位', () => {
    expect(resourceTypeLabel('api_key')).toBe('API Key')
    expect(resourceTypeLabel('new_resource')).toBe('new_resource')
    expect(resourceTypeLabel(null)).toBe('—')
  })

  it('formatDateTime:空值/非法/合法三分支', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('bad')).toBe('bad')
    expect(formatDateTime('2026-09-01T00:00:00Z')).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
  })

  it('normalizeDateRange:null 透传,不构造区间', () => {
    expect(normalizeDateRange(null)).toBeNull()
  })

  it('normalizeDateRange:同一天归一化为 [当日0点, 当日23:59:59.999]', () => {
    const dayStart = new Date(2026, 8, 1, 0, 0, 0, 0).getTime()
    expect(normalizeDateRange([dayStart, dayStart])).toEqual([
      dayStart,
      new Date(2026, 8, 1, 23, 59, 59, 999).getTime(),
    ])
  })

  it('normalizeDateRange:跨天时结束日归一化到当天末尾,起始日归一化到 0 点', () => {
    const start = new Date(2026, 8, 1, 15, 30, 0, 0).getTime()
    const end = new Date(2026, 8, 5, 0, 0, 0, 0).getTime()
    expect(normalizeDateRange([start, end])).toEqual([
      new Date(2026, 8, 1, 0, 0, 0, 0).getTime(),
      new Date(2026, 8, 5, 23, 59, 59, 999).getTime(),
    ])
  })
})

// ---------- controller(租户选项加载 + 筛选联动) ----------

describe('logs useController', () => {
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

  it('挂载后拉取租户列表构建筛选选项(排除平台租户 id=0);失败不阻塞', async () => {
    vi.mocked(listTenants).mockResolvedValue({
      items: [
        { id: 0, name: '平台租户' },
        { id: 1, name: 'T1' },
        { id: 2, name: 'T2' },
      ],
    } as Awaited<ReturnType<typeof listTenants>>)

    const { controller, wrapper } = mountController()
    await vi.waitFor(() => expect(controller.tenantFilterOptions.value).toHaveLength(2))
    expect(controller.tenantFilterOptions.value).toEqual([
      { label: 'T1', value: 1 },
      { label: 'T2', value: 2 },
    ])
    wrapper.unmount()
  })

  it('租户选项加载失败:选项保持为空不抛异常', async () => {
    vi.mocked(listTenants).mockRejectedValue(new Error('boom'))
    const { controller, wrapper } = mountController()
    await vi.waitFor(() => expect(listTenants).toHaveBeenCalled())
    expect(controller.tenantFilterOptions.value).toEqual([])
    wrapper.unmount()
  })

  it('资源列:有 resource_type 渲染标签,无则不渲染标签仅显示占位', async () => {
    vi.mocked(listAuditLogs).mockResolvedValue({
      items: [
        makeLog(),
        makeLog({ id: 2, action: 'login', resource_type: null, resource_id: null }),
      ],
      total: 2,
      page: 1,
      page_size: 20,
      total_pages: 1,
    })
    vi.mocked(listTenants).mockRejectedValue(new Error('nope'))

    let controller!: ReturnType<typeof useController>
    const wrapper = mount({
      setup: () => {
        controller = useController()
        return () => h('div', [h(controller.LmTable)])
      },
    }, { attachTo: document.body })
    await vi.waitFor(
      () => expect(document.body.querySelectorAll('tbody tr').length).toBe(2),
      { timeout: 3000 },
    )

    const rows = [...document.body.querySelectorAll('tbody tr')]
    expect(rows[0].textContent).toContain('API Key')
    // 第二行 resource_type/resource_id 均为 null:不出现 '—' 占位标签,仅显示 '--'
    expect(rows[1].textContent).not.toContain('—')
    expect(rows[1].textContent).toContain('--')
    wrapper.unmount()
  })

  it('租户列:tenant_id 为空或 0 统一展示「平台」,其余展示租户名/回退 #id', async () => {
    vi.mocked(listAuditLogs).mockResolvedValue({
      items: [
        makeLog({ id: 1, tenant_id: null }),
        makeLog({ id: 2, tenant_id: 0 }),
        makeLog({ id: 3, tenant_id: 3 }),
        makeLog({ id: 4, tenant_id: 99 }),
      ],
      total: 4,
      page: 1,
      page_size: 20,
      total_pages: 1,
    })
    vi.mocked(listTenants).mockResolvedValue({
      items: [{ id: 3, name: '企业A' }],
    } as Awaited<ReturnType<typeof listTenants>>)

    let controller!: ReturnType<typeof useController>
    const wrapper = mount({
      setup: () => {
        controller = useController()
        return () => h('div', [h(controller.LmTable)])
      },
    }, { attachTo: document.body })
    await vi.waitFor(
      () => expect(document.body.querySelectorAll('tbody tr').length).toBe(4),
      { timeout: 3000 },
    )

    const rows = [...document.body.querySelectorAll('tbody tr')]
    expect(rows[0].textContent).toContain('平台')
    expect(rows[1].textContent).toContain('平台')
    expect(rows[2].textContent).toContain('企业A')
    expect(rows[3].textContent).toContain('#99')
    wrapper.unmount()
  })

  it('onActionChange/onTenantChange/onDateChange 更新查询参数并触发重取', async () => {
    vi.mocked(listTenants).mockRejectedValue(new Error('nope'))
    const { controller, wrapper } = mountController()
    await vi.waitFor(() => expect(listAuditLogs).toHaveBeenCalled())

    controller.onActionChange('login')
    expect(controller.param.action).toBe('login')

    controller.onTenantChange(3)
    expect(controller.param.tenant_id).toBe(3)

    const dayStart = new Date(2026, 8, 15).getTime()
    controller.onDateChange([dayStart, dayStart])
    expect(controller.param.dateRange).toEqual([
      dayStart,
      new Date(2026, 8, 15, 23, 59, 59, 999).getTime(),
    ])

    await vi.waitFor(() => {
      const lastCall = vi.mocked(listAuditLogs).mock.calls.at(-1)?.[0]
      expect(lastCall).toMatchObject({
        action: 'login',
        tenant_id: 3,
        since_ms: dayStart,
        until_ms: new Date(2026, 8, 15, 23, 59, 59, 999).getTime(),
      })
    })
    wrapper.unmount()
  })

  it('onDateChange:daterange 同一天归一化为 [0点, 23:59:59.999] 后下发请求', async () => {
    vi.mocked(listTenants).mockRejectedValue(new Error('nope'))
    const { controller, wrapper } = mountController()
    await vi.waitFor(() => expect(listAuditLogs).toHaveBeenCalled())

    const dayStart = new Date(2026, 8, 1).getTime()
    controller.onDateChange([dayStart, dayStart])
    expect(controller.param.dateRange).toEqual([
      dayStart,
      new Date(2026, 8, 1, 23, 59, 59, 999).getTime(),
    ])

    await vi.waitFor(() => {
      const lastCall = vi.mocked(listAuditLogs).mock.calls.at(-1)?.[0]
      expect(lastCall).toMatchObject({
        since_ms: dayStart,
        until_ms: new Date(2026, 8, 1, 23, 59, 59, 999).getTime(),
      })
    })
    wrapper.unmount()
  })
})
